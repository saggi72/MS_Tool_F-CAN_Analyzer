"""
==============================================================
ui/dialog_update_data.py  —  Update Data dialog (UDS tab)
==============================================================

Independent QDialog opened by the "Update data" button on the UDS tab.

Layout (per Image 3):

  ┌─ Update Data ──────────────────────────────────────┐
  │  Vehicle Name [____________]            [Save]     │
  │  [Add DBC]                                         │
  │  ┌───────────────────────────────────────────────┐ │
  │  │ ECU Name │ Req ID │ Resp ID │ DLL Path  [...] │ │
  │  │ ...                                           │ │
  │  └───────────────────────────────────────────────┘ │
  │                       [ + ]                        │
  └────────────────────────────────────────────────────┘

Public API
----------
UpdateDataDialog(parent=None, registry: VehicleRegistry, preselect: str|None)
    .exec()                  -> standard QDialog.exec()
    .saved_vehicle: str|None -> name of vehicle written to disk (None if Cancel)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog,
    QMessageBox, QAbstractItemView, QWidget,
)

from core.diag_config import (
    VehicleRegistry, extract_diag_ecus, save_vehicle_json,
    create_default_dids, _normalize_hex_id,
)

logger = logging.getLogger(__name__)


# ==============================================================
# COLUMN INDEXES
# ==============================================================

COL_ECU      = 0
COL_REQ_ID   = 1
COL_RESP_ID  = 2
COL_DLL_PATH = 3
COL_DLL_BTN  = 4   # browse button column
N_COLS       = 5


# ==============================================================
# DIALOG
# ==============================================================

class UpdateDataDialog(QDialog):
    """
    Build / edit / save a vehicle diagnostic JSON.

    Workflow:
      1. User types a Vehicle Name (e.g. "ModelX_2026").
      2. Either:
           • clicks "Add DBC" → DBC parsed, ECU rows auto-filled
           • clicks "+"       → blank row appended for manual entry
      3. Edits any cell inline (ECU name / Req ID / Resp ID).
      4. Clicks "..." in DLL Path to browse a .dll for that row.
      5. Clicks "Save" → writes `config/DID/<vehicle>_diagnostic.json`.
    """

    vehicle_saved = pyqtSignal(str)  # emitted with vehicle_model after Save

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        registry: Optional[VehicleRegistry] = None,
        preselect: Optional[str] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Update Data — Vehicle / ECU Configuration")
        self.setMinimumSize(820, 480)
        self._registry = registry or VehicleRegistry()

        # Biến trạng thái khởi tạo ban đầu
        self._dbc_path: Optional[str] = None
        self._functional_id: Optional[str] = None
        self.saved_vehicle: Optional[str] = None

        self._build_ui()
        self._wire()

        # Đảm bảo dọn dẹp sạch sẽ form trước khi nạp dữ liệu mới
        self._clear_form()

        # Preselect: load an existing vehicle's data for editing
        if preselect:
            self._load_existing_vehicle(preselect)

    # ─── OVERRIDE EXEC TO CLEAN FORM ──────────────────────────

    def exec(self) -> int:
        """Override exec to ensure the form is entirely cleared every time it opens."""
        self._clear_form()
        return super().exec()

    # ─── CLEAR FORM LOGIC ─────────────────────────────────────

    def _clear_form(self) -> None:
        """Resets all UI elements and state variables to ensure a clean slate."""
        logger.debug("Clearing UpdateDataDialog form history.")

        # 1. Clear vehicle name input
        if hasattr(self, "_edit_vehicle") and self._edit_vehicle:
            self._edit_vehicle.clear()

        # 2. Reset backend states
        self._dbc_path = None
        self._functional_id = None
        self.saved_vehicle = None

        # 3. Wipe all rows from the table
        if hasattr(self, "_tbl") and self._tbl:
            self._tbl.setRowCount(0)

        # 4. Revert DBC label to default state
        if hasattr(self, "_lbl_dbc") and self._lbl_dbc:
            self._lbl_dbc.setText("(no DBC loaded — rows are manual)")
            self._lbl_dbc.setStyleSheet("color:#9fa8c4; font-style:italic; font-size:11px;")

    # ─── UI ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # ── Top row: Vehicle Name + Save ──────────────────────
        top = QHBoxLayout()
        top.setSpacing(6)

        lbl = QLabel("Vehicle Name")
        lbl.setStyleSheet("font-weight:600; color:#555e7a;")
        top.addWidget(lbl)

        self._edit_vehicle = QLineEdit()
        self._edit_vehicle.setPlaceholderText("e.g. ModelX_2026")
        self._edit_vehicle.setMinimumWidth(220)
        top.addWidget(self._edit_vehicle)

        top.addStretch()

        self._btn_save = QPushButton("Save")
        self._btn_save.setFixedWidth(80)
        self._btn_save.setStyleSheet(
            "QPushButton{background:#3b5bdb;color:white;font-weight:700;"
            "border:1px solid #3251c4;border-radius:4px;padding:6px 14px;}"
            "QPushButton:hover{background:#3251c4;}"
        )
        top.addWidget(self._btn_save)
        root.addLayout(top)

        # ── Add DBC button row ────────────────────────────────
        dbc_row = QHBoxLayout()
        dbc_row.setSpacing(6)

        self._btn_add_dbc = QPushButton("Add DBC")
        self._btn_add_dbc.setFixedWidth(110)
        self._btn_add_dbc.setStyleSheet(
            "QPushButton{background:#e8f4fd;color:#1971c2;font-weight:600;"
            "border:1px solid #74c0fc;border-radius:4px;padding:5px 12px;}"
            "QPushButton:hover{background:#d0ebff;}"
        )
        dbc_row.addWidget(self._btn_add_dbc)

        self._lbl_dbc = QLabel("(no DBC loaded — rows are manual)")
        self._lbl_dbc.setStyleSheet("color:#9fa8c4; font-style:italic; font-size:11px;")
        dbc_row.addWidget(self._lbl_dbc)
        dbc_row.addStretch()
        root.addLayout(dbc_row)

        # ── Table ─────────────────────────────────────────────
        self._tbl = QTableWidget(0, N_COLS)
        self._tbl.setHorizontalHeaderLabels(
            ["ECU Name", "Req ID", "Resp ID", "DLL Path", ""]
        )
        self._tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tbl.setAlternatingRowColors(True)
        self._tbl.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )

        header = self._tbl.horizontalHeader()
        header.setSectionResizeMode(COL_ECU,      QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_REQ_ID,   QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_RESP_ID,  QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_DLL_PATH, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_DLL_BTN,  QHeaderView.ResizeMode.Fixed)
        self._tbl.setColumnWidth(COL_ECU,     160)
        self._tbl.setColumnWidth(COL_REQ_ID,   80)
        self._tbl.setColumnWidth(COL_RESP_ID,  80)
        self._tbl.setColumnWidth(COL_DLL_BTN,  36)
        root.addWidget(self._tbl, stretch=1)

        # ── + button (centered) ───────────────────────────────
        plus_row = QHBoxLayout()
        plus_row.addStretch()
        self._btn_add_row = QPushButton("+")
        self._btn_add_row.setFixedSize(56, 28)
        self._btn_add_row.setStyleSheet(
            "QPushButton{background:#ffffff;color:#1a1a2e;font-size:16px;"
            "font-weight:700;border:1px solid #c8cdd8;border-radius:4px;}"
            "QPushButton:hover{background:#eef0f8;border-color:#9fa8c4;}"
        )
        plus_row.addWidget(self._btn_add_row)
        plus_row.addStretch()
        root.addLayout(plus_row)

    def _wire(self) -> None:
        self._btn_save.clicked.connect(self._on_save)
        self._btn_add_dbc.clicked.connect(self._on_add_dbc)
        self._btn_add_row.clicked.connect(lambda: self._append_row("", "", "", ""))

    # ─── Row management ───────────────────────────────────────

    def _append_row(self, ecu: str, req_id: str, resp_id: str, dll_path: str) -> int:
        """Append a row; returns its index."""
        row = self._tbl.rowCount()
        self._tbl.insertRow(row)
        self._tbl.setItem(row, COL_ECU,      QTableWidgetItem(ecu))
        self._tbl.setItem(row, COL_REQ_ID,   QTableWidgetItem(req_id or ""))
        self._tbl.setItem(row, COL_RESP_ID,  QTableWidgetItem(resp_id or ""))
        self._tbl.setItem(row, COL_DLL_PATH, QTableWidgetItem(dll_path or ""))

        # Inline browse button in last column
        btn = QPushButton("…")
        btn.setFixedSize(28, 22)
        btn.setStyleSheet(
            "QPushButton{background:#f1f3f9;border:1px solid #c8cdd8;border-radius:3px;}"
            "QPushButton:hover{background:#dde2f0;}"
        )
        btn.clicked.connect(lambda _checked=False, r=row: self._browse_dll(r))
        self._tbl.setCellWidget(row, COL_DLL_BTN, btn)
        return row

    def _browse_dll(self, row: int) -> None:
        actual = self._row_of_button(self.sender(), fallback=row)
        if actual is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Select DLL for ECU", "", "DLL Files (*.dll);;All Files (*)"
        )
        if path:
            self._tbl.setItem(actual, COL_DLL_PATH, QTableWidgetItem(path))

    def _row_of_button(self, sender, fallback: int) -> Optional[int]:
        for r in range(self._tbl.rowCount()):
            if self._tbl.cellWidget(r, COL_DLL_BTN) is sender:
                return r
        return fallback if 0 <= fallback < self._tbl.rowCount() else None

    # ─── Add DBC ──────────────────────────────────────────────

    def _on_add_dbc(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select DBC File", "", "DBC Files (*.dbc);;All Files (*)"
        )
        if not path:
            return
        try:
            ecu_table, functional_id = extract_diag_ecus(path)
        except ImportError as ex:
            QMessageBox.critical(self, "DBC Parse Error", str(ex))
            return
        except Exception as ex:
            logger.exception("DBC parse failed")
            QMessageBox.critical(self, "DBC Parse Error", f"Failed to parse:\n{ex}")
            return

        if not ecu_table:
            QMessageBox.information(
                self, "Add DBC",
                "No diagnostic ECUs (ending in _DiagReq/_DiagResp) found in this DBC."
            )
            return

        self._dbc_path = path
        self._functional_id = functional_id
        self._lbl_dbc.setText(
            f"DBC: {Path(path).name}   ·   Functional ID: {functional_id or '—'}   "
            f"·   {len(ecu_table)} ECUs imported"
        )

        for ecu_name, info in ecu_table.items():
            if self._find_row_by_ecu(ecu_name) is not None:
                continue
            self._append_row(
                ecu=ecu_name,
                req_id=info.get("request_id") or "",
                resp_id=info.get("response_id") or "",
                dll_path=info.get("dll_path") or "",
            )

    def _find_row_by_ecu(self, ecu_name: str) -> Optional[int]:
        for r in range(self._tbl.rowCount()):
            item = self._tbl.item(r, COL_ECU)
            if item and item.text().strip() == ecu_name:
                return r
        return None

    # ─── Load existing vehicle for editing ────────────────────

    def _load_existing_vehicle(self, vehicle: str) -> None:
        data = self._registry.get_vehicle(vehicle)
        if not data:
            return
        self._edit_vehicle.setText(data.get("vehicle_model", vehicle))
        self._functional_id = data.get("functional_id") or None
        self._dbc_path = data.get("dbc_file") or None
        if self._dbc_path:
            self._lbl_dbc.setText(
                f"DBC: {self._dbc_path}   ·   Functional ID: {self._functional_id or '—'}"
            )
        for ecu_name, info in data.get("ecus", {}).items():
            self._append_row(
                ecu=ecu_name,
                req_id=info.get("request_id") or "",
                resp_id=info.get("response_id") or "",
                dll_path=info.get("dll_path") or "",
            )

    # ─── Save ─────────────────────────────────────────────────

    def _collect_table(self) -> Optional[Dict[str, Dict]]:
        ecu_data: Dict[str, Dict] = {}
        for r in range(self._tbl.rowCount()):
            name = (self._tbl.item(r, COL_ECU).text().strip()
                    if self._tbl.item(r, COL_ECU) else "")
            req  = (self._tbl.item(r, COL_REQ_ID).text().strip()
                    if self._tbl.item(r, COL_REQ_ID) else "")
            resp = (self._tbl.item(r, COL_RESP_ID).text().strip()
                    if self._tbl.item(r, COL_RESP_ID) else "")
            dll  = (self._tbl.item(r, COL_DLL_PATH).text().strip()
                    if self._tbl.item(r, COL_DLL_PATH) else "")

            if not name and not req and not resp and not dll:
                continue

            if not name:
                QMessageBox.warning(self, "Validation",
                                    f"Row {r + 1}: ECU Name is required.")
                return None
            if name in ecu_data:
                QMessageBox.warning(self, "Validation",
                                    f"Duplicate ECU name: '{name}'.")
                return None

            req_norm  = _normalize_hex_id(req)  if req  else None
            resp_norm = _normalize_hex_id(resp) if resp else None
            if req and req_norm is None:
                QMessageBox.warning(self, "Validation",
                                    f"Row {r + 1}: invalid Req ID '{req}'.")
                return None
            if resp and resp_norm is None:
                QMessageBox.warning(self, "Validation",
                                    f"Row {r + 1}: invalid Resp ID '{resp}'.")
                return None

            ecu_data[name] = {
                "request_id":       req_norm,
                "response_id":      resp_norm,
                "request_message":  f"{name}_DiagReq"  if req_norm  else None,
                "response_message": f"{name}_DiagResp" if resp_norm else None,
                "dll_path":         dll,
                "dids":             create_default_dids(),
            }
        return ecu_data

    def _on_save(self) -> None:
        vehicle = self._edit_vehicle.text().strip()
        if not vehicle:
            QMessageBox.warning(self, "Validation", "Vehicle Name is required.")
            self._edit_vehicle.setFocus()
            return

        ecu_data = self._collect_table()
        if ecu_data is None:
            return
        if not ecu_data:
            QMessageBox.warning(self, "Validation",
                                "Add at least one ECU before saving.")
            return

        try:
            out_path = save_vehicle_json(
                vehicle_model=vehicle,
                dbc_path=self._dbc_path or "",
                functional_id=self._functional_id,
                ecu_data=ecu_data,
                config_dir=self._registry.config_dir,
            )
        except (OSError, ValueError) as ex:
            QMessageBox.critical(self, "Save Error", f"Cannot write file:\n{ex}")
            return

        self.saved_vehicle = vehicle
        self.vehicle_saved.emit(vehicle)
        QMessageBox.information(
            self, "Saved",
            f"Configuration saved:\n{out_path}\n\n{len(ecu_data)} ECUs."
        )
        self.accept()