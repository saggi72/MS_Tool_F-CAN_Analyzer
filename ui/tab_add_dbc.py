"""
==============================================================
ui/tab_add_dbc.py   — CAN Network & DBC Database Management
==============================================================

Fixes applied:
  [FIX-1] Rename network: button ✏ Rename → edit inline → confirm/cancel
  [FIX-2] Columns resizable: all columns Interactive + setStretchLastSection(False)
  [FIX-3] Search works correctly: 1-char threshold, right logic order,
          red highlight on no-match, style reset on clear

Layout 3-panel:
  ┌──────────────────┬──────────────────────┬───────────────────┐
  │ Network List     │  Message Browser     │  Signal Detail    │
  │  ● Engine  CH1   │  🔍 [search…] [✓All] │  Message Info     │
  │  ○ Body    CH2   │  ▶ Engine ────────── │  ───────────────  │
  │ ─────────────    │    EngineSpeed 0x100 │  Signal table     │
  │ Network Detail   │    BrakePressure 0x2 │  (all cols resize)│
  │  Name [    ✏]   │  ▶ Body ──────────── │  ───────────────  │
  │  CH:   CH1       │    DoorStatus  0x300 │  Value Defs       │
  │  DBC: [path][..] │                      │                   │
  │  ✓ 128 msgs      │                      │                   │
  │  [Load][↻][✕]   │                      │                   │
  └──────────────────┴──────────────────────┴───────────────────┘
"""

from __future__ import annotations

import csv
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

# ─── Qt5/Qt6 dual compat ──────────────────────────────────────
try:
    from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot, QThread, QObject, QTimer
    from PyQt6.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
        QGroupBox, QLabel, QPushButton, QLineEdit,
        QListWidget, QListWidgetItem, QFileDialog, QMessageBox,
        QTreeWidget, QTreeWidgetItem, QHeaderView, QAbstractItemView,
        QTableWidget, QTableWidgetItem, QTextEdit, QCheckBox,
        QFormLayout, QInputDialog,
    )
    from PyQt6.QtGui import QFont, QColor, QBrush
    _UR          = Qt.ItemDataRole.UserRole
    _UR1         = Qt.ItemDataRole.UserRole.value + 1
    _AC          = Qt.AlignmentFlag.AlignCenter
    _AR          = Qt.AlignmentFlag.AlignRight
    _AL          = Qt.AlignmentFlag.AlignLeft
    _AV          = Qt.AlignmentFlag.AlignVCenter
    _HORIZ       = Qt.Orientation.Horizontal
    _SINGLE      = QAbstractItemView.SelectionMode.SingleSelection
    _ROWS        = QAbstractItemView.SelectionBehavior.SelectRows
    _NOEDIT      = QAbstractItemView.EditTrigger.NoEditTriggers
    _INTERACTIVE = QHeaderView.ResizeMode.Interactive
    _STRETCH     = QHeaderView.ResizeMode.Stretch
    _YES         = QMessageBox.StandardButton.Yes
    _NO          = QMessageBox.StandardButton.No
except ImportError:
    from PyQt5.QtCore import Qt, pyqtSignal, pyqtSlot, QThread, QObject, QTimer   # type: ignore
    from PyQt5.QtWidgets import (                                                   # type: ignore
        QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
        QGroupBox, QLabel, QPushButton, QLineEdit,
        QListWidget, QListWidgetItem, QFileDialog, QMessageBox,
        QTreeWidget, QTreeWidgetItem, QHeaderView, QAbstractItemView,
        QTableWidget, QTableWidgetItem, QTextEdit, QCheckBox,
        QFormLayout, QInputDialog,
    )
    from PyQt5.QtGui import QFont, QColor, QBrush                                  # type: ignore
    _UR          = Qt.UserRole
    _UR1         = Qt.UserRole + 1
    _AC          = Qt.AlignCenter
    _AR          = Qt.AlignRight
    _AL          = Qt.AlignLeft
    _AV          = Qt.AlignVCenter
    _HORIZ       = Qt.Horizontal
    _SINGLE      = QAbstractItemView.SingleSelection
    _ROWS        = QAbstractItemView.SelectRows
    _NOEDIT      = QAbstractItemView.NoEditTriggers
    _INTERACTIVE = QHeaderView.Interactive
    _STRETCH     = QHeaderView.Stretch
    _YES         = QMessageBox.Yes
    _NO          = QMessageBox.No

from models.can_network import CanNetwork
from models.config_manager import ConfigManager, DbcConfigHelper, ConfigError
from models.config_compatibility import ConfigUpgrader
from core.dbc_parser import DbcRegistry
from middleware.signals import SignalBus

logger = logging.getLogger(__name__)

try:
    import cantools
    _CANTOOLS_OK = True
except ImportError:
    cantools = None
    _CANTOOLS_OK = False
    logger.warning("cantools not installed — DBC loading unavailable")

# ── Constants ─────────────────────────────────────────────────
_DBC_ENCODINGS         = ("utf-8", "cp1252", "latin-1")
_DBC_FALLBACK_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
_SEARCH_DELAY_MS        = 250          # debounce delay
_PLACEHOLDER            = "..."        # lazy-load sentinel
_NAME_RE                = re.compile(r"^[a-zA-Z0-9_\-]+$")
_HEX_RE                 = re.compile(r"^0x([0-9a-fA-F]+)$")
_HEXH_RE                = re.compile(r"^([0-9a-fA-F]+)h$")
_CSV_HEADER = [
    "Network","Message","FrameID","DLC","CycleMS","SendType",
    "Signal","Start","Length","ByteOrder","Factor","Offset","Unit","Comment",
]


# ==============================================================
# DBC LOAD WORKER  (QObject + moveToThread — correct Qt pattern)
# ==============================================================

class DbcLoadWorker(QObject):
    """Parse DBC in background. 4-strategy fallback for encoding issues."""

    finished = pyqtSignal(str, bool, str)   # (net_name, ok, errmsg)
    progress = pyqtSignal(str, str)          # (net_name, status_text)

    def __init__(self, network: CanNetwork, path: str):
        super().__init__()
        self._network = network
        self._path    = path

    @pyqtSlot()
    def run(self) -> None:
        name = self._network.name
        path = self._path

        if not _CANTOOLS_OK:
            self.finished.emit(name, False, "cantools not installed")
            return
        if not os.path.isfile(path):
            self.finished.emit(name, False, f"File not found: {path}")
            return

        self.progress.emit(name, f"Parsing {os.path.basename(path)}…")

        db, err = self._load(path)
        if db is None:
            self.finished.emit(name, False, err)
            return

        from models.can_network import NodeCacheBuilder
        self._network.database   = db
        self._network.dbc_file   = path
        self._network.node_cache = NodeCacheBuilder.build(db)
        self.finished.emit(name, True, "")

    def _load(self, path: str):
        last = None
        # Strategy 1: default
        try:
            return cantools.db.load_file(path, strict=False), ""
        except TypeError:
            try:
                return cantools.db.load_file(path), ""
            except Exception as e:
                last = e
        except Exception as e:
            last = e
        # Strategy 2: explicit encodings
        for enc in _DBC_ENCODINGS:
            try:
                return cantools.db.load_file(path, encoding=enc, strict=False), ""
            except Exception as e:
                last = e
        # Strategy 3: alternate import
        try:
            import cantools.database as ctdb
            return ctdb.load_file(path), ""
        except Exception as e:
            last = e
        # Strategy 4: manual read
        for enc in _DBC_FALLBACK_ENCODINGS:
            try:
                with open(path, "r", encoding=enc, errors="replace") as f:
                    content = f.read()
                return cantools.db.load_string(content, database_format="dbc"), ""
            except Exception as e:
                last = e
        return None, str(last) if last else "Unknown error"


