"""
==============================================================
ui/tab_console.py  —  UDS Console Sub-Tab
==============================================================

Chứa các widget cho Console sub-tab:
  • HexCommandEdit   — Hex auto-format command input
  • UdsConfigPanel   — Vehicle / ECU selectors + DLL controls
  • LogBoxTable      — Mini CAN trace (left side)
  • ConsoleBox       — Decoded TX/RX stream (right side)
  • ConsoleTab       — Container kết hợp tất cả các widget trên
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QSplitter, QLabel, QPushButton, QLineEdit, QComboBox,
    QPlainTextEdit, QFrame, QHeaderView,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QFileDialog,
    QMessageBox,
)

from core.diag_config import VehicleRegistry, hex_id_to_int
from core.dll_loader  import EcuDllAdapter


# ==============================================================
# HEX COMMAND LINE EDIT
# ==============================================================

class HexCommandEdit(QLineEdit):
    """
    Auto-formats user input as hex:
      • forces uppercase
      • filters non-hex characters
      • inserts a single space after every 2 hex chars
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("cmdInput")
        self.setPlaceholderText("Type the command here  (e.g. 22 F1 91)")
        self._guard = False
        self.textEdited.connect(self._on_text_edited)

    def _on_text_edited(self, txt: str) -> None:
        if self._guard:
            return
        cleaned = "".join(c for c in txt.upper() if c in "0123456789ABCDEF")
        formatted = " ".join(cleaned[i:i + 2] for i in range(0, len(cleaned), 2))
        if formatted != txt:
            self._guard = True
            self.setText(formatted)
            self.setCursorPosition(len(formatted))
            self._guard = False

    def get_bytes(self) -> Optional[bytes]:
        """Parsed bytes, or None if empty / invalid."""
        s = self.text().replace(" ", "")
        if not s:
            return None
        if len(s) % 2:
            return None
        try:
            return bytes.fromhex(s)
        except ValueError:
            return None


# ==============================================================
# CONFIG PANEL  (Vehicle / ECU selectors + DLL controls)
# ==============================================================

