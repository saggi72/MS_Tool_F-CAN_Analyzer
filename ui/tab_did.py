"""
==============================================================
ui/tab_did.py  —  UDS DID Sub-Tab  (ISO 14229)
==============================================================

Chứa DidTab — widget quản lý việc đọc Data Identifier (DID):
  • Nhập DID hex, gửi lệnh Read DID đơn lẻ hoặc Read All
  • Hiển thị kết quả dạng bảng: DID | Name | Value | Raw
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit,
    QHeaderView, QTableWidget, QTableWidgetItem, QAbstractItemView,
)

from core.uds_engine import UdsServiceBuilder


# ==============================================================
# DID SUB-TAB
# ==============================================================

class DidTab(QWidget):
    """
    Sub-tab đọc/hiển thị DID.

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
        tb = QHBoxLayout()
        tb.setSpacing(6)

        tb.addWidget(QLabel("DID (hex):"))
        self._edit_did = QLineEdit("F180")
        self._edit_did.setFixedWidth(70)
        tb.addWidget(self._edit_did)

        btn_read = QPushButton("▶  Read DID")
        btn_read.setProperty("class", "primary")
        btn_read.setFixedWidth(95)
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

        # ── Result table ──────────────────────────────────────
        self._tbl = QTableWidget(0, 4)
        self._tbl.setHorizontalHeaderLabels(["DID (hex)", "Name", "Value", "Raw"])
        self._tbl.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self._tbl.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        self._tbl.setColumnWidth(0, 90)
        self._tbl.setColumnWidth(1, 170)
        self._tbl.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self._tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tbl.setAlternatingRowColors(True)
        lay.addWidget(self._tbl, stretch=1)

    # ─── Private actions ──────────────────────────────────────

    def _add_row(self) -> None:
        row = self._tbl.rowCount()
        self._tbl.insertRow(row)
        self._tbl.setItem(row, 0, QTableWidgetItem("F180"))
        for c in (1, 2, 3):
            self._tbl.setItem(row, c, QTableWidgetItem(""))

    def _remove_row(self) -> None:
        r = self._tbl.currentRow()
        if r >= 0:
            self._tbl.removeRow(r)

    def _send_read_did(self) -> None:
        try:
            did = int(self._edit_did.text().strip(), 16)
        except ValueError:
            return
        self.send_request.emit(
            UdsServiceBuilder.read_did(did),
            f"ReadDID 0x{did:04X}",
        )

    def _send_read_all(self) -> None:
        for r in range(self._tbl.rowCount()):
            it = self._tbl.item(r, 0)
            if it:
                try:
                    did = int(it.text().strip(), 16)
                    self.send_request.emit(
                        UdsServiceBuilder.read_did(did),
                        f"ReadDID 0x{did:04X}",
                    )
                except ValueError:
                    pass

    # ─── Public API ───────────────────────────────────────────

    def add_did_result(self, did: int, name: str, value: str, raw: str) -> None:
        """
        Cập nhật hoặc thêm mới một hàng kết quả DID vào bảng.

        Args:
            did:   DID dưới dạng int.
            name:  Tên mô tả DID (có thể rỗng).
            value: Giá trị đã decode.
            raw:   Chuỗi hex raw.
        """
        # Nếu đã tồn tại hàng với DID này thì cập nhật
        for r in range(self._tbl.rowCount()):
            it = self._tbl.item(r, 0)
            if it and it.text() == f"0x{did:04X}":
                self._tbl.setItem(r, 2, QTableWidgetItem(value))
                self._tbl.setItem(r, 3, QTableWidgetItem(raw))
                return
        # Chưa có thì thêm hàng mới
        r = self._tbl.rowCount()
        self._tbl.insertRow(r)
        self._tbl.setItem(r, 0, QTableWidgetItem(f"0x{did:04X}"))
        self._tbl.setItem(r, 1, QTableWidgetItem(name))
        self._tbl.setItem(r, 2, QTableWidgetItem(value))
        self._tbl.setItem(r, 3, QTableWidgetItem(raw))