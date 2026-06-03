"""
==============================================================
ui/main_window.py
Main Application Window — Dependency Injected
==============================================================

Dependencies injected từ Main.py:
    hw_manager    — HardwareManager (đã wire signal_bus trong __init__)
    dbc_registry  — DbcRegistry
    signal_bus    — SignalBus
    can_logger    — CanLogger
    dev_namespace — dict (optional)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

try:
    from PyQt6.QtCore import Qt, QDateTime, QSize, QTimer, pyqtSignal, pyqtSlot
    from PyQt6.QtGui import QCloseEvent, QFont
    from PyQt6.QtWidgets import (
        QHBoxLayout, QMainWindow, QMessageBox, QPushButton,
        QScrollArea, QStatusBar, QTabWidget,
        QVBoxLayout, QWidget,
    )
    QT_VERSION   = 6
    STRONG_FOCUS = Qt.FocusPolicy.StrongFocus
    HAND_CURSOR  = Qt.CursorShape.PointingHandCursor
    BTN_YES      = QMessageBox.StandardButton.Yes
    BTN_NO       = QMessageBox.StandardButton.No
except ImportError:
    from PyQt5.QtCore import Qt, QDateTime, QSize, QTimer, pyqtSignal, pyqtSlot   # type: ignore
    from PyQt5.QtGui import QFont                                                   # type: ignore
    from PyQt5.QtCore import QEvent                                                 # type: ignore
    from PyQt5.QtWidgets import (                                                   # type: ignore
        QHBoxLayout, QMainWindow, QMessageBox, QPushButton,
        QScrollArea, QStatusBar, QTabWidget,
        QVBoxLayout, QWidget,
    )
    QCloseEvent  = QEvent                                                           # type: ignore
    QT_VERSION   = 5
    STRONG_FOCUS = Qt.StrongFocus
    HAND_CURSOR  = Qt.PointingHandCursor
    BTN_YES      = QMessageBox.Yes
    BTN_NO       = QMessageBox.No

logger = logging.getLogger(__name__)


# ==============================================================
# STYLE
# ==============================================================

class MenuStyle:
    NORMAL = (
        "QPushButton{"
        "padding:5px 10px;background:#f0f0f0;"
        "border:1px solid #ccc;border-radius:3px;font-weight:bold;}"
        "QPushButton:hover{background:#e0e0e0;}"
    )
    ACTIVE = (
        "QPushButton{"
        "padding:5px 10px;background:#4CAF50;color:white;"
        "border:1px solid #45a049;border-radius:3px;font-weight:bold;}"
        "QPushButton:hover{background:#45a049;}"
    )


# ==============================================================
# TAB REGISTRY
# ==============================================================

TAB_DEFINITIONS = [
    ("File",       "File configuration",             0),
    ("Add DBC",    "Load DBC files",                 1),
    ("HW connect", "Hardware connection settings",   2),
    ("Trace",      "CAN frame trace",                3),
    ("SendCAN",    "Send CAN messages via hardware", 4),
    ("UDS",        "UDS diagnostic services",        5),
    ("Help",       "Application help",               6),
]

TAB_FILE       = 0
TAB_ADD_DBC    = 1
TAB_HW_CONNECT = 2
TAB_TRACE      = 3
TAB_SEND_CAN   = 4
TAB_UDS        = 5
TAB_HELP       = 6


# ==============================================================
# MAIN WINDOW
# ==============================================================

class MainWindow(QMainWindow):
    """
    Cửa sổ chính.

    Không chứa business logic — delegate hết cho Tab con và Core.
    signal_bus là kênh duy nhất để Core → UI giao tiếp.
    hw_manager đã tự wire signals → signal_bus khi được khởi tạo
    trong Main.py, nên MainWindow không cần wire lại.
    """

    ui_layout_changed = pyqtSignal()

    def __init__(
        self,
        hw_manager:    Any,
        dbc_registry:  Any,
        signal_bus:    Any,
        can_logger:    Any,
        dev_namespace: Optional[Dict[str, Any]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        try:
            logger.info("=" * 70)
            logger.info("MainWindow init started (Qt%d)", QT_VERSION)

            self.hw_manager    = hw_manager
            self.dbc_registry  = dbc_registry
            self.signal_bus    = signal_bus
            self.can_logger    = can_logger
            self.dev_namespace = dev_namespace or {}

            self.current_protocol = "CAN"
            self.menu_buttons: Dict[int, QPushButton] = {}

            self._setup_window()
            self._build_main_layout()
            self._connect_signal_bus()

            self._status_timer = QTimer(self)
            self._status_timer.setInterval(1000)
            self._status_timer.timeout.connect(self._update_status_bar)
            self._status_timer.start()

            logger.info("MainWindow ready.")

        except Exception as e:
            logger.critical("MainWindow init failed: %s", e, exc_info=True)
            raise

    # ------------------------------------------------------------------
    # WINDOW SETUP
    # ------------------------------------------------------------------

    def _setup_window(self) -> None:
        self.setWindowTitle("MS Tool — CAN Configuration & Diagnostic Manager")
        self.resize(1400, 800)
        self.setMinimumSize(QSize(900, 650))

    # ------------------------------------------------------------------
    # LAYOUT
    # ------------------------------------------------------------------

    def _build_main_layout(self) -> None:
        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(container)

        self._menu_layout = QHBoxLayout()
        self._menu_layout.setContentsMargins(5, 5, 5, 5)
        self._menu_layout.setSpacing(4)
        self._build_menu_buttons(self._menu_layout)
        root.addLayout(self._menu_layout, stretch=0)

        root.addWidget(self._build_content_area(), stretch=1)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._update_status_bar()

    def _build_menu_buttons(self, layout: QHBoxLayout) -> None:
        for name, tip, idx in TAB_DEFINITIONS:
            btn = self._make_menu_btn(name, tip, idx)
            self.menu_buttons[idx] = btn
            layout.addWidget(btn)
        layout.addStretch()
        self.menu_buttons[TAB_FILE].setStyleSheet(MenuStyle.ACTIVE)

    def _make_menu_btn(self, text: str, tooltip: str, idx: int) -> QPushButton:
        btn = QPushButton(text)
        btn.setToolTip(tooltip)
        btn.setStyleSheet(MenuStyle.NORMAL)
        btn.setFocusPolicy(STRONG_FOCUS)
        btn.setCursor(HAND_CURSOR)
        btn.setMinimumWidth(85)
        btn.setMinimumHeight(34)
        btn.clicked.connect(lambda _checked, i=idx: self._switch_tab(i))
        return btn

    def _switch_tab(self, idx: int) -> None:
        for btn in self.menu_buttons.values():
            btn.setStyleSheet(MenuStyle.NORMAL)
        if idx in self.menu_buttons:
            self.menu_buttons[idx].setStyleSheet(MenuStyle.ACTIVE)
        if 0 <= idx < self.tabs.count():
            self.tabs.setCurrentIndex(idx)
            self._update_status_bar()
        else:
            logger.warning("Invalid tab index: %d", idx)

    def _build_content_area(self) -> QWidget:
        wrap = QWidget()
        lay  = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.tabBar().hide()
        self.tabs.setMinimumSize(600, 450)
        self._build_tabs()

        scroll = QScrollArea()
        scroll.setWidget(self.tabs)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{border:none;}")
        scroll.setMinimumSize(500, 350)

        lay.addWidget(scroll, stretch=1)
        return wrap

    # ------------------------------------------------------------------
    # TABS
    # ------------------------------------------------------------------

    def _build_tabs(self) -> None:
        from ui.tab_file_config import FileTab
        from ui.tab_add_dbc     import AddDbcTab
        from ui.tab_hw_connect  import HwConnectTab
        from ui.tab_trace       import TraceTab
        from ui.tab_send_can    import SendCanTab
        from ui.tab_uds_diag    import UdsDiagTab
        from ui.tab_help        import HelpTab

        # 0 — File
        self.file_tab = FileTab(self)
        self.tabs.addTab(self.file_tab, "File")

        # 1 — Add DBC
        self.add_dbc_tab = AddDbcTab(
            dbc_registry=self.dbc_registry,
            signal_bus=self.signal_bus,
        )
        self.tabs.addTab(self.add_dbc_tab, "Add DBC")

        # 2 — HW connect
        self.hw_connect_tab = HwConnectTab(
            hw_manager=self.hw_manager,
            signal_bus=self.signal_bus,
        )
        self.tabs.addTab(self.hw_connect_tab, "HW connect")

        # main_window.py — _build_tabs()
        self.hw_connect_tab.hw_buses_changed.connect(self._on_hw_buses_changed)

        # DBC loaded / networks changed → push to HwConnectTab + TraceTab + SendCanTab
        self.signal_bus.dbc_loaded.connect(self._on_dbc_loaded)
        self.signal_bus.networks_changed.connect(self._on_networks_changed)

        # 3 — Trace
        self.trace_tab = TraceTab(
            hw_manager=self.hw_manager,
            dbc_registry=self.dbc_registry,
        )
        self.tabs.addTab(self.trace_tab, "Trace")

        # 4 — Send CAN
        # _send_networks is a live dict updated by _sync_send_networks().
        # SendCanTab holds a reference to it — no re-injection needed on change.
        self._send_networks: Dict[str, Any] = {}
        # Ensure hw_buses exists so tab_hw_connect can populate it at connect time.
        # SendCanTab reads self.hw_buses at TX time via main_window reference.
        if not hasattr(self, "hw_buses"):
            self.hw_buses: Dict[str, Any] = {}
        self.send_can_tab = SendCanTab(
            hw_manager=self.hw_manager,
            networks=self._send_networks,
            signal_bus=self.signal_bus,
            main_window=self,   # SendCanTab reads main_window.hw_buses at TX time
        )
        self.tabs.addTab(self.send_can_tab, "SendCAN")

        # 5 — UDS
        self.uds_tab = UdsDiagTab(
            hw_manager=self.hw_manager,
            dbc_registry=self.dbc_registry,
            signal_bus=self.signal_bus,
        )
        self.tabs.addTab(self.uds_tab, "UDS")

        # 6 — Help
        self.help_tab = HelpTab(self)
        self.tabs.addTab(self.help_tab, "Help")

        # Dev console (opt-in via env var)
        if os.environ.get("DEV_CONSOLE_ENABLED", "0") == "1":
            self._add_dev_console()

        # Config loaded from FileTab
        if hasattr(self.file_tab, "config_loaded"):
            self.file_tab.config_loaded.connect(self._on_config_loaded)

        # Auto-switch to Trace on first channel connect
        self.signal_bus.channel_state_changed.connect(self._on_channel_state_nav)

        # Eager initial sync — if config was auto-loaded before tabs were built
        # (e.g. FileTab emits config_loaded during __init__), the networks
        # are already in add_dbc_tab._networks but _sync_send_networks hasn't
        # run yet.  Do it once now so the SendCAN channel combo is pre-populated.
        _initial_nets = getattr(self.add_dbc_tab, "_networks", {})
        if _initial_nets:
            self._sync_send_networks(_initial_nets)

        logger.info("Tabs built: %d", self.tabs.count())

    def _add_dev_console(self) -> None:
        try:
            from ui.tab_dev_console import DevConsoleTab
            self.dev_namespace.update({
                "trace_tab":  self.trace_tab,
                "hw_manager": self.hw_manager,
                "networks":   {},
            })
            self._dev_tab = DevConsoleTab(namespace=self.dev_namespace)
            self.tabs.addTab(self._dev_tab, "Dev")
            dev_idx = self.tabs.count() - 1
            btn = self._make_menu_btn("Dev", "Developer console", dev_idx)
            self.menu_buttons[dev_idx] = btn
            self._menu_layout.insertWidget(self._menu_layout.count() - 1, btn)
            logger.info("Dev console tab added")
        except Exception as e:
            logger.warning("Dev console not loaded: %s", e)

    # ------------------------------------------------------------------
    # SIGNAL BUS WIRING
    # ------------------------------------------------------------------

    def _connect_signal_bus(self) -> None:
        """
        Connect SignalBus → MainWindow slots.

        NOTE: hw_manager already forwarded its own signals → signal_bus
        inside HardwareManager._wire_signal_bus(), so no need to connect
        hw_manager signals directly here.
        """
        sb = self.signal_bus
        sb.channel_state_changed.connect(self._on_channel_state_changed)
        sb.channel_stats_updated.connect(self._on_stats_updated)
        sb.channel_error.connect(self._on_channel_error)
        sb.status_message.connect(self._on_status_message)
        if hasattr(sb, "buses_changed"):
            sb.buses_changed.connect(self._on_buses_changed)

    # ------------------------------------------------------------------
    # SLOTS — hardware state
    # ------------------------------------------------------------------

    @pyqtSlot(int, str)
    def _on_channel_state_changed(self, channel: int, state: str) -> None:
        self._update_status_bar()
        logger.debug("HW CH%d → %s", channel, state)
        if hasattr(self, "trace_tab"):
            self.trace_tab._refresh_channel_labels()
        # Refresh SendCAN channel combo whenever any channel connects/disconnects
        if hasattr(self, "add_dbc_tab"):
            nets = getattr(self.add_dbc_tab, "_networks", {})
            self._sync_send_networks(nets)

    @pyqtSlot(int, float, int)
    def _on_stats_updated(self, channel: int, busload: float, fps: int) -> None:
        self._update_status_bar()

    @pyqtSlot(int, str)
    def _on_channel_error(self, channel: int, message: str) -> None:
        logger.error("CH%d: %s", channel, message)
        if "fatal" in message.lower() or "max retries" in message.lower():
            QMessageBox.warning(
                self, f"Channel {channel} Error",
                f"CH{channel}: {message}",
            )

    @pyqtSlot(str, int)
    def _on_status_message(self, message: str, timeout_ms: int) -> None:
        self.status_bar.showMessage(message, timeout_ms)

    @pyqtSlot(int, str)
    def _on_channel_state_nav(self, channel: int, state: str) -> None:
        """Auto-switch to Trace tab when first channel connects."""
        if state == "CONNECTED" and self.tabs.currentIndex() == TAB_HW_CONNECT:
            self._switch_tab(TAB_TRACE)
            logger.info("Auto-switched to Trace (CH%d connected)", channel)

    @pyqtSlot(dict)
    def _on_buses_changed(self, active_buses: dict) -> None:
        """
        buses_changed emitted by HardwareManager whenever connected-bus
        set changes.  Refresh Trace + SendCAN channel combos + status bar.
        active_buses = {str(channel_index): BusManager}
        """
        nets = getattr(self.add_dbc_tab, "_networks", {}) \
               if hasattr(self, "add_dbc_tab") else {}
        self._sync_trace_networks(nets)
        self._sync_send_networks(nets)
        self._update_status_bar()

    @pyqtSlot(dict)
    def _on_hw_buses_changed(self, hw_buses: dict) -> None:
        """
        Nhận {net_name: BusManager} từ HwConnectTab,
        ghi vào self.hw_buses để SendCanTab đọc tại TX time.
        """
        self.hw_buses = hw_buses

        # Cập nhật combo + trạng thái kết nối trong SendCanTab
        nets = getattr(self.add_dbc_tab, "_networks", {})
        self._sync_send_networks(nets)
        self._update_status_bar()

    # ------------------------------------------------------------------
    # SLOTS — DBC / network bridge
    # ------------------------------------------------------------------

    @pyqtSlot(str, int)
    def _on_dbc_loaded(self, net_name: str, msg_count: int) -> None:
        nets = getattr(self.add_dbc_tab, "_networks", {})
        if hasattr(self, "hw_connect_tab"):
            self.hw_connect_tab.set_networks(nets)
        self._sync_trace_networks(nets)
        self._sync_send_networks(nets)
        logger.debug("DBC loaded '%s' (%d msgs) — networks pushed", net_name, msg_count)

    @pyqtSlot()
    def _on_networks_changed(self) -> None:
        nets = getattr(self.add_dbc_tab, "_networks", {})
        if hasattr(self, "hw_connect_tab"):
            self.hw_connect_tab.set_networks(nets)
        self._sync_trace_networks(nets)
        self._sync_send_networks(nets)

    def _sync_send_networks(self, nets: dict) -> None:
        """
        Keep self._send_networks in sync with the live CanNetwork dict.

        Updates the dict in-place (clear + update) so the reference
        SendCanTab already holds stays valid — no re-injection needed.
        Then calls _populate_channel_combo() so the channel dropdown
        reflects any newly connected buses or freshly loaded DBCs.
        """
        if not hasattr(self, "_send_networks"):
            return
        self._send_networks.clear()
        self._send_networks.update(nets or {})
        if hasattr(self, "send_can_tab"):
            self.send_can_tab._populate_channel_combo()

    def _sync_trace_networks(self, nets: dict) -> None:
        """
        Push BOTH channel labels AND full network objects to TraceTab.

        TraceTab needs:
          • set_channel_labels({ch_idx: name}) — friendly label in 'Chn' column
          • set_networks({name: CanNetwork})    — for DBC decode (signals, node)

        CanNetwork.channel_index is 1-based; HW channel keys are 0-based.
        Channel labels register BOTH representations to be safe.
        """
        if not hasattr(self, "trace_tab"):
            return

        # 1. Channel labels (for display + filter combo)
        if hasattr(self.trace_tab, "set_channel_labels"):
            labels: Dict[Any, str] = {}
            try:
                for name, net in (nets or {}).items():
                    ch_idx = getattr(net, "channel_index", None)
                    if ch_idx is not None:
                        labels[ch_idx]     = name   # 1-based (CanNetwork)
                        labels[ch_idx - 1] = name   # 0-based (HW channel key)
                if labels:
                    self.trace_tab.set_channel_labels(labels)
            except Exception as e:
                logger.debug("set_channel_labels failed: %s", e)

        # 2. Full network objects (for DBC decode)
        if hasattr(self.trace_tab, "set_networks"):
            try:
                self.trace_tab.set_networks(nets or {})
            except Exception as e:
                logger.debug("set_networks failed: %s", e)

    # ------------------------------------------------------------------
    # STATUS BAR
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _update_status_bar(self) -> None:
        try:
            # Network count
            net_count = 0
            if hasattr(self.dbc_registry, "__len__"):
                net_count = len(self.dbc_registry)
            elif hasattr(self.dbc_registry, "_by_name"):
                net_count = len(self.dbc_registry._by_name)

            # Active HW channels
            active_ch = 0
            if hasattr(self.hw_manager, "get_active_channel_count"):
                active_ch = self.hw_manager.get_active_channel_count()
            elif hasattr(self.hw_manager, "_managers"):
                active_ch = sum(
                    1 for m in self.hw_manager._managers.values()
                    if getattr(m, "is_connected", False)
                )

            # Logger recording
            rec = ""
            if getattr(self.can_logger, "is_recording", False):
                n = getattr(self.can_logger, "frames_logged", 0)
                rec = f" | ⏺ REC {n:,}"

            ts = QDateTime.currentDateTime().toString("HH:mm:ss")
            self.status_bar.showMessage(
                f"Protocol: {self.current_protocol}"
                f" | Networks (DBC): {net_count}"
                f" | Active HW channels: {active_ch}"
                f"{rec} | {ts}"
            )
        except Exception as e:
            logger.error("Status bar update failed: %s", e)

    # ------------------------------------------------------------------
    # CONFIG LOADING (from FileTab)
    # ------------------------------------------------------------------

    @pyqtSlot(dict)
    def _on_config_loaded(self, config: dict) -> None:
        try:
            logger.info("Config received from FileTab — processing")
            from models.config_manager       import DbcConfigHelper
            from models.config_compatibility import ConfigUpgrader

            ok, err = DbcConfigHelper.validate_config(config)
            if not ok:
                QMessageBox.warning(self, "Invalid Config",
                                    f"Cấu hình không hợp lệ:\n{err}")
                return

            cfg = ConfigUpgrader.normalize_config(config)

            if hasattr(self, "add_dbc_tab") and hasattr(self.add_dbc_tab, "load_config"):
                self.add_dbc_tab.load_config(cfg)
                nets = getattr(self.add_dbc_tab, "_networks", {})
                if hasattr(self, "hw_connect_tab"):
                    self.hw_connect_tab.set_networks(nets)
                self._sync_trace_networks(nets)
                self._sync_send_networks(nets)
                self._switch_tab(TAB_ADD_DBC)

            self._update_status_bar()

        except Exception as e:
            logger.error("Config load error: %s", e, exc_info=True)
            QMessageBox.critical(self, "Config Error",
                                 f"Lỗi đồng bộ cấu hình:\n{e}")

    # ------------------------------------------------------------------
    # LIFECYCLE
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        reply = QMessageBox.question(
            self, "Quit",
            "Dừng tất cả kênh phần cứng và thoát?",
            BTN_YES | BTN_NO, BTN_YES,
        )
        if reply != BTN_YES:
            event.ignore()
            return

        logger.info("MainWindow closing — shutting down")

        # 1. Stop status timer
        self._status_timer.stop()

        # 2. Stop all HW channels + RX threads
        try:
            if hasattr(self.hw_manager, "stop_all"):
                self.hw_manager.stop_all()
                logger.info("HardwareManager.stop_all() done")
        except Exception as e:
            logger.error("stop_all failed: %s", e)

        # 3. Stop logger if recording
        try:
            if getattr(self.can_logger, "is_recording", False):
                self.can_logger.stop_recording()
        except Exception as e:
            logger.error("Logger stop failed: %s", e)

        event.accept()