import logging
import os                                                    # [MỚI] dựng đường dẫn thư mục dữ liệu Program
import queue
import sys                                                   # [MỚI] phát hiện chạy từ exe (frozen)

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QDockWidget, QLabel, QMainWindow, QMdiArea

from core import system_log
from core.database_manager import DatabaseManager
from core.frame_pump import FramePump
from core.hardware import QUEUE_MAXSIZE, HardwareManager
from core.measurement_session import MeasurementSession
from core.program_store import ProgramStore                  # [MỚI] nguồn dữ liệu Program dùng chung
from core.window_registry import WindowRegistry, WindowSpec
from core.workspace_manager import WorkspaceManager
from models.hw_settings import HwSettingsStore
from ui.dialogs.channel_mapping_dialog import ChannelMappingDialog
from ui.dialogs.hardware_config_dialog import HardwareConfigDialog
from ui.generated.MainWindow import Ui_MainWindow
from ui.widgets.system_message_widget import SystemMessageWidget
from windows.base_window import BaseWindow
from windows.database_window import DatabaseWindow
from windows.diagnostic_setting_window import DiagnosticSettingWindow   # [MỚI] cửa sổ Diagnostic Setting
from windows.trace_window import TraceWindow
from windows.uds_console_window import UdsConsoleWindow                 # [MỚI] cửa sổ UDS Console

logger = logging.getLogger(__name__)

# Action (checkable) in MainWindow.ui that shows/hides the System Messages dock.
# [SỬA] trước đây là "actionHomeSystemMessages" (có chữ s) nên không khớp với
#       self.ui.actionHomeSystemMessage trong MainWindow.py, nút menu không nối được với dock.
SYSTEM_MESSAGES_ACTION = "actionHomeSystemMessage"

# Cửa sổ mở ra SAU khi đã Start: False = vẫn ở chế độ dừng, người dùng tự bấm Play của cửa sổ;
#                                True  = chạy ngay.
NEW_WINDOWS_START_WHEN_RUNNING = False


