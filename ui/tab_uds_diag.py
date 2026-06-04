"""
==============================================================
ui/tab_uds_diag.py  —  UDS Diagnostic Tab  (ISO 14229)
==============================================================

Layout theo UI ảnh:
  ┌─ Top bar ───────────────────────────────────────────────┐
  │  Channel[___▼] Scan Apps:MS_Tool  P2(ms)[__] P2*(ms)[__]│
  │  STmin(ms)[__]  [Connect]                               │
  ├─ Sub-tabs ──────────────────────────────────────────────┤
  │  [Console] [DID] [DTC] [...]                            │
  ├─────────────────────────────────────────────────────────┤
  │  Config row:                                            │
  │    Vehicle [___]  Request ID [7DF]  DLL File [___]      │
  │    ECU     [___]  Response ID[7E8]  Change DLL File     │
  │                                                         │
  │  Command bar:                                           │
  │    [Type the command here.....] [Send] [Clear]          │
  │    [Tester Present (S3E)] [ON]                          │
  │    Press Enter or Push Send for Send Command            │
  │                                                         │
  │  ┌─ Log (left) ──┐  ┌─ Data request/response (right) ─┐│
  │  │               │  │                                  ││
  │  └───────────────┘  └──────────────────────────────────┘│
  └─────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Optional

from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, pyqtSlot, QTimer,
)
from PyQt6.QtGui import QFont, QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QSplitter, QGroupBox, QLabel, QPushButton, QLineEdit,
    QComboBox, QCheckBox, QPlainTextEdit, QSpinBox,
    QFrame, QTabWidget, QSizePolicy, QScrollArea,
    QMessageBox, QButtonGroup, QToolButton,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QTableWidget,
    QTableWidgetItem, QAbstractItemView, QTextEdit,
    QFileDialog,
)

from core.hardware import HardwareManager, CanFrame
from core.uds_engine import (
    UdsEngine, UdsRequest, UdsResponse,
    UdsService, UdsSession, UdsNrc, UdsServiceBuilder,
)
from core.dbc_parser import DbcRegistry
from middleware.signals import SignalBus

logger = logging.getLogger(__name__)


# ==============================================================
# STYLE CONSTANTS
# ==============================================================

STYLE_SHEET = """
/* ── Global ─────────────────────────────────────────── */
QWidget {
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px;
    color: #1a1a2e;
    background-color: #f5f6fa;
}

/* ── Group boxes ─────────────────────────────────────── */
QGroupBox {
    border: 1px solid #dde1ea;
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 6px;
    background: #ffffff;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    font-weight: 600;
    font-size: 11px;
    color: #555e7a;
    left: 10px;
}

/* ── Buttons ─────────────────────────────────────────── */
QPushButton {
    background: #ffffff;
    color: #1a1a2e;
    border: 1px solid #c8cdd8;
    border-radius: 4px;
    padding: 3px 10px;
    min-height: 24px;
}
QPushButton:hover {
    background: #eef0f8;
    border-color: #9fa8c4;
}
QPushButton:pressed {
    background: #dde2f0;
}
QPushButton:disabled {
    color: #aab0bf;
    border-color: #e0e3ea;
    background: #f8f9fc;
}

