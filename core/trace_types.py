"""Kiểu dữ liệu, hằng số cột và hàm phân tích ID dùng chung cho cửa sổ Trace."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

# ---------------- Chỉ số cột ----------------
(COL_TIME, COL_CNT, COL_CHN, COL_ID, COL_FPS,
 COL_NAME, COL_TYPE, COL_DIR, COL_DLC) = range(9)          # 9 cột chung
COL_DATALEN, COL_BRS, COL_ESI = 9, 10, 11                   # CAN FD
COL_PRIO, COL_PGN, COL_SA, COL_DA = 12, 13, 14, 15          # J1939
COL_NODEID, COL_FUNC = 16, 17                               # CANopen
COL_BYTE0 = 18
MAX_BYTES = 64
NCOLS = COL_BYTE0 + MAX_BYTES                               # 82

HEADERS = [
    "Absolute Time", "Counter", "Chn", "Identifier", "FPS", "Message Name",
    "Type", "Dir", "DLC", "Data Len.", "BRS", "ESI",
    "Priority", "PGN", "SA", "DA", "Node ID", "Function",
] + [f"{i:02d}" for i in range(MAX_BYTES)]

COMMON_COLS = tuple(range(9))


# ---------------- Protocol ----------------
class Protocol(Enum):
    CAN = "CAN"
    CAN_FD = "CAN FD"
    J1939 = "J1939"
    CANOPEN = "CANopen"


@dataclass(frozen=True)
class ProtocolSpec:
    extra_cols: tuple[int, ...]
    max_bytes: int
    id_header: str = "Identifier"


PROTOCOL_SPECS: dict[Protocol, ProtocolSpec] = {
    Protocol.CAN:     ProtocolSpec((), 8),
    Protocol.CAN_FD:  ProtocolSpec((COL_DATALEN, COL_BRS, COL_ESI), 64),
    Protocol.J1939:   ProtocolSpec((COL_PRIO, COL_PGN, COL_SA, COL_DA), 8),
    Protocol.CANOPEN: ProtocolSpec((COL_NODEID, COL_FUNC), 8, "COB-ID"),
}


def visible_columns(protocol: Protocol) -> set[int]:
    spec = PROTOCOL_SPECS[protocol]
    return (set(COMMON_COLS) | set(spec.extra_cols)
            | {COL_BYTE0 + i for i in range(spec.max_bytes)})


# ---------------- Frame ----------------
_FD_LENGTHS = (12, 16, 20, 24, 32, 48, 64)      # DLC 9..15


@dataclass(slots=True)
class CanFrame:
    timestamp: float                 # giây, tính từ lúc bắt đầu đo
    channel: int
    can_id: int
    data: bytes = b""
    is_extended: bool = False
    is_fd: bool = False
    brs: bool = False
    esi: bool = False
    is_tx: bool = False

    @property
    def length(self) -> int:
        return len(self.data)

    @property
    def dlc(self) -> int:
        n = len(self.data)
        if n <= 8:
            return n
        for code, size in enumerate(_FD_LENGTHS, start=9):
            if n <= size:
                return code
        return 15

    @property
    def type_label(self) -> str:
        if not self.is_fd:
            return "CAN2.0"
        return "CANFD_BRS" if self.brs else "CANFD"

    @property
    def direction(self) -> str:
        return "Tx" if self.is_tx else "Rx"


def accepts(protocol: Protocol, f: CanFrame) -> bool:
    """Frame nào được đưa vào bảng ở từng protocol."""
    if protocol is Protocol.CAN:
        return not f.is_fd
    if protocol is Protocol.CAN_FD:
        return True
    if protocol is Protocol.J1939:
        return f.is_extended
    return (not f.is_fd) and (not f.is_extended)         # CANopen: COB-ID 11 bit


# ---------------- Định dạng ----------------
def fmt_id(f: CanFrame) -> str:
    return f"{f.can_id:08X}" if f.is_extended else f"{f.can_id:03X}"


def fmt_time(ts: float) -> str:
    ts = max(ts, 0.0)
    h, m, s = int(ts // 3600), int(ts % 3600 // 60), ts % 60
    return f"{h}:{m:02d}:{s:09.6f}" if h else f"{m:02d}:{s:09.6f}"


# ---------------- J1939 / CANopen ----------------
def parse_j1939(can_id: int) -> tuple[int, int, int, int]:
    """Trả về (priority, pgn, sa, da) từ ID 29 bit."""
    prio = (can_id >> 26) & 0x7
    pf = (can_id >> 16) & 0xFF
    ps = (can_id >> 8) & 0xFF
    sa = can_id & 0xFF
    dp_edp = (can_id >> 24) & 0x3
    if pf < 240:                                   # PDU1: PS là địa chỉ đích
        return prio, (dp_edp << 16) | (pf << 8), sa, ps
    return prio, (dp_edp << 16) | (pf << 8) | ps, sa, 0xFF   # PDU2: broadcast


_CANOPEN_FUNC = {
    0x080: "EMCY", 0x180: "PDO", 0x200: "PDO", 0x280: "PDO", 0x300: "PDO",
    0x380: "PDO", 0x400: "PDO", 0x480: "PDO", 0x500: "PDO",
    0x580: "SDO", 0x600: "SDO", 0x700: "HB",
}


def parse_canopen(can_id: int) -> tuple[str, str]:
    """Trả về (function, node_id) từ COB-ID."""
    cob = can_id & 0x7FF
    if cob == 0x000:
        return "NMT", ""
    if cob == 0x080:
        return "SYNC", ""
    if cob == 0x100:
        return "TIME", ""
    return _CANOPEN_FUNC.get(cob & 0x780, ""), str(cob & 0x7F)


# ---------------- Decoder ----------------
@dataclass(slots=True)
class DecodedMessage:
    name: str = ""
    signals: dict[str, str] = field(default_factory=dict)   # tên -> "giá trị đơn vị"


Decoder = Callable[[int, int, bytes], Optional[DecodedMessage]]   # (channel, can_id, data)


def cantools_decoder(db) -> Decoder:
    """Bọc một cantools.database thành callback decoder(can_id, data)."""

    def decode(channel: int, can_id: int, data: bytes) -> Optional[DecodedMessage]:  # channel bị bỏ qua
        try:
            msg = db.get_message_by_frame_id(can_id)
        except KeyError:
            return None
        out = DecodedMessage(name=msg.name)
        try:
            values = msg.decode(bytes(data), decode_choices=True)
        except Exception:
            return out
        units = {s.name: (s.unit or "") for s in msg.signals}
        for name, v in values.items():
            if isinstance(v, float):
                txt = f"{v:.3f}".rstrip("0").rstrip(".") or "0"
            else:
                txt = str(v)
            unit = units.get(name, "")
            out.signals[name] = f"{txt} {unit}".strip() if unit and not isinstance(v, str) else txt
        return out
    return decode