# ==============================================================
# SEARCH ENGINE  (name substring + hex/decimal ID)
# ==============================================================

class _Entry:
    __slots__ = ("name_lower", "item", "keys")
    def __init__(self, name_lower, item, keys=()):
        self.name_lower = name_lower
        self.item       = item
        self.keys       = keys          # pre-computed ID strings

class SearchEngine:
    """
    Builds an in-memory index from message/signal names and IDs.
    search() returns matching QTreeWidgetItem list in O(n).

    Supports:
      "Engine"   → substring match on message/signal names
      "0x1A0"    → exact hex ID match
      "1A0h"     → hex with h-suffix
      "416"      → decimal ID (exact)
    """

    def __init__(self):
        self._msgs: List[_Entry] = []
        self._sigs: List[_Entry] = []

    def clear(self):
        self._msgs.clear()
        self._sigs.clear()

    def add_message(self, msg, item: QTreeWidgetItem):
        fid  = getattr(msg, "frame_id", 0)
        keys = (f"0x{fid:x}", f"0x{fid:X}", f"{fid:x}h", f"{fid:X}h", str(fid))
        self._msgs.append(_Entry(msg.name.lower(), item, keys))

    def add_signal(self, sig, parent_item: QTreeWidgetItem):
        self._sigs.append(_Entry(sig.name.lower(), parent_item))

    def search(self, query: str) -> List[QTreeWidgetItem]:
        if not query:
            return []
        q      = query.lower().strip()
        is_id  = bool(_HEX_RE.match(q) or _HEXH_RE.match(q) or q.isdigit())
        out    = []
        seen   = set()

        # message pass
        for e in self._msgs:
            hit = any(q == k.lower() for k in e.keys) if is_id \
                  else q in e.name_lower
            if hit:
                eid = id(e.item)
                if eid not in seen:
                    out.append(e.item)
                    seen.add(eid)

        # signal pass (only for name queries)
        if not is_id:
            for e in self._sigs:
                if q in e.name_lower:
                    eid = id(e.item)
                    if eid not in seen:
                        out.append(e.item)
                        seen.add(eid)
        return out

    @property
    def is_empty(self) -> bool:
        return not self._msgs and not self._sigs


# ==============================================================
# NETWORK DETAIL PANEL  (bottom of left column)
# ==============================================================