# [MỚI] Thư mục gốc của tool (cạnh main.py). main_window.py nằm trong ui/ nên lên 2 cấp.
#       Khi đóng gói exe thì lấy thư mục chứa exe. Nếu core/ đã có tool_root() thì import hàm đó thay thế.
def _tool_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MainWindow(QMainWindow):
    """Vỏ ứng dụng: nạp giao diện từ pyuic6 và nối logic Python vào."""

    frame_received = pyqtSignal(object)      # bus nội bộ: nguồn CAN thật sẽ phát vào đây

    def __init__(self):
        super().__init__()
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

        self._setup_system_messages()            # SỚM NHẤT, trước các manager

        self.hw_store = HwSettingsStore(self)

        # [MỚI] MỘT ProgramStore duy nhất cho cả Diagnostic Setting và UDS Console.
        #       Dữ liệu lưu ở <gốc tool>/Diagnostics Setting/<Program>/...
        #       Không tạo thêm store ở nơi khác, nếu không signal programs_changed sẽ không đồng bộ.
        self.programs = ProgramStore(os.path.join(_tool_root(), "Diagnostics Setting"))

        self.database = DatabaseManager(self)
        # keep hardware mapping when a network is renamed (mapping is keyed by network name)
        self.database.network_renamed.connect(self.hw_store.rename_network)

        # hardware: one shared RX queue, one HardwareManager, one Start/Stop session
        self.rx_queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
        self.hw_manager = HardwareManager(self.rx_queue, parent=self)
        # Diagnostic buses (own Vector app "MS_Tool_Diagnostic"): separate manager whose primary
        # queue is a 1-slot sink, so diagnostic RX goes ONLY to its subscribers (UdsEngine), never to Trace.
        self.diag_hw_manager = HardwareManager(queue.Queue(maxsize=1), parent=self)
        self.session = MeasurementSession(self.hw_store, self.database,
                                          self.hw_manager, self.diag_hw_manager, self)

        # RX bridge: rx_queue -> (convert) -> frame_received -> Trace / Graphic ...
        self.pump = FramePump(self.hw_manager, self.database, parent=self)
        self.pump.frame_ready.connect(self.frame_received)
        self.session.running_changed.connect(self.pump.set_running)
        self.session.running_changed.connect(self.database.set_locked)   # đang đo: không đổi tên / xóa network
        self.database.networks_changed.connect(self.pump.invalidate)
        self.database.network_renamed.connect(lambda *_: self.pump.invalidate())
        self.workspaces = WorkspaceManager(self.ui.workspaceTabs, self)
        self.registry = WindowRegistry(self.workspaces, self)

        self._register_windows()
        self._connect_actions()
        self._setup_run_controls()

        logger.info("MS_Tool ready")

    # ---------- system messages ----------
    def _setup_system_messages(self):
        self.system_log = system_log.install()
        self.sysMsgWidget = SystemMessageWidget()
        self.system_log.message.connect(self.sysMsgWidget.append_message)

        dock = QDockWidget("System Messages", self)
        dock.setObjectName("dockSystemMessages")     # cần để saveState sau này
        dock.setWidget(self.sysMsgWidget)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self.dockSystemMessages = dock

        act = getattr(self.ui, SYSTEM_MESSAGES_ACTION, None)
        if act is not None:
            act.setCheckable(True)
            act.setChecked(True)
            act.toggled.connect(dock.setVisible)
            dock.visibilityChanged.connect(act.setChecked)   # bấm X trên dock thì nút cũng bỏ tick

    # ---------- đăng ký cửa sổ ----------
    def _register_windows(self):
        reg = self.registry
        reg.register("actionBusTrace",
                     WindowSpec("trace", "Trace", self._make_trace, (1000, 600)))
        reg.register("actionAdd_CAN_Database",
                     WindowSpec("can_database", "CAN Database", self._make_database, (1100, 650)))
        # [MỚI] Menu Diagnostic > Setting và Diagnostic > UDS Console.
        #       Hai action này đã có sẵn trong MainWindow.ui nên không phải sửa file .ui.
        #       Sau khi đăng ký, chúng nằm trong registered_actions() và không còn báo [TODO].
        reg.register("actionDiagnosticsSetting",
                     WindowSpec("diag_setting", "Diagnostic Setting",
                                self._make_diag_setting, (900, 520)))
        reg.register("actionDiagnosticsUDSConsole",
                     WindowSpec("uds_console", "UDS Console",
                                self._make_uds_console, (1000, 600)))
        reg.bind(self.ui)

    def _make_trace(self) -> TraceWindow:
        win = TraceWindow(decoder=self.database.trace_decoder)
        self.frame_received.connect(win.on_frame)
        self.database.decoders_changed.connect(win.refresh_decoder)
        return win

    def _make_database(self) -> DatabaseWindow:
        return DatabaseWindow(self.database)

    # [MỚI] Factory cho Diagnostic Setting: dùng chung self.programs.
    #       Khi danh sách Program đổi (tạo / xóa / đổi tên) thì combo trong cửa sổ tự nạp lại.
    def _make_diag_setting(self) -> DiagnosticSettingWindow:
        win = DiagnosticSettingWindow(self.programs)
        self.programs.programs_changed.connect(win.reload_programs)
        return win

    # [MỚI] Factory cho UDS Console: cần self.programs (đọc ECU/ID/rule) và
    #       self.diag_hw_manager (bus diagnostic riêng, không đi vào Trace).
    def _make_uds_console(self) -> UdsConsoleWindow:
        win = UdsConsoleWindow(self.programs, self.diag_hw_manager)
        self.programs.programs_changed.connect(win.reload_programs)
        return win

    # ---------- hardware dialogs ----------
    def _open_hw_config(self):
        HardwareConfigDialog(self.hw_store, hw_manager=self.hw_manager, parent=self).exec()

    def _networks_for_mapping(self):
        """[(net_name, 'CH1'), ...] — mọi network trong database (kể cả chưa nạp DBC)."""
        nets = sorted(self.database.networks.values(),
                      key=lambda n: n.channel_index or 0)
        return [(n.name, f"CH{n.channel_index}" if n.channel_index else "")
                for n in nets]

    def _open_channel_mapping(self):
        ChannelMappingDialog(
            self.hw_store,
            networks_provider=self._networks_for_mapping,
            running=self.session.is_running,
            parent=self,
        ).exec()

    # ---------- Start / Stop ----------
    def _setup_run_controls(self):
        ui = self.ui
        self.lblRunState = QLabel()
        ui.statusbar.addPermanentWidget(self.lblRunState)

        ui.actionHomeStart.triggered.connect(lambda _checked=False: self.session.start())
        ui.actionHomeStop.triggered.connect(lambda _checked=False: self.session.stop())
        self.session.running_changed.connect(self._on_running_changed)
        self.session.running_changed.connect(self._set_windows_active)
        self.registry.window_created.connect(self._on_window_created)
        self._on_running_changed(False)

    def _on_running_changed(self, running: bool):
        self.ui.actionHomeStart.setEnabled(not running)
        self.ui.actionHomeStop.setEnabled(running)
        self.lblRunState.setText("●  Running" if running else "■  Stopped")
        self.lblRunState.setStyleSheet(
            "padding:0 8px;font-weight:bold;color:#2e7d32;" if running
            else "padding:0 8px;color:#666;")

    # ---------- cửa sổ chức năng: dừng / chạy ----------
    def _function_windows(self):
        for area in self.findChildren(QMdiArea):
            for sub in area.subWindowList():
                w = sub.widget()
                if isinstance(w, BaseWindow):
                    yield w

    def _set_windows_active(self, active: bool):
        """HOME > Start / Stop: cho mọi cửa sổ đang mở chạy hoặc dừng.
        (Diagnostic Setting không override on_active_changed nên không bị ảnh hưởng;
         UDS Console dùng nó để bật/tắt nút Send.)"""
        for w in self._function_windows():
            w.set_active(active)

    def _on_window_created(self, widget):
        """Cửa sổ mới luôn sinh ra ở chế độ dừng (BaseWindow mặc định)."""
        if (NEW_WINDOWS_START_WHEN_RUNNING and self.session.is_running
                and isinstance(widget, BaseWindow)):
            widget.set_active(True)

    # ---------- action ----------
    def _connect_actions(self):
        ui = self.ui
        ui.actionFileExit.triggered.connect(self.close)
        ui.actionLayoutCascade.triggered.connect(self.workspaces.cascade)
        ui.actionLayoutTile.triggered.connect(self.workspaces.tile)
        ui.actionLayoutMinimizeAll.triggered.connect(self.workspaces.minimize_all)
        ui.actionHardwareConfiguration.triggered.connect(self._open_hw_config)
        ui.actionHardwareChannelMapping.triggered.connect(self._open_channel_mapping)

        # Action chưa có chức năng: báo [TODO] ở status bar
        handled = {
            "actionFileExit", "actionLayoutCascade",
            "actionLayoutTile", "actionLayoutMinimizeAll",
            "actionHardwareConfiguration", "actionHardwareChannelMapping",
            "actionHomeStart", "actionHomeStop",
            SYSTEM_MESSAGES_ACTION,
        } | self.registry.registered_actions()

        for action in self.findChildren(QAction):
            name = action.objectName()
            if name.startswith("action") and name not in handled:
                action.triggered.connect(
                    lambda _checked=False, n=name:
                        self.ui.statusbar.showMessage(f"[TODO] {n}", 3000))

    def closeEvent(self, event):
        self.session.stop()                  # đóng bus trước khi thoát
        self.database.shutdown()
        system_log.uninstall()
        super().closeEvent(event)