class UdsConfigPanel(QFrame):
    """
    Two rows of selectors:
      Row 0: Vehicle [▼]  Request ID [__]  DLL File [__]      [Update data]
      Row 1: ECU     [▼]  Response ID[__]  [Change DLL File]  [Save]

    Vehicle combo lists *_diagnostic.json under config/DID.
    ECU combo is repopulated whenever Vehicle changes.
    Request/Response IDs and DLL File are auto-filled from the selected ECU.
    """

    request_open_dialog = pyqtSignal()                  # Update Data clicked
    ecu_changed         = pyqtSignal(str, str, dict)    # (vehicle, ecu, ecu_dict)
    dll_changed         = pyqtSignal(str, str, str)     # (vehicle, ecu, new_path)
    config_saved        = pyqtSignal()

    def __init__(self, registry: VehicleRegistry, parent=None):
        super().__init__(parent)
        self.setObjectName("configFrame")
        self._registry = registry
        self._loading  = False
        self._build()
        self.refresh_vehicles()

    def _build(self) -> None:
        root = QGridLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setHorizontalSpacing(6)
        root.setVerticalSpacing(4)

        LBL_S = ("color:#555e7a; font-size:11px; font-weight:500; "
                 "background:transparent; border:none;")

        def _lbl(t: str) -> QLabel:
            l = QLabel(t); l.setStyleSheet(LBL_S); return l

        # ── Row 0 ─────────────────────────────────────────────
        root.addWidget(_lbl("Vehicle"), 0, 0)
        self._cmb_vehicle = QComboBox()
        self._cmb_vehicle.setFixedWidth(160)
        self._cmb_vehicle.currentTextChanged.connect(self._on_vehicle_changed)
        root.addWidget(self._cmb_vehicle, 0, 1)

        root.addWidget(_lbl("Request ID"), 0, 2)
        self._edit_req_id = QLineEdit()
        self._edit_req_id.setReadOnly(True)
        self._edit_req_id.setFixedWidth(80)
        root.addWidget(self._edit_req_id, 0, 3)

        root.addWidget(_lbl("DLL File"), 0, 4)
        self._edit_dll = QLineEdit()
        self._edit_dll.setReadOnly(True)
        self._edit_dll.setMinimumWidth(220)
        self._edit_dll.setPlaceholderText("Path to DLL file...")
        root.addWidget(self._edit_dll, 0, 5)

        self._btn_update = QPushButton("Update data")
        self._btn_update.setProperty("class", "update")
        self._btn_update.setFixedWidth(110)
        self._btn_update.clicked.connect(self.request_open_dialog.emit)
        root.addWidget(self._btn_update, 0, 6)

        # ── Row 1 ─────────────────────────────────────────────
        root.addWidget(_lbl("ECU"), 1, 0)
        self._cmb_ecu = QComboBox()
        self._cmb_ecu.setFixedWidth(160)
        self._cmb_ecu.currentTextChanged.connect(self._on_ecu_changed)
        root.addWidget(self._cmb_ecu, 1, 1)

        root.addWidget(_lbl("Response ID"), 1, 2)
        self._edit_resp_id = QLineEdit()
        self._edit_resp_id.setReadOnly(True)
        self._edit_resp_id.setFixedWidth(80)
        root.addWidget(self._edit_resp_id, 1, 3)

        self._btn_change_dll = QPushButton("Change DLL File")
        self._btn_change_dll.setFixedWidth(140)
        self._btn_change_dll.clicked.connect(self._on_change_dll)
        root.addWidget(self._btn_change_dll, 1, 4, 1, 2)

        self._btn_save = QPushButton("Save")
        self._btn_save.setProperty("class", "save")
        self._btn_save.setFixedWidth(70)
        self._btn_save.clicked.connect(self.config_saved.emit)
        root.addWidget(self._btn_save, 1, 6)

        root.setColumnStretch(7, 1)

    # ─── Public ───────────────────────────────────────────────

    def refresh_vehicles(self, preselect: Optional[str] = None) -> None:
        """Rescan config dir and rebuild vehicle dropdown."""
        self._loading = True
        try:
            names = self._registry.rescan()
            self._cmb_vehicle.blockSignals(True)
            self._cmb_vehicle.clear()
            self._cmb_vehicle.addItems(names)
            self._cmb_vehicle.blockSignals(False)

            if preselect and preselect in names:
                self._cmb_vehicle.setCurrentText(preselect)
            elif names:
                self._cmb_vehicle.setCurrentIndex(0)
            else:
                self._cmb_ecu.clear()
                self._edit_req_id.clear()
                self._edit_resp_id.clear()
                self._edit_dll.clear()
                return
        finally:
            self._loading = False
        # Trigger downstream update
        self._on_vehicle_changed(self._cmb_vehicle.currentText())

    def current_vehicle(self) -> str:
        return self._cmb_vehicle.currentText().strip()

    def current_ecu(self) -> str:
        return self._cmb_ecu.currentText().strip()

    def current_ecu_info(self) -> Optional[dict]:
        return self._registry.get_ecu(self.current_vehicle(), self.current_ecu())

    def current_ids(self) -> Optional[tuple[int, int]]:
        """Return (tx_id, rx_id) as ints, or None if invalid."""
        tx = hex_id_to_int(self._edit_req_id.text())
        rx = hex_id_to_int(self._edit_resp_id.text())
        if tx is None or rx is None:
            return None
        return tx, rx

    def current_dll_path(self) -> str:
        return self._edit_dll.text().strip()

    # ─── Slots ────────────────────────────────────────────────

    def _on_vehicle_changed(self, vehicle: str) -> None:
        if self._loading:
            return
        self._cmb_ecu.blockSignals(True)
        self._cmb_ecu.clear()
        ecus = self._registry.ecu_names(vehicle)
        self._cmb_ecu.addItems(ecus)
        self._cmb_ecu.blockSignals(False)
        if ecus:
            self._cmb_ecu.setCurrentIndex(0)
            self._on_ecu_changed(self._cmb_ecu.currentText())
        else:
            self._edit_req_id.clear()
            self._edit_resp_id.clear()
            self._edit_dll.clear()

    def _on_ecu_changed(self, ecu: str) -> None:
        info = self._registry.get_ecu(self.current_vehicle(), ecu)
        if not info:
            self._edit_req_id.clear()
            self._edit_resp_id.clear()
            self._edit_dll.clear()
            return
        self._edit_req_id.setText(info.get("request_id")  or "")
        self._edit_resp_id.setText(info.get("response_id") or "")
        self._edit_dll.setText(info.get("dll_path") or "")
        self.ecu_changed.emit(self.current_vehicle(), ecu, info)

    def _on_change_dll(self) -> None:
        vehicle = self.current_vehicle()
        ecu = self.current_ecu()
        if not vehicle or not ecu:
            QMessageBox.information(self, "Change DLL",
                                    "Select a Vehicle and ECU first.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select DLL for {ecu}", "", "DLL Files (*.dll);;All Files (*)"
        )
        if path:
            self._edit_dll.setText(path)
            self.dll_changed.emit(vehicle, ecu, path)


# ==============================================================
# LOG BOX  (mini CAN trace — left side)
# ==============================================================