class NetworkDetailPanel(QGroupBox):
    """
    Shows/edits one CanNetwork.

    [FIX-1] Rename: ✏ Rename button → inline edit (yellow bg) →
            Enter/OK confirms, Esc cancels → emit rename_requested.
    """

    dbc_loaded       = pyqtSignal(str)       # network_name on success
    rename_requested = pyqtSignal(str, str)  # (old_name, new_name)

    def __init__(self, dbc_registry: DbcRegistry, signal_bus: SignalBus,
                 parent=None):
        super().__init__("Network Detail", parent)
        self._registry   = dbc_registry
        self._signal_bus = signal_bus
        self._network:   Optional[CanNetwork]    = None
        self._thread:    Optional[QThread]       = None
        self._worker:    Optional[DbcLoadWorker] = None
        self._renaming   = False
        self._build_ui()

    # ── UI ────────────────────────────────────────────────────

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(6)

        form = QFormLayout()
        form.setSpacing(5)

        # [FIX-1] Name row: editable QLineEdit + Rename button
        name_row = QHBoxLayout()
        self._edit_name = QLineEdit()
        self._edit_name.setReadOnly(True)
        self._edit_name.setStyleSheet("background:#f0f0f0;")
        self._edit_name.setPlaceholderText("Network name")
        # Esc key cancels rename
        self._edit_name.installEventFilter(self)

        self._btn_rename = QPushButton("✏")
        self._btn_rename.setFixedWidth(30)
        self._btn_rename.setToolTip("Rename network (Enter = confirm, Esc = cancel)")
        self._btn_rename.setEnabled(False)
        self._btn_rename.clicked.connect(self._toggle_rename)
        name_row.addWidget(self._edit_name)
        name_row.addWidget(self._btn_rename)
        form.addRow("Name:", name_row)

        self._lbl_ch = QLabel("—")
        form.addRow("Channel:", self._lbl_ch)

        dbc_row = QHBoxLayout()
        self._edit_dbc = QLineEdit()
        self._edit_dbc.setPlaceholderText("No DBC file loaded")
        self._edit_dbc.setReadOnly(True)
        self._btn_browse = QPushButton("…")
        self._btn_browse.setFixedWidth(30)
        self._btn_browse.setEnabled(False)
        self._btn_browse.clicked.connect(self._on_browse)
        dbc_row.addWidget(self._edit_dbc)
        dbc_row.addWidget(self._btn_browse)
        form.addRow("DBC:", dbc_row)
        lay.addLayout(form)

        # Status
        self._lbl_status = QLabel("No network selected")
        self._lbl_status.setWordWrap(True)
        self._lbl_status.setStyleSheet(
            "color:#666;padding:4px;background:#f9f9f9;border-radius:3px;"
        )
        lay.addWidget(self._lbl_status)

        # Action buttons
        row = QHBoxLayout()
        self._btn_load = QPushButton("⚡ Load DBC")
        self._btn_load.setEnabled(False)
        self._btn_load.setStyleSheet(
            "QPushButton{background:#1976D2;color:white;border-radius:3px;"
            "padding:3px 8px;font-weight:bold;}"
            "QPushButton:hover{background:#1565C0;}"
            "QPushButton:disabled{background:#bbb;color:#eee;}"
        )
        self._btn_load.clicked.connect(self._on_load)

        self._btn_reload = QPushButton("↻")
        self._btn_reload.setFixedWidth(30)
        self._btn_reload.setEnabled(False)
        self._btn_reload.setToolTip("Reload DBC")
        self._btn_reload.clicked.connect(self._on_reload)

        self._btn_clear = QPushButton("✕")
        self._btn_clear.setFixedWidth(30)
        self._btn_clear.setEnabled(False)
        self._btn_clear.setToolTip("Clear DBC")
        self._btn_clear.clicked.connect(self._on_clear)

        row.addWidget(self._btn_load)
        row.addWidget(self._btn_reload)
        row.addWidget(self._btn_clear)
        row.addStretch()
        lay.addLayout(row)

        # Topology
        self._grp_topo = QGroupBox("Node Topology")
        tv = QVBoxLayout(self._grp_topo)
        self._lbl_topo = QLabel("—")
        self._lbl_topo.setWordWrap(True)
        self._lbl_topo.setStyleSheet("font-family:Consolas;font-size:9pt;")
        tv.addWidget(self._lbl_topo)
        self._grp_topo.setVisible(False)
        lay.addWidget(self._grp_topo)
        lay.addStretch()

    # ── Rename logic [FIX-1] ─────────────────────────────────

    def _toggle_rename(self):
        if self._renaming:
            self._confirm_rename()
        else:
            self._start_rename()

    def _start_rename(self):
        if not self._network:
            return
        self._renaming = True
        self._edit_name.setReadOnly(False)
        self._edit_name.setStyleSheet(
            "background:#fff9c4;border:1px solid #f9a825;"
        )
        self._edit_name.setFocus()
        self._edit_name.selectAll()
        self._btn_rename.setText("✓")
        self._btn_rename.setToolTip("Confirm rename (Enter)")
        # Enter key confirms
        self._edit_name.returnPressed.connect(self._confirm_rename)

    def _confirm_rename(self):
        """Commit rename: emit signal, reset UI."""
        if not self._renaming or not self._network:
            return

        new_name = self._edit_name.text().strip()
        old_name = self._network.name
        self._end_rename(old_name)      # reset UI first

        if new_name and new_name != old_name:
            self.rename_requested.emit(old_name, new_name)

    def _cancel_rename(self):
        """Cancel: restore original name."""
        if not self._renaming or not self._network:
            return
        orig = self._network.name
        self._edit_name.setText(orig)
        self._end_rename(orig)

    def _end_rename(self, name: str):
        """Reset rename UI state."""
        self._renaming = False
        self._edit_name.setText(name)
        self._edit_name.setReadOnly(True)
        self._edit_name.setStyleSheet("background:#f0f0f0;")
        self._btn_rename.setText("✏")
        self._btn_rename.setToolTip("Rename network (Enter = confirm, Esc = cancel)")
        try:
            self._edit_name.returnPressed.disconnect()
        except Exception:
            pass

    # eventFilter: Esc cancels rename
    def eventFilter(self, obj, event):
        try:
            from PyQt6.QtCore import QEvent
            key_press = QEvent.Type.KeyPress
        except ImportError:
            from PyQt5.QtCore import QEvent   # type: ignore
            key_press = QEvent.KeyPress
        if obj is self._edit_name:
            if event.type() == key_press:
                try:
                    key = event.key()
                    esc = Qt.Key.Key_Escape if hasattr(Qt.Key, "Key_Escape") \
                          else Qt.Key_Escape
                    if key == esc:
                        self._cancel_rename()
                        return True
                except Exception:
                    pass
        return super().eventFilter(obj, event)

    # ── Public API ────────────────────────────────────────────

    def load_network(self, network: CanNetwork):
        self._network = network
        self._end_rename(network.name)      # ensures clean state
        self._lbl_ch.setText(network.get_channel_display() or "—")
        self._edit_dbc.setText(network.dbc_file or "")
        self._btn_browse.setEnabled(True)
        self._btn_rename.setEnabled(True)
        self._btn_load.setEnabled(bool(network.dbc_file))
        self._btn_reload.setEnabled(network.is_loaded)
        self._btn_clear.setEnabled(network.is_loaded)
        self._refresh_status()

    def clear(self):
        self._cancel_rename()
        self._network = None
        self._edit_name.clear()
        self._lbl_ch.setText("—")
        self._edit_dbc.clear()
        self._set_status("No network selected", "#666", "#f9f9f9")
        self._grp_topo.setVisible(False)
        for b in (self._btn_browse, self._btn_load,
                  self._btn_reload, self._btn_clear, self._btn_rename):
            b.setEnabled(False)

    # ── Private helpers ───────────────────────────────────────

    def _refresh_status(self):
        if not self._network:
            return
        net = self._network
        if net.is_loaded:
            self._set_status(
                f"✓  {net.message_count} msgs  |  {net.node_count} nodes",
                "#2e7d32", "#f1f8e9"
            )
            nodes = net.get_all_nodes()
            if nodes:
                self._lbl_topo.setText(
                    ", ".join(nodes[:20]) + ("…" if len(nodes) > 20 else "")
                )
                self._grp_topo.setVisible(True)
        else:
            self._set_status("○  No DBC loaded", "#666", "#f9f9f9")
            self._grp_topo.setVisible(False)

    def _set_status(self, txt, fg, bg):
        self._lbl_status.setText(txt)
        self._lbl_status.setStyleSheet(
            f"color:{fg};padding:4px;background:{bg};border-radius:3px;"
        )

    @pyqtSlot()
    def _on_browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select DBC File", "",
            "DBC Files (*.dbc);;All Files (*)"
        )
        if path:
            self._edit_dbc.setText(path)
            self._btn_load.setEnabled(True)
            if self._network:
                self._network.dbc_file = path

    @pyqtSlot()
    def _on_load(self):
        if not self._network:
            return
        path = self._edit_dbc.text().strip()
        if not path:
            QMessageBox.warning(self, "No Path", "Browse for a DBC file first.")
            return
        if not os.path.isfile(path):
            QMessageBox.critical(self, "Not Found", f"File not found:\n{path}")
            return
        self._start_load(path)

    @pyqtSlot()
    def _on_reload(self):
        if self._network and self._network.dbc_file:
            self._start_load(self._network.dbc_file)

    @pyqtSlot()
    def _on_clear(self):
        if self._network:
            self._network.clear_dbc()
            self._registry.unregister(self._network.name)
            self._refresh_status()
            self._btn_reload.setEnabled(False)
            self._btn_clear.setEnabled(False)

    def _start_load(self, path: str):
        if not self._network:
            return
        if self._thread and self._thread.isRunning():
            return  # already loading

        self._thread = QThread(self)
        self._worker = DbcLoadWorker(self._network, path)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._thread.quit)

        self._btn_load.setEnabled(False)
        self._btn_load.setText("Loading…")
        self._thread.start()

    @pyqtSlot(str, str)
    def _on_progress(self, name: str, msg: str):
        self._set_status(f"⏳  {msg}", "#1565C0", "#e3f2fd")

    @pyqtSlot(str, bool, str)
    def _on_finished(self, name: str, ok: bool, err: str):
        self._btn_load.setText("⚡ Load DBC")
        self._btn_load.setEnabled(True)
        if ok and self._network:
            self._registry.register(self._network)
            self._refresh_status()
            self._btn_reload.setEnabled(True)
            self._btn_clear.setEnabled(True)
            self.dbc_loaded.emit(name)
            self._signal_bus.dbc_loaded.emit(name, self._network.message_count)
            logger.info(f"DBC loaded: {name} → {self._network.message_count} msgs")
        else:
            self._set_status(f"✕  {err}", "#c62828", "#ffebee")
            QMessageBox.critical(self, "DBC Load Error", f"Failed:\n{err}")


# ==============================================================
# ADD DBC TAB — main widget
# ==============================================================

