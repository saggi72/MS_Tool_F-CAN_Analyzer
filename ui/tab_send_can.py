"""
ui/tab_send_can.py — Send CAN Tab (MS_Tool_F, PyQt6)
=====================================================

UI layout: matches the reference (Document 5) exactly:
  Top bar:  Type | Baud | [DataBaud hidden] | Network | CRC | Connect | SEND | STOP
  Left:     Network combo → Message list → [Add >>]
  Right:    Search | Signal Editor (table + preview) | TX Queue | Log

TX path:
  1. self._main_window.hw_buses[net_name]  ← primary (tab_hw_connect stores here)
  2. hw_manager.transmit(CanFrame)         ← fallback

Signal editor Physical column:
  • QComboBox  when signal has DBC choices (value table)
  • QDoubleSpinBox(min…max, step=scale) otherwise

Auto-generator detection from signal name AND DBC comment:
  comment/name "check sum" / "checksum" / "crc"  → Gen Checksum
  comment/name "alive counter"                    → Alive Counter
  comment/name "counter"                          → Gen Counter
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QTimer, QMutex, QMutexLocker, pyqtSlot
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from core.can_backend import CanFrame
from models.can_network import CanNetwork

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────
TICK_MS          = 10
UI_REFRESH_MS    = 150
STATS_UPDATE_MS  = 500
MAX_LOG_LINES    = 800
LOG_BOX_HEIGHT   = 140
DEFAULT_CYCLE_MS = 100
MAX_CYCLE_MS     = 60_000
CAN_CLASSIC_MAX  = 8
CAN_FD_MAX       = 64
TX_LOG_ENABLED   = True
DEFAULT_FD_BAUD  = 2_000_000

GENERATOR_OPTIONS = ["None", "Gen Checksum", "Gen Counter",
                     "Alive Counter", "Ramp", "Random"]
BAUD_RATES        = ["125000", "250000", "500000", "1000000"]
FD_DATA_BITRATES  = ["1000000", "2000000", "4000000", "5000000"]


# ──────────────────────────────────────────────────────────────
# CRC
# ──────────────────────────────────────────────────────────────
class CrcAlgorithm:
    VARIANTS: Dict[str, Dict] = {
        "CRC-8/SAE-J1850": {"poly": 0x1D, "init": 0xFF, "xor_out": 0xFF},
        "CRC-8/AUTOSAR":   {"poly": 0x2F, "init": 0xFF, "xor_out": 0xFF},
        "XOR-8":           {"poly": None, "init": 0x00, "xor_out": 0x00},
    }
    _tables: Dict[int, List[int]] = {}

    @classmethod
    def _table(cls, poly: int) -> List[int]:
        if poly not in cls._tables:
            t = [0] * 256
            for i in range(256):
                c = i
                for _ in range(8):
                    c = ((c << 1) ^ poly) & 0xFF if (c & 0x80) else (c << 1) & 0xFF
                t[i] = c
            cls._tables[poly] = t
        return cls._tables[poly]

    @classmethod
    def compute(cls, data: bytes, variant: str = "CRC-8/SAE-J1850") -> int:
        cfg = cls.VARIANTS.get(variant, cls.VARIANTS["CRC-8/SAE-J1850"])
        if cfg["poly"] is None:
            r = cfg["init"]
            for b in data: r ^= b
            return r ^ cfg["xor_out"]
        t = cls._table(cfg["poly"])
        crc = cfg["init"]
        for b in data: crc = t[(crc ^ b) & 0xFF]
        return crc ^ cfg["xor_out"]


# ──────────────────────────────────────────────────────────────
# SIGNAL TYPE DETECTOR
# ──────────────────────────────────────────────────────────────
class SignalTypeDetector:
    @staticmethod
    def detect(name: str, comment: str = "") -> str:
        """
        Priority: DBC comment keywords → signal name patterns.
        comment "check sum"/"checksum"/"crc"  → Gen Checksum
        comment "alive counter"               → Alive Counter
        comment "counter"                     → Gen Counter
        """
        c = (comment or "").lower()
        if re.search(r"check.?sum|\bcrc\b", c):   return "Gen Checksum"
        if re.search(r"alive.?counter",      c):   return "Alive Counter"
        if re.search(r"\bcounter\b",         c):   return "Gen Counter"
        n = (name or "").lower()
        if re.search(r"checksum|chksum|\bcrc\b",                n): return "Gen Checksum"
        if re.search(r"alive.?counter|\balv\b|rolling.?counter", n): return "Alive Counter"
        if re.search(r"msg.?cnt|message.?counter|\bcounter\b",   n): return "Gen Counter"
        return "None"


# ──────────────────────────────────────────────────────────────
# SIGNAL ENCODER
# ──────────────────────────────────────────────────────────────
def _sig_byte_set(start: int, length: int, bo: str) -> set:
    indices: set = set()
    if bo == "big_endian":
        cb, cbit = start // 8, start % 8
        for i in range(length):
            indices.add(cb)
            if i < length - 1:
                if cbit == 0: cb += 1; cbit = 7
                else: cbit -= 1
    else:
        for i in range(length): indices.add((start + i) // 8)
    return indices


class SignalEncoder:
    @staticmethod
    def encode(data: List[int], start: int, length: int,
               raw: int, bo: str = "little_endian") -> None:
        raw &= (1 << length) - 1
        if bo == "big_endian":
            cb, cbit = start // 8, start % 8
            for i in range(length):
                bit = (raw >> (length - 1 - i)) & 1
                if i > 0:
                    if cbit == 0: cb += 1; cbit = 7
                    else: cbit -= 1
                if cb < len(data):
                    if bit: data[cb] |=  (1 << cbit)
                    else:   data[cb] &= ~(1 << cbit)
        else:
            for i in range(length):
                bp, bb = (start + i) // 8, (start + i) % 8
                if bp < len(data):
                    if (raw >> i) & 1: data[bp] |=  (1 << bb)
                    else:              data[bp] &= ~(1 << bb)


# ──────────────────────────────────────────────────────────────
# ALIVE COUNTER
# ──────────────────────────────────────────────────────────────
class AliveCounter:
    def __init__(self, bit_length: int = 4) -> None:
        self._inv = (1 << bit_length) - 1
        self._cur = 0
    def next(self) -> int:
        v = self._cur; self._cur = (self._cur + 1) % self._inv; return v
    def reset(self) -> None: self._cur = 0


# ──────────────────────────────────────────────────────────────
# TX ENTRY
# ──────────────────────────────────────────────────────────────
class TxEntry:
    MODES = ["Cyclic", "Event", "Cyclic+Event"]

    def __init__(self, *, name: str, frame_id: int, dlc: int,
                 network_name: str, channel_index: int,
                 signals: List[Dict], crc_variant: str,
                 is_fd: bool = False, brs: bool = False) -> None:
        self.name          = name
        self.frame_id      = frame_id
        self.dlc           = dlc
        self.network_name  = network_name
        self.channel_index = channel_index
        self.signals       = signals
        self.crc_variant   = crc_variant
        self.is_fd         = is_fd
        self.brs           = brs
        self.data: List[int] = [0] * max(dlc, CAN_CLASSIC_MAX)
        self.cycle_ms      = DEFAULT_CYCLE_MS
        self.tx_offset_ms  = 0
        self.send_mode     = "Cyclic"
        self.active        = True
        self.tx_count      = 0
        self._tick_acc     = 0
        self._alive: Dict[str, AliveCounter] = {}
        self._rolls: Dict[str, int] = {}
        self._init_generators()

    def _init_generators(self) -> None:
        for s in self.signals:
            g = s.get("generator", "None")
            if g == "Alive Counter" and s["name"] not in self._alive:
                self._alive[s["name"]] = AliveCounter(s["length"])
            elif g == "Gen Counter" and s["name"] not in self._rolls:
                self._rolls[s["name"]] = 0

    def ensure_counter(self, name: str, gen: str, length: int) -> None:
        if gen == "Alive Counter" and name not in self._alive:
            self._alive[name] = AliveCounter(length)
        if gen == "Gen Counter" and name not in self._rolls:
            self._rolls[name] = 0

    def reset_counters(self) -> None:
        for a in self._alive.values(): a.reset()
        for k in self._rolls: self._rolls[k] = 0
        self._tick_acc = 0

    def should_send(self, tick_ms: int) -> bool:
        if self.send_mode == "Event": return False
        if self._tick_acc == 0 and self.tx_offset_ms > 0:
            self._tick_acc = self.tx_offset_ms
        self._tick_acc += tick_ms
        if self._tick_acc >= self.cycle_ms:
            self._tick_acc -= self.cycle_ms
            if self._tick_acc >= self.cycle_ms: self._tick_acc = 0
            return True
        return False

    def encode_all_signals(self) -> None:
        for s in self.signals:
            if s.get("generator") == "Gen Checksum": continue
            SignalEncoder.encode(self.data, s["start"], s["length"],
                                 s["raw"], s.get("byte_order", "little_endian"))
        for s in self.signals:
            if s.get("generator") == "Gen Checksum":
                chk = self._crc(s); s["raw"] = chk
                SignalEncoder.encode(self.data, s["start"], s["length"],
                                     chk, s.get("byte_order", "little_endian"))

    def _crc(self, chk_sig: Dict) -> int:
        excl = _sig_byte_set(chk_sig["start"], chk_sig["length"],
                             chk_sig.get("byte_order", "little_endian"))
        payload = bytes(self.data[i] for i in range(self.dlc) if i not in excl)
        return CrcAlgorithm.compute(payload, self.crc_variant) & ((1 << chk_sig["length"]) - 1)

    def tick_generators(self, is_event_send: bool = False) -> None:
        for s in self.signals:
            g = s.get("generator", "None")
            if g == "Alive Counter":
                a = self._alive.get(s["name"])
                if a is None:
                    a = AliveCounter(s["length"]); self._alive[s["name"]] = a
                s["raw"] = a.next()
            elif g == "Gen Counter":
                mv = (1 << s["length"]) - 1
                cur = self._rolls.get(s["name"], 0)
                s["raw"] = cur; self._rolls[s["name"]] = (cur + 1) & mv
            elif g == "Ramp":
                s["raw"] = (s["raw"] + 1) & ((1 << s["length"]) - 1)
            elif g == "Random":
                s["raw"] = random.randint(0, (1 << s["length"]) - 1)
        self.encode_all_signals()

    def get_data_hex(self) -> str:
        return " ".join(f"{self.data[i]:02X}" for i in range(self.dlc))

    def make_can_frame(self) -> CanFrame:
        return CanFrame(timestamp=time.monotonic(),
                        channel=str(self.channel_index),
                        can_id=self.frame_id, dlc=self.dlc,
                        data=bytes(self.data[:self.dlc]),
                        is_extended=(self.frame_id > 0x7FF),
                        is_fd=self.is_fd)

    def to_dict(self) -> Dict:
        return {"name": self.name, "frame_id": self.frame_id, "dlc": self.dlc,
                "network_name": self.network_name, "channel_index": self.channel_index,
                "cycle_ms": self.cycle_ms, "tx_offset_ms": self.tx_offset_ms,
                "send_mode": self.send_mode, "active": self.active,
                "crc_variant": self.crc_variant, "is_fd": self.is_fd, "brs": self.brs,
                "signals": [{k: v for k, v in s.items() if k != "choices"} for s in self.signals]}

    @classmethod
    def from_dict(cls, d: Dict) -> "TxEntry":
        e = cls(name=d["name"], frame_id=d["frame_id"], dlc=d["dlc"],
                network_name=d.get("network_name", ""),
                channel_index=d.get("channel_index", 0),
                signals=d.get("signals", []),
                crc_variant=d.get("crc_variant", "CRC-8/SAE-J1850"),
                is_fd=d.get("is_fd", False), brs=d.get("brs", False))
        e.cycle_ms     = d.get("cycle_ms", DEFAULT_CYCLE_MS)
        e.tx_offset_ms = d.get("tx_offset_ms", 0)
        e.send_mode    = d.get("send_mode", "Cyclic")
        e.active       = d.get("active", True)
        return e


# ══════════════════════════════════════════════════════════════
# SEND CAN TAB
# ══════════════════════════════════════════════════════════════
class SendCanTab(QWidget):
    """
    Send CAN Tab — UI matches Document-5 layout exactly.

    Injected by MainWindow:
        hw_manager    — HardwareManager
        networks      — Dict[str, CanNetwork]  live reference
        signal_bus    — SignalBus
        main_window   — MainWindow reference (to read hw_buses at TX time)
    """

    CRC_VARIANTS = list(CrcAlgorithm.VARIANTS.keys())

    def __init__(self, hw_manager, networks: Dict[str, CanNetwork],
                 signal_bus, main_window=None,
                 parent=None) -> None:
        super().__init__(parent)

        self._hw         = hw_manager
        self._nets       = networks        # live dict reference
        self._sigbus     = signal_bus
        self._main_win   = main_window     # for hw_buses lookup at TX time

        self._tx_queue: OrderedDict[int, TxEntry] = OrderedDict()
        self._queue_mutex    = QMutex()
        self._current_edit_fid: int = -1

        # Active channel derived from the Network combo selection
        self._active_ch_idx: int = -1
        self._active_net: Optional[CanNetwork] = None

        # Stats
        self._stats: Dict[str, int] = {"tx": 0, "rx": 0, "err": 0}
        self._log_line_count = 0

        # Timers
        self._tx_timer = QTimer(self)
        self._tx_timer.setInterval(TICK_MS)
        self._tx_timer.timeout.connect(self._tx_tick)

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(UI_REFRESH_MS)
        self._ui_timer.timeout.connect(self._ui_refresh)

        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(STATS_UPDATE_MS)
        self._stats_timer.timeout.connect(self._update_stats)
        self._stats_timer.start()

        self._build_ui()
        self._connect_signals()
        self._refresh_network_combo()

    # ──────────────────────────────────────────────────────────
    # SIGNAL WIRING
    # ──────────────────────────────────────────────────────────
    def _connect_signals(self) -> None:
        if hasattr(self._sigbus, "buses_changed"):
            self._sigbus.buses_changed.connect(self._on_buses_changed)
        if hasattr(self._sigbus, "networks_changed"):
            self._sigbus.networks_changed.connect(self._on_networks_changed)
        self.destroyed.connect(self._cleanup)

    # ──────────────────────────────────────────────────────────
    # UI BUILD  (layout matches Document-5 exactly)
    # ──────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(3, 3, 3, 3)
        root.setSpacing(2)
        root.addLayout(self._build_connect_bar())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_dbc_browser())
        splitter.addWidget(self._build_right_panel())
        splitter.setSizes([220, 800])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, stretch=1)

    def _build_connect_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(4)

        # CAN Type
        bar.addWidget(QLabel("Type:"))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["CAN 2.0", "CAN FD"])
        self.type_combo.currentTextChanged.connect(self._on_can_type_changed)
        bar.addWidget(self.type_combo)

        # Arbitration baud
        bar.addWidget(QLabel("Baud:"))
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(BAUD_RATES)
        self.baud_combo.setCurrentText("500000")
        self.baud_combo.setEditable(True)
        bar.addWidget(self.baud_combo)

        # Data baud (CAN FD only — hidden by default)
        self.data_baud_label = QLabel("DataBaud:")
        self.data_baud_combo = QComboBox()
        self.data_baud_combo.addItems(FD_DATA_BITRATES)
        self.data_baud_combo.setCurrentText(str(DEFAULT_FD_BAUD))
        self.data_baud_combo.setEditable(True)
        bar.addWidget(self.data_baud_label)
        bar.addWidget(self.data_baud_combo)
        self.data_baud_label.setVisible(False)
        self.data_baud_combo.setVisible(False)

        # Network / channel selector  (replaces old ch_combo + scan btn)
        bar.addWidget(QLabel("Network:"))
        self.net_select_combo = QComboBox()
        self.net_select_combo.setMinimumWidth(180)
        self.net_select_combo.currentTextChanged.connect(self._on_net_select_changed)
        bar.addWidget(self.net_select_combo)

        bar.addStretch()

        # CRC variant
        bar.addWidget(QLabel("CRC:"))
        self.crc_combo = QComboBox()
        self.crc_combo.addItems(self.CRC_VARIANTS)
        bar.addWidget(self.crc_combo)

        # SEND / STOP  (Connect is now implicit — we use hw_buses at send time)
        self.send_btn = QPushButton("SEND")
        self.send_btn.setMinimumWidth(80)
        self.send_btn.setStyleSheet(
            "QPushButton{font-weight:bold;background:#d1e7dd;border-radius:3px;}")
        self.send_btn.clicked.connect(self._on_send)
        bar.addWidget(self.send_btn)

        self.stop_btn = QPushButton("STOP")
        self.stop_btn.setMinimumWidth(80)
        self.stop_btn.setStyleSheet(
            "QPushButton{font-weight:bold;background:#f8d7da;border-radius:3px;}")
        self.stop_btn.clicked.connect(self._on_stop)
        bar.addWidget(self.stop_btn)

        return bar

    def _on_can_type_changed(self, text: str) -> None:
        is_fd = (text == "CAN FD")
        self.data_baud_label.setVisible(is_fd)
        self.data_baud_combo.setVisible(is_fd)

    def _build_dbc_browser(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 2, 2, 2)

        lay.addWidget(QLabel("Network"))
        self.net_combo = QComboBox()
        self.net_combo.currentTextChanged.connect(self._load_dbc_messages)
        lay.addWidget(self.net_combo)

        lay.addWidget(QLabel("Messages"))
        self.msg_list = QListWidget()
        self.msg_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.msg_list.itemClicked.connect(self._on_msg_clicked)
        self.msg_list.itemDoubleClicked.connect(self._on_msg_dblclick)
        lay.addWidget(self.msg_list)

        self.add_btn = QPushButton("Add Selected  ▶▶")
        self.add_btn.clicked.connect(self._add_selected_to_queue)
        lay.addWidget(self.add_btn)
        return w

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(3)

        # Search row
        sr = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search message/signal or 0xID...")
        self.search_edit.textChanged.connect(self._on_search)
        sr.addWidget(self.search_edit)
        sa = QPushButton("Add")
        sa.clicked.connect(self._add_search_result)
        sr.addWidget(sa)
        lay.addLayout(sr)

        # Signal Editor group
        sg = QGroupBox("Signal Editor")
        sl = QVBoxLayout()
        self.msg_header = QLabel("Select or add a message")
        self.msg_header.setFont(QFont("", -1, QFont.Weight.Bold))
        sl.addWidget(self.msg_header)

        self.sig_table = QTableWidget()
        self.sig_table.setColumnCount(7)
        self.sig_table.setHorizontalHeaderLabels(
            ["Signal", "Raw", "Physical", "Generator", "Start", "Length", "Comment"])
        h = self.sig_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, w_ in {1: 60, 2: 105, 3: 112, 4: 52, 5: 56}.items():
            h.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.sig_table.setColumnWidth(col, w_)
        h.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.sig_table.setAlternatingRowColors(True)
        self.sig_table.cellChanged.connect(self._on_sig_cell_changed)
        sl.addWidget(self.sig_table)

        self.data_preview = QLineEdit()
        self.data_preview.setReadOnly(True)
        self.data_preview.setFont(QFont("Consolas", 10))
        sl.addWidget(self.data_preview)
        sg.setLayout(sl)
        lay.addWidget(sg)

        # TX Queue group
        qg = QGroupBox("TX Queue")
        ql = QVBoxLayout()
        self.tx_table = QTableWidget()
        self.tx_table.setColumnCount(8)
        self.tx_table.setHorizontalHeaderLabels([
            "Message", "ID", "DLC", "Cycle(ms)", "Mode", "Data", "BRS", "Active"])
        th = self.tx_table.horizontalHeader()
        th.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, w_ in {1: 60, 2: 35, 3: 70, 4: 95}.items():
            th.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.tx_table.setColumnWidth(col, w_)
        th.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        th.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self.tx_table.setColumnWidth(6, 35)
        th.setSectionResizeMode(7, QHeaderView.ResizeMode.Fixed)
        self.tx_table.setColumnWidth(7, 45)
        self.tx_table.setAlternatingRowColors(True)
        self.tx_table.itemClicked.connect(self._on_queue_row_clicked)
        ql.addWidget(self.tx_table)

        br = QHBoxLayout()
        for lbl, slot in [("Remove", self._remove_from_queue),
                           ("Clear All", self._clear_queue)]:
            b = QPushButton(lbl); b.clicked.connect(slot); br.addWidget(b)
        br.addStretch()
        for lbl, slot in [("Export...", self._export_queue),
                           ("Import...", self._import_queue)]:
            b = QPushButton(lbl); b.clicked.connect(slot); br.addWidget(b)
        ql.addLayout(br)
        qg.setLayout(ql)
        lay.addWidget(qg)

        # Stats + log bar
        lb = QHBoxLayout()
        self.stats_label = QLabel("TX:0  Err:0")
        self.stats_label.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        lb.addWidget(self.stats_label)
        lb.addStretch()
        self.conn_label = QLabel("No network selected")
        self.conn_label.setStyleSheet("color:red;font-weight:bold;")
        lb.addWidget(self.conn_label)
        cl = QPushButton("Clear")
        cl.setMaximumWidth(50)
        cl.clicked.connect(self._clear_log)
        lb.addWidget(cl)
        lay.addLayout(lb)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(LOG_BOX_HEIGHT)
        self.log_box.setFont(QFont("Consolas", 9))
        lay.addWidget(self.log_box)
        return w

    # ──────────────────────────────────────────────────────────
    # NETWORK COMBO MANAGEMENT
    # ──────────────────────────────────────────────────────────
    def _refresh_network_combo(self) -> None:
        """Rebuild both the top-bar Network selector and the DBC browser combo."""
        # ── top-bar selector ──────────────────────────────────
        prev_sel = self.net_select_combo.currentText()
        self.net_select_combo.blockSignals(True)
        self.net_select_combo.clear()
        for net_name, net in self._nets.items():
            if net.channel_index is None:
                continue
            dbc_file = getattr(net, "dbc_file", None) or ""
            fname = dbc_file.replace("\\", "/").split("/")[-1] if dbc_file else "(no DBC)"
            # Check if the bus is connected in hw_buses
            hw_buses = self._get_hw_buses()
            connected = "✓ " if net_name in hw_buses else ""
            label = f"{connected}{net_name} / CH{net.channel_index} · {fname}"
            self.net_select_combo.addItem(label, net_name)
        if prev_sel:
            for i in range(self.net_select_combo.count()):
                if self.net_select_combo.itemData(i) == prev_sel:
                    self.net_select_combo.setCurrentIndex(i); break
        self.net_select_combo.blockSignals(False)
        self._on_net_select_changed(self.net_select_combo.currentText())

        # ── DBC browser combo ─────────────────────────────────
        prev_dbc = self.net_combo.currentText()
        self.net_combo.blockSignals(True)
        self.net_combo.clear()
        self.net_combo.addItems(self._nets.keys())
        if prev_dbc:
            self.net_combo.setCurrentText(prev_dbc)
        self.net_combo.blockSignals(False)
        self._load_dbc_messages(self.net_combo.currentText())

    def _get_hw_buses(self) -> dict:
        """Get the live hw_buses dict from MainWindow."""
        # Primary: MainWindow.hw_buses (set by tab_hw_connect)
        if self._main_win is not None and hasattr(self._main_win, "hw_buses"):
            return self._main_win.hw_buses
        # Fallback: hw_manager may expose it
        if hasattr(self._hw, "hw_buses"):
            return self._hw.hw_buses
        return {}

    @pyqtSlot(str)
    def _on_net_select_changed(self, _label: str) -> None:
        idx = self.net_select_combo.currentIndex()
        if idx < 0:
            self._active_ch_idx = -1
            self._active_net    = None
            self.conn_label.setText("No network selected")
            self.conn_label.setStyleSheet("color:red;font-weight:bold;")
            return
        net_name = self.net_select_combo.itemData(idx)
        net = self._nets.get(net_name)
        self._active_net    = net
        self._active_ch_idx = net.channel_index if net else -1

        hw_buses = self._get_hw_buses()
        connected = net_name in hw_buses if net_name else False

        if connected:
            self.conn_label.setText(
                f"✓ {net_name} / CH{self._active_ch_idx} — connected")
            self.conn_label.setStyleSheet("color:#1b5e20;font-weight:bold;")
        else:
            self.conn_label.setText(
                f"{net_name} / CH{self._active_ch_idx} — connect in HW tab first")
            self.conn_label.setStyleSheet("color:#e65100;font-weight:bold;")

    @pyqtSlot(dict)
    def _on_buses_changed(self, _: dict) -> None:
        self._refresh_network_combo()

    @pyqtSlot()
    def _on_networks_changed(self) -> None:
        self._refresh_network_combo()

    # Called by MainWindow._sync_send_networks
    def _populate_channel_combo(self) -> None:
        self._refresh_network_combo()

    # ──────────────────────────────────────────────────────────
    # DBC BROWSER
    # ──────────────────────────────────────────────────────────
    @pyqtSlot(str)
    def _load_dbc_messages(self, network: str) -> None:
        self.msg_list.clear()
        if not network:
            return
        net = self._nets.get(network)
        if not net or not getattr(net, "database", None):
            return
        for msg in sorted(net.database.messages, key=lambda m: m.frame_id):
            cycle = getattr(msg, "cycle_time", None)
            st    = getattr(msg, "send_type",  None)
            extra = f"DLC:{msg.length}"
            if cycle: extra += f"  {cycle}ms"
            if st:    extra += f"  [{st}]"
            item = QListWidgetItem(f"[0x{msg.frame_id:03X}] {msg.name} ({extra})")
            item.setData(Qt.ItemDataRole.UserRole, msg)
            self.msg_list.addItem(item)

    @pyqtSlot(str)
    def _on_search(self, text: str) -> None:
        q = text.lower().strip()
        for i in range(self.msg_list.count()):
            item = self.msg_list.item(i)
            item.setHidden(bool(q) and q not in item.text().lower())

    def _add_search_result(self) -> None:
        for i in range(self.msg_list.count()):
            item = self.msg_list.item(i)
            if not item.isHidden():
                msg = item.data(Qt.ItemDataRole.UserRole)
                if msg: self._add_dbc_msg_to_queue(msg); return

    def _on_msg_clicked(self, item: QListWidgetItem) -> None:
        msg = item.data(Qt.ItemDataRole.UserRole)
        if msg: self._show_dbc_msg_preview(msg)

    def _on_msg_dblclick(self, item: QListWidgetItem) -> None:
        msg = item.data(Qt.ItemDataRole.UserRole)
        if msg: self._add_dbc_msg_to_queue(msg)

    def _add_selected_to_queue(self) -> None:
        for item in self.msg_list.selectedItems():
            msg = item.data(Qt.ItemDataRole.UserRole)
            if msg: self._add_dbc_msg_to_queue(msg)

    # ──────────────────────────────────────────────────────────
    # SIGNAL PREVIEW (read-only, from DBC browser click)
    # ──────────────────────────────────────────────────────────
    def _show_dbc_msg_preview(self, dbc_msg) -> None:
        cycle = getattr(dbc_msg, "cycle_time", None)
        hdr = f"{dbc_msg.name}  (0x{dbc_msg.frame_id:X})  DLC:{dbc_msg.length}"
        if cycle: hdr += f"  Cycle:{cycle}ms"
        self.msg_header.setText(hdr)
        self._current_edit_fid = -1
        self.sig_table.blockSignals(True)
        self.sig_table.setRowCount(len(dbc_msg.signals))
        for r, sig in enumerate(dbc_msg.signals):
            comment = getattr(sig, "comment", "") or ""
            self._fill_sig_row(r, {
                "name":    getattr(sig, "name",   ""),
                "raw":     0,
                "factor":  getattr(sig, "scale",  1.0) or 1.0,
                "offset":  getattr(sig, "offset", 0.0),
                "length":  getattr(sig, "length", 0),
                "start":   getattr(sig, "start",  0),
                "comment": comment,
                "choices": dict(sig.choices) if getattr(sig, "choices", None) else {},
                "generator": SignalTypeDetector.detect(sig.name, comment),
                "min": float(sig.minimum) if getattr(sig, "minimum", None) is not None else None,
                "max": float(sig.maximum) if getattr(sig, "maximum", None) is not None else None,
            }, editable=False)
        self.sig_table.blockSignals(False)
        self.data_preview.setText(
            f"[0x{dbc_msg.frame_id:X}] DLC:{dbc_msg.length} DATA: "
            + "00 " * dbc_msg.length)

    # ──────────────────────────────────────────────────────────
    # ADD TO QUEUE
    # ──────────────────────────────────────────────────────────
    def _add_dbc_msg_to_queue(self, dbc_msg) -> None:
        # Determine target network from DBC browser combo
        dbc_net_name = self.net_combo.currentText()
        dbc_net = self._nets.get(dbc_net_name)
        ch_idx = dbc_net.channel_index if dbc_net else (self._active_ch_idx or 0)

        fid = dbc_msg.frame_id
        if fid in self._tx_queue:
            self._load_tx_entry_to_editor(fid); return

        signals: List[Dict] = []
        for sig in dbc_msg.signals:
            bo = "big_endian" if (hasattr(sig, "byte_order") and
                                  "big" in str(sig.byte_order).lower()) else "little_endian"
            choices = {}
            if getattr(sig, "choices", None):
                try: choices = dict(sig.choices)
                except Exception: pass
            comment = getattr(sig, "comment", "") or ""
            scale   = getattr(sig, "scale",  1.0) or 1.0
            offset  = getattr(sig, "offset", 0.0)
            length  = getattr(sig, "length", 0)
            raw_max = (1 << length) - 1 if length else 255
            min_v   = getattr(sig, "minimum", None)
            max_v   = getattr(sig, "maximum", None)
            signals.append({
                "name":       getattr(sig, "name", ""),
                "start":      getattr(sig, "start", 0),
                "length":     length,
                "byte_order": bo,
                "factor":     scale,
                "offset":     offset,
                "min":        float(min_v) if min_v is not None else float(offset),
                "max":        float(max_v) if max_v is not None else float(raw_max * scale + offset),
                "raw":        0,
                "choices":    choices,
                "generator":  SignalTypeDetector.detect(sig.name, comment),
                "comment":    comment,
            })

        is_fd = (dbc_msg.length > CAN_CLASSIC_MAX)
        entry = TxEntry(name=dbc_msg.name, frame_id=fid, dlc=dbc_msg.length,
                        network_name=dbc_net_name, channel_index=ch_idx,
                        signals=signals, crc_variant=self.crc_combo.currentText(),
                        is_fd=is_fd, brs=is_fd)

        dbc_cycle = getattr(dbc_msg, "cycle_time", None)
        if dbc_cycle and dbc_cycle > 0: entry.cycle_ms = int(dbc_cycle)

        st_raw = getattr(dbc_msg, "send_type", None)
        if st_raw:
            st = str(st_raw).lower()
            if "event" in st and "cyclic" in st: entry.send_mode = "Cyclic+Event"
            elif "event" in st or "ifactive" in st: entry.send_mode = "Event"

        with QMutexLocker(self._queue_mutex):
            self._tx_queue[fid] = entry
        self._refresh_tx_table()
        self._load_tx_entry_to_editor(fid)
        self._log(f"Added: {dbc_msg.name} (0x{fid:X}) DLC:{dbc_msg.length} "
                  f"CH{ch_idx} Cycle:{entry.cycle_ms}ms Mode:{entry.send_mode}"
                  + (" [FD]" if is_fd else ""))

    # ──────────────────────────────────────────────────────────
    # SIGNAL ROW FILL  (shared by preview + editable)
    # ──────────────────────────────────────────────────────────
    def _fill_sig_row(self, row: int, sig: Dict, editable: bool) -> None:
        def _ro(text: str) -> QTableWidgetItem:
            it = QTableWidgetItem(text)
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            return it

        self.sig_table.setItem(row, 0, _ro(sig["name"]))
        self.sig_table.setItem(row, 1, QTableWidgetItem(str(sig["raw"])))

        # Physical: choices → QComboBox; else QDoubleSpinBox(min…max)
        if sig.get("choices"):
            cb = QComboBox()
            for rv, txt in sorted(sig["choices"].items()):
                cb.addItem(str(txt), rv)
            for i in range(cb.count()):
                if cb.itemData(i) == sig["raw"]: cb.setCurrentIndex(i); break
            if editable:
                cb.currentIndexChanged.connect(
                    lambda _, r=row: self._on_choice_changed(r))
            self.sig_table.setCellWidget(row, 2, cb)
        else:
            scale  = sig.get("factor", 1.0) or 1.0
            offset = sig.get("offset", 0.0)
            lo = sig.get("min", offset)
            hi = sig.get("max", ((1 << sig.get("length", 8)) - 1) * scale + offset)
            phys = sig["raw"] * scale + offset
            sp = QDoubleSpinBox()
            sp.setDecimals(4)
            sp.setRange(float(lo), float(hi))
            sp.setSingleStep(float(scale))
            sp.setValue(round(float(phys), 4))
            if editable:
                sp.valueChanged.connect(
                    lambda v, r=row: self._on_phys_spinbox_changed(r, v))
            else:
                sp.setReadOnly(True)
            self.sig_table.setCellWidget(row, 2, sp)

        # Generator combo
        gc = QComboBox()
        gc.addItems(GENERATOR_OPTIONS)
        gc.setCurrentText(sig.get("generator", "None"))
        if editable:
            gc.currentTextChanged.connect(
                lambda v, r=row: self._on_gen_changed(r, v))
        self.sig_table.setCellWidget(row, 3, gc)

        self.sig_table.setItem(row, 4, _ro(str(sig.get("start", 0))))
        self.sig_table.setItem(row, 5, _ro(str(sig.get("length", 0))))
        self.sig_table.setItem(row, 6, _ro(sig.get("comment", "")))

    def _load_tx_entry_to_editor(self, fid: int) -> None:
        entry = self._tx_queue.get(fid)
        if not entry: return
        self._current_edit_fid = fid
        fd_tag = " [FD]" if entry.is_fd else ""
        self.msg_header.setText(
            f"{entry.name}  (0x{entry.frame_id:X})  DLC:{entry.dlc}"
            f"{fd_tag}  [TX Queue — CH{entry.channel_index}]")
        self.sig_table.blockSignals(True)
        self.sig_table.setRowCount(len(entry.signals))
        for r, sig in enumerate(entry.signals):
            self._fill_sig_row(r, sig, editable=True)
        self.sig_table.blockSignals(False)
        self._update_data_preview(entry)

    # ──────────────────────────────────────────────────────────
    # SIGNAL EDITING SLOTS
    # ──────────────────────────────────────────────────────────
    @pyqtSlot(int, int)
    def _on_sig_cell_changed(self, row: int, col: int) -> None:
        """Only raw (col 1) is a plain text cell now; physical is a widget."""
        if self._current_edit_fid < 0: return
        entry = self._tx_queue.get(self._current_edit_fid)
        if not entry or row >= len(entry.signals): return
        sig = entry.signals[row]
        if col != 1: return
        item = self.sig_table.item(row, 1)
        if not item: return
        try: raw = int(item.text())
        except ValueError: return
        raw = max(0, min(raw, (1 << sig["length"]) - 1))
        sig["raw"] = raw
        # Sync physical spinbox
        w = self.sig_table.cellWidget(row, 2)
        if w is not None and not sig.get("choices") and hasattr(w, "setValue"):
            w.blockSignals(True)
            w.setValue(round(raw * sig["factor"] + sig["offset"], 4))
            w.blockSignals(False)
        entry.encode_all_signals()
        self._update_data_preview(entry)
        self._refresh_tx_data_col(entry.frame_id)

    def _on_phys_spinbox_changed(self, row: int, phys: float) -> None:
        if self._current_edit_fid < 0: return
        entry = self._tx_queue.get(self._current_edit_fid)
        if not entry or row >= len(entry.signals): return
        sig = entry.signals[row]
        if sig["factor"] != 0:
            raw = int(round((phys - sig["offset"]) / sig["factor"]))
            raw = max(0, min(raw, (1 << sig["length"]) - 1))
            sig["raw"] = raw
            self.sig_table.blockSignals(True)
            ri = self.sig_table.item(row, 1)
            if ri: ri.setText(str(raw))
            self.sig_table.blockSignals(False)
        entry.encode_all_signals()
        self._update_data_preview(entry)
        self._refresh_tx_data_col(entry.frame_id)

    def _on_choice_changed(self, row: int) -> None:
        if self._current_edit_fid < 0: return
        entry = self._tx_queue.get(self._current_edit_fid)
        if not entry or row >= len(entry.signals): return
        cb = self.sig_table.cellWidget(row, 2)
        if not cb: return
        sig = entry.signals[row]
        sig["raw"] = cb.currentData()
        self.sig_table.blockSignals(True)
        ri = self.sig_table.item(row, 1)
        if ri: ri.setText(str(sig["raw"]))
        self.sig_table.blockSignals(False)
        entry.encode_all_signals()
        self._update_data_preview(entry)
        self._refresh_tx_data_col(entry.frame_id)

    def _on_gen_changed(self, row: int, val: str) -> None:
        if self._current_edit_fid < 0: return
        entry = self._tx_queue.get(self._current_edit_fid)
        if not entry or row >= len(entry.signals): return
        sig = entry.signals[row]
        sig["generator"] = val
        entry.ensure_counter(sig["name"], val, sig["length"])

    def _update_data_preview(self, entry: TxEntry) -> None:
        fd = " [FD]" if entry.is_fd else ""
        self.data_preview.setText(
            f"[0x{entry.frame_id:X}] DLC:{entry.dlc}{fd}  "
            f"CH{entry.channel_index}  DATA: {entry.get_data_hex()}")

    # ──────────────────────────────────────────────────────────
    # TX TABLE
    # ──────────────────────────────────────────────────────────
    def _refresh_tx_table(self) -> None:
        self.tx_table.setRowCount(len(self._tx_queue))
        for row, (fid, e) in enumerate(self._tx_queue.items()):
            def _ro(t: str) -> QTableWidgetItem:
                it = QTableWidgetItem(t)
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                return it

            self.tx_table.setItem(row, 0, _ro(f"{e.name} [CH{e.channel_index}]"))
            self.tx_table.setItem(row, 1, _ro(f"0x{e.frame_id:X}"))
            self.tx_table.setItem(row, 2, _ro(str(e.dlc)))

            cs = QSpinBox()
            cs.setRange(1, MAX_CYCLE_MS); cs.setValue(e.cycle_ms); cs.setSuffix("ms")
            cs.valueChanged.connect(lambda v, f=fid: self._set_cycle(f, v))
            self.tx_table.setCellWidget(row, 3, cs)

            mc = QComboBox(); mc.addItems(TxEntry.MODES); mc.setCurrentText(e.send_mode)
            mc.currentTextChanged.connect(lambda m, f=fid: self._set_mode(f, m))
            self.tx_table.setCellWidget(row, 4, mc)

            self.tx_table.setItem(row, 5, _ro(e.get_data_hex()))

            brs_cb = QCheckBox(); brs_cb.setChecked(e.brs); brs_cb.setEnabled(e.is_fd)
            brs_cb.setToolTip("Bit Rate Switch (CAN FD)")
            brs_cb.stateChanged.connect(
                lambda s, f=fid: self._set_brs(f, s == Qt.CheckState.Checked.value))
            bw = QWidget(); bl = QHBoxLayout(bw)
            bl.addWidget(brs_cb); bl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            bl.setContentsMargins(0, 0, 0, 0)
            self.tx_table.setCellWidget(row, 6, bw)

            act_cb = QCheckBox(); act_cb.setChecked(e.active)
            act_cb.stateChanged.connect(
                lambda s, f=fid: self._set_active(f, s == Qt.CheckState.Checked.value))
            aw = QWidget(); al = QHBoxLayout(aw)
            al.addWidget(act_cb); al.setAlignment(Qt.AlignmentFlag.AlignCenter)
            al.setContentsMargins(0, 0, 0, 0)
            self.tx_table.setCellWidget(row, 7, aw)

            color = QColor(220, 255, 220) if e.active else QColor(242, 242, 242)
            for c in range(self.tx_table.columnCount()):
                it = self.tx_table.item(row, c)
                if it: it.setBackground(color)

    def _refresh_tx_data_col(self, fid: int) -> None:
        for row, (f, e) in enumerate(self._tx_queue.items()):
            if f == fid:
                it = self.tx_table.item(row, 5)
                if it: it.setText(e.get_data_hex())
                break

    def _on_queue_row_clicked(self, item: QTableWidgetItem) -> None:
        row = item.row()
        keys = list(self._tx_queue.keys())
        if row < len(keys): self._load_tx_entry_to_editor(keys[row])

    def _remove_from_queue(self) -> None:
        rows = {it.row() for it in self.tx_table.selectedItems()}
        keys = list(self._tx_queue.keys())
        with QMutexLocker(self._queue_mutex):
            for r in sorted(rows, reverse=True):
                if r < len(keys): del self._tx_queue[keys[r]]
        self._refresh_tx_table()
        if not self._tx_queue: self._current_edit_fid = -1

    def _clear_queue(self) -> None:
        with QMutexLocker(self._queue_mutex): self._tx_queue.clear()
        self._refresh_tx_table()
        self._current_edit_fid = -1
        self._log("Queue cleared")

    def _set_cycle(self, fid, v):
        if fid in self._tx_queue: self._tx_queue[fid].cycle_ms = v
    def _set_mode(self, fid, m):
        if fid in self._tx_queue: self._tx_queue[fid].send_mode = m
    def _set_active(self, fid, v: bool):
        if fid in self._tx_queue: self._tx_queue[fid].active = v
    def _set_brs(self, fid, v: bool):
        if fid in self._tx_queue: self._tx_queue[fid].brs = v

    # ──────────────────────────────────────────────────────────
    # EXPORT / IMPORT
    # ──────────────────────────────────────────────────────────
    def _export_queue(self) -> None:
        if not self._tx_queue: self._log("Nothing to export"); return
        path, _ = QFileDialog.getSaveFileName(self, "Export TX Queue", "tx_queue.json", "JSON (*.json)")
        if not path: return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump([e.to_dict() for e in self._tx_queue.values()], f, indent=2)
            self._log(f"Exported {len(self._tx_queue)} message(s)")
        except Exception as ex: self._log(f"Export failed: {ex}")

    def _import_queue(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import TX Queue", "", "JSON (*.json)")
        if not path: return
        try:
            with open(path, encoding="utf-8") as f: data = json.load(f)
            count = 0
            with QMutexLocker(self._queue_mutex):
                for d in data:
                    e = TxEntry.from_dict(d)
                    if e.frame_id not in self._tx_queue:
                        self._tx_queue[e.frame_id] = e; count += 1
            self._refresh_tx_table()
            self._log(f"Imported {count} message(s)")
        except Exception as ex: self._log(f"Import failed: {ex}")

    # ──────────────────────────────────────────────────────────
    # SEND / STOP
    # ──────────────────────────────────────────────────────────
    def _on_send(self) -> None:
        active = [e for e in self._tx_queue.values() if e.active]
        if not active: self._log("WARNING: No active messages in queue"); return

        for e in active: e.reset_counters()

        # Immediate one-shot for Event / Cyclic+Event
        for e in active:
            if e.send_mode in ("Event", "Cyclic+Event"):
                e.tick_generators(is_event_send=True)
                self._send_frame(e)

        self._tx_timer.start()
        self._ui_timer.start()
        self.send_btn.setStyleSheet(
            "QPushButton{font-weight:bold;background:#a5d6a7;border-radius:3px;}")
        self._log(f"TX STARTED — {len(active)} active message(s)")

    def _on_stop(self) -> None:
        self._tx_timer.stop()
        self._ui_timer.stop()
        self.send_btn.setStyleSheet(
            "QPushButton{font-weight:bold;background:#d1e7dd;border-radius:3px;}")
        self._log("TX STOPPED")

    def _tx_tick(self) -> None:
        for entry in list(self._tx_queue.values()):
            if not entry.active: continue
            if not entry.should_send(TICK_MS): continue
            entry.tick_generators()
            self._send_frame(entry)

    def _send_frame(self, entry: TxEntry) -> bool:
        """
        Transmit one frame.

        Path 1: MainWindow.hw_buses[net_name]  — primary (tab_hw_connect stores here)
        Path 2: hw_manager.transmit(CanFrame)  — fallback
        """
        import can as _can
        try:
            ok = False

            # ── Path 1: bus from tab_hw_connect via MainWindow.hw_buses ──
            hw_buses = self._get_hw_buses()
            bus_mgr = hw_buses.get(entry.network_name)
            if bus_mgr is not None:
                raw_bus = getattr(bus_mgr, "bus", None)
                if raw_bus is not None:
                    msg = _can.Message(
                        arbitration_id=entry.frame_id,
                        data=bytes(entry.data[:entry.dlc]),
                        is_extended_id=(entry.frame_id > 0x7FF),
                        is_fd=entry.is_fd,
                        dlc=entry.dlc,
                    )
                    raw_bus.send(msg)
                    ok = True

            # ── Path 2: HardwareManager.transmit() ───────────────────────
            if not ok and hasattr(self._hw, "transmit"):
                ok = self._hw.transmit(entry.make_can_frame())

            if ok:
                entry.tx_count += 1
                self._stats["tx"] += 1
                if TX_LOG_ENABLED:
                    self._log(f"[TX] 0x{entry.frame_id:03X}  CH{entry.channel_index}"
                              f"  DLC:{entry.dlc}  {entry.get_data_hex()}")
            else:
                self._stats["err"] += 1
                self._log(f"[TX FAIL] 0x{entry.frame_id:03X}  {entry.network_name}"
                          " — not connected (connect in HW tab first)")
            return ok
        except Exception as ex:
            self._stats["err"] += 1
            self._log(f"[TX ERR] 0x{entry.frame_id:03X}: {ex}")
            return False

    # ──────────────────────────────────────────────────────────
    # UI REFRESH
    # ──────────────────────────────────────────────────────────
    def _ui_refresh(self) -> None:
        if self._current_edit_fid >= 0:
            entry = self._tx_queue.get(self._current_edit_fid)
            if entry:
                self._update_live_signals(entry)
                self._update_data_preview(entry)
        for fid, entry in self._tx_queue.items():
            if entry.active: self._refresh_tx_data_col(fid)

    def _update_live_signals(self, entry: TxEntry) -> None:
        self.sig_table.blockSignals(True)
        try:
            for r, sig in enumerate(entry.signals):
                if sig.get("generator", "None") == "None": continue
                ri = self.sig_table.item(r, 1)
                if ri: ri.setText(str(sig["raw"]))
                w = self.sig_table.cellWidget(r, 2)
                if w is not None and not sig.get("choices") and hasattr(w, "setValue"):
                    w.blockSignals(True)
                    w.setValue(round(sig["raw"] * sig["factor"] + sig["offset"], 4))
                    w.blockSignals(False)
        finally:
            self.sig_table.blockSignals(False)

    # ──────────────────────────────────────────────────────────
    # STATS / LOG
    # ──────────────────────────────────────────────────────────
    def _update_stats(self) -> None:
        self.stats_label.setText(f"TX:{self._stats['tx']}  Err:{self._stats['err']}")

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log_box.append(f"[{ts}] {msg}")
        self._log_line_count += 1
        if self._log_line_count > MAX_LOG_LINES:
            from PyQt6.QtGui import QTextCursor
            cur = self.log_box.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.Start)
            for _ in range(MAX_LOG_LINES // 2):
                cur.movePosition(QTextCursor.MoveOperation.Down,
                                 QTextCursor.MoveMode.KeepAnchor)
            cur.removeSelectedText()
            self._log_line_count -= MAX_LOG_LINES // 2
        logger.debug("SendCAN: %s", msg)

    def _clear_log(self) -> None:
        self.log_box.clear(); self._log_line_count = 0

    # ──────────────────────────────────────────────────────────
    # CLEANUP
    # ──────────────────────────────────────────────────────────
    def _cleanup(self) -> None:
        self._tx_timer.stop()
        self._ui_timer.stop()
        self._stats_timer.stop()
        logger.debug("SendCanTab cleaned up")