QPushButton[class="primary"] {
    background: #3b5bdb;
    color: white;
    border: 1px solid #3251c4;
    font-weight: 600;
    min-width: 60px;
}
QPushButton[class="primary"]:hover { background: #3251c4; }
QPushButton[class="primary"]:pressed { background: #2a45a8; }

QPushButton[class="success"] {
    background: #2f9e44;
    color: white;
    border: 1px solid #277a39;
}
QPushButton[class="success"]:hover { background: #277a39; }

QPushButton[class="warning"] {
    background: #e67700;
    color: white;
    border: 1px solid #c96500;
}
QPushButton[class="warning"]:hover { background: #c96500; }

QPushButton[class="danger"] {
    background: #c92a2a;
    color: white;
    border: 1px solid #b02525;
}
QPushButton[class="danger"]:hover { background: #b02525; }

/* Toggle button ON/OFF */
QPushButton[class="toggle-off"] {
    background: #f1f3f9;
    color: #555e7a;
    border: 1px solid #c8cdd8;
    border-radius: 4px;
    font-weight: 700;
    min-width: 50px;
    min-height: 24px;
    font-size: 11px;
}
QPushButton[class="toggle-on"] {
    background: #2f9e44;
    color: white;
    border: 1px solid #277a39;
    border-radius: 4px;
    font-weight: 700;
    min-width: 50px;
    min-height: 24px;
    font-size: 11px;
}

/* TP (Tester Present) button - shown right of command row */
QPushButton[class="tp-off"] {
    background: #f1f3f9;
    color: #555e7a;
    border: 1px solid #c8cdd8;
    border-radius: 4px;
    font-weight: 700;
    min-width: 40px;
    min-height: 26px;
    font-size: 12px;
}
QPushButton[class="tp-on"] {
    background: #2f9e44;
    color: white;
    border: 1px solid #277a39;
    border-radius: 4px;
    font-weight: 700;
    min-width: 40px;
    min-height: 26px;
    font-size: 12px;
}

/* Save button */
QPushButton[class="save"] {
    background: #e8f4fd;
    color: #1971c2;
    border: 1px solid #74c0fc;
    font-weight: 600;
    min-width: 55px;
    min-height: 24px;
}
QPushButton[class="save"]:hover { background: #d0ebff; }

/* Connect button */
QPushButton[class="connect"] {
    background: #e8f4fd;
    color: #1971c2;
    border: 1px solid #74c0fc;
    font-weight: 600;
    min-width: 75px;
    min-height: 24px;
    padding: 3px 12px;
}
QPushButton[class="connect"]:hover { background: #d0ebff; }
QPushButton[class="connect-active"] {
    background: #d3f9d8;
    color: #2b8a3e;
    border: 1px solid #8ce99a;
    font-weight: 600;
    min-width: 75px;
    min-height: 24px;
    padding: 3px 12px;
}

/* ── LineEdit / SpinBox ───────────────────────────────── */
QLineEdit, QSpinBox {
    background: #ffffff;
    border: 1px solid #c8cdd8;
    border-radius: 3px;
    padding: 2px 6px;
    min-height: 22px;
    selection-background-color: #c5d1f8;
    font-size: 12px;
}
QLineEdit:focus, QSpinBox:focus {
    border-color: #3b5bdb;
}
QLineEdit[readOnly="true"] {
    background: #f8f9fc;
    color: #555e7a;
}

/* ── ComboBox ─────────────────────────────────────────── */
QComboBox {
    background: #ffffff;
    border: 1px solid #c8cdd8;
    border-radius: 3px;
    padding: 2px 6px;
    min-height: 22px;
    font-size: 12px;
}
QComboBox:focus { border-color: #3b5bdb; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #c8cdd8;
    selection-background-color: #e8eeff;
    selection-color: #1a1a2e;
}

/* ── Sub-tabs ─────────────────────────────────────────── */
QTabWidget::pane {
    border: 1px solid #dde1ea;
    border-top: none;
    background: #ffffff;
}
QTabBar::tab {
    background: #edf0f7;
    border: 1px solid #d0d4e0;
    border-bottom: none;
    padding: 5px 28px;
    margin-right: 2px;
    border-radius: 4px 4px 0 0;
    color: #555e7a;
    font-size: 12px;
    min-width: 70px;
}
QTabBar::tab:selected {
    background: #2f9e44;
    color: white;
    font-weight: 600;
}
QTabBar::tab:hover:!selected { background: #e4e8f5; }

/* ── Log / Data areas ─────────────────────────────────── */
QPlainTextEdit#logArea, QPlainTextEdit#dataArea {
    background: #fafbff;
    border: 1px solid #dde1ea;
    border-radius: 4px;
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 11px;
    color: #1a1a2e;
}

/* ── Command input ────────────────────────────────────── */
QLineEdit#cmdInput {
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 12px;
    background: #ffffff;
    border: 1px solid #c8cdd8;
    border-radius: 3px;
    padding: 3px 8px;
    min-height: 26px;
}
QLineEdit#cmdInput:focus { border-color: #3b5bdb; }

/* ── Channel bar line edits ───────────────────────────── */
QLineEdit#channelBarEdit {
    font-size: 12px;
    background: #ffffff;
    border: 1px solid #c8cdd8;
    border-radius: 3px;
    padding: 2px 6px;
    min-height: 22px;
    max-width: 70px;
}

/* ── Table ────────────────────────────────────────────── */
QTableWidget {
    background: #ffffff;
    border: 1px solid #dde1ea;
    gridline-color: #eef0f7;
    selection-background-color: #e8eeff;
    selection-color: #1a1a2e;
    font-size: 12px;
}
QTableWidget QHeaderView::section {
    background: #f0f2f9;
    border: none;
    border-right: 1px solid #dde1ea;
    border-bottom: 1px solid #dde1ea;
    padding: 4px 8px;
    font-weight: 600;
    font-size: 11px;
    color: #555e7a;
}

/* ── Splitter ─────────────────────────────────────────── */
QSplitter::handle {
    background: #dde1ea;
    width: 3px;
    margin: 2px 0;
}

/* ── Scrollbar ────────────────────────────────────────── */
QScrollBar:vertical {
    background: #f0f2f9;
    width: 6px;
    border-radius: 3px;
}
QScrollBar::handle:vertical {
    background: #c8cdd8;
    border-radius: 3px;
    min-height: 20px;
}
QScrollBar::handle:vertical:hover { background: #9fa8c4; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }

/* Config frame */
QFrame#configFrame {
    background: #ffffff;
    border: 1px solid #dde1ea;
    border-radius: 4px;
}

/* Channel bar frame */
QFrame#channelBarFrame {
    background: #f8f9fc;
    border-bottom: 1px solid #dde1ea;
    border-radius: 0px;
}

/* Separator label */
QLabel#sepLabel {
    color: #9fa8c4;
    font-size: 11px;
    background: transparent;
    border: none;
}
"""


# ==============================================================
# WORKER THREAD
# ==============================================================

class UdsWorkerThread:
    """Quản lý lifecycle của UdsEngine trong QThread."""

    def __init__(self, hw_manager: HardwareManager):
        self._hw_manager    = hw_manager
        self._thread:  Optional[QThread]   = None
        self._engine:  Optional[UdsEngine] = None
        self._req_queue: queue.Queue       = queue.Queue(maxsize=100)
        self._stop_event: threading.Event  = threading.Event()

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
# CHANNEL BAR  (top of the entire UDS tab)
# ==============================================================

class ChannelBar(QFrame):
    """
    Top channel/timing config bar — khớp với ảnh:
      Channel [combo▼]  Scan  Apps: MS_Tool  P2(ms)[__]  P2*(ms)[__]  STmin(ms)[__]  [Connect]  [×]
    """

    connect_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("channelBarFrame")
        self.setFixedHeight(36)
        self._connected = False
        self._build()

    def _build(self) -> None:
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(6)

        _lbl = lambda t: self._make_label(t)

        lay.addWidget(_lbl("Channel:"))

        self._cmb_channel = QComboBox()
        self._cmb_channel.setMinimumWidth(160)
        self._cmb_channel.setMaximumWidth(200)
        self._cmb_channel.addItem("Vector VH1640 Ch1 [FD]")
        self._cmb_channel.addItem("Vector VH1640 Ch2 [FD]")
        self._cmb_channel.addItem("PEAK PCAN-USB Ch1")
        lay.addWidget(self._cmb_channel)

        lay.addWidget(_lbl("Scan"))

        self._btn_x1 = self._make_x_btn()
        lay.addWidget(self._btn_x1)

        lay.addWidget(_lbl("Apps:"))
        self._lbl_app = QLabel("MS_Tool")
        self._lbl_app.setStyleSheet(
            "font-size:12px; color:#1a1a2e; background:transparent; border:none;"
        )
        lay.addWidget(self._lbl_app)

        self._btn_x2 = self._make_x_btn()
        lay.addWidget(self._btn_x2)

        lay.addSpacing(8)

        # P2
        lay.addWidget(_lbl("P2 (ms)"))
        self._edit_p2 = self._make_edit("P2", "2200")
        lay.addWidget(self._edit_p2)

        # P2*
        lay.addWidget(_lbl("P2* (ms)"))
        self._edit_p2s = self._make_edit("P2*", "2200")
        lay.addWidget(self._edit_p2s)

        self._btn_x3 = self._make_x_btn()
        lay.addWidget(self._btn_x3)

        # STmin
        lay.addWidget(_lbl("STmin (ms)"))
        self._edit_stmin = self._make_edit("STmin", "0.2")
        lay.addWidget(self._edit_stmin)

        lay.addSpacing(4)

        # Connect
        self._btn_connect = QPushButton("Connect")
        self._btn_connect.setProperty("class", "connect")
        self._btn_connect.setFixedHeight(26)
        self._btn_connect.clicked.connect(self._on_connect_click)
        lay.addWidget(self._btn_connect)

        self._btn_x4 = self._make_x_btn()
        lay.addWidget(self._btn_x4)

        lay.addStretch()

    def _make_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "font-size:12px; color:#555e7a; background:transparent; border:none;"
        )
        return lbl

    def _make_edit(self, name: str, default_value: str = "") -> QLineEdit:
        e = QLineEdit()
        e.setObjectName("channelBarEdit")
        e.setFixedWidth(68)
        e.setFixedHeight(24)
        e.setText(default_value)
        e.setPlaceholderText("")
        return e

    def _make_x_btn(self) -> QPushButton:
        btn = QPushButton("×")
        btn.setFixedSize(18, 18)
        btn.setStyleSheet(
            "QPushButton{background:transparent;border:none;color:#9fa8c4;"
            "font-size:14px;font-weight:700;padding:0;}"
            "QPushButton:hover{color:#c92a2a;}"
        )
        return btn

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

    def get_timing(self) -> dict:
        def _safe_int(txt):
            try:
                return int(txt.strip())
            except ValueError:
                return None
        return {
            "p2_ms":    _safe_int(self._edit_p2.text()),
            "p2s_ms":   _safe_int(self._edit_p2s.text()),
            "stmin_ms": _safe_int(self._edit_stmin.text()),
        }

    def get_channel_name(self) -> str:
        return self._cmb_channel.currentText()


# ==============================================================
# CONFIG PANEL
# ==============================================================

class UdsConfigPanel(QFrame):
    """
    Config panel — 2 rows:
      Row 0: Vehicle [___]  Request ID [7DF]  DLL File [path]          [Save]
      Row 1: ECU     [___]  Response ID[7E8]  [Change DLL File]        [Save] (span)
    """

    dll_changed  = pyqtSignal(str)
    config_saved = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("configFrame")
        self._build()

    def _build(self) -> None:
        root = QGridLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setHorizontalSpacing(6)
        root.setVerticalSpacing(4)

        S = "color:#555e7a; font-size:11px; font-weight:500; background:transparent; border:none;"

        # ── Row 0 ──────────────────────────────────────────────
        root.addWidget(self._lbl("Vehicle", S), 0, 0)

        self._edit_vehicle = QLineEdit()
        self._edit_vehicle.setPlaceholderText("Vehicle name")
        self._edit_vehicle.setFixedWidth(120)
        root.addWidget(self._edit_vehicle, 0, 1)

        root.addWidget(self._lbl("Request ID", S), 0, 2)

        self._edit_req_id = QLineEdit("7DF")
        self._edit_req_id.setFixedWidth(70)
        root.addWidget(self._edit_req_id, 0, 3)

        root.addWidget(self._lbl("DLL File", S), 0, 4)

        self._edit_dll = QLineEdit()
        self._edit_dll.setPlaceholderText("Path to DLL file...")
        self._edit_dll.setReadOnly(True)
        self._edit_dll.setMinimumWidth(200)
        root.addWidget(self._edit_dll, 0, 5)

        self._btn_save = QPushButton("Save")
        self._btn_save.setProperty("class", "save")
        self._btn_save.setFixedWidth(55)
        self._btn_save.clicked.connect(self.config_saved.emit)
        root.addWidget(self._btn_save, 0, 6, 2, 1)  # span 2 rows

        # ── Row 1 ──────────────────────────────────────────────
        root.addWidget(self._lbl("ECU", S), 1, 0)

        self._edit_ecu = QLineEdit()
        self._edit_ecu.setPlaceholderText("ECU name")
        self._edit_ecu.setFixedWidth(120)
        root.addWidget(self._edit_ecu, 1, 1)

        root.addWidget(self._lbl("Response ID", S), 1, 2)

        self._edit_resp_id = QLineEdit("7E8")
        self._edit_resp_id.setFixedWidth(70)
        root.addWidget(self._edit_resp_id, 1, 3)

        self._btn_change_dll = QPushButton("Change DLL File")
        self._btn_change_dll.clicked.connect(self._on_change_dll)
        self._btn_change_dll.setFixedWidth(130)
        root.addWidget(self._btn_change_dll, 1, 4, 1, 2)

        root.setColumnStretch(7, 1)

    @staticmethod
    def _lbl(text: str, style: str) -> QLabel:
        l = QLabel(text)
        l.setStyleSheet(style)
        return l

    def _on_change_dll(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select DLL File", "", "DLL Files (*.dll);;All Files (*)"
        )
        if path:
            self._edit_dll.setText(path)
            self.dll_changed.emit(path)

    def get_config(self) -> Optional[tuple]:
        try:
            tx = int(self._edit_req_id.text().strip(), 16)
            rx = int(self._edit_resp_id.text().strip(), 16)
            return tx, rx
        except ValueError:
            return None

    @property
    def vehicle(self) -> str:
        return self._edit_vehicle.text().strip()

    @property
    def ecu(self) -> str:
        return self._edit_ecu.text().strip()

    @property
    def dll_path(self) -> str:
        return self._edit_dll.text().strip()


# ==============================================================
# LOG + DATA PANEL
# ==============================================================

class LogDataPanel(QWidget):
    """Log (left) + Data request/response (right) với QSplitter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── Left: Log ───────────────────────────────────────────
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(2)

        lbl_log = QLabel("Log")
        lbl_log.setStyleSheet("color:#555e7a; font-size:11px; font-weight:600; background:transparent;")
        lv.addWidget(lbl_log)

        self._log = QPlainTextEdit()
        self._log.setObjectName("logArea")
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(3000)
        lv.addWidget(self._log)

        # ── Right: Data ─────────────────────────────────────────
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(2)

        lbl_data = QLabel("Data request and response show here")
        lbl_data.setStyleSheet("color:#555e7a; font-size:11px; font-weight:600; background:transparent;")
        rv.addWidget(lbl_data)

        self._data = QPlainTextEdit()
        self._data.setObjectName("dataArea")
        self._data.setReadOnly(True)
        self._data.setMaximumBlockCount(3000)
        rv.addWidget(self._data)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([280, 560])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        lay.addWidget(splitter)

    # ── public API ────────────────────────────────────────────

    def append_log(self, text: str, kind: str = "info") -> None:
        ts = time.strftime("%H:%M:%S")
        color = {
            "rx":      "#1971c2",
            "tx":      "#2f9e44",
            "err":     "#c92a2a",
            "warn":    "#e67700",
            "pending": "#5c7cfa",
            "info":    "#555e7a",
        }.get(kind, "#555e7a")
        self._log.appendHtml(
            f'<span style="font-family:Consolas,monospace;font-size:11px;">'
            f'<span style="color:#b0b7c9;">[{ts}]</span> '
            f'<span style="color:{color};">{text}</span></span>'
        )

    def append_data(self, text: str, kind: str = "info") -> None:
        ts = time.strftime("%H:%M:%S")
        color = {
            "rx":      "#1971c2",
            "tx":      "#2f9e44",
            "err":     "#c92a2a",
            "warn":    "#e67700",
            "pending": "#5c7cfa",
            "info":    "#1a1a2e",
        }.get(kind, "#1a1a2e")
        self._data.appendHtml(
            f'<span style="font-family:Consolas,monospace;font-size:11px;">'
            f'<span style="color:#b0b7c9;">[{ts}]</span> '
            f'<span style="color:{color};">{text}</span></span>'
        )

    def clear_log(self) -> None:
        self._log.clear()

    def clear_data(self) -> None:
        self._data.clear()


# ==============================================================
# CONSOLE SUB-TAB
# ==============================================================

class ConsoleTab(QWidget):
    """
    Console tab — layout khớp ảnh:
      1. Config panel (2 rows)
      2. Command row: [cmdInput] [Send] [Clear]    [Tester Present (S3E)] [ON]
      3. Hint label: "Press Enter or Push Send for Send Command"
      4. Log + Data (splitter, stretch=1)
    """

    send_request = pyqtSignal(bytes, str)
    tp_toggled   = pyqtSignal(bool, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tp_enabled = False
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(5)

        # ── 1. Config panel ────────────────────────────────────
        self._config = UdsConfigPanel()
        root.addWidget(self._config)

        # ── 2. Command row + Tester Present ────────────────────
        cmd_row = QHBoxLayout()
        cmd_row.setSpacing(5)

        self._edit_cmd = QLineEdit()
        self._edit_cmd.setObjectName("cmdInput")
        self._edit_cmd.setPlaceholderText("Type the command here")
        self._edit_cmd.returnPressed.connect(self._on_send)
        cmd_row.addWidget(self._edit_cmd)

        self._btn_send = QPushButton("Send")
        self._btn_send.setProperty("class", "primary")
        self._btn_send.setFixedWidth(60)
        self._btn_send.setFixedHeight(28)
        self._btn_send.clicked.connect(self._on_send)
        cmd_row.addWidget(self._btn_send)

        self._btn_clear = QPushButton("Clear")
        self._btn_clear.setFixedWidth(52)
        self._btn_clear.setFixedHeight(28)
        self._btn_clear.clicked.connect(self._on_clear)
        cmd_row.addWidget(self._btn_clear)

        # Spacer before Tester Present
        cmd_row.addSpacing(10)

        # Tester Present group (box look)
        tp_frame = QFrame()
        tp_frame.setStyleSheet(
            "QFrame{background:#f8f9fc;border:1px solid #dde1ea;"
            "border-radius:4px;padding:0;}"
        )
        tp_lay = QHBoxLayout(tp_frame)
        tp_lay.setContentsMargins(8, 2, 6, 2)
        tp_lay.setSpacing(6)

        lbl_tp = QLabel("Tester Present (S3E)")
        lbl_tp.setStyleSheet(
            "font-size:11px;color:#555e7a;background:transparent;border:none;"
        )
        tp_lay.addWidget(lbl_tp)

        self._btn_tp = QPushButton("ON")
        self._btn_tp.setCheckable(True)
        self._btn_tp.setFixedWidth(38)
        self._btn_tp.setFixedHeight(24)
        self._btn_tp.setProperty("class", "tp-off")
        self._btn_tp.toggled.connect(self._on_tp_btn_toggled)
        tp_lay.addWidget(self._btn_tp)

        cmd_row.addWidget(tp_frame)
        root.addLayout(cmd_row)

        # ── 3. Hint ────────────────────────────────────────────
        hint = QLabel("Press Enter or Push Send for Send Command")
        hint.setStyleSheet(
            "color:#9fa8c4; font-size:10px; font-style:italic; background:transparent;"
        )
        hint.setAlignment(Qt.AlignmentFlag.AlignRight)
        root.addWidget(hint)

        # ── 4. Log + Data ──────────────────────────────────────
        self._log_data = LogDataPanel()
        root.addWidget(self._log_data, stretch=1)

    # ── internal ──────────────────────────────────────────────

    def _on_clear(self) -> None:
        self._log_data.clear_log()
        self._log_data.clear_data()

    def _on_send(self) -> None:
        cmd = self._edit_cmd.text().strip()
        if not cmd:
            return
        try:
            raw = bytes.fromhex(cmd.replace(" ", ""))
        except ValueError:
            self.log("✕ Invalid hex command", "err")
            return
        if not raw:
            return
        self._edit_cmd.clear()
        self.send_request.emit(raw, f"Console 0x{raw[0]:02X}")

    def _on_tp_btn_toggled(self, checked: bool) -> None:
        """Command-bar TP button toggled by user."""
        self._tp_enabled = checked
        cls = "tp-on" if checked else "tp-off"
        self._btn_tp.setProperty("class", cls)
        self._btn_tp.style().unpolish(self._btn_tp)
        self._btn_tp.style().polish(self._btn_tp)
        self.tp_toggled.emit(checked, 0x3E)

    # ── public API ────────────────────────────────────────────

    def log(self, text: str, kind: str = "info") -> None:
        self._log_data.append_log(text, kind)

    def log_data(self, text: str, kind: str = "info") -> None:
        self._log_data.append_data(text, kind)

    def get_config(self) -> Optional[tuple]:
        return self._config.get_config()

    @property
    def config_panel(self) -> UdsConfigPanel:
        return self._config


# ==============================================================
# DID SUB-TAB
# ==============================================================

class DidTab(QWidget):
    """Sub-tab DID — 0x22 ReadDataByIdentifier."""

    send_request = pyqtSignal(bytes, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        tb = QHBoxLayout()
        tb.setSpacing(6)

        tb.addWidget(QLabel("DID (hex):"))
        self._edit_did = QLineEdit("F180")
        self._edit_did.setFixedWidth(65)
        tb.addWidget(self._edit_did)

        btn_read = QPushButton("▶  Read DID")
        btn_read.setProperty("class", "primary")
        btn_read.setFixedWidth(90)
        btn_read.clicked.connect(self._send_read_did)
        tb.addWidget(btn_read)

        btn_all = QPushButton("Read All")
        btn_all.setFixedWidth(72)
        btn_all.clicked.connect(self._send_read_all)
        tb.addWidget(btn_all)

        tb.addStretch()

        btn_add = QPushButton("+ Add")
        btn_add.setFixedWidth(60)
        btn_add.clicked.connect(self._add_row)
        tb.addWidget(btn_add)

        btn_del = QPushButton("Remove")
        btn_del.setFixedWidth(65)
        btn_del.clicked.connect(self._remove_row)
        tb.addWidget(btn_del)

        lay.addLayout(tb)

        self._tbl = QTableWidget()
        self._tbl.setColumnCount(4)
        self._tbl.setHorizontalHeaderLabels(["DID (hex)", "Name", "Value", "Raw"])
        self._tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._tbl.setColumnWidth(0, 90)
        self._tbl.setColumnWidth(1, 170)
        self._tbl.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self._tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tbl.setAlternatingRowColors(True)
        lay.addWidget(self._tbl, stretch=1)

        note = QLabel("ℹ  DID: DBC mapping, auto-parse, batch read — coming soon")
        note.setStyleSheet(
            "color:#5c7cfa; font-size:11px; font-style:italic;"
            "padding:3px 8px; background:#eef2ff; border-radius:4px;"
        )
        lay.addWidget(note)

    def _add_row(self) -> None:
        row = self._tbl.rowCount()
        self._tbl.insertRow(row)
        self._tbl.setItem(row, 0, QTableWidgetItem("F180"))
        for c in (1, 2, 3):
            self._tbl.setItem(row, c, QTableWidgetItem(""))

    def _remove_row(self) -> None:
        row = self._tbl.currentRow()
        if row >= 0:
            self._tbl.removeRow(row)

    def _send_read_did(self) -> None:
        try:
            did = int(self._edit_did.text().strip(), 16)
        except ValueError:
            return
        self.send_request.emit(UdsServiceBuilder.read_did(did), f"ReadDataByID 0x{did:04X}")

    def _send_read_all(self) -> None:
        for row in range(self._tbl.rowCount()):
            item = self._tbl.item(row, 0)
            if item:
                try:
                    did = int(item.text().strip(), 16)
                    self.send_request.emit(UdsServiceBuilder.read_did(did), f"ReadDataByID 0x{did:04X}")
                except ValueError:
                    pass

    def add_did_result(self, did: int, name: str, value: str, raw: str) -> None:
        for row in range(self._tbl.rowCount()):
            item = self._tbl.item(row, 0)
            if item and item.text() == f"0x{did:04X}":
                self._tbl.setItem(row, 2, QTableWidgetItem(value))
                self._tbl.setItem(row, 3, QTableWidgetItem(raw))
                return
        row = self._tbl.rowCount()
        self._tbl.insertRow(row)
        self._tbl.setItem(row, 0, QTableWidgetItem(f"0x{did:04X}"))
        self._tbl.setItem(row, 1, QTableWidgetItem(name))
        self._tbl.setItem(row, 2, QTableWidgetItem(value))
        self._tbl.setItem(row, 3, QTableWidgetItem(raw))


# ==============================================================
# DTC SUB-TAB
# ==============================================================

class DtcTab(QWidget):
    """Sub-tab DTC — 0x19 ReadDTCInformation / 0x14 ClearDTC."""

    send_request = pyqtSignal(bytes, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(6)

        ctrl.addWidget(QLabel("Status Mask:"))
        self._edit_mask = QLineEdit("FF")
        self._edit_mask.setFixedWidth(55)
        ctrl.addWidget(self._edit_mask)

        btn_read = QPushButton("▶  Read DTC")
        btn_read.setProperty("class", "primary")
        btn_read.setFixedWidth(100)
        btn_read.clicked.connect(self._send_read_dtc)
        ctrl.addWidget(btn_read)

        btn_clear = QPushButton("✕  Clear DTC")
        btn_clear.setProperty("class", "danger")
        btn_clear.setFixedWidth(100)
        btn_clear.clicked.connect(self._confirm_clear_dtc)
        ctrl.addWidget(btn_clear)

        ctrl.addStretch()

        self._lbl_count = QLabel("DTCs: —")
        self._lbl_count.setStyleSheet("font-size:11px; color:#555e7a;")
        ctrl.addWidget(self._lbl_count)

        lay.addLayout(ctrl)

        self._tbl = QTableWidget()
        self._tbl.setColumnCount(4)
        self._tbl.setHorizontalHeaderLabels(["DTC Code", "Status", "Description", "Raw"])
        self._tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._tbl.setColumnWidth(0, 100)
        self._tbl.setColumnWidth(1, 88)
        self._tbl.setColumnWidth(3, 110)
        self._tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tbl.setAlternatingRowColors(True)
        self._tbl.setStyleSheet("QTableWidget{alternate-background-color:#fff5f5;}")
        lay.addWidget(self._tbl, stretch=1)

        note = QLabel("ℹ  DTC: status bit decode, description lookup — coming soon")
        note.setStyleSheet(
            "color:#5c7cfa; font-size:11px; font-style:italic;"
            "padding:3px 8px; background:#eef2ff; border-radius:4px;"
        )
        lay.addWidget(note)

    def _send_read_dtc(self) -> None:
        try:
            mask = int(self._edit_mask.text().strip(), 16)
        except ValueError:
            return
        self.send_request.emit(UdsServiceBuilder.read_dtc_by_status(mask), f"ReadDTCByStatusMask 0x{mask:02X}")

    def _confirm_clear_dtc(self) -> None:
        ret = QMessageBox.question(
            self, "Confirm Clear DTC",
            "Clear ALL diagnostic trouble codes?\n\nThis action cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Yes:
            self.send_request.emit(UdsServiceBuilder.clear_dtc(), "ClearDiagnosticInformation (all)")

    def add_dtc(self, code: str, status: str, description: str, raw: str) -> None:
        row = self._tbl.rowCount()
        self._tbl.insertRow(row)
        self._tbl.setItem(row, 0, QTableWidgetItem(code))
        self._tbl.setItem(row, 1, QTableWidgetItem(status))
        self._tbl.setItem(row, 2, QTableWidgetItem(description))
        self._tbl.setItem(row, 3, QTableWidgetItem(raw))
        self._lbl_count.setText(f"DTCs: {self._tbl.rowCount()}")

    def clear_dtc_table(self) -> None:
        self._tbl.setRowCount(0)
        self._lbl_count.setText("DTCs: 0")


# ==============================================================
# MAIN UDS DIAG TAB
# ==============================================================

class UdsDiagTab(QWidget):
    """
    Main UDS Diagnostic Tab (ISO 14229).

    Layout:
      ┌─ ChannelBar ──────────────────────────────────────────┐
      │  Channel[▼]  Scan  Apps  P2  P2*  STmin  [Connect]   │
      ├─ QTabWidget (sub-tabs) ───────────────────────────────┤
      │  [Console] [DID] [DTC] [...]                          │
      │  Console:                                             │
      │    UdsConfigPanel (Vehicle/ECU + IDs + DLL)           │
      │    [cmdInput][Send][Clear]  [Tester Present][ON]      │
      │    "Press Enter or..."                                │
      │    LogDataPanel (Log | Data)                          │
      └───────────────────────────────────────────────────────┘
    """

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

        self._uds_worker = UdsWorkerThread(hw_manager)
        self._engine: Optional[UdsEngine] = None

        self._tp_timer = QTimer(self)
        self._tp_timer.setInterval(2000)
        self._tp_timer.timeout.connect(self._on_tp_tick)

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

        # ── Channel bar (top) ──────────────────────────────────
        self._channel_bar = ChannelBar()
        self._channel_bar.connect_clicked.connect(self._on_connect_clicked)
        root.addWidget(self._channel_bar)

        # ── Sub-tab area ───────────────────────────────────────
        tab_container = QWidget()
        tab_container.setStyleSheet("background:#f5f6fa;")
        tc_lay = QVBoxLayout(tab_container)
        tc_lay.setContentsMargins(6, 4, 6, 6)
        tc_lay.setSpacing(0)

        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(False)

        # Console
        self._tab_console = ConsoleTab()
        self._tab_console.send_request.connect(self._on_send_request)
        self._tab_console.tp_toggled.connect(self._on_tp_toggle_changed)
        self._tabs.addTab(self._tab_console, "Console")

        # DID
        self._tab_did = DidTab()
        self._tab_did.send_request.connect(self._on_send_request)
        self._tabs.addTab(self._tab_did, "DID")

        # DTC
        self._tab_dtc = DtcTab()
        self._tab_dtc.send_request.connect(self._on_send_request)
        self._tabs.addTab(self._tab_dtc, "DTC")

        # Placeholder
        ph = QWidget()
        ph_lbl = QLabel("More services coming soon...")
        ph_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph_lbl.setStyleSheet("color:#9fa8c4; font-size:13px; font-style:italic;")
        QVBoxLayout(ph).addWidget(ph_lbl)
        self._tabs.addTab(ph, "···")

        tc_lay.addWidget(self._tabs)
        root.addWidget(tab_container, stretch=1)

    # ─── Connect ──────────────────────────────────────────────

    def _on_connect_clicked(self) -> None:
        timing = self._channel_bar.get_timing()
        ch_name = self._channel_bar.get_channel_name()
        self._tab_console.log(
            f"ℹ  Channel {ch_name} — connect toggled  "
            f"P2={timing['p2_ms']} P2*={timing['p2s_ms']} STmin={timing['stmin_ms']}",
            "info"
        )

    # ─── Send request ─────────────────────────────────────────

    @pyqtSlot(bytes, str)
    def _on_send_request(self, payload: bytes, description: str) -> None:
        cfg = self._tab_console.get_config()
        if not cfg:
            self._tab_console.log("✕ Invalid IDs in config", "err")
            return
        tx_id, rx_id = cfg

        req = UdsRequest(
            service_id=payload[0],
            payload=payload,
            channel=1,
            tx_id=tx_id,
            rx_id=rx_id,
            timeout_ms=1000,
            description=description,
        )

        hex_str = payload.hex(' ').upper()
        self._tab_console.log(f"→  {description}  [{hex_str}]", "tx")
        self._tab_console.log_data(f"TX  {description}\n    {hex_str}", "tx")

        if not self._uds_worker.send_request(req):
            self._tab_console.log("✕ Request queue full — engine busy", "err")

    # ─── Engine callbacks ─────────────────────────────────────

    @pyqtSlot(object)
    def _on_uds_response(self, response: UdsResponse) -> None:
        if not response.success:
            return

        service = response.service_id
        data    = response.data
        elapsed = response.elapsed_ms
        hex_str = data.hex(' ').upper()

        self._tab_console.log(f"←  [{hex_str}]  ({elapsed:.1f} ms)", "rx")
        self._tab_console.log_data(f"RX  [{hex_str}]  ({elapsed:.1f} ms)", "rx")

        if service == UdsService.READ_DATA_BY_ID and len(data) >= 2:
            did = (data[0] << 8) | data[1]
            raw = data[2:].hex(' ').upper()
            self._tab_did.add_did_result(did, "", raw, hex_str)
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
                dtc_bytes.hex(' ').upper(),
            )
            idx += 4

    @pyqtSlot(str)
    def _on_engine_log(self, text: str) -> None:
        if "←" in text or "OK" in text:
            kind = "rx"
        elif "✕" in text or "Error" in text or "Timeout" in text:
            kind = "err"
        elif "⏳" in text or "pending" in text.lower():
            kind = "pending"
        elif "→" in text:
            kind = "tx"
        else:
            kind = "info"
        self._tab_console.log(text, kind)

    # ─── Tester Present ───────────────────────────────────────

    @pyqtSlot(bool, int)
    def _on_tp_toggle_changed(self, enabled: bool, s3e_tx_id: int) -> None:
        if enabled:
            if self._engine:
                self._engine.set_tester_present(s3e_tx_id, 1, 2000)
            self._tp_timer.start()
            self._tab_console.log(
                f"ℹ  Tester Present ON  (S3E TX=0x{s3e_tx_id:03X})", "info"
            )
        else:
            self._tp_timer.stop()
            if self._engine:
                self._engine.set_tester_present(None, 1)
            self._tab_console.log("ℹ  Tester Present OFF", "info")

    def _on_tp_tick(self) -> None:
        pass  # Engine handles internally

    # ─── RX feed ──────────────────────────────────────────────

    def feed_can_frame(self, frame: CanFrame) -> None:
        if self._engine:
            self._engine.feed_frame(frame)

    # ─── Lifecycle ────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._tp_timer.stop()
        self._uds_worker.stop()
        super().closeEvent(event)