class AddDbcTab(QWidget):
    """3-panel: Network list + detail | Message tree | Signal detail."""

    def __init__(self, dbc_registry: DbcRegistry, signal_bus: SignalBus,
                 parent=None):
        super().__init__(parent)
        self._registry   = dbc_registry
        self._signal_bus = signal_bus
        self._networks:  Dict[str, CanNetwork] = {}
        self._next_ch:   int = 1
        self._se         = SearchEngine()
        self._timer      = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._do_search)
        self._build_ui()

    # ==================================================
    # UI BUILD
    # ==================================================

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(5)
        root.addLayout(self._build_toolbar())

        sp = QSplitter(_HORIZ)
        sp.addWidget(self._build_left())
        sp.addWidget(self._build_center())
        sp.addWidget(self._build_right())
        sp.setSizes([260, 430, 340])
        sp.setStretchFactor(0, 0)
        sp.setStretchFactor(1, 2)
        sp.setStretchFactor(2, 1)
        root.addWidget(sp, stretch=1)

        self._lbl_status = QLabel("No networks loaded")
        self._lbl_status.setStyleSheet(
            "color:#666;font-family:Consolas;font-size:9pt;padding:1px;"
        )
        root.addWidget(self._lbl_status)

    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(6)

        self._btn_add = QPushButton("＋ Add Network")
        self._btn_add.setStyleSheet(
            "QPushButton{background:#388E3C;color:white;border-radius:3px;"
            "padding:4px 12px;font-weight:bold;}"
            "QPushButton:hover{background:#2E7D32;}"
        )
        self._btn_add.clicked.connect(self._on_add)

        self._btn_remove = QPushButton("－ Remove")
        self._btn_remove.setEnabled(False)
        self._btn_remove.clicked.connect(self._on_remove)

        self._btn_export = QPushButton("📊 Export CSV")
        self._btn_export.clicked.connect(self._on_export_csv)

        bar.addWidget(self._btn_add)
        bar.addWidget(self._btn_remove)
        bar.addStretch()
        bar.addWidget(self._btn_export)
        return bar

    # ── Left: network list + detail ──────────────────────────

    def _build_left(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 4, 0)
        v.setSpacing(4)

        v.addWidget(QLabel("Networks:"))
        self._list = QListWidget()
        self._list.setMinimumWidth(180)
        self._list.setAlternatingRowColors(True)
        self._list.currentRowChanged.connect(self._on_net_selected)
        v.addWidget(self._list, stretch=1)

        self._panel = NetworkDetailPanel(self._registry, self._signal_bus)
        self._panel.dbc_loaded.connect(self._on_dbc_loaded)
        self._panel.rename_requested.connect(self._on_rename)
        v.addWidget(self._panel, stretch=1)
        return w

    # ── Center: search + node/message tree ──────────────────

    def _build_center(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 0, 4, 0)
        v.setSpacing(4)

        # Search row
        row = QHBoxLayout()
        self._edit_search = QLineEdit()
        self._edit_search.setPlaceholderText(
            "🔍  message name / node / 0x1A0 / decimal…"
        )
        self._edit_search.setClearButtonEnabled(True)
        self._edit_search.textChanged.connect(self._on_search_text_changed)
        row.addWidget(self._edit_search)

        self._chk_all = QCheckBox("All Networks")
        self._chk_all.setChecked(True)
        self._chk_all.stateChanged.connect(self._rebuild_tree)
        row.addWidget(self._chk_all)
        v.addLayout(row)

        # View mode toggle: Node view vs Flat message view
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("View:"))
        self._chk_node_view = QCheckBox("Node topology (TX/RX)")
        self._chk_node_view.setChecked(True)
        self._chk_node_view.stateChanged.connect(self._rebuild_tree)
        mode_row.addWidget(self._chk_node_view)
        mode_row.addStretch()
        v.addLayout(mode_row)

        # Tree widget — 4 cột cơ bản, all Interactive
        # Cấu trúc:
        #   Network (bold, xanh đậm)
        #   └── Node_Name (xanh lá)
        #       ├── 📤 TX Messages (N)
        #       │   └── MessageName  0xID  DLC  Cycle  Type  #Sigs
        #       └── 📥 RX Messages (M)
        #           └── MessageName  0xID  DLC  Cycle  Type  #Sigs
        # Hoặc (flat view):
        #   Network
        #   └── MessageName  0xID  DLC  Cycle  Type  #Sigs
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(
            ["Name / Message", "0xID", "DLC", "Cycle", "Type", "Sigs"]
        )
        h = self._tree.header()
        for c in range(6):
            h.setSectionResizeMode(c, _INTERACTIVE)
        for c, w_ in {0: 200, 1: 68, 2: 35, 3: 58, 4: 72, 5: 38}.items():
            self._tree.setColumnWidth(c, w_)
        h.setStretchLastSection(False)
        self._tree.setFont(QFont("Consolas", 9))
        self._tree.setSelectionMode(_SINGLE)
        self._tree.setAlternatingRowColors(True)
        self._tree.setUniformRowHeights(True)
        self._tree.itemClicked.connect(self._on_tree_click)
        self._tree.itemExpanded.connect(self._on_expanded)
        v.addWidget(self._tree, stretch=1)
        return w

    # ── Right: message info + full signal table ──────────────

    # Cột signal theo yêu cầu (17 cột)
    _SIG_COLS = [
        ("Name",            90),
        ("Message",         90),
        ("Mux/Group",       65),
        ("Start Bit",       58),
        ("Length(Bit)",     70),
        ("Byte Order",      68),
        ("Value Type",      62),
        ("Initial Value",   72),
        ("Factor",          58),
        ("Offset",          55),
        ("Min",             55),
        ("Max",             55),
        ("Unit",            50),
        ("Value Table",     75),
        ("Comment",        100),
        ("Message ID",      70),
        ("GenSigSendType",  90),
        ("GenSigTimeOut",   80),
    ]

    def _build_right(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 0, 0, 0)
        v.setSpacing(4)

        # Message info compact
        grp = QGroupBox("Message Info")
        gv  = QVBoxLayout(grp)
        gv.setContentsMargins(6, 4, 6, 4)
        self._lbl_info = QLabel("No message selected")
        self._lbl_info.setWordWrap(True)
        self._lbl_info.setFont(QFont("Consolas", 9))
        self._lbl_info.setStyleSheet("color:#495057;")
        gv.addWidget(self._lbl_info)
        v.addWidget(grp)

        v.addWidget(QLabel("Signals:"))

        # Full 17-column signal table, all Interactive
        self._tbl = QTableWidget()
        self._tbl.setColumnCount(len(self._SIG_COLS))
        self._tbl.setHorizontalHeaderLabels(
            [h for h, _ in self._SIG_COLS]
        )
        sh = self._tbl.horizontalHeader()
        for c, (_, w_) in enumerate(self._SIG_COLS):
            sh.setSectionResizeMode(c, _INTERACTIVE)
            self._tbl.setColumnWidth(c, w_)
        sh.setStretchLastSection(False)
        self._tbl.setFont(QFont("Consolas", 9))
        self._tbl.setAlternatingRowColors(True)
        self._tbl.setEditTriggers(_NOEDIT)
        self._tbl.setSelectionBehavior(_ROWS)
        self._tbl.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
            if hasattr(QAbstractItemView, "ScrollMode")
            else QAbstractItemView.ScrollPerPixel           # type: ignore
        )
        self._tbl.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
            if hasattr(QAbstractItemView, "ScrollMode")
            else QAbstractItemView.ScrollPerPixel           # type: ignore
        )
        self._tbl.itemClicked.connect(self._on_sig_click)
        v.addWidget(self._tbl, stretch=1)
        return w

    # ==================================================
    # NETWORK MANAGEMENT
    # ==================================================

    @pyqtSlot()
    def _on_add(self):
        name, ok = QInputDialog.getText(
            self, "Add Network", "Network name:"
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        if not _NAME_RE.match(name):
            QMessageBox.warning(self, "Invalid Name",
                "Only alphanumeric, _ and - are allowed.")
            return
        if name in self._networks:
            QMessageBox.warning(self, "Duplicate",
                f"Network '{name}' already exists.")
            return
        try:
            net = CanNetwork(name=name, channel_index=self._next_ch)
            self._networks[name] = net
            self._next_ch += 1

            item = QListWidgetItem(f"○  {name}  (CH{net.channel_index})")
            item.setData(_UR, name)
            self._list.addItem(item)
            self._list.setCurrentItem(item)
            self._btn_remove.setEnabled(True)
            self._update_status()
            # Notify HwConnectTab + other listeners
            self._signal_bus.networks_changed.emit()
        except ValueError as e:
            QMessageBox.critical(self, "Error", str(e))

    @pyqtSlot()
    def _on_remove(self):
        cur = self._list.currentItem()
        if not cur:
            return
        name = cur.data(_UR)
        if QMessageBox.question(self, "Remove",
                f"Remove network '{name}'?", _YES | _NO) != _YES:
            return
        self._networks.pop(name, None)
        self._registry.unregister(name)
        self._list.takeItem(self._list.row(cur))
        self._panel.clear()
        self._tree.clear()
        self._se.clear()
        if not self._networks:
            self._btn_remove.setEnabled(False)
        self._update_status()
        # Notify HwConnectTab + other listeners
        self._signal_bus.networks_changed.emit()

    @pyqtSlot(int)
    def _on_net_selected(self, row: int):
        if row < 0:
            self._panel.clear()
            return
        item = self._list.item(row)
        if not item:
            return
        name = item.data(_UR)
        net  = self._networks.get(name)
        if net:
            self._panel.load_network(net)
            self._btn_remove.setEnabled(True)
            self._rebuild_tree()

    @pyqtSlot(str, str)
    def _on_rename(self, old_name: str, new_name: str):
        """Validate + apply rename from NetworkDetailPanel."""
        if not new_name:
            self._panel._end_rename(old_name)
            return
        if not _NAME_RE.match(new_name):
            QMessageBox.warning(self, "Invalid Name",
                "Only alphanumeric, _ and - allowed.")
            self._panel._end_rename(old_name)
            return
        if new_name in self._networks and new_name != old_name:
            QMessageBox.warning(self, "Duplicate",
                f"'{new_name}' already exists.")
            self._panel._end_rename(old_name)
            return

        net = self._networks.pop(old_name, None)
        if not net:
            return
        net.name = new_name
        self._networks[new_name] = net

        self._registry.unregister(old_name)
        if net.is_loaded:
            self._registry.register(net)

        # update list item
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(_UR) == old_name:
                item.setData(_UR, new_name)
                icon = "●" if net.is_loaded else "○"
                tag  = f"  [{net.message_count} msgs]" if net.is_loaded else ""
                item.setText(f"{icon}  {new_name}  (CH{net.channel_index}){tag}")
                break

        self._panel._end_rename(new_name)
        self._rebuild_tree()
        self._update_status()
        # Notify HwConnectTab (name changed → table needs refresh)
        self._signal_bus.networks_changed.emit()
        logger.info(f"Renamed: '{old_name}' → '{new_name}'")

    @pyqtSlot(str)
    def _on_dbc_loaded(self, net_name: str):
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(_UR) == net_name:
                net = self._networks.get(net_name)
                if net:
                    item.setText(
                        f"●  {net_name}  (CH{net.channel_index})"
                        f"  [{net.message_count} msgs]"
                    )
                    item.setForeground(QColor("#2e7d32"))
                break
        self._rebuild_tree()
        self._update_status()

    # ==================================================
    # MESSAGE / NODE TREE
    # ==================================================

    # Màu sắc theo cấp độ trong tree
    _C_NETWORK = ("#052c65", "#cfe2ff")   # fg, bg — network header
    _C_NODE    = ("#1b5e20", "#e8f5e9")   # node header
    _C_TX      = ("#4a148c", "#f3e5f5")   # TX group
    _C_RX      = ("#b71c1c", "#ffebee")   # RX group
    _C_MSG     = ("#212121", None)         # regular message row

    def _make_msg_item(self, msg, net_name: str) -> QTreeWidgetItem:
        """Tạo QTreeWidgetItem cho 1 message (dùng chung ở nhiều chỗ)."""
        cycle = str(getattr(msg, "cycle_time", "") or "")
        stype = str(getattr(msg, "send_type",  "") or "")
        mi = QTreeWidgetItem([
            msg.name,
            f"0x{msg.frame_id:03X}",
            str(msg.length),
            cycle,
            stype,
            str(len(msg.signals)),
        ])
        mi.setData(0, _UR,  ("message", msg))
        mi.setData(0, _UR1, net_name)
        # Placeholder cho lazy-expand signals
        if msg.signals:
            mi.addChild(QTreeWidgetItem([_PLACEHOLDER]))
        if "event" in stype.lower():
            mi.setForeground(4, QBrush(QColor("#664d03")))
        return mi

    def _make_group_item(self, label: str, fg: str, bg: str,
                         tag: str = "") -> QTreeWidgetItem:
        """Tạo header item cho network / node / TX / RX group."""
        text = f"{label}  {tag}" if tag else label
        item = QTreeWidgetItem([text, "", "", "", "", ""])
        item.setFont(0, QFont("", -1, QFont.Weight.Bold))
        item.setForeground(0, QBrush(QColor(fg)))
        if bg:
            item.setBackground(0, QBrush(QColor(bg)))
        return item

    def _rebuild_tree(self):
        """
        Rebuild toàn bộ tree.

        Node-view (checkbox on):
          Network
          └── NodeName
              ├── 📤 TX Messages (N)
              │   └── msg items…
              └── 📥 RX Messages (M)
                  └── msg items…

        Flat-view (checkbox off):
          Network
          └── msg items… (sorted by frame_id)
        """
        self._tree.clear()
        self._se.clear()

        show_all   = self._chk_all.isChecked()
        node_view  = self._chk_node_view.isChecked()

        sel_net: set = set()
        if not show_all:
            cur = self._list.currentItem()
            if cur:
                sel_net.add(cur.data(_UR))

        for net_name, net in self._networks.items():
            if not show_all and sel_net and net_name not in sel_net:
                continue
            if not net.is_loaded or not net.database:
                continue

            # ── Network header ──────────────────────────────
            n_msgs = len(net.database.messages)
            net_item = self._make_group_item(
                net_name, *self._C_NETWORK,
                tag=f"({n_msgs} msgs)"
            )
            net_item.setData(0, _UR, ("network", net_name))

            if node_view:
                self._fill_node_view(net_item, net, net_name)
            else:
                self._fill_flat_view(net_item, net, net_name)

            if net_item.childCount() > 0:
                self._tree.addTopLevelItem(net_item)
                net_item.setExpanded(True)

    def _fill_node_view(self, net_item: QTreeWidgetItem,
                        net, net_name: str) -> None:
        """
        Điền tree theo Node topology: mỗi node có TX và RX groups.

        node_cache structure (từ CanNetwork):
          {node_name: {tx_messages:[...], rx_messages:[...], ...}}

        Ngoài ra còn có "Unknown" node cho messages không có sender/receiver.
        """
        cache     = net.node_cache       # {node_name: {tx_messages, rx_messages, ...}}
        db_msgs   = {m.frame_id: m for m in net.database.messages}
        placed    = set()                # frame_ids đã được đặt vào node

        # ── Nodes từ topology cache ──────────────────────────
        for node_name in sorted(cache.keys()):
            info      = cache[node_name]
            tx_msgs   = info.get("tx_messages", [])
            rx_msgs   = info.get("rx_messages", [])
            if not tx_msgs and not rx_msgs:
                continue

            node_item = self._make_group_item(
                f"🖧  {node_name}", *self._C_NODE,
                tag=f"(TX:{len(tx_msgs)}  RX:{len(rx_msgs)})"
            )
            node_item.setData(0, _UR, ("node", node_name))

            # TX group
            if tx_msgs:
                tx_item = self._make_group_item(
                    "📤  TX Messages", *self._C_TX,
                    tag=f"({len(tx_msgs)})"
                )
                tx_item.setData(0, _UR, ("tx_group", node_name))
                for msg in sorted(tx_msgs, key=lambda m: m.frame_id):
                    mi = self._make_msg_item(msg, net_name)
                    tx_item.addChild(mi)
                    placed.add(msg.frame_id)
                    self._se.add_message(msg, mi)
                    for sig in msg.signals:
                        self._se.add_signal(sig, mi)
                node_item.addChild(tx_item)
                tx_item.setExpanded(True)

            # RX group
            if rx_msgs:
                rx_item = self._make_group_item(
                    "📥  RX Messages", *self._C_RX,
                    tag=f"({len(rx_msgs)})"
                )
                rx_item.setData(0, _UR, ("rx_group", node_name))
                for msg in sorted(rx_msgs, key=lambda m: m.frame_id):
                    mi = self._make_msg_item(msg, net_name)
                    rx_item.addChild(mi)
                    placed.add(msg.frame_id)
                    self._se.add_message(msg, mi)
                    for sig in msg.signals:
                        self._se.add_signal(sig, mi)
                node_item.addChild(rx_item)
                rx_item.setExpanded(True)

            net_item.addChild(node_item)
            node_item.setExpanded(True)

        # ── Unassigned: messages không thuộc node nào ────────
        unassigned = [m for m in sorted(net.database.messages,
                                         key=lambda m: m.frame_id)
                      if m.frame_id not in placed]
        if unassigned:
            ua_item = self._make_group_item(
                "⚬  Unassigned", "#546e7a", "#eceff1",
                tag=f"({len(unassigned)})"
            )
            ua_item.setData(0, _UR, ("unassigned", net_name))
            for msg in unassigned:
                mi = self._make_msg_item(msg, net_name)
                ua_item.addChild(mi)
                self._se.add_message(msg, mi)
                for sig in msg.signals:
                    self._se.add_signal(sig, mi)
            net_item.addChild(ua_item)
            ua_item.setExpanded(False)

    def _fill_flat_view(self, net_item: QTreeWidgetItem,
                        net, net_name: str) -> None:
        """Flat view: tất cả messages thẳng dưới network, sort theo frame_id."""
        for msg in sorted(net.database.messages, key=lambda m: m.frame_id):
            mi = self._make_msg_item(msg, net_name)
            net_item.addChild(mi)
            self._se.add_message(msg, mi)
            for sig in msg.signals:
                self._se.add_signal(sig, mi)

    def _on_expanded(self, item: QTreeWidgetItem):
        """Lazy-expand signals khi user mở rộng message node."""
        if item.childCount() != 1 or item.child(0).text(0) != _PLACEHOLDER:
            return
        data = item.data(0, _UR)
        if not data or data[0] != "message":
            return
        item.takeChild(0)
        for sig in data[1].signals:
            si = QTreeWidgetItem([sig.name, "", "", "", "", ""])
            si.setData(0, _UR, ("signal", sig))
            si.setForeground(0, QBrush(QColor("#0d47a1")))
            item.addChild(si)

    # ==================================================
    # SIGNAL DETAIL  (17 columns)
    # ==================================================

    def _on_tree_click(self, item: QTreeWidgetItem, col: int):
        """Dispatch click trên bất kỳ node nào trong tree."""
        data = item.data(0, _UR)
        if not data:
            return
        kind = data[0]
        if kind == "message":
            self._show_message(data[1])
        elif kind == "signal":
            # Click trực tiếp vào signal leaf → show parent message signals
            parent = item.parent()
            if parent:
                pd = parent.data(0, _UR)
                if pd and pd[0] == "message":
                    self._show_message(pd[1])
        # network / node / group clicks → không làm gì

    def _show_message(self, msg):
        """Hiện thông tin message + load signals vào 17-cột table."""
        # ── Message Info ──────────────────────────────────────
        frame_id_hex = f"0x{msg.frame_id:03X}"
        frame_id_ext = " (Extended)" if getattr(msg, "is_extended_id", False) else ""
        lines = [
            f"{'Name':<12}: {msg.name}",
            f"{'Frame ID':<12}: {frame_id_hex}  ({msg.frame_id}){frame_id_ext}",
            f"{'DLC':<12}: {msg.length} bytes",
        ]
        for attr, label in [
            ("cycle_time", "Cycle"),
            ("send_type",  "SendType"),
            ("comment",    "Comment"),
        ]:
            v = getattr(msg, attr, None)
            if v:
                lines.append(f"{label:<12}: {v}")
        self._lbl_info.setText("\n".join(lines))

        # ── Signal table ──────────────────────────────────────
        sigs = msg.signals
        self._tbl.setRowCount(len(sigs))

        frame_id_str = f"0x{msg.frame_id:03X}"
        msg_name_str = msg.name

        for r, sig in enumerate(sigs):
            self._fill_signal_row(r, sig, msg_name_str, frame_id_str)

        # Resize row heights cho dễ đọc
        self._tbl.resizeRowsToContents()

    def _fill_signal_row(self, r: int, sig, msg_name: str, frame_id: str):
        """
        Điền 1 hàng signal với đầy đủ 17 cột.

        Cột theo yêu cầu:
          0  Name
          1  Message
          2  Multiplexing/Group
          3  Start Bit
          4  Length(Bit)
          5  Byte Order
          6  Value Type
          7  Initial Value
          8  Factor
          9  Offset
          10 Min
          11 Max
          12 Unit
          13 Value Table
          14 Comment
          15 Message ID
          16 GenSigSendType
          17 GenSigTimeOut
        """
        def _g(attr, default=""):
            v = getattr(sig, attr, None)
            return "" if v is None else str(v)

        # ── Trích xuất giá trị ────────────────────────────────
        # Multiplexing
        mux_id   = getattr(sig, "multiplexer_ids", None)
        mux_ind  = getattr(sig, "is_multiplexer",  False)
        if mux_ind:
            mux_str = "MUX_IND"
        elif mux_id:
            mux_str = f"M{mux_id[0]}" if len(mux_id) == 1 else str(mux_id)
        else:
            mux_str = ""

        # Byte order
        bo_raw = str(getattr(sig, "byte_order", "") or "")
        byte_order = "Big Endian" if "big" in bo_raw.lower() else "Little Endian"

        # Value type (signed/unsigned/float)
        is_float  = getattr(sig, "is_float",  False)
        is_signed = getattr(sig, "is_signed", True)
        if is_float:
            val_type = "Float" if getattr(sig, "length", 0) <= 32 else "Double"
        else:
            val_type = "Signed" if is_signed else "Unsigned"

        # Initial value
        init_val = getattr(sig, "initial", None)
        init_str = str(init_val) if init_val is not None else ""

        # Min / Max
        min_val  = getattr(sig, "minimum", None)
        max_val  = getattr(sig, "maximum", None)
        min_str  = str(min_val) if min_val is not None else ""
        max_str  = str(max_val) if max_val is not None else ""

        # Value table (choices)
        choices  = getattr(sig, "choices", None)
        if choices:
            try:
                vt_str = "; ".join(f"{k}={v}" for k, v in choices.items())
            except Exception:
                vt_str = str(choices)
        else:
            vt_str = ""

        # cantools custom attributes (GenSig*)
        attrs     = getattr(sig, "dbc", None) or getattr(sig, "_dbc", None)
        gen_attrs = {}
        if attrs:
            for attr_name in ("GenSigSendType", "GenSigTimeOut"):
                v = getattr(attrs, attr_name, None)
                if v is None and hasattr(attrs, "attributes"):
                    v = attrs.attributes.get(attr_name)
                if v is not None:
                    gen_attrs[attr_name] = str(v.value if hasattr(v, "value") else v)

        # Fallback: cantools >= 4.x lưu trong signal.dbc_specifics
        if not gen_attrs:
            dbc_spec = getattr(sig, "dbc_specifics", None)
            if dbc_spec and hasattr(dbc_spec, "attributes"):
                for attr_name in ("GenSigSendType", "GenSigTimeOut"):
                    v = dbc_spec.attributes.get(attr_name)
                    if v is not None:
                        gen_attrs[attr_name] = str(
                            v.value if hasattr(v, "value") else v
                        )

        gen_send = gen_attrs.get("GenSigSendType", "")
        gen_tout = gen_attrs.get("GenSigTimeOut",  "")

        # ── Gán vào table ─────────────────────────────────────
        values = [
            (sig.name,                      _AC),  # 0  Name
            (msg_name,                      _AL),  # 1  Message
            (mux_str,                       _AC),  # 2  Mux/Group
            (str(getattr(sig,"start",0)),   _AC),  # 3  Start Bit
            (str(getattr(sig,"length",0)),  _AC),  # 4  Length(Bit)
            (byte_order,                    _AC),  # 5  Byte Order
            (val_type,                      _AC),  # 6  Value Type
            (init_str,                      _AR),  # 7  Initial Value
            (str(getattr(sig,"scale",1.0)), _AR),  # 8  Factor
            (str(getattr(sig,"offset",0.0)),_AR),  # 9  Offset
            (min_str,                       _AR),  # 10 Min
            (max_str,                       _AR),  # 11 Max
            (str(getattr(sig,"unit","") or ""), _AC),  # 12 Unit
            (vt_str,                        _AL),  # 13 Value Table
            (str(getattr(sig,"comment","") or ""), _AL),  # 14 Comment
            (frame_id,                      _AC),  # 15 Message ID
            (gen_send,                      _AC),  # 16 GenSigSendType
            (gen_tout,                      _AC),  # 17 GenSigTimeOut
        ]

        for c, (txt, align) in enumerate(values):
            it = QTableWidgetItem(str(txt))
            it.setTextAlignment(int(align) | int(_AV))
            it.setData(_UR, sig)
            self._tbl.setItem(r, c, it)

        # Tô nền cho mux signals để phân biệt
        if mux_str:
            bg = QColor("#fff9c4") if mux_str == "MUX_IND" else QColor("#e8f5e9")
            for c in range(len(self._SIG_COLS)):
                it = self._tbl.item(r, c)
                if it:
                    it.setBackground(QBrush(bg))

    def _on_sig_click(self, item: QTableWidgetItem):
        """
        Click signal row — hiện popup tooltip với value table đầy đủ
        (không cần widget riêng vì đã có cột Value Table trong bảng).
        """
        sig = item.data(_UR)
        if not sig:
            return
        choices = getattr(sig, "choices", None)
        if not choices:
            return
        # Highlight cột Value Table để user biết có data
        row = item.row()
        vt_item = self._tbl.item(row, 13)   # col 13 = Value Table
        if vt_item:
            self._tbl.scrollToItem(vt_item)
            self._tbl.setCurrentItem(vt_item)

    # ==================================================
    # SEARCH  [FIX-3]
    # ==================================================

    @pyqtSlot(str)
    def _on_search_text_changed(self, text: str):
        """Reset immediately on empty; debounce otherwise."""
        self._timer.stop()
        if not text.strip():
            self._edit_search.setStyleSheet("")   # clear red hint
            self._reset_visibility()
            return
        self._timer.start(_SEARCH_DELAY_MS)

    def _do_search(self):
        """
        [FIX-3] Correct order:
          1. Get matches first (while tree is fully visible)
          2. Then hide all message-level items
          3. Then show only matches + their parents
          4. Red hint if zero matches; clear style on match
        """
        query = self._edit_search.text().strip()
        if not query:
            self._reset_visibility()
            return

        # Nothing indexed yet → show all silently
        if self._se.is_empty:
            self._reset_visibility()
            return

        matches = self._se.search(query)   # ← search BEFORE hiding

        # Hide every message node (keep network headers visible)
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            for j in range(top.childCount()):
                top.child(j).setHidden(True)

        if not matches:
            # Show red border on search box; keep tree visible (all hidden = blank)
            # Instead restore all so user sees "no match" in context
            self._reset_visibility()
            self._edit_search.setStyleSheet(
                "QLineEdit{background:#ffebee;border:1px solid #ef9a9a;}"
            )
            return

        self._edit_search.setStyleSheet("")   # clear red hint

        first = None
        for item in matches:
            # Walk up to show item and all ancestors
            cur = item
            while cur:
                cur.setHidden(False)
                self._tree.expandItem(cur)
                cur = cur.parent()
            if first is None:
                first = item

        if first:
            self._tree.scrollToItem(first)
            self._tree.setCurrentItem(first)

    def _reset_visibility(self):
        """Show all tree items (iterative, safe for large trees)."""
        stack = [self._tree.topLevelItem(i)
                 for i in range(self._tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            item.setHidden(False)
            stack.extend(item.child(j) for j in range(item.childCount()))

    # ==================================================
    # EXPORT CSV  (17-column format)
    # ==================================================

    _CSV_HEADER_FULL = [
        "Network","Message","Message_ID","DLC","CycleTime","SendType",
        "Signal","Mux_Group","Start_Bit","Length_Bit","Byte_Order",
        "Value_Type","Initial_Value","Factor","Offset","Min","Max",
        "Unit","Value_Table","Comment","GenSigSendType","GenSigTimeOut",
    ]

    @pyqtSlot()
    def _on_export_csv(self):
        if not self._networks:
            QMessageBox.information(self, "Empty", "No networks loaded.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Signal List", "signals.csv", "CSV (*.csv)"
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(self._CSV_HEADER_FULL)
                for net_name, net in self._networks.items():
                    if not net.is_loaded or not net.database:
                        continue
                    for msg in sorted(net.database.messages,
                                      key=lambda m: m.frame_id):
                        cy  = str(getattr(msg, "cycle_time", "") or "")
                        st  = str(getattr(msg, "send_type",  "") or "")
                        fid = f"0x{msg.frame_id:03X}"
                        for sig in msg.signals:
                            bo_raw = str(getattr(sig,"byte_order","") or "")
                            bo  = "Big Endian" if "big" in bo_raw.lower() \
                                  else "Little Endian"
                            mux_id  = getattr(sig,"multiplexer_ids",None)
                            mux_ind = getattr(sig,"is_multiplexer",False)
                            if mux_ind:
                                mux = "MUX_IND"
                            elif mux_id:
                                mux = f"M{mux_id[0]}" if len(mux_id)==1 \
                                      else str(mux_id)
                            else:
                                mux = ""
                            is_float  = getattr(sig,"is_float",False)
                            is_signed = getattr(sig,"is_signed",True)
                            vtype = ("Float" if is_float and
                                      getattr(sig,"length",0)<=32
                                      else "Double" if is_float
                                      else "Signed" if is_signed
                                      else "Unsigned")
                            choices = getattr(sig,"choices",None)
                            vt = "; ".join(f"{k}={v}"
                                           for k,v in choices.items()) \
                                 if choices else ""
                            # GenSig attributes
                            gen_send, gen_tout = "", ""
                            for attr_src in (
                                getattr(sig,"dbc_specifics",None),
                                getattr(sig,"dbc",None),
                            ):
                                if attr_src and hasattr(attr_src,"attributes"):
                                    for k,attr_n in [("GenSigSendType","GenSigSendType"),
                                                     ("GenSigTimeOut","GenSigTimeOut")]:
                                        v = attr_src.attributes.get(attr_n)
                                        if v is not None:
                                            s = str(v.value if hasattr(v,"value")
                                                    else v)
                                            if k == "GenSigSendType":
                                                gen_send = s
                                            else:
                                                gen_tout = s

                            min_v = getattr(sig,"minimum",None)
                            max_v = getattr(sig,"maximum",None)
                            init  = getattr(sig,"initial",None)
                            w.writerow([
                                net_name, msg.name, fid, msg.length, cy, st,
                                sig.name, mux,
                                getattr(sig,"start",0),
                                getattr(sig,"length",0), bo, vtype,
                                "" if init is None else init,
                                getattr(sig,"scale",1.0),
                                getattr(sig,"offset",0.0),
                                "" if min_v is None else min_v,
                                "" if max_v is None else max_v,
                                getattr(sig,"unit","") or "", vt,
                                getattr(sig,"comment","") or "",
                                gen_send, gen_tout,
                            ])
            self._lbl_status.setText(f"Exported: {path}")
            logger.info(f"CSV exported: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", str(e))

    # ==================================================
    # CONFIG LOAD / SAVE
    # ==================================================

    def load_config(self, config: dict):
        """Restore from config dict (called by MainWindow / FileTab)."""
        config = ConfigUpgrader.upgrade_config(config)
        self._networks.clear()
        self._list.clear()
        self._panel.clear()
        self._tree.clear()
        self._se.clear()
        self._next_ch = 1

        missing, failed = [], []

        for entry in config.get("networks", []):
            name     = entry.get("name", "")
            ch       = entry.get("channel", self._next_ch)
            dbc_path = entry.get("dbc_path", "")
            if not name:
                continue
            try:
                net = CanNetwork(name=name, channel_index=ch)
                self._networks[name] = net
                self._next_ch = max(self._next_ch, ch + 1)

                if dbc_path:
                    net.dbc_file = dbc_path
                    if os.path.isfile(dbc_path):
                        try:
                            net.load_dbc(dbc_path)
                            self._registry.register(net)
                        except Exception as e:
                            failed.append((name, str(e)))
                    else:
                        missing.append(name)

                icon  = "●" if net.is_loaded else ("⚠" if dbc_path else "○")
                tag   = f"  [{net.message_count} msgs]" if net.is_loaded else \
                        " (DBC missing)" if (dbc_path and not net.is_loaded) else ""
                color = QColor("#2e7d32") if net.is_loaded else \
                        QColor("#b26a00") if dbc_path else QColor("#333")
                item  = QListWidgetItem(f"{icon}  {name}  (CH{ch}){tag}")
                item.setData(_UR, name)
                item.setForeground(color)
                self._list.addItem(item)
            except Exception as e:
                logger.warning(f"Skip '{name}': {e}")

        if self._networks:
            self._list.setCurrentRow(0)
            self._btn_remove.setEnabled(True)
        self._rebuild_tree()
        self._update_status()

        if missing or failed:
            lines = []
            for n in missing:
                lines.append(f"  ⚠  {n}: DBC not found")
            for n, e in failed:
                lines.append(f"  ✕  {n}: {e}")
            QMessageBox.warning(self, "Config Loaded (Issues)",
                f"{len(self._networks)} networks loaded.\n\n"
                + "\n".join(lines)
                + "\n\nUse 'Add DBC' tab to fix missing files.")

    def save_config(self, config_name: str):
        if not self._networks:
            QMessageBox.information(self, "Empty", "No networks to save.")
            return
        try:
            cfg  = DbcConfigHelper.create_config_from_networks(
                config_name, self._networks)
            path = ConfigManager.save_config(cfg, config_name)
            QMessageBox.information(self, "Saved", f"Saved:\n{path}")
        except ConfigError as e:
            QMessageBox.critical(self, "Save Error", str(e))

    # ==================================================
    # STATUS
    # ==================================================

    def _update_status(self):
        n   = len(self._networks)
        ld  = sum(1 for v in self._networks.values() if v.is_loaded)
        msgs= sum(v.message_count for v in self._networks.values())
        self._lbl_status.setText(
            f"Networks: {n}  |  DBC loaded: {ld}  |  Messages: {msgs}"
        )