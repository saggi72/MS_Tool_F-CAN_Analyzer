"""
==============================================================
ui/tab_dtc.py  —  UDS DTC Sub-Tab  (ISO 14229)
==============================================================

Chứa DtcTab — widget quản lý Diagnostic Trouble Codes:
  • Nhập Status Mask, gửi lệnh Read DTC hoặc Clear DTC
  • Hiển thị kết quả dạng bảng: DTC Code | Status | Description | Raw
  • Hỏi xác nhận trước khi xoá toàn bộ DTC
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit,
    QHeaderView, QTableWidget, QTableWidgetItem, QAbstractItemView,
    QMessageBox,
)

from core.uds_engine import UdsServiceBuilder


# ==============================================================
# DTC SUB-TAB
# ==============================================================

class DtcTab(QWidget):
    """
    Sub-tab đọc/xoá DTC.

    Signals:
        send_request(bytes, str) — payload và description để gửi UDS request.
    """

    send_request = pyqtSignal(bytes, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        # ── Toolbar ───────────────────────────────────────────
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
        btn_clear.clicked.connect(self._confirm_clear)
        ctrl.addWidget(btn_clear)

        ctrl.addStretch()

        self._lbl_count = QLabel("DTCs: —")
        self._lbl_count.setStyleSheet("font-size:11px; color:#555e7a;")
        ctrl.addWidget(self._lbl_count)

        lay.addLayout(ctrl)

        # ── Result table ──────────────────────────────────────
        self._tbl = QTableWidget(0, 4)
        self._tbl.setHorizontalHeaderLabels(
            ["DTC Code", "Status", "Description", "Raw"]
        )
        self._tbl.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self._tbl.setColumnWidth(0, 100)
        self._tbl.setColumnWidth(1, 88)
        self._tbl.setColumnWidth(3, 110)
        self._tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tbl.setAlternatingRowColors(True)
        lay.addWidget(self._tbl, stretch=1)

    # ─── Private actions ──────────────────────────────────────

    def _send_read_dtc(self) -> None:
        try:
            mask = int(self._edit_mask.text().strip(), 16)
        except ValueError:
            return
        self.send_request.emit(
            UdsServiceBuilder.read_dtc_by_status(mask),
            f"ReadDTCByStatusMask 0x{mask:02X}",
        )

    def _confirm_clear(self) -> None:
        ret = QMessageBox.question(
            self,
            "Confirm Clear DTC",
            "Clear ALL diagnostic trouble codes?\n\nThis action cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Yes:
            self.send_request.emit(
                UdsServiceBuilder.clear_dtc(),
                "ClearDiagnosticInformation (all)",
            )

    # ─── Public API ───────────────────────────────────────────

    def add_dtc(self, code: str, status: str, description: str, raw: str) -> None:
        """
        Thêm một hàng DTC mới vào bảng và cập nhật bộ đếm.

        Args:
            code:        Chuỗi mã DTC, ví dụ "P001234".
            status:      Chuỗi trạng thái hex, ví dụ "0x09".
            description: Mô tả lỗi (có thể rỗng nếu chưa có DB).
            raw:         Chuỗi hex raw bytes.
        """
        r = self._tbl.rowCount()
        self._tbl.insertRow(r)
        self._tbl.setItem(r, 0, QTableWidgetItem(code))
        self._tbl.setItem(r, 1, QTableWidgetItem(status))
        self._tbl.setItem(r, 2, QTableWidgetItem(description))
        self._tbl.setItem(r, 3, QTableWidgetItem(raw))
        self._lbl_count.setText(f"DTCs: {self._tbl.rowCount()}")

    def clear_dtc_table(self) -> None:
        """Xoá toàn bộ hàng trong bảng và reset bộ đếm."""
        self._tbl.setRowCount(0)
        self._lbl_count.setText("DTCs: 0")