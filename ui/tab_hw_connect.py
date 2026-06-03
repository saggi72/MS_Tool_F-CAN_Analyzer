"""
==============================================================
ui/tab_hw_connect.py
Hardware Connection Tab — Network → Hardware Mapping
==============================================================

Fixes applied
─────────────
  [FIX-1] Default app name = APP_NAME ("MS_Tool") hiển thị sẵn trong ô Vector App
  [FIX-2] channel_state_changed emit đúng channel_index từ CanNetwork thay vì
          luôn emit i+1 theo thứ tự connect
  [FIX-3] Sau khi connect thành công: start QueueFeedRxThread per network,
          push bus vào HardwareManager để TraceTab/SendCAN nhận frame qua rx_queue
  [FIX-4] disconnect_all / disconnect_one dừng RxThread trước khi đóng bus
  [FIX-5] hw_buses_changed emit dict {net_name: BusManager} đúng format

Data flow (sau fix):
  HwConnectTab._connect_all_active()
    → BusManager.connect(cfg)                   # mở bus vật lý
    → QueueFeedRxThread(bus, queue, net_name)   # tag frame bằng net_name
    → rx_thread.start()                         # bắt đầu đọc
    → signal_bus.channel_state_changed(ch_idx, "CONNECTED")   # ch_idx từ CanNetwork
    → hw_buses_changed({net_name: mgr})         # TraceTab + SendCAN
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget, QAbstractItemView,
)

from core.hardware import (
    APP_NAME,
    BusConfig,
    BusManager,
    HwChannel,
    HwScanner,
    QueueFeedRxThread,
    is_can_available,
)
from middleware.signals import SignalBus

logger = logging.getLogger(__name__)


# ==============================================================
# CONSTANTS
# ==============================================================

BITRATE_OPTIONS: List[Tuple[str, int]] = [
    ("1 Mbit/s",    1_000_000),
    ("800 kbit/s",    800_000),
    ("500 kbit/s",    500_000),
    ("250 kbit/s",    250_000),
    ("125 kbit/s",    125_000),
    ("100 kbit/s",    100_000),
    ("50 kbit/s",      50_000),
    ("20 kbit/s",      20_000),
    ("10 kbit/s",      10_000),
    ("Custom",              0),
]

FD_BITRATE_OPTIONS: List[Tuple[str, int]] = [
    ("8 Mbit/s",   8_000_000),
    ("4 Mbit/s",   4_000_000),
    ("2 Mbit/s",   2_000_000),
    ("1 Mbit/s",   1_000_000),
    ("500 kbit/s",   500_000),
]

ALL_BACKENDS = ["VECTOR", "PEAK", "CAN_ANALYZER", "VIRTUAL"]

# Table columns
COL_NO        = 0
COL_STATUS    = 1
COL_NAME      = 2
COL_BUS_CH    = 3
COL_ACTIVE    = 4
COL_HW_SELECT = 5
NUM_COLS      = 6


# ==============================================================
# SCAN THREAD
# ==============================================================

class HwScanThread(QThread):
    """Background scan — uses scan_complete to avoid QThread.finished conflict."""
    scan_complete = pyqtSignal(list)    # List[HwChannel]
    progress      = pyqtSignal(str)
    scan_error    = pyqtSignal(str)

    def __init__(self, backends: List[str], parent=None) -> None:
        super().__init__(parent)
        self._backends = backends

    def run(self) -> None:
        try:
            self.progress.emit("Scanning hardware…")
            channels = HwScanner.scan(self._backends)
            self.scan_complete.emit(channels)
        except Exception as e:
            logger.error("HwScanThread error: %s", e, exc_info=True)
            self.scan_error.emit(str(e))


# ==============================================================
# HW CONNECT TAB
# ==============================================================

class HwConnectTab(QWidget):
    """
    Maps logical CAN networks (from AddDbcTab) to physical hardware.

    State per connected network
    ───────────────────────────
      _hw_buses    : {net_name: BusManager}       — owns bus lifecycle
      _rx_threads  : {net_name: QueueFeedRxThread} — feeds shared rx_queue
    """

    hw_connected     = pyqtSignal()
    hw_buses_changed = pyqtSignal(dict)   # Dict[str, BusManager]

    def __init__(
        self,
        hw_manager,           # HardwareManager — exposes ._rx_queue
        signal_bus: SignalBus,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._hw_manager  = hw_manager
        self._signal_bus  = signal_bus

        # Networks from AddDbcTab: net_name → CanNetwork
        self._networks:   Dict[str, object]             = {}
        # Live bus managers: net_name → BusManager
        self._hw_buses:   Dict[str, BusManager]         = {}
        # RX threads: net_name → QueueFeedRxThread
        self._rx_threads: Dict[str, QueueFeedRxThread]  = {}

        self._hw_channels: List[HwChannel]    = []
        self._scan_thread: Optional[HwScanThread] = None

        self._build_ui()
        logger.debug("HwConnectTab initialized")

    # ── Public API ────────────────────────────────────────────

    def set_networks(self, networks: Dict[str, object]) -> None:
        """Called by MainWindow whenever AddDbcTab networks change."""
        self._networks = dict(networks)
        self._refresh_table()
        self._log(f"Table refreshed: {len(self._networks)} network(s) from Add DBC")

    def get_hw_buses(self) -> Dict[str, BusManager]:
        return dict(self._hw_buses)

    # ── UI Build ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(5, 5, 5, 5)
        root.setSpacing(4)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setSizes([240, 900])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, stretch=1)

        log_grp = QGroupBox("Log")
        log_lay = QVBoxLayout(log_grp)
        log_lay.setContentsMargins(4, 4, 4, 4)
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMaximumHeight(180)
        self._log_box.setFont(QFont("Consolas", 9))
        log_lay.addWidget(self._log_box)
        root.addWidget(log_grp)

    def _build_left_panel(self) -> QWidget:
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(8)
        lay.setContentsMargins(4, 4, 4, 4)

        # Select Device
        dev_grp = QGroupBox("Select Device")
        dev_lay = QVBoxLayout(dev_grp)
        dev_lay.setSpacing(4)
        self._device_checks: Dict[str, QCheckBox] = {}
        defaults_on = {"VECTOR", "VIRTUAL"}
        for name in ALL_BACKENDS:
            cb = QCheckBox(name)
            cb.setChecked(name in defaults_on)
            self._device_checks[name] = cb
            dev_lay.addWidget(cb)
        if sys.platform != "win32":
            cb = QCheckBox("SOCKETCAN")
            cb.setChecked(False)
            self._device_checks["SOCKETCAN"] = cb
            dev_lay.addWidget(cb)
        lay.addWidget(dev_grp)

        self._lbl_ch_count = QLabel("Channel count: 0")
        self._lbl_ch_count.setStyleSheet("font-weight:bold;")
        lay.addWidget(self._lbl_ch_count)

        # Bus Settings
        bus_grp  = QGroupBox("Bus Settings")
        bus_form = QFormLayout(bus_grp)
        bus_form.setSpacing(6)

        self._combo_baud = QComboBox()
        for label, _ in BITRATE_OPTIONS:
            self._combo_baud.addItem(label)
        self._combo_baud.setCurrentIndex(2)   # 500k default
        self._combo_baud.currentTextChanged.connect(self._on_baud_changed)
        bus_form.addRow("Baudrate:", self._combo_baud)

        self._edit_custom_baud = QLineEdit()
        self._edit_custom_baud.setPlaceholderText("e.g. 333333")
        self._edit_custom_baud.setVisible(False)
        bus_form.addRow("", self._edit_custom_baud)

        self._combo_type = QComboBox()
        self._combo_type.addItems(["CAN 2.0", "CAN FD"])
        self._combo_type.currentTextChanged.connect(self._on_type_changed)
        bus_form.addRow("Type:", self._combo_type)

        self._lbl_fd   = QLabel("FD Data Rate:")
        self._combo_fd = QComboBox()
        for label, _ in FD_BITRATE_OPTIONS:
            self._combo_fd.addItem(label)
        self._combo_fd.setCurrentIndex(2)   # 2 Mbit/s default
        self._lbl_fd.setVisible(False)
        self._combo_fd.setVisible(False)
        bus_form.addRow(self._lbl_fd, self._combo_fd)

        # [FIX-1] Default app name = APP_NAME, user có thể sửa
        self._edit_app = QLineEdit()
        self._edit_app.setText(APP_NAME)
        self._edit_app.setPlaceholderText("Leave empty = direct mode")
        self._edit_app.setToolTip(
            "Vector app name for xlSetApplConfig.\n"
            f"Default: {APP_NAME}\n"
            "Leave empty to use direct channel index."
        )
        bus_form.addRow("Vector App:", self._edit_app)

        lay.addWidget(bus_grp)

        self._btn_scan = QPushButton("🔍  Scan Devices")
        self._btn_scan.setMinimumHeight(34)
        self._btn_scan.setStyleSheet(
            "QPushButton{font-weight:bold;background:#e3f2fd;"
            "border-radius:3px;padding:4px 10px;}"
            "QPushButton:disabled{background:#ccc;}"
            "QPushButton:hover{background:#bbdefb;}"
        )
        self._btn_scan.clicked.connect(self._on_scan)
        lay.addWidget(self._btn_scan)

        self._lbl_scan_status = QLabel("")
        self._lbl_scan_status.setStyleSheet("color:#555;font-size:9pt;")
        self._lbl_scan_status.setWordWrap(True)
        lay.addWidget(self._lbl_scan_status)

        lay.addStretch()
        return w

    def _build_right_panel(self) -> QWidget:
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(6)
        lay.setContentsMargins(4, 4, 4, 4)

        tbl_grp = QGroupBox("Network → Hardware Mapping")
        tbl_lay = QVBoxLayout(tbl_grp)
        tbl_lay.setContentsMargins(4, 4, 4, 4)

        self._table = QTableWidget()
        self._table.setColumnCount(NUM_COLS)
        self._table.setHorizontalHeaderLabels([
            "#", "Status", "Name Network", "Bus CAN/CH",
            "Active", "Hardware Channel Select",
        ])
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(COL_NO,        QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(COL_STATUS,    QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(COL_NAME,      QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(COL_BUS_CH,    QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(COL_ACTIVE,    QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(COL_HW_SELECT, QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(COL_NO,     38)
        self._table.setColumnWidth(COL_STATUS, 55)
        self._table.setColumnWidth(COL_NAME,  140)
        self._table.setColumnWidth(COL_BUS_CH, 80)
        self._table.setColumnWidth(COL_ACTIVE, 70)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setFont(QFont("Consolas", 9))
        tbl_lay.addWidget(self._table)
        lay.addWidget(tbl_grp, stretch=1)

        btn_row = QHBoxLayout()
        self._btn_refresh = QPushButton("🔄  Refresh Networks")
        self._btn_refresh.setToolTip("Reload network list from Add DBC tab")
        self._btn_refresh.clicked.connect(self._on_refresh_clicked)

        self._btn_connect = QPushButton("🔗  Connect All Active")
        self._btn_connect.setStyleSheet(
            "QPushButton{font-weight:bold;background:#d1e7dd;"
            "border-radius:3px;padding:4px 12px;}"
            "QPushButton:hover{background:#a3cfbb;}"
        )
        self._btn_connect.clicked.connect(self._connect_all_active)

        self._btn_disconnect = QPushButton("⛓  Disconnect All")
        self._btn_disconnect.setStyleSheet(
            "QPushButton{background:#f8d7da;border-radius:3px;padding:4px 12px;}"
            "QPushButton:hover{background:#f1aeb5;}"
        )
        self._btn_disconnect.clicked.connect(self._disconnect_all)

        for b in (self._btn_refresh, self._btn_connect, self._btn_disconnect):
            btn_row.addWidget(b)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        self._lbl_summary = QLabel("No connections")
        self._lbl_summary.setStyleSheet("font-weight:bold;color:#555;")
        lay.addWidget(self._lbl_summary)

        return w

    # ── Settings helpers ──────────────────────────────────────

    def _on_baud_changed(self, text: str) -> None:
        self._edit_custom_baud.setVisible(text == "Custom")

    def _on_type_changed(self, text: str) -> None:
        fd = (text == "CAN FD")
        self._lbl_fd.setVisible(fd)
        self._combo_fd.setVisible(fd)

    def _get_bitrate(self) -> int:
        if self._combo_baud.currentText() == "Custom":
            try:
                return int(self._edit_custom_baud.text())
            except ValueError:
                self._log("WARNING: Invalid custom baud — using 500 000")
                return 500_000
        return BITRATE_OPTIONS[self._combo_baud.currentIndex()][1]

    def _get_fd_bitrate(self) -> int:
        return FD_BITRATE_OPTIONS[self._combo_fd.currentIndex()][1]

    def _is_fd(self) -> bool:
        return self._combo_type.currentText() == "CAN FD"

    # ── Scan ──────────────────────────────────────────────────

    def _on_scan(self) -> None:
        if not is_can_available():
            QMessageBox.critical(self, "Missing library",
                "python-can is required.\nInstall:  pip install python-can")
            self._log("ERROR: python-can not installed")
            return

        backends = [n for n, cb in self._device_checks.items() if cb.isChecked()]
        if not backends:
            self._log("WARNING: No device type selected.")
            return

        self._btn_scan.setEnabled(False)
        self._lbl_scan_status.setText("Scanning…")
        self._log(f"Scanning: {', '.join(backends)}")

        self._scan_thread = HwScanThread(backends, parent=self)
        self._scan_thread.progress.connect(lambda m: self._lbl_scan_status.setText(m))
        self._scan_thread.scan_complete.connect(self._on_scan_done)
        self._scan_thread.scan_error.connect(self._on_scan_error)
        self._scan_thread.start()

    def _on_scan_done(self, channels: List[HwChannel]) -> None:
        self._hw_channels = channels
        self._btn_scan.setEnabled(True)
        count = len(channels)
        self._lbl_ch_count.setText(f"Channel count: {count}")
        self._lbl_scan_status.setText(f"Scan complete: {count} hardware channel(s)")
        for ch in channels:
            self._log(f"  Found: {ch.label}")
        self._log(f"Scan complete: {count} hardware channel(s)")
        self._scan_thread = None
        self._refresh_table()

    def _on_scan_error(self, msg: str) -> None:
        self._btn_scan.setEnabled(True)
        self._lbl_scan_status.setText(f"Error: {msg}")
        self._log(f"SCAN ERROR: {msg}")
        self._scan_thread = None

    # ── Network table ─────────────────────────────────────────

    def _save_selections(self) -> Dict[str, Tuple[str, str]]:
        saved: Dict[str, Tuple[str, str]] = {}
        for row in range(self._table.rowCount()):
            name_item = self._table.item(row, COL_NAME)
            if not name_item:
                continue
            net_name = name_item.text()
            active_w = self._table.cellWidget(row, COL_ACTIVE)
            hw_w     = self._table.cellWidget(row, COL_HW_SELECT)
            saved[net_name] = (
                active_w.currentText() if active_w else "No",
                hw_w.currentText()     if hw_w     else "(none)",
            )
        return saved

    def _refresh_table(self) -> None:
        prev = self._save_selections()
        self._table.setRowCount(0)
        if not self._networks:
            return

        self._table.setRowCount(len(self._networks))

        for row, (net_name, network) in enumerate(self._networks.items()):
            mgr          = self._hw_buses.get(net_name)
            is_connected = mgr is not None and mgr.is_connected

            # Col 0: row number
            no_item = QTableWidgetItem(str(row + 1))
            no_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            no_item.setFlags(no_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, COL_NO, no_item)

            # Col 1: status
            st_item = QTableWidgetItem("●" if is_connected else "✗")
            st_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            st_item.setFlags(st_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            st_item.setForeground(QColor("#2e7d32" if is_connected else "#c62828"))
            st_item.setFont(QFont("", 11, QFont.Weight.Bold))
            self._table.setItem(row, COL_STATUS, st_item)

            # Col 2: network name
            name_item = QTableWidgetItem(net_name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name_item.setFont(QFont("", -1, QFont.Weight.Bold))
            self._table.setItem(row, COL_NAME, name_item)

            # Col 3: channel display (from CanNetwork)
            ch_str = ""
            if hasattr(network, "get_channel_display"):
                ch_str = network.get_channel_display() or ""
            elif hasattr(network, "channel_index") and network.channel_index:
                ch_str = f"CH{network.channel_index}"
            ch_item = QTableWidgetItem(ch_str)
            ch_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            ch_item.setFlags(ch_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, COL_BUS_CH, ch_item)

            # Col 4: Active combo
            active_cb = QComboBox()
            active_cb.addItems(["No", "Yes"])
            if is_connected:
                active_cb.setCurrentText("Yes")
            elif net_name in prev:
                active_cb.setCurrentText(prev[net_name][0])
            self._table.setCellWidget(row, COL_ACTIVE, active_cb)

            # Col 5: HW channel combo
            hw_cb = QComboBox()
            hw_cb.addItem("(none)", None)
            for ch in self._hw_channels:
                hw_cb.addItem(ch.label, ch)
            prev_hw = prev.get(net_name, ("No", "(none)"))[1]
            if prev_hw and prev_hw != "(none)":
                idx = hw_cb.findText(prev_hw)
                if idx >= 0:
                    hw_cb.setCurrentIndex(idx)
            self._table.setCellWidget(row, COL_HW_SELECT, hw_cb)

        self._table.resizeRowsToContents()

    @pyqtSlot()
    def _on_refresh_clicked(self) -> None:
        self._refresh_table()
        self._log(f"Table refreshed: {len(self._networks)} network(s) from Add DBC")

    # ── Connect / Disconnect ──────────────────────────────────

    def _connect_all_active(self) -> None:
        if not is_can_available():
            self._log("ERROR: python-can not installed")
            QMessageBox.critical(self, "Missing library",
                "python-can is required.\nInstall:  pip install python-can")
            return

        bitrate    = self._get_bitrate()
        fd_bitrate = self._get_fd_bitrate()
        is_fd      = self._is_fd()
        app_name   = self._edit_app.text().strip()

        ok_count  = 0
        err_count = 0
        used_hw:  Dict[str, str] = {}

        # Pre-scan already-connected to block duplicate hw usage
        for row in range(self._table.rowCount()):
            name_item = self._table.item(row, COL_NAME)
            if not name_item:
                continue
            nn  = name_item.text()
            mgr = self._hw_buses.get(nn)
            if mgr and mgr.is_connected:
                hw_w = self._table.cellWidget(row, COL_HW_SELECT)
                if hw_w and hw_w.currentText() != "(none)":
                    used_hw[hw_w.currentText()] = nn

        for row in range(self._table.rowCount()):
            active_w = self._table.cellWidget(row, COL_ACTIVE)
            if not active_w or active_w.currentText() != "Yes":
                continue

            name_item = self._table.item(row, COL_NAME)
            if not name_item:
                continue
            net_name = name_item.text()

            # Already connected — skip
            mgr = self._hw_buses.get(net_name)
            if mgr and mgr.is_connected:
                self._log(f"'{net_name}' already connected — skipped")
                ok_count += 1
                continue

            hw_w = self._table.cellWidget(row, COL_HW_SELECT)
            if not hw_w:
                continue
            hw_ch: Optional[HwChannel] = hw_w.currentData()

            if hw_ch is None:
                self._log(f"WARNING: '{net_name}' — no hardware channel selected")
                self._set_row_status(row, "⚠", QColor("#b26a00"))
                err_count += 1
                continue

            hw_label = hw_w.currentText()
            if hw_label in used_hw:
                self._log(
                    f"FAILED: '{net_name}' → {hw_label} already used by "
                    f"'{used_hw[hw_label]}'"
                )
                self._set_row_status(row, "✗", QColor("#c62828"))
                err_count += 1
                continue

            # Log channel mapping for debug
            logger.debug(
                "Channel map: net='%s' hw_label='%s' "
                "hw.channel=%r hw.hw_channel=%r hw.interface=%r",
                net_name, hw_label,
                getattr(hw_ch, "channel", "?"),
                getattr(hw_ch, "hw_channel", "?"),
                getattr(hw_ch, "interface", "?"),
            )

            cfg = BusConfig(
                hw              = hw_ch,
                bitrate         = bitrate,
                is_fd           = is_fd,
                fd_data_bitrate = fd_bitrate,
                app_name        = app_name,
            )

            if net_name not in self._hw_buses:
                self._hw_buses[net_name] = BusManager(log_fn=self._log)
            bus_mgr = self._hw_buses[net_name]

            self._log(f"Connecting '{net_name}' → {hw_label} @ {bitrate}…")
            success = bus_mgr.connect(cfg)

            if success:
                used_hw[hw_label] = net_name
                self._set_row_status(row, "●", QColor("#2e7d32"))
                ok_count += 1
                self._log(
                    f"CONNECTED: {net_name} → {hw_label} "
                    f"@ {bitrate // 1000} kbps"
                    f"{' [FD]' if is_fd else ''}"
                )

                # [FIX-3] Start RX thread, tag frame với net_name
                self._start_rx_thread(net_name, bus_mgr)

            else:
                self._set_row_status(row, "✗", QColor("#c62828"))
                err_count += 1
                self._log(f"FAILED: {net_name} → {hw_label}")

        total   = ok_count + err_count
        summary = f"Connected: {ok_count}/{total}"
        if err_count:
            summary += f"  |  Errors: {err_count}"
            self._lbl_summary.setStyleSheet("color:#c62828;font-weight:bold;")
        else:
            self._lbl_summary.setStyleSheet("color:#2e7d32;font-weight:bold;")
        self._lbl_summary.setText(summary)

        if ok_count > 0:
            self.hw_connected.emit()
            # [FIX-5] emit đúng dict format {net_name: BusManager}
            self.hw_buses_changed.emit(dict(self._hw_buses))
            # [FIX-2] emit channel_state_changed với ch_idx từ CanNetwork
            self._emit_channel_states("CONNECTED")

    def _start_rx_thread(self, net_name: str, bus_mgr: BusManager) -> None:
        """
        [FIX-3] Tạo QueueFeedRxThread cho network vừa connect.

        Thread tag mỗi CanFrame với channel = net_name (str) để
        TraceTab/DbcRegistry có thể lookup bằng tên network.
        Frame được push vào rx_queue của HardwareManager.
        """
        # Dừng thread cũ nếu còn
        old_rx = self._rx_threads.pop(net_name, None)
        if old_rx:
            old_rx.stop()

        rx_queue = getattr(self._hw_manager, "_rx_queue", None)
        if rx_queue is None:
            logger.warning(
                "_start_rx_thread: HardwareManager has no _rx_queue, "
                "RX frames for '%s' will not reach TraceTab", net_name
            )
            return

        rx = QueueFeedRxThread(
            bus           = bus_mgr.bus,
            bus_mutex     = bus_mgr.bus_mutex,
            rx_queue      = rx_queue,
            channel_index = net_name,   # tag frame bằng net_name, không phải int
        )
        rx.error_occurred.connect(
            lambda msg, nn=net_name: self._log(f"RX ERROR [{nn}]: {msg}")
        )
        self._rx_threads[net_name] = rx
        rx.start()
        logger.info("RX thread started for network '%s'", net_name)

    def _emit_channel_states(self, state: str) -> None:
        """
        [FIX-2] Emit channel_state_changed với channel_index thực từ CanNetwork.

        Thay vì emit i+1 theo thứ tự connect, lấy channel_index từ
        CanNetwork object trong self._networks.
        """
        for net_name, bus_mgr in self._hw_buses.items():
            if not bus_mgr.is_connected:
                continue
            network = self._networks.get(net_name)
            ch_idx  = getattr(network, "channel_index", None) if network else None
            if ch_idx is None:
                # Fallback: dùng hw channel nếu có
                cfg = None
                for row in range(self._table.rowCount()):
                    ni = self._table.item(row, COL_NAME)
                    if ni and ni.text() == net_name:
                        hw_w = self._table.cellWidget(row, COL_HW_SELECT)
                        if hw_w:
                            hw_ch = hw_w.currentData()
                            if hw_ch and isinstance(getattr(hw_ch, "channel", None), int):
                                ch_idx = hw_ch.channel
                        break
                if ch_idx is None:
                    ch_idx = 0
            self._signal_bus.channel_state_changed.emit(ch_idx, state)
            logger.debug(
                "Emitted channel_state_changed(%d, '%s') for net='%s'",
                ch_idx, state, net_name,
            )

    def _disconnect_all(self) -> None:
        closed = 0
        for net_name in list(self._hw_buses.keys()):
            self._stop_network(net_name)
            closed += 1

        self._hw_buses.clear()

        for row in range(self._table.rowCount()):
            self._set_row_status(row, "✗", QColor("#c62828"))
            active_w = self._table.cellWidget(row, COL_ACTIVE)
            if active_w:
                active_w.setCurrentText("No")

        self._lbl_summary.setText(f"Disconnected {closed} bus(es)")
        self._lbl_summary.setStyleSheet("font-weight:bold;color:#555;")
        self._log(f"Disconnect all — {closed} bus(es) closed")

        self.hw_connected.emit()
        self.hw_buses_changed.emit({})
        self._emit_channel_states_for_names(
            [nn for nn in self._networks], "DISCONNECTED"
        )

    def _stop_network(self, net_name: str) -> None:
        """[FIX-4] Dừng RX thread TRƯỚC khi đóng bus."""
        rx = self._rx_threads.pop(net_name, None)
        if rx:
            rx.stop()
            logger.debug("RX thread stopped for '%s'", net_name)

        mgr = self._hw_buses.get(net_name)
        if mgr:
            try:
                mgr.disconnect()
                self._log(f"Disconnected: {net_name}")
            except Exception as e:
                self._log(f"Error disconnecting '{net_name}': {e}")

    def disconnect_one(self, net_name: str) -> None:
        if net_name not in self._hw_buses:
            return
        self._stop_network(net_name)
        self._hw_buses.pop(net_name, None)
        self._refresh_table()
        self._log(f"Disconnected: {net_name}")
        self.hw_buses_changed.emit(dict(self._hw_buses))

        network = self._networks.get(net_name)
        ch_idx  = getattr(network, "channel_index", 0) if network else 0
        self._signal_bus.channel_state_changed.emit(ch_idx, "DISCONNECTED")

    def _emit_channel_states_for_names(
        self, net_names: List[str], state: str
    ) -> None:
        """Emit channel_state_changed cho danh sách network names."""
        for net_name in net_names:
            network = self._networks.get(net_name)
            ch_idx  = getattr(network, "channel_index", 0) if network else 0
            self._signal_bus.channel_state_changed.emit(ch_idx, state)

    # ── Helpers ───────────────────────────────────────────────

    def _set_row_status(self, row: int, text: str, color: QColor) -> None:
        item = self._table.item(row, COL_STATUS)
        if item:
            item.setText(text)
            item.setForeground(color)

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_box.append(f"[{ts}] {msg}")
        logger.debug("HwConnect: %s", msg)

    def closeEvent(self, event) -> None:
        self._disconnect_all()
        super().closeEvent(event)