class LogBoxTable(QTableWidget):
    """
    Append-only table: [Timestamp | CAN ID | DLC | Data].
    Auto-trims to keep memory bounded.
    """

    COL_TS, COL_ID, COL_DLC, COL_DATA = 0, 1, 2, 3
    MAX_ROWS = 5000

    def __init__(self, parent=None):
        super().__init__(0, 4, parent)
        self.setHorizontalHeaderLabels(["Timestamp", "CAN ID", "DLC", "Data"])
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(18)
        h = self.horizontalHeader()
        h.setSectionResizeMode(self.COL_TS,   QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(self.COL_ID,   QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(self.COL_DLC,  QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(self.COL_DATA, QHeaderView.ResizeMode.Stretch)
        self.setColumnWidth(self.COL_TS,   95)
        self.setColumnWidth(self.COL_ID,   70)
        self.setColumnWidth(self.COL_DLC,  40)

    def append_frame(self, ts: float, can_id: int, dlc: int, data: bytes) -> None:
        row = self.rowCount()
        if row >= self.MAX_ROWS:
            self.removeRow(0)
            row -= 1
        self.insertRow(row)
        self.setItem(row, self.COL_TS,   QTableWidgetItem(f"{ts:.6f}"))
        self.setItem(row, self.COL_ID,   QTableWidgetItem(f"0x{can_id:X}"))
        self.setItem(row, self.COL_DLC,  QTableWidgetItem(str(dlc)))
        self.setItem(row, self.COL_DATA, QTableWidgetItem(data.hex(" ").upper()))
        # auto-scroll
        if row == self.rowCount() - 1:
            self.scrollToBottom()

    def clear_all(self) -> None:
        self.setRowCount(0)


# ==============================================================
# CONSOLE BOX  (decoded TX/RX — right side)
# ==============================================================

class ConsoleBox(QPlainTextEdit):
    """
    UDS request/response stream. Each line:
        [hh:mm:ss] TX/RX  description  hex   [(decoded ASCII)]

    `set_decoder(adapter)` switches between raw-hex fallback and DLL-based
    decoding. Until a decoder is set the box falls back to a printable-ASCII
    test followed by hex.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dataArea")
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)
        self._adapter: Optional[EcuDllAdapter] = None

    def set_decoder(self, adapter: Optional[EcuDllAdapter]) -> None:
        self._adapter = adapter

    def append_tx(self, description: str, payload: bytes) -> None:
        ts = time.strftime("%H:%M:%S")
        hex_str = payload.hex(" ").upper()
        self.appendHtml(
            f'<span style="font-family:Consolas,monospace;">'
            f'<span style="color:#b0b7c9;">[{ts}]</span> '
            f'<span style="color:#2f9e44; font-weight:600;">TX</span> '
            f'<span style="color:#1a1a2e;">{description}</span><br>'
            f'<span style="color:#444; padding-left:60px;">{hex_str}</span></span>'
        )

    def append_rx(self, payload: bytes, elapsed_ms: float,
                  did: Optional[int] = None) -> None:
        ts = time.strftime("%H:%M:%S")
        hex_str = payload.hex(" ").upper()
        decoded = self._decode(did or 0, payload)
        decoded_html = ""
        if decoded and decoded != hex_str:
            decoded_html = (
                f'<br><span style="color:#1971c2; padding-left:60px; '
                f'font-style:italic;">"{decoded}"</span>'
            )
        self.appendHtml(
            f'<span style="font-family:Consolas,monospace;">'
            f'<span style="color:#b0b7c9;">[{ts}]</span> '
            f'<span style="color:#1971c2; font-weight:600;">RX</span> '
            f'<span style="color:#555e7a;">({elapsed_ms:.1f} ms)</span><br>'
            f'<span style="color:#444; padding-left:60px;">{hex_str}</span>'
            f'{decoded_html}</span>'
        )

    def append_info(self, text: str, kind: str = "info") -> None:
        ts = time.strftime("%H:%M:%S")
        color = {
            "info": "#555e7a", "warn": "#e67700", "err": "#c92a2a",
            "pending": "#5c7cfa",
        }.get(kind, "#555e7a")
        self.appendHtml(
            f'<span style="font-family:Consolas,monospace;">'
            f'<span style="color:#b0b7c9;">[{ts}]</span> '
            f'<span style="color:{color};">{text}</span></span>'
        )

    def _decode(self, did: int, payload: bytes) -> str:
        if self._adapter and self._adapter.is_loaded:
            return self._adapter.decode_payload(did, payload)
        return EcuDllAdapter.fallback_decode(payload)


# ==============================================================
# CONSOLE SUB-TAB
# ==============================================================

class ConsoleTab(QWidget):
    """Container cho Config panel + command row + Log/Console boxes."""

    send_request        = pyqtSignal(bytes, str)
    tp_toggled          = pyqtSignal(bool, int)
    request_open_dialog = pyqtSignal()
    ecu_changed         = pyqtSignal(str, str, dict)
    dll_changed         = pyqtSignal(str, str, str)
    config_saved        = pyqtSignal()

    def __init__(self, registry: VehicleRegistry, parent=None):
        super().__init__(parent)
        self._tp_enabled = False
        self._build(registry)

    def _build(self, registry: VehicleRegistry) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(5)

        # Config panel
        self._config = UdsConfigPanel(registry)
        self._config.request_open_dialog.connect(self.request_open_dialog.emit)
        self._config.ecu_changed.connect(self.ecu_changed.emit)
        self._config.dll_changed.connect(self.dll_changed.emit)
        self._config.config_saved.connect(self.config_saved.emit)
        root.addWidget(self._config)

        # Command row
        cmd_row = QHBoxLayout()
        cmd_row.setSpacing(6)

        self._edit_cmd = HexCommandEdit()
        self._edit_cmd.returnPressed.connect(self._on_send)
        cmd_row.addWidget(self._edit_cmd)

        self._btn_send = QPushButton("Send")
        self._btn_send.setProperty("class", "primary")
        self._btn_send.setFixedSize(64, 28)
        self._btn_send.clicked.connect(self._on_send)
        cmd_row.addWidget(self._btn_send)

        self._btn_clear = QPushButton("Clear")
        self._btn_clear.setFixedSize(56, 28)
        self._btn_clear.clicked.connect(self._on_clear)
        cmd_row.addWidget(self._btn_clear)

        cmd_row.addSpacing(15)
        lbl_tp = QLabel("Tester Present (S3E):")
        lbl_tp.setStyleSheet("font-size:11px; color:#555e7a; font-weight:600;")
        cmd_row.addWidget(lbl_tp)

        self._btn_tp = QPushButton("ON")
        self._btn_tp.setCheckable(True)
        self._btn_tp.setFixedSize(46, 28)
        self._btn_tp.setProperty("class", "toggle-off")
        self._btn_tp.toggled.connect(self._on_tp_btn_toggled)
        cmd_row.addWidget(self._btn_tp)
        cmd_row.addStretch()
        root.addLayout(cmd_row)

        hint = QLabel("Press Enter or push Send to dispatch the command")
        hint.setStyleSheet("color:#9fa8c4; font-size:10px; font-style:italic;")
        hint.setAlignment(Qt.AlignmentFlag.AlignRight)
        root.addWidget(hint)

        # Log Box  |  Console Box  (left=Log, right=Console)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(2)
        lbl_left = QLabel("Log Box  ·  mini CAN trace")
        lbl_left.setStyleSheet(
            "color:#555e7a; font-size:11px; font-weight:600; background:transparent;"
        )
        lv.addWidget(lbl_left)
        self._log_box = LogBoxTable()
        lv.addWidget(self._log_box)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(2)
        lbl_right = QLabel("Console Box  ·  TX / RX (decoded if available)")
        lbl_right.setStyleSheet(
            "color:#555e7a; font-size:11px; font-weight:600; background:transparent;"
        )
        rv.addWidget(lbl_right)
        self._console_box = ConsoleBox()
        rv.addWidget(self._console_box)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([380, 460])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, stretch=1)

    # ─── public ───────────────────────────────────────────────

    @property
    def config_panel(self) -> UdsConfigPanel:
        return self._config

    @property
    def log_box(self) -> LogBoxTable:
        return self._log_box

    @property
    def console_box(self) -> ConsoleBox:
        return self._console_box

    # ─── slots ────────────────────────────────────────────────

    def _on_clear(self) -> None:
        self._log_box.clear_all()
        self._console_box.clear()

    def _on_send(self) -> None:
        raw = self._edit_cmd.get_bytes()
        if raw is None:
            self._console_box.append_info("✕ Invalid hex command", "err")
            return
        self._edit_cmd.clear()
        descr = f"Console 0x{raw[0]:02X}"
        self.send_request.emit(raw, descr)

    def _on_tp_btn_toggled(self, checked: bool) -> None:
        self._tp_enabled = checked
        cls = "toggle-on" if checked else "toggle-off"
        self._btn_tp.setProperty("class", cls)
        self._btn_tp.style().unpolish(self._btn_tp)
        self._btn_tp.style().polish(self._btn_tp)
        ids = self._config.current_ids()
        tx_id = ids[0] if ids else 0
        self.tp_toggled.emit(checked, tx_id)