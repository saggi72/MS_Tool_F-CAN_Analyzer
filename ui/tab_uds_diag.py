"""
==============================================================
ui/tab_uds_diag.py  —  UDS Diagnostic Tab  (ISO 14229)
==============================================================

File CHÍNH quản lý toàn bộ tab UDS Diagnostic.
Các sub-tab được import từ:
  • ui.tab_console  →  ConsoleTab (+ UdsConfigPanel, LogBoxTable, ConsoleBox)
  • ui.tab_did      →  DidTab
  • ui.tab_dtc      →  DtcTab

Layout:
  ┌─ ChannelBar ─────────────────────────────────────────────┐
  │  Channel[▼] Scan Apps:MS_Tool  P2 P2* STmin  [Connect]   │
  ├─ Sub-tabs ───────────────────────────────────────────────┤
  │  [Console] [DID] [DTC] [...]                             │
  └──────────────────────────────────────────────────────────┘

Threading:
  • Hardware RX chạy trong QueueFeedRxThread (infra sẵn có) → shared rx_queue.
  • UdsEngine chạy trong QThread riêng.
  • Tab giữ một deque nhỏ được nạp bởi feed_can_frame() và drain trên
    QTimer 20 Hz → không bao giờ block GUI dù có hàng nghìn frame/giây.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QComboBox,
    QFrame, QTabWidget, QMessageBox,
)

from core.hardware    import HardwareManager, CanFrame
from core.uds_engine  import (
    UdsEngine, UdsRequest, UdsResponse,
    UdsService, UdsServiceBuilder,
)
from core.dbc_parser  import DbcRegistry
from middleware.signals import SignalBus

from core.diag_config import VehicleRegistry
from core.dll_loader  import EcuDllAdapter
from ui.dialog_update_data import UpdateDataDialog

# Sub-tab imports
from ui.tab_console import ConsoleTab
from ui.tab_did     import DidTab
from ui.tab_dtc     import DtcTab

logger = logging.getLogger(__name__)


# ==============================================================
# STYLE  (dùng chung cho toàn bộ tab UDS)
# ==============================================================

STYLE_SHEET = """
QWidget {
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px;
    color: #1a1a2e;
    background-color: #f5f6fa;
}
QPushButton {
    background: #ffffff; color: #1a1a2e;
    border: 1px solid #c8cdd8; border-radius: 4px;
    padding: 3px 10px; min-height: 24px;
}
QPushButton:hover    { background: #eef0f8; border-color: #9fa8c4; }
QPushButton:pressed  { background: #dde2f0; }
QPushButton:disabled { color: #aab0bf; border-color: #e0e3ea; background: #f8f9fc; }

QPushButton[class="primary"] {
    background: #3b5bdb; color: white; font-weight: 700;
    border: 1px solid #3251c4; min-width: 60px;
}
QPushButton[class="primary"]:hover  { background: #3251c4; }
QPushButton[class="primary"]:pressed{ background: #2a45a8; }

QPushButton[class="danger"] {
    background: #c92a2a; color: white; border: 1px solid #b02525;
}
QPushButton[class="danger"]:hover { background: #b02525; }

QPushButton[class="toggle-off"] {
    background: #ffffff; color: #555e7a;
    border: 1px solid #c8cdd8; border-radius: 4px;
    font-weight: 700; min-width: 50px; padding: 3px 8px;
}
QPushButton[class="toggle-on"] {
    background: #2f9e44; color: white;
    border: 1px solid #277a39; border-radius: 4px;
    font-weight: 700; min-width: 50px; padding: 3px 8px;
}
QPushButton[class="save"] {
    background: #e8f4fd; color: #1971c2;
    border: 1px solid #74c0fc; font-weight: 600;
    min-width: 55px; min-height: 24px;
}
QPushButton[class="save"]:hover { background: #d0ebff; }

QPushButton[class="update"] {
    background: #fff4e6; color: #d9480f;
    border: 1px solid #ffa94d; font-weight: 700;
    min-width: 95px; min-height: 24px;
}
QPushButton[class="update"]:hover { background: #ffe8cc; }

QPushButton[class="connect"] {
    background: #e8f4fd; color: #1971c2;
    border: 1px solid #74c0fc; font-weight: 600;
    min-width: 75px; min-height: 24px; padding: 3px 12px;
}
QPushButton[class="connect"]:hover { background: #d0ebff; }
QPushButton[class="connect-active"] {
    background: #d3f9d8; color: #2b8a3e;
    border: 1px solid #8ce99a; font-weight: 600;
    min-width: 75px; min-height: 24px; padding: 3px 12px;
}

QLineEdit, QComboBox {
    background: #ffffff; border: 1px solid #c8cdd8; border-radius: 3px;
    padding: 2px 6px; min-height: 22px; font-size: 12px;
    selection-background-color: #c5d1f8;
}
QLineEdit:focus, QComboBox:focus { border-color: #3b5bdb; }
QLineEdit[readOnly="true"] { background: #f8f9fc; color: #555e7a; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background: #ffffff; border: 1px solid #c8cdd8;
    selection-background-color: #e8eeff; selection-color: #1a1a2e;
}

QTabWidget::pane {
    border: 1px solid #dde1ea; border-top: none; background: #ffffff;
}
QTabBar::tab {
    background: #edf0f7; border: 1px solid #d0d4e0; border-bottom: none;
    padding: 5px 28px; margin-right: 2px; border-radius: 4px 4px 0 0;
    color: #555e7a; font-size: 12px; min-width: 70px;
}
QTabBar::tab:selected     { background: #2f9e44; color: white; font-weight: 600; }
QTabBar::tab:hover:!selected { background: #e4e8f5; }

QPlainTextEdit#logArea, QPlainTextEdit#dataArea {
    background: #fafbff; border: 1px solid #dde1ea; border-radius: 4px;
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 11px; color: #1a1a2e;
}
QLineEdit#cmdInput {
    font-family: 'Consolas', 'Courier New', monospace; font-size: 13px;
    background: #ffffff; border: 1px solid #c8cdd8; border-radius: 3px;
    padding: 3px 8px; min-height: 26px;
}
QLineEdit#cmdInput:focus { border-color: #3b5bdb; }

QFrame#configFrame {
    background: #ffffff; border: 1px solid #dde1ea; border-radius: 4px;
}
QFrame#channelBarFrame {
    background: #f8f9fc; border-bottom: 1px solid #dde1ea; border-radius: 0;
}

QTableWidget {
    background: #ffffff; border: 1px solid #dde1ea;
    gridline-color: #eef0f7; selection-background-color: #e8eeff;
    selection-color: #1a1a2e; font-size: 11px;
    font-family: 'Consolas', 'Courier New', monospace;
}
QTableWidget QHeaderView::section {
    background: #f0f2f9; border: none; border-right: 1px solid #dde1ea;
    border-bottom: 1px solid #dde1ea; padding: 4px 8px;
    font-weight: 600; font-size: 11px; color: #555e7a;
    font-family: 'Segoe UI', sans-serif;
}
QScrollBar:vertical { background: #f0f2f9; width: 6px; border-radius: 3px; }
QScrollBar::handle:vertical { background: #c8cdd8; border-radius: 3px; min-height: 20px; }
QScrollBar::handle:vertical:hover { background: #9fa8c4; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QSplitter::handle { background: #dde1ea; width: 3px; margin: 2px 0; }
"""


# ==============================================================
# WORKER  (UDS engine trên QThread riêng)
# ==============================================================

class UdsWorkerThread:
    """Lifecycle wrapper bao quanh UdsEngine trong một QThread."""

    def __init__(self, hw_manager: HardwareManager):
        self._hw_manager   = hw_manager
        self._thread:      Optional[QThread]    = None
        self._engine:      Optional[UdsEngine]  = None
        self._req_queue:   queue.Queue          = queue.Queue(maxsize=100)
        self._stop_event:  threading.Event      = threading.Event()

    def start(self) -> UdsEngine:
        self._stop_event.clear()
        self._thread = QThread()
        self._engine = UdsEngine(self._hw_manager, self._req_queue, self._stop_event)
        self._engine.moveToThread(self._thread)
        self._thread.started.connect(self._engine.run)
        self._thread.start()
        return self._engine

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.quit()
            self._thread.wait(3000)
        self._thread = None
        self._engine = None

    def send_request(self, req: UdsRequest) -> bool:
        try:
            self._req_queue.put_nowait(req)
            return True
        except queue.Full:
            logger.warning("UDS request queue full")
            return False

    @property
    def engine(self) -> Optional[UdsEngine]:
        return self._engine


# ==============================================================
# CHANNEL BAR  (HW Setting, top bar)
# ==============================================================

class ChannelBar(QFrame):
    """Top bar: channel selector + Scan + Apps:MS_Tool + P2/P2*/STmin + Connect."""

    connect_clicked = pyqtSignal()
    scan_clicked    = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("channelBarFrame")
        self.setFixedHeight(38)
        self._connected = False
        self._build()

    def _build(self) -> None:
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(6)

        def _lbl(t: str) -> QLabel:
            l = QLabel(t)
            l.setStyleSheet(
                "font-size:12px; color:#555e7a; background:transparent; border:none;"
            )
            return l

        lay.addWidget(_lbl("Channel:"))
        self._cmb_channel = QComboBox()
        self._cmb_channel.setMinimumWidth(160)
        self._cmb_channel.setMaximumWidth(220)
        self._cmb_channel.addItem("Vector VH1640 Ch1 [FD]")
        self._cmb_channel.addItem("Vector VH1640 Ch2 [FD]")
        self._cmb_channel.addItem("PEAK PCAN-USB Ch1")
        lay.addWidget(self._cmb_channel)

        self._btn_scan = QPushButton("Scan")
        self._btn_scan.setFixedWidth(56)
        self._btn_scan.setFixedHeight(26)
        self._btn_scan.clicked.connect(self.scan_clicked.emit)
        lay.addWidget(self._btn_scan)

        lay.addWidget(_lbl("Apps:"))
        self._lbl_app = QLabel("MS_Tool")
        self._lbl_app.setStyleSheet(
            "font-size:12px; color:#1a1a2e; font-weight:600; "
            "background:transparent; border:none;"
        )
        lay.addWidget(self._lbl_app)

        lay.addSpacing(10)

        lay.addWidget(_lbl("P2 (ms)"))
        self._edit_p2 = self._make_edit("1000")
        lay.addWidget(self._edit_p2)

        lay.addWidget(_lbl("P2* (ms)"))
        self._edit_p2s = self._make_edit("1000")
        lay.addWidget(self._edit_p2s)

        lay.addWidget(_lbl("STmin (ms)"))
        self._edit_stmin = self._make_edit("0.2")
        lay.addWidget(self._edit_stmin)

        lay.addSpacing(6)
        self._btn_connect = QPushButton("Connect")
        self._btn_connect.setProperty("class", "connect")
        self._btn_connect.setFixedHeight(26)
        self._btn_connect.clicked.connect(self._on_connect_click)
        lay.addWidget(self._btn_connect)

        lay.addStretch()

    def _make_edit(self, default: str) -> QLineEdit:
        e = QLineEdit(default)
        e.setFixedWidth(68)
        e.setFixedHeight(24)
        return e

    def _on_connect_click(self) -> None:
        self._connected = not self._connected
        if self._connected:
            self._btn_connect.setText("Disconnect")
            self._btn_connect.setProperty("class", "connect-active")
        else:
            self._btn_connect.setText("Connect")
            self._btn_connect.setProperty("class", "connect")
        self._btn_connect.style().unpolish(self._btn_connect)
        self._btn_connect.style().polish(self._btn_connect)
        self.connect_clicked.emit()

    def populate_channels(self, items: list[str]) -> None:
        """Thay thế danh sách channel (ví dụ sau khi Scan)."""
        current = self._cmb_channel.currentText()
        self._cmb_channel.blockSignals(True)
        self._cmb_channel.clear()
        for it in items:
            self._cmb_channel.addItem(it)
        idx = self._cmb_channel.findText(current)
        if idx >= 0:
            self._cmb_channel.setCurrentIndex(idx)
        self._cmb_channel.blockSignals(False)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_timing(self) -> dict:
        def _f(t: str) -> Optional[float]:
            try:    return float(t.strip())
            except ValueError: return None
        return {
            "p2_ms":    _f(self._edit_p2.text()),
            "p2s_ms":   _f(self._edit_p2s.text()),
            "stmin_ms": _f(self._edit_stmin.text()),
        }

    def get_channel_name(self) -> str:
        return self._cmb_channel.currentText()


# ==============================================================
# MAIN UDS DIAG TAB
# ==============================================================

class UdsDiagTab(QWidget):
    """
    Main UDS Diagnostic Tab.

    Quản lý ChannelBar, UdsWorkerThread và ba sub-tab:
      • ConsoleTab  (tab_console.py)
      • DidTab      (tab_did.py)
      • DtcTab      (tab_dtc.py)

    Constructor signature giữ nguyên để tương thích với main_window.
    """

    # Giới hạn bộ đệm nhận frame
    _RX_DRAIN_BURST = 200
    _RX_BUFFER_MAX  = 5000

    def __init__(
        self,
        hw_manager:   HardwareManager,
        dbc_registry: DbcRegistry,
        signal_bus:   SignalBus,
        parent=None,
    ):
        super().__init__(parent)
        self.setStyleSheet(STYLE_SHEET)

        self._hw_manager   = hw_manager
        self._dbc_registry = dbc_registry
        self._signal_bus   = signal_bus

        # Backend objects
        self._vehicle_registry = VehicleRegistry()
        self._dll_adapter      = EcuDllAdapter()
        self._uds_worker       = UdsWorkerThread(hw_manager)
        self._engine: Optional[UdsEngine] = None

        # Frame ingestion (thread-safe deque drain bằng UI timer)
        self._rx_buffer:  deque = deque(maxlen=self._RX_BUFFER_MAX)
        self._rx_lock           = threading.Lock()

        # Tester Present timer
        self._tp_timer = QTimer(self)
        self._tp_timer.setInterval(2000)
        self._tp_timer.timeout.connect(self._on_tp_tick)

        # Log Box pump — 20 Hz
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(50)
        self._log_timer.timeout.connect(self._drain_rx_buffer)
        self._log_timer.start()

        self._build_ui()
        self._start_engine()

    # ─── Engine ───────────────────────────────────────────────

    def _start_engine(self) -> None:
        self._engine = self._uds_worker.start()
        self._engine.response_received.connect(self._on_uds_response)
        self._engine.log_message.connect(self._on_engine_log)

    # ─── UI ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Channel bar
        self._channel_bar = ChannelBar()
        self._channel_bar.connect_clicked.connect(self._on_connect_clicked)
        self._channel_bar.scan_clicked.connect(self._on_scan_clicked)
        root.addWidget(self._channel_bar)

        # Sub-tabs container
        container = QWidget()
        container.setStyleSheet("background:#f5f6fa;")
        cl = QVBoxLayout(container)
        cl.setContentsMargins(6, 4, 6, 6)
        cl.setSpacing(0)

        self._tabs = QTabWidget()

        # ── Console ──────────────────────────────────────────
        self._tab_console = ConsoleTab(self._vehicle_registry)
        self._tab_console.send_request.connect(self._on_send_request)
        self._tab_console.tp_toggled.connect(self._on_tp_toggle_changed)
        self._tab_console.request_open_dialog.connect(self._open_update_dialog)
        self._tab_console.ecu_changed.connect(self._on_ecu_selected)
        self._tab_console.dll_changed.connect(self._on_dll_relink)
        self._tab_console.config_saved.connect(self._on_save_current_dll)
        self._tabs.addTab(self._tab_console, "Console")

        # ── DID ───────────────────────────────────────────────
        self._tab_did = DidTab()
        self._tab_did.send_request.connect(self._on_send_request)
        self._tabs.addTab(self._tab_did, "DID")

        # ── DTC ───────────────────────────────────────────────
        self._tab_dtc = DtcTab()
        self._tab_dtc.send_request.connect(self._on_send_request)
        self._tabs.addTab(self._tab_dtc, "DTC")

        # ── Placeholder ───────────────────────────────────────
        ph = QWidget()
        ph_lbl = QLabel("More services coming soon...")
        ph_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph_lbl.setStyleSheet(
            "color:#9fa8c4; font-size:13px; font-style:italic;"
        )
        QVBoxLayout(ph).addWidget(ph_lbl)
        self._tabs.addTab(ph, "···")

        cl.addWidget(self._tabs)
        root.addWidget(container, stretch=1)

    # ─── HW bar actions ───────────────────────────────────────

    def _on_scan_clicked(self) -> None:
        """Trigger hardware rescan qua HardwareManager."""
        self._tab_console.console_box.append_info("ℹ  Scanning hardware...", "info")
        try:
            scanner = getattr(self._hw_manager, "scan", None)
            channels: list[str] = []
            if callable(scanner):
                result = scanner()
                if isinstance(result, list):
                    channels = [str(x) for x in result]
            if channels:
                self._channel_bar.populate_channels(channels)
                self._tab_console.console_box.append_info(
                    f"ℹ  Scan complete: {len(channels)} channel(s)", "info"
                )
            else:
                self._tab_console.console_box.append_info(
                    "ℹ  Scan returned no channels (using built-in defaults)", "warn"
                )
        except Exception as ex:  # noqa: BLE001
            logger.exception("Scan failed")
            self._tab_console.console_box.append_info(f"✕ Scan error: {ex}", "err")

    def _on_connect_clicked(self) -> None:
        timing = self._channel_bar.get_timing()
        ch = self._channel_bar.get_channel_name()
        state = "CONNECTED" if self._channel_bar.is_connected else "DISCONNECTED"
        self._tab_console.console_box.append_info(
            f"ℹ  [{state}] {ch}  ·  P2={timing['p2_ms']}  "
            f"P2*={timing['p2s_ms']}  STmin={timing['stmin_ms']}",
            "info",
        )

    # ─── Update Data dialog ───────────────────────────────────

    def _open_update_dialog(self) -> None:
        preselect = self._tab_console.config_panel.current_vehicle() or None
        dlg = UpdateDataDialog(self, registry=self._vehicle_registry,
                               preselect=preselect)
        if dlg.exec() == UpdateDataDialog.DialogCode.Accepted and dlg.saved_vehicle:
            self._tab_console.config_panel.refresh_vehicles(
                preselect=dlg.saved_vehicle
            )
            self._tab_console.console_box.append_info(
                f"ℹ  Vehicle saved: {dlg.saved_vehicle}", "info"
            )

    # ─── ECU selection / DLL ──────────────────────────────────

    @pyqtSlot(str, str, dict)
    def _on_ecu_selected(self, vehicle: str, ecu: str, info: dict) -> None:
        dll = info.get("dll_path") or ""
        if dll:
            ok = self._dll_adapter.load(dll)
            self._tab_console.console_box.set_decoder(
                self._dll_adapter if ok else None
            )
            kind = "info" if ok else "warn"
            msg = (f"ℹ  {ecu}: DLL loaded — {Path(dll).name}"
                   if ok else f"⚠  {ecu}: DLL not loadable, using fallback decode")
        else:
            self._dll_adapter.unload()
            self._tab_console.console_box.set_decoder(None)
            msg  = f"ℹ  {ecu}: no DLL configured — fallback decode active"
            kind = "info"
        self._tab_console.console_box.append_info(msg, kind)

    @pyqtSlot(str, str, str)
    def _on_dll_relink(self, vehicle: str, ecu: str, new_path: str) -> None:
        ok = self._dll_adapter.load(new_path)
        self._tab_console.console_box.set_decoder(
            self._dll_adapter if ok else None
        )
        self._tab_console.console_box.append_info(
            f"{'ℹ' if ok else '⚠'}  DLL re-linked for {ecu}: {Path(new_path).name}",
            "info" if ok else "warn",
        )

    def _on_save_current_dll(self) -> None:
        cfg = self._tab_console.config_panel
        vehicle, ecu = cfg.current_vehicle(), cfg.current_ecu()
        if not vehicle or not ecu:
            return
        path = cfg.current_dll_path()
        if self._vehicle_registry.update_ecu_dll(vehicle, ecu, path):
            self._tab_console.console_box.append_info(
                f"ℹ  Saved DLL link for {vehicle} / {ecu}", "info"
            )
        else:
            self._tab_console.console_box.append_info(
                "✕ Failed to save DLL link", "err"
            )

    # ─── UDS send ─────────────────────────────────────────────

    @pyqtSlot(bytes, str)
    def _on_send_request(self, payload: bytes, description: str) -> None:
        ids = self._tab_console.config_panel.current_ids()
        if not ids:
            self._tab_console.console_box.append_info(
                "✕ No valid Request/Response IDs — select an ECU first", "err"
            )
            return
        tx_id, rx_id = ids
        req = UdsRequest(
            service_id=payload[0],
            payload=payload,
            channel=1,
            tx_id=tx_id,
            rx_id=rx_id,
            timeout_ms=1000,
            description=description,
        )
        self._tab_console.console_box.append_tx(description, payload)
        if not self._uds_worker.send_request(req):
            self._tab_console.console_box.append_info(
                "✕ Request queue full — engine busy", "err"
            )

    # ─── UDS engine callbacks ─────────────────────────────────

    @pyqtSlot(object)
    def _on_uds_response(self, response: UdsResponse) -> None:
        if not response.success:
            self._tab_console.console_box.append_info(
                f"✕ {getattr(response, 'description', 'UDS')} failed", "err"
            )
            return

        service = response.service_id
        data    = response.data
        elapsed = response.elapsed_ms

        did = None
        if service == UdsService.READ_DATA_BY_ID and len(data) >= 2:
            did = (data[0] << 8) | data[1]

        self._tab_console.console_box.append_rx(data, elapsed, did=did)

        if service == UdsService.READ_DATA_BY_ID and did is not None:
            raw = data[2:].hex(" ").upper()
            decoded = (
                self._dll_adapter.decode_payload(did, data[2:])
                if self._dll_adapter.is_loaded
                else EcuDllAdapter.fallback_decode(data[2:])
            )
            self._tab_did.add_did_result(did, "", decoded, raw)

        elif service == UdsService.READ_DTC:
            self._tab_dtc.clear_dtc_table()
            self._parse_dtc_response(data)

    def _parse_dtc_response(self, data: bytes) -> None:
        if len(data) < 2:
            return
        idx = 2
        while idx + 3 < len(data):
            dtc_bytes = data[idx:idx + 3]
            status    = data[idx + 3]
            dtc_code  = (dtc_bytes[0] << 16) | (dtc_bytes[1] << 8) | dtc_bytes[2]
            self._tab_dtc.add_dtc(
                f"P{dtc_code:06X}",
                f"0x{status:02X}",
                "",
                dtc_bytes.hex(" ").upper(),
            )
            idx += 4

    @pyqtSlot(str)
    def _on_engine_log(self, text: str) -> None:
        if "✕" in text or "Error" in text or "Timeout" in text:
            kind = "err"
        elif "⏳" in text or "pending" in text.lower():
            kind = "pending"
        else:
            kind = "info"
        self._tab_console.console_box.append_info(text, kind)

    # ─── Tester Present ───────────────────────────────────────

    @pyqtSlot(bool, int)
    def _on_tp_toggle_changed(self, enabled: bool, tx_id: int) -> None:
        if enabled:
            if self._engine and tx_id:
                self._engine.set_tester_present(tx_id, 1, 2000)
            self._tp_timer.start()
            self._tab_console.console_box.append_info(
                f"ℹ  Tester Present ON  (TX=0x{tx_id:03X})", "info"
            )
        else:
            self._tp_timer.stop()
            if self._engine:
                self._engine.set_tester_present(None, 1)
            self._tab_console.console_box.append_info(
                "ℹ  Tester Present OFF", "info"
            )

    def _on_tp_tick(self) -> None:
        pass  # Engine tự điều khiển việc gửi

    # ─── Frame ingestion ──────────────────────────────────────

    def feed_can_frame(self, frame: CanFrame) -> None:
        """
        Gọi từ bất kỳ thread nào (thường là drainer của main_window hoặc
        trực tiếp từ hardware). Đẩy frame vào:
          • deque nội bộ → drain bởi UI timer vào Log Box
          • UdsEngine    → correlate response
        """
        try:
            with self._rx_lock:
                self._rx_buffer.append(frame)
        except Exception:  # noqa: BLE001
            logger.exception("feed_can_frame: buffer append failed")
        if self._engine:
            try:
                self._engine.feed_frame(frame)
            except Exception:  # noqa: BLE001
                logger.exception("UDS engine feed_frame failed")

    def _drain_rx_buffer(self) -> None:
        """Lấy tối đa _RX_DRAIN_BURST frame mỗi tick và append vào Log Box."""
        with self._rx_lock:
            batch = []
            for _ in range(self._RX_DRAIN_BURST):
                if not self._rx_buffer:
                    break
                batch.append(self._rx_buffer.popleft())
        if not batch:
            return
        for fr in batch:
            try:
                self._tab_console.log_box.append_frame(
                    ts=float(getattr(fr, "timestamp", time.time())),
                    can_id=int(getattr(fr, "can_id", 0)),
                    dlc=int(getattr(fr, "dlc", 0)),
                    data=bytes(getattr(fr, "data", b"")),
                )
            except Exception:  # noqa: BLE001
                logger.exception("Log Box append failed for frame")

    # ─── Lifecycle ────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._log_timer.stop()
        self._tp_timer.stop()
        self._uds_worker.stop()
        super().closeEvent(event)