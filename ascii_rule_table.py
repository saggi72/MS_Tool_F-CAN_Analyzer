"""Logic cho tab AsciiRule (không sửa file pyuic6).

Dùng:
    self.setupUi(self)
    self.ascii_rule = AsciiRuleTable(self.tableAsciiRule)
    self.btnAddRow.clicked.connect(self.ascii_rule.add_row)       # nút Add row (nếu có)
    self.btnRemoveRow.clicked.connect(self.ascii_rule.remove_selected_rows)
    # đổi ECU Apply:
    self.comboBoxECUList.currentTextChanged.connect(self.ascii_rule.set_ecu)
    # nạp / lưu:
    self.ascii_rule.load_json(path);  self.ascii_rule.save_json(path)

Định dạng file ASCIIRule.json:
    {"All": [{"request": "22F187", "mask": "1110000000000000000000"}],
     "BCM": [ ... ]}
mask: ký tự '1' = byte đó dịch sang ASCII, '0' = giữ hex.
mask[i] ứng với Byte#i; Byte#0 là byte giá trị đầu tiên (sau SID + DID),
vd 62 F1 88 01 02 03 -> Byte#0 = 01. Tối thiểu 22 ký tự, dài hơn nếu thêm cột.
Rule riêng của ECU ghi đè rule "All" cùng request.
"""
import json
import os
from PyQt6 import QtWidgets
from PyQt6.QtCore import Qt

MIN_BYTES = 22                  # Byte#0 .. Byte#21
REQUEST_COL = 0
FIRST_BYTE_COL = 1
ALL = "All"
CELL_FLAGS = (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
              | Qt.ItemFlag.ItemIsUserCheckable)

# Rule mặc định cho "All": (request, số byte đầu cần dịch ASCII)
DEFAULT_RULES = [
    ("22F191", 5), ("22F188", 5), ("22F164", 5), ("22F104", 5), ("22F105", 5),
    ("22F103", 5), ("22F102", 5), ("22F106", 5), ("22F109", 5),
    ("22F194", 21), ("22F190", 21), ("22F15C", 21),
]


def make_mask(n_ascii: int, length: int = MIN_BYTES) -> str:
    return ("1" * n_ascii).ljust(max(length, MIN_BYTES), "0")


class AsciiRuleTable:
    def __init__(self, table: QtWidgets.QTableWidget):
        self.t = table
        self.ecu = ALL
        self.data = {ALL: [{"request": r, "mask": make_mask(n)} for r, n in DEFAULT_RULES]}
        self._setup_table()
        self._show(self.ecu)

    # ---------- cấu hình bảng ----------
    def _setup_table(self):
        t = self.t
        t.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectItems)
        t.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        t.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        t.setSortingEnabled(False)
        t.verticalHeader().setVisible(True)
        t.horizontalHeader().setSectionsMovable(False)
        t.horizontalHeader().setStretchLastSection(False)
        t.horizontalHeader().sectionDoubleClicked.connect(self._toggle_column)
        t.horizontalHeader().sectionClicked.connect(self._header_clicked)
        self._set_columns(MIN_BYTES)

    @property
    def n_bytes(self):
        return self.t.columnCount() - FIRST_BYTE_COL - 1     # trừ cột Sub_Func và More

    @property
    def more_col(self):
        return self.t.columnCount() - 1

    def _set_columns(self, n_bytes):
        t = self.t
        t.setColumnCount(FIRST_BYTE_COL + n_bytes + 1)
        t.setHorizontalHeaderLabels(["Sub_Func"] + [f"Byte#{i}" for i in range(n_bytes)] + ["More"])
        t.horizontalHeaderItem(FIRST_BYTE_COL).setToolTip(
            "Byte#0 là byte giá trị đầu tiên. Ví dụ 62 F1 88 01 02 03 -> Byte#0 = 01.\n"
            "Tick = dịch byte sang ASCII. Double-click tiêu đề cột để chọn/bỏ cả cột.\n"
            "Bấm 'More' để thêm một cột byte.")
        t.horizontalHeaderItem(t.columnCount() - 1).setToolTip("Bấm để thêm cột Byte")
        t.setColumnWidth(REQUEST_COL, 100)
        hdr = t.horizontalHeader()
        for c in range(FIRST_BYTE_COL, t.columnCount()):
            hdr.setSectionResizeMode(c, QtWidgets.QHeaderView.ResizeMode.Fixed)
            t.setColumnWidth(c, 52 if c == t.columnCount() - 1 else 46)

    # ---------- thêm cột / hàng ----------
    def add_byte_column(self):
        t = self.t
        col = self.more_col
        t.insertColumn(col)
        t.setHorizontalHeaderItem(col, QtWidgets.QTableWidgetItem(f"Byte#{col - FIRST_BYTE_COL}"))
        t.horizontalHeader().setSectionResizeMode(col, QtWidgets.QHeaderView.ResizeMode.Fixed)
        t.setColumnWidth(col, 46)
        for r in range(t.rowCount()):
            self._set_cell(r, col, False)

    def add_row(self, request: str = "", mask: str = ""):
        t = self.t
        r = t.rowCount()
        t.insertRow(r)
        t.setItem(r, REQUEST_COL, QtWidgets.QTableWidgetItem(request))
        for i in range(self.n_bytes):
            self._set_cell(r, FIRST_BYTE_COL + i, i < len(mask) and mask[i] == "1")
        dummy = QtWidgets.QTableWidgetItem()
        dummy.setFlags(Qt.ItemFlag.NoItemFlags)
        t.setItem(r, self.more_col, dummy)

    def remove_selected_rows(self):
        for r in sorted({i.row() for i in self.t.selectedIndexes()}, reverse=True):
            self.t.removeRow(r)

    def _set_cell(self, r, c, checked):
        it = QtWidgets.QTableWidgetItem()
        it.setFlags(CELL_FLAGS)
        it.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.t.setItem(r, c, it)

    # ---------- header ----------
    def _header_clicked(self, col):
        if col == self.more_col:
            self.add_byte_column()

    def _toggle_column(self, col):
        if not (FIRST_BYTE_COL <= col < self.more_col):
            return
        cells = [self.t.item(r, col) for r in range(self.t.rowCount())]
        all_on = all(c.checkState() == Qt.CheckState.Checked for c in cells)
        state = Qt.CheckState.Unchecked if all_on else Qt.CheckState.Checked
        for c in cells:
            c.setCheckState(state)

    # ---------- bảng <-> dữ liệu ----------
    def _read_table(self) -> list:
        rules = []
        for r in range(self.t.rowCount()):
            req = (self.t.item(r, REQUEST_COL).text() or "").replace(" ", "").upper()
            if not req:
                continue
            bits = "".join(
                "1" if self.t.item(r, FIRST_BYTE_COL + i).checkState() == Qt.CheckState.Checked else "0"
                for i in range(self.n_bytes))
            rules.append({"request": req, "mask": bits})
        return rules

    def _show(self, ecu):
        rules = self.data.get(ecu, [])
        need = max([len(r["mask"]) for r in rules] + [MIN_BYTES])
        self.t.setRowCount(0)
        self._set_columns(need)
        for r in rules:
            self.add_row(r["request"], r["mask"])

    def set_ecu(self, ecu: str):
        """Gọi khi đổi comboBoxECUList: lưu bảng hiện tại rồi hiển thị rule của ECU mới."""
        self.data[self.ecu] = self._read_table()
        self.ecu = ecu or ALL
        self.data.setdefault(self.ecu, [])
        self._show(self.ecu)

    def effective_rules(self, ecu: str) -> dict:
        """request -> mask; rule ECU ghi đè rule All."""
        self.data[self.ecu] = self._read_table()
        merged = {r["request"]: r["mask"] for r in self.data.get(ALL, [])}
        if ecu != ALL:
            merged.update({r["request"]: r["mask"] for r in self.data.get(ecu, [])})
        return merged

    # ---------- file ----------
    def to_dict(self) -> dict:
        self.data[self.ecu] = self._read_table()
        return {k: v for k, v in self.data.items() if v or k == ALL}

    def from_dict(self, data: dict):
        self.data = {k: [dict(r) for r in v] for k, v in data.items()}
        self.data.setdefault(ALL, [])
        self.ecu = ALL
        self._show(ALL)

    def save_json(self, path: str):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        if os.path.exists(path):
            os.replace(path, path + ".bak")
        os.replace(tmp, path)

    def load_json(self, path: str):
        with open(path, encoding="utf-8") as f:
            self.from_dict(json.load(f))


# ---------- dùng rule khi nhận response ----------
def format_response(resp: bytes, rules: dict) -> str:
    """resp = 62 F1 88 01 02 03 ... ; rules = effective_rules(ecu).
    Trả về hex gốc + chuỗi ASCII của các byte có mask '1'."""
    hex_txt = resp.hex(" ").upper()
    if len(resp) < 3 or resp[0] != 0x62:
        return hex_txt
    mask = rules.get(f"22{resp[1]:02X}{resp[2]:02X}")
    if not mask:
        return hex_txt
    data = resp[3:]
    text = "".join(
        (chr(b) if 32 <= b < 127 else ".")
        for i, b in enumerate(data) if i < len(mask) and mask[i] == "1")
    return f"{hex_txt}  |  {text}"