"""
==============================================================
core/uds_engine.py  —  UDS Diagnostic Engine (ISO 14229)
==============================================================

Supports Classic CAN (ISO 15765-2, 8-byte frames) and
CAN FD (ISO 15765-2:2016, up to 64-byte frames).

Pass ``is_fd=True`` to ``UdsEngine`` / ``IsoTpLayer`` to switch
to FD mode.  All ISO-TP framing, padding, loopback filtering,
and CanFrame construction update automatically.

"""

from __future__ import annotations

import logging
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
from enum import IntEnum
from typing import Optional, List, Set

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from core.hardware import HardwareManager, CanFrame, CanFrameType

logger = logging.getLogger(__name__)


# ==============================================================
# UDS CONSTANTS
# ==============================================================

class UdsService(IntEnum):
    DIAGNOSTIC_SESSION_CONTROL = 0x10
    ECU_RESET                  = 0x11
    SECURITY_ACCESS            = 0x27
    READ_DATA_BY_ID            = 0x22
    WRITE_DATA_BY_ID           = 0x2E
    CLEAR_DTC                  = 0x14
    READ_DTC                   = 0x19
    ROUTINE_CONTROL            = 0x31
    REQUEST_DOWNLOAD           = 0x34
    TRANSFER_DATA              = 0x36
    TRANSFER_EXIT              = 0x37
    TESTER_PRESENT             = 0x3E
    NEGATIVE_RESPONSE          = 0x7F


class UdsSession(IntEnum):
    DEFAULT     = 0x01
    PROGRAMMING = 0x02
    EXTENDED    = 0x03


class UdsNrc(IntEnum):
    GENERAL_REJECT                              = 0x10
    SERVICE_NOT_SUPPORTED                       = 0x11
    SUBFUNCTION_NOT_SUPPORTED                   = 0x12
    INCORRECT_LENGTH                            = 0x13
    BUSY_REPEAT_REQUEST                         = 0x21
    CONDITIONS_NOT_CORRECT                      = 0x22
    REQUEST_SEQUENCE_ERROR                      = 0x24
    NO_RESPONSE_FROM_SUBNET_COMPONENT           = 0x25
    UPLOAD_DOWNLOAD_NOT_ACCEPTED                = 0x70
    TRANSFER_DATA_SUSPENDED                     = 0x71
    GENERAL_PROGRAMMING_FAILURE                 = 0x72
    WRONG_BLOCK_SEQUENCE_COUNTER                = 0x73
    REQUEST_CORRECTLY_RECEIVED_RESPONSE_PENDING = 0x78
    SUBFUNCTION_NOT_SUPPORTED_IN_SESSION        = 0x7E
    SERVICE_NOT_SUPPORTED_IN_SESSION            = 0x7F
    SECURITY_ACCESS_DENIED                      = 0x33
    INVALID_KEY                                 = 0x35
    EXCEEDED_NUMBER_OF_ATTEMPTS                 = 0x36
    REQUIRED_TIME_DELAY_NOT_EXPIRED             = 0x37

    @classmethod
    def description(cls, code: int) -> str:
        try:
            return cls(code).name.replace("_", " ").title()
        except ValueError:
            return f"Unknown NRC 0x{code:02X}"


_SERVICES_WITH_SUBFUNC = frozenset({
    UdsService.DIAGNOSTIC_SESSION_CONTROL,
    UdsService.ECU_RESET,
    UdsService.SECURITY_ACCESS,
    UdsService.ROUTINE_CONTROL,
    UdsService.TESTER_PRESENT,
})

_SLOW_SERVICE_MIN_MS = 2000
_SLOW_SESSIONS = frozenset({UdsSession.PROGRAMMING, UdsSession.EXTENDED})

# NRCs where the standard mandates the client must re-send the request.
# 0x21 busyRepeatRequest  — ECU explicitly asks us to retry
# 0x37 requiredTimeDelayNotExpired — SA delay; retry after longer pause
_RETRYABLE_NRCS: frozenset[int] = frozenset({0x21, 0x37})
_NRC_RETRY_DELAY_MS: dict[int, int] = {0x21: 200, 0x37: 500}

# Security Access sub-functions that request a seed (odd numbers)
_SA_SEED_SUBF = frozenset(range(0x01, 0x42, 2))   # 0x01, 0x03, 0x05, ...

# Depth of the UdsEngine's PRIVATE rx buffer (one fan-out subscriber queue on
# the HardwareManager).  Large enough to absorb a multi-frame response plus
# unrelated bus traffic between request start and response, but bounded so it
# can be reset cheaply.  The producer drops the oldest frame on overflow.
_UDS_RX_MAXSIZE = 50_000


# ==============================================================
# CAN FD DLC helpers  (ISO 15765-2:2016 Table 4)
# ==============================================================

# Valid discrete CAN FD frame payload sizes in bytes.
# Sizes above 8 jump in non-contiguous steps.
_FD_VALID_SIZES = (8, 12, 16, 20, 24, 32, 48, 64)


def _fd_pad_size(length: int) -> int:
    """Return the smallest valid CAN FD frame size that can hold *length* bytes."""
    for size in _FD_VALID_SIZES:
        if length <= size:
            return size
    raise ValueError(f"ISO-TP payload too large for CAN FD: {length} bytes (max 64)")


# ==============================================================
# ISO 15765-2
# ==============================================================

class IsoTpError(Exception):
    pass


class IsoTpLayer:
    """
    ISO 15765-2 segmentation and reassembly.

    ``is_fd=False`` (default) — Classic CAN mode: 8-byte frames,
    SF up to 7 bytes, FF+CF for longer payloads.

    ``is_fd=True`` — CAN FD mode (ISO 15765-2:2016):
      • Frames padded to the nearest discrete CAN FD size (8/12/…/64 bytes).
      • SF compat mode if DL ≤ 7 (byte 0 = [0x00 | DL], same as classic).
      • SF escape mode if 7 < DL ≤ 62 (byte 0 = 0x00, byte 1 = DL).
      • FF standard if total ≤ 4095 (2-byte header, data from byte 2).
      • FF escape if total > 4095 (6-byte header with 32-bit DL, data from byte 6).
      • CF carries up to (frame_size - 1) bytes each.
    """

    PAD_BYTE = 0x00

    def __init__(self, is_fd: bool = False) -> None:
        self.is_fd = is_fd

    # ── Internal geometry ──────────────────────────────────────────────

    @property
    def _frame_size(self) -> int:
        """Physical CAN frame payload capacity (bytes)."""
        return 64 if self.is_fd else 8

    @property
    def _cf_data_max(self) -> int:
        """Maximum UDS payload bytes that fit in one Consecutive Frame."""
        return self._frame_size - 1   # 1 byte consumed by PCI (0x2X)

    def _pad(self, data: bytes) -> bytes:
        """Pad *data* to the correct frame size with PAD_BYTE."""
        target = _fd_pad_size(len(data)) if self.is_fd else 8
        return data.ljust(target, bytes([self.PAD_BYTE]))

    # ── Public API ─────────────────────────────────────────────────────

    def segment(self, data: bytes) -> List[bytes]:
        """
        Segment *data* into a list of ISO-TP frame payloads ready for TX.

        Each returned bytes object is already padded to the correct discrete
        frame size.  For single-frame messages the list has exactly one entry.
        """
        return self._segment_fd(data) if self.is_fd else self._segment_classic(data)

    def reassemble(self, frames: List[bytes]) -> bytes:
        """
        Reassemble a list of raw ISO-TP frame payloads into the original UDS
        application payload.  Handles both classic and FD escape encodings.
        """
        if not frames:
            raise IsoTpError("No frames to reassemble")
        first      = frames[0]
        pci        = first[0]
        frame_type = (pci & 0xF0) >> 4

        # ── Single Frame ───────────────────────────────────────────────
        if frame_type == 0x00:
            if pci == 0x00:
                # CAN FD escape SF: byte 0 = 0x00, byte 1 = DL
                if len(first) < 2:
                    raise IsoTpError("FD escape SF too short")
                length = first[1]
                return bytes(first[2:2 + length])
            # Classic / FD-compat SF: byte 0 = [0x0N]
            length = pci & 0x0F
            return bytes(first[1:1 + length])

        # ── First + Consecutive Frames ─────────────────────────────────
        if frame_type == 0x01:
            if pci == 0x10 and len(first) > 1 and first[1] == 0x00:
                # CAN FD escape FF: DL in bytes 2-5 (big-endian 32-bit)
                if len(first) < 6:
                    raise IsoTpError("FD escape FF too short")
                length = (
                    (first[2] << 24) | (first[3] << 16) |
                    (first[4] <<  8) |  first[5]
                )
                payload = bytearray(first[6:])
            else:
                # Classic/standard FF: DL in nibble of byte 0 + byte 1
                length  = ((pci & 0x0F) << 8) | first[1]
                payload = bytearray(first[2:])

            for cf in frames[1:]:
                if (cf[0] & 0xF0) >> 4 != 0x02:
                    raise IsoTpError(f"Expected CF (0x2x), got {cf[0]:#x}")
                payload.extend(cf[1:])   # full CF payload after PCI byte

            return bytes(payload[:length])

        raise IsoTpError(f"Unexpected frame type: {frame_type:#x}")

    # ── Segmentation internals ─────────────────────────────────────────

    def _segment_classic(self, data: bytes) -> List[bytes]:
        """Classic CAN 8-byte segmentation."""
        total = len(data)
        if total <= 7:
            # SF: [0x0N, data...] padded to 8
            return [(bytes([total]) + data).ljust(8, bytes([self.PAD_BYTE]))]

        frames: List[bytes] = []
        # FF: [0x1X, 0xXX, 6 bytes data]
        ff = bytes([0x10 | ((total >> 8) & 0x0F), total & 0xFF]) + data[:6]
        frames.append(ff.ljust(8, bytes([self.PAD_BYTE])))
        remaining = data[6:]
        sn = 1
        while remaining:
            chunk     = remaining[:7]
            remaining = remaining[7:]
            cf = bytes([0x20 | (sn & 0x0F)]) + chunk
            frames.append(cf.ljust(8, bytes([self.PAD_BYTE])))
            sn = (sn + 1) & 0x0F
        return frames

    def _segment_fd(self, data: bytes) -> List[bytes]:
        """CAN FD segmentation per ISO 15765-2:2016."""
        total  = len(data)
        frames: List[bytes] = []

        # ── Single Frame ───────────────────────────────────────────────
        if total <= 7:
            # FD compat SF: same encoding as classic CAN (byte 0 = [0x00|DL])
            return [self._pad(bytes([total]) + data)]

        if total <= 62:
            # FD escape SF: byte 0 = 0x00, byte 1 = DL (8 ≤ DL ≤ 62)
            return [self._pad(bytes([0x00, total]) + data)]

        # ── Multi-Frame ────────────────────────────────────────────────
        if total <= 4095:
            # Standard FF header (2 bytes): data starts at byte 2
            ff_data_cap = self._frame_size - 2          # 62 for 64-byte frame
            ff = bytes([0x10 | ((total >> 8) & 0x0F), total & 0xFF]) + data[:ff_data_cap]
            frames.append(self._pad(ff))
            remaining = data[ff_data_cap:]
        else:
            # Escape FF header (6 bytes): data starts at byte 6
            ff_data_cap = self._frame_size - 6          # 58 for 64-byte frame
            ff = bytes([
                0x10, 0x00,
                (total >> 24) & 0xFF,
                (total >> 16) & 0xFF,
                (total >>  8) & 0xFF,
                 total        & 0xFF,
            ]) + data[:ff_data_cap]
            frames.append(self._pad(ff))
            remaining = data[ff_data_cap:]

        # Consecutive Frames
        sn = 1
        while remaining:
            chunk     = remaining[:self._cf_data_max]
            remaining = remaining[self._cf_data_max:]
            cf = bytes([0x20 | (sn & 0x0F)]) + chunk
            frames.append(self._pad(cf))
            sn = (sn + 1) & 0x0F

        return frames


def _fmt(data: bytes) -> str:
    """Format frame bytes for the console LogBox, one frame per line.

    Classic CAN frames are always ≤ 8 bytes, so this renders the full
    8-byte frame.  CAN FD frames (up to 64 bytes) are rendered in full on
    a single line — the previous ``data[:8]`` cap hid every FD payload byte
    past byte 8, so an FD response only ever showed its first 8 bytes in the
    LogBox.  Showing all bytes is harmless for classic CAN (it never exceeds
    8) and gives the requested "all data in one line per frame" for CAN FD.
    """
    return "[" + " ".join(f"{b:02X}" for b in data) + "]"


class UdsServiceBuilder:
    """
    Convenience factory for common UDS request payloads.
    All methods return a ``bytes`` object ready to pass as ``UdsRequest.payload``.
    """

    @staticmethod
    def read_did(did: int) -> bytes:
        """ReadDataByIdentifier (0x22) — 2-byte DID."""
        return bytes([0x22, (did >> 8) & 0xFF, did & 0xFF])

    @staticmethod
    def write_did(did: int, value: bytes) -> bytes:
        """WriteDataByIdentifier (0x2E) — 2-byte DID + value bytes."""
        return bytes([0x2E, (did >> 8) & 0xFF, did & 0xFF]) + value

    @staticmethod
    def read_dtc(status_mask: int = 0xFF) -> bytes:
        """ReadDTCInformation (0x19 02) — report DTCByStatusMask."""
        return bytes([0x19, 0x02, status_mask & 0xFF])

    @staticmethod
    def ecu_reset(reset_type: int = 0x01) -> bytes:
        """ECUReset (0x11) — default hard reset."""
        return bytes([0x11, reset_type & 0xFF])

    @staticmethod
    def session_control(session: int) -> bytes:
        """DiagnosticSessionControl (0x10)."""
        return bytes([0x10, session & 0xFF])

    @staticmethod
    def tester_present(suppress_response: bool = True) -> bytes:
        """TesterPresent (0x3E)."""
        sub = 0x80 if suppress_response else 0x00
        return bytes([0x3E, sub])


def _is_uds_candidate(data: bytes) -> bool:
    """Return True if *data* could be a UDS ISO-TP response (classic or FD)."""
    if not data:
        return False
    pci        = data[0]
    frame_type = (pci & 0xF0) >> 4

    if frame_type == 0x00:
        if pci == 0x00:
            # CAN FD escape SF: byte 1 holds DL (8-62)
            length = data[1] if len(data) > 1 else 0
            return 8 <= length <= 62
        length = pci & 0x0F
        return 1 <= length <= 7

    if frame_type == 0x01:
        if pci == 0x10 and len(data) > 1 and data[1] == 0x00:
            # CAN FD escape FF (DL > 4095) — at least 6 bytes needed
            return len(data) >= 6
        total = ((pci & 0x0F) << 8) | (data[1] if len(data) > 1 else 0)
        return total > 7

    if frame_type in (0x02, 0x03):
        return True

    return False


# ==============================================================
# UDS REQUEST / RESPONSE
# ==============================================================

@dataclass
class UdsRequest:
    service_id:     int
    payload:        bytes
    channel:        int
    tx_id:          int
    rx_id:          int
    timeout_ms:     int = 1000
    description:    str = ""
    max_retries:    int = 2    # auto-retry on timeout or retryable NRC; 0 = no retry
    retry_delay_ms: int = 200  # ms to wait between attempts
    p2star_ms:      int = 5000 # NRC-0x78 (response-pending) extended deadline
    fc_stmin_ms:    int = 0    # STmin we request in our Flow Control (0 = fastest)
    fc_stmin_byte:  Optional[int] = None   # raw STmin byte for our FC; overrides fc_stmin_ms (0xF1..0xF9 = 0.1..0.9 ms)


@dataclass
class UdsResponse:
    service_id:  int
    success:     bool
    data:        bytes
    elapsed_ms:  float
    error_code:  int        = 0
    error_msg:   str        = ""
    raw_frames:  List[bytes] = field(default_factory=list)


# ==============================================================
# UDS ENGINE
# ==============================================================

class UdsEngine(QObject):
    """
    UDS request-response engine.  Runs in UdsWorkerThread.

    Pass ``is_fd=True`` to enable CAN FD mode (ISO 15765-2:2016).
    All framing, padding, and CanFrame construction adjust automatically.

    Loopback filter architecture (FIX-R1/R3):
    ─────────────────────────────────────────
    Two separate sets, both protected by _tx_lock:

      _tp_frame_bytes : frozenset[bytes]
          A single padded entry — the current Tester Present frame.
          Set by set_tester_present(); never wiped by per-request logic.
          Updated atomically when the TP TX-ID changes.

      _req_tx_set : set[bytes]
          Per-request TX frames (request SFs/FF/CFs + our own FC frames).
          Cleared at the START of each new request, populated during it.

    _is_own_tx() checks both sets under the lock — a frame is "ours"
    if it appears in either set.

    For classic CAN, each key is 8 bytes (padded).
    For CAN FD, each key is the full padded frame (up to 64 bytes).
    Comparisons are always against ``frame.data[:frame.dlc]`` from the
    echo, so the byte-for-byte match remains exact (FIX-R1).

    Early-response buffer (FIX-R2):
    ────────────────────────────────
    _early_response_frame holds at most one CanFrame that arrived on
    rx_id while _wait_for_flow_control() was running but was not an FC.
    _wait_for_response() drains this slot before touching rx_queue.

    Auto Key Injection (NEW-AUTO-KEY):
    ────────────────────────────────────
    When sa_auto_inject=True (default), after a seed response is parsed
    and key bytes are calculated via the 32-bit DLL subprocess, the engine
    enqueues a follow-up UdsRequest (27 XX <key>) into its own _req_queue.
    The key request inherits channel/tx_id/rx_id/timeout_ms from the
    original seed request.  The rx_queue is flushed of stale frames before
    the key request is processed, mirroring the hw_scanner.py behaviour.
    Set sa_auto_inject=False to revert to manual key entry.
    """

    response_received   = pyqtSignal(object)   # UdsResponse
    log_message         = pyqtSignal(str)       # ISO-TP frame logs
    security_key_ready  = pyqtSignal(int, bytes, str)
    # ^ emitted when key is calculated: (seed_subf, key_bytes, suggested_cmd)

    MAX_NRC78       = 20
    FC_WAIT_TIMEOUT = 1.0   # seconds to wait for ECU Flow Control

    def __init__(
        self,
        hw_manager:    HardwareManager,
        request_queue: queue.Queue,
        stop_event:    threading.Event,
        is_fd:         bool = False,
    ):
        super().__init__()
        self._paused = False
        self._hw        = hw_manager
        self._req_queue = request_queue
        self._stop      = stop_event
        self._iso_tp    = IsoTpLayer(is_fd=is_fd)

        # ── Dedicated UDS rx buffer (fan-out subscriber) ───────────────────
        # The engine no longer shares the Trace tab's rx_queue.  Instead it
        # owns a private buffer and registers it with the HardwareManager so
        # every received frame is copied into it.  Benefits:
        #   • Trace and UDS never steal frames from each other.
        #   • The buffer can be fully reset before each request, so a new
        #     request can never reassemble leftover frames from a previous
        #     response (the cause of duplicated / stale replies).
        # Falls back to the legacy shared queue if the manager predates the
        # subscriber API.
        self._owns_rx_queue = False
        add_sub = getattr(self._hw, "add_rx_subscriber", None)
        if callable(add_sub):
            self._rx_queue: Optional[queue.Queue] = queue.Queue(maxsize=_UDS_RX_MAXSIZE)
            add_sub(self._rx_queue)
            self._owns_rx_queue = True
            logger.info("UdsEngine: using private fan-out rx buffer")
        else:
            self._rx_queue = self._resolve_rx_queue()

        # ── FIX-R3: two-tier TX registration ──────────────────────────────
        self._tp_frame_bytes: Set[bytes] = set()   # permanent TP entry
        self._req_tx_set:     Set[bytes] = set()   # per-request frames
        self._tx_lock = threading.Lock()
        # ──────────────────────────────────────────────────────────────────

        # FIX-R2: one-slot buffer for a non-FC response seen during FC wait
        self._early_response_frame: Optional[CanFrame] = None

        # UDS-candidate sniffed IDs for post-timeout hint
        self._sniff_uds_ids: dict = {}

        # P4: flag to pause Tester Present during a request
        self._request_active = False

        # STOP: when set, the in-flight request stops immediately (no more
        # retries) and returns "Aborted by user".  Cleared at the start of the
        # next request.  Pair with the worker draining _req_queue.
        self._abort = threading.Event()

        # Tester Present keepalive
        self._tp_tx_id:       Optional[int] = None
        self._tp_channel:     int   = 0
        self._tp_interval_ms: int   = 2000
        self._last_tp_sent:   float = 0.0

        # P3: SecurityAccess DLL config
        self._sa_dll_path:      str = ""
        self._sa_python32_path: str = ""
        self._sa_script_path:   str = ""
        self._sa_tmp_key_file:  str = ""

        # NEW-AUTO-KEY: flag to enable/disable automatic key injection
        self._sa_auto_inject: bool = True

        # NEW-AUTO-KEY: store the originating seed request so the key
        # injection thread can enqueue a follow-up request using the same
        # channel/tx_id/rx_id/timeout_ms parameters.
        self._pending_seed_req: Optional[UdsRequest] = None
        self._pending_seed_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Configuration API
    # ------------------------------------------------------------------

    def set_security_dll(
        self,
        dll_path:      str,
        python32_path: str = "",
        script_path:   str = "",
        tmp_key_file:  str = "",
    ) -> None:
        """Configure 32-bit Security Access DLL paths."""
        self._sa_dll_path      = dll_path
        self._sa_python32_path = python32_path
        self._sa_script_path   = script_path
        self._sa_tmp_key_file  = tmp_key_file

    def set_auto_inject(self, enabled: bool) -> None:
        """
        NEW-AUTO-KEY: Enable or disable automatic Security Access key
        injection.  When True (default), the engine sends the calculated
        key directly to the ECU after receiving a seed, without requiring
        any action from the UI.  When False, the engine only emits the
        security_key_ready signal and logs the suggested command.
        """
        self._sa_auto_inject = enabled

    def set_fd_mode(self, is_fd: bool) -> None:
        """Switch the ISO-TP layer between Classic CAN and CAN FD at runtime."""
        self._iso_tp.is_fd = is_fd

    @property
    def rx_queue(self) -> Optional[queue.Queue]:
        """The engine's private rx buffer.

        Exposed so other diagnostic consumers that run while the engine is
        paused (e.g. the DTC scan sender) can read from the SAME dedicated
        buffer instead of competing with the Trace tab on the shared queue.
        """
        return self._rx_queue

    # ------------------------------------------------------------------
    # rx_queue resolver
    # ------------------------------------------------------------------

    def _resolve_rx_queue(self) -> Optional[queue.Queue]:
        for name in ("rx_queue", "_rx_queue", "rxqueue", "_rxqueue"):
            q = getattr(self._hw, name, None)
            if isinstance(q, queue.Queue):
                logger.info("UdsEngine: using hw_manager.%s", name)
                return q
        getter = getattr(self._hw, "get_rx_queue", None)
        if callable(getter):
            q = getter()
            if isinstance(q, queue.Queue):
                return q
        queue_like = [a for a in dir(self._hw)
                      if "queue" in a.lower() or "rx" in a.lower()]
        logger.error("UdsEngine: no rx_queue on hw_manager. Candidates: %s", queue_like)
        return None

    # ------------------------------------------------------------------
    # FD CanFrame factory helpers
    # ------------------------------------------------------------------

    def _make_frame(
        self,
        channel:   int,
        can_id:    int,
        data:      bytes,
    ) -> CanFrame:
        """
        Create a TX CanFrame with the correct FD flags for the current mode.

        In FD mode: is_fd=True, is_bitrate_switch=True (BRS), frame_type=CAN_FD_BRS.
        In classic mode: all flags False, frame_type=CAN_20.
        """
        if self._iso_tp.is_fd:
            return CanFrame(
                timestamp         = time.monotonic(),
                channel           = channel,
                can_id            = can_id,
                dlc               = len(data),
                data              = data,
                direction         = "Tx",
                is_fd             = True,
                is_bitrate_switch = True,
                frame_type        = CanFrameType.CAN_FD_BRS,
            )
        return CanFrame(
            timestamp  = time.monotonic(),
            channel    = channel,
            can_id     = can_id,
            dlc        = len(data),
            data       = data,
            direction  = "Tx",
        )

    # ------------------------------------------------------------------
    # FIX-R1 + FIX-R3: centralised TX registration
    # ------------------------------------------------------------------

    def _build_tp_bytes(self) -> bytes:
        """
        Build the Tester Present frame payload (3 bytes, padded).
        Uses IsoTpLayer._pad() so FD mode pads to the correct discrete size.
        For 3 bytes, _fd_pad_size(3) == 8, so TP is always 8 bytes.
        """
        return self._iso_tp._pad(b"\x02\x3E\x80")

    def _register_request_tx(self, frame_data: bytes) -> None:
        """
        Register a per-request TX frame (request SF/FF/CF or our FC).

        FIX-R1: stores the exact padded frame bytes so comparisons against
        incoming echoes are byte-for-byte exact.  Classic CAN keys are 8
        bytes; CAN FD keys are up to 64 bytes (the full padded frame).
        """
        with self._tx_lock:
            self._req_tx_set.add(bytes(frame_data))

    def _is_own_tx(self, frame_data: bytes) -> bool:
        """
        FIX-R3: Check both permanent TP set and per-request set atomically.
        frame_data must be ``frame.data[:frame.dlc]`` — the exact DLC slice
        of the received echo.
        """
        key = bytes(frame_data)
        with self._tx_lock:
            return key in self._tp_frame_bytes or key in self._req_tx_set

    def _transmit(self, can_frame: CanFrame) -> bool:
        """
        Transmit a frame and register it in the per-request TX set so
        its echo is filtered.  Uses frame.dlc so FD frames register all
        padded bytes, not just the first 8.
        """
        self._register_request_tx(bytes(can_frame.data[:can_frame.dlc]))
        return self._hw.transmit(can_frame)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """Pause engine loop — called during DTC scan to avoid rx_queue race."""
        self._paused = True

    def resume(self) -> None:
        """Resume engine loop after DTC scan completes."""
        self._paused = False

    def abort_current(self) -> None:
        """STOP: signal the in-flight request to bail out immediately.

        Safe to call from any thread.  The flag is checked in the retry loop
        and inside _wait_for_response(); it is cleared when the next request
        starts.  The caller should also drain the request queue so no queued
        requests are processed after the abort.
        """
        self._abort.set()

    @pyqtSlot()
    def run(self) -> None:
        if self._rx_queue is None:
            self.log_message.emit(
                "✕ UdsEngine cannot start: hw_manager.rx_queue not found."
            )
            return
        logger.info("UdsEngine started (is_fd=%s)", self._iso_tp.is_fd)
        try:
            while not self._stop.is_set():
                if self._paused:
                    time.sleep(0.05)
                    continue
                try:
                    req: UdsRequest = self._req_queue.get(timeout=0.1)
                    self._process_request(req)
                except queue.Empty:
                    self._maybe_reset_overflow()
                    self._maybe_send_tester_present()
        finally:
            # Detach our private buffer so the HardwareManager stops fanning
            # frames into a queue nobody drains anymore.
            if self._owns_rx_queue:
                rem = getattr(self._hw, "remove_rx_subscriber", None)
                if callable(rem):
                    rem(self._rx_queue)
        logger.info("UdsEngine stopped")

    def _maybe_reset_overflow(self) -> None:
        """Discard accumulated bus traffic from the idle buffer.

        Between requests the engine does not drain its buffer, so on a busy
        bus the fan-out fills it with traffic that is irrelevant to any UDS
        transaction.  When it grows past 50 % of capacity, clear it so the
        next request starts clean and response detection stays fast.  This is
        silent housekeeping — see _reset_rx_buffer() — so it never spams the
        UDS console while the user is merely connected.
        """
        if self._rx_queue is None or self._request_active:
            return
        cap = getattr(self._rx_queue, "maxsize", 0) or 0
        if cap and self._rx_queue.qsize() >= int(cap * 0.5):
            self._reset_rx_buffer(reason="idle buffer > 50%")

    # ------------------------------------------------------------------
    # Stale frame flush
    # ------------------------------------------------------------------

    def _flush_stale_frames(self, rx_id: int) -> None:
        """Reset the private rx buffer before a request.

        Because the engine owns a dedicated fan-out buffer (the Trace tab
        keeps its own copy of every frame), the entire buffer can be safely
        discarded here.  This guarantees a fresh request never reassembles
        leftover frames from a previous response — the root cause of
        duplicated / stale replies.  ``rx_id`` is accepted for call-site
        compatibility but no longer needed: nothing in the buffer is worth
        keeping at the start of a new transaction.
        """
        self._reset_rx_buffer(reason="new request")

    def _reset_rx_buffer(self, reason: str = "") -> None:
        """Fully drain the private rx buffer (non-blocking).

        Internal housekeeping only.  Frames discarded here are unrelated bus
        traffic that piled up while the engine was idle, so this logs at debug
        level and is NEVER emitted to the UDS console — otherwise a busy bus
        would print a "cleared N frames" line every few seconds and on every
        request.
        """
        if self._rx_queue is None:
            return
        n = 0
        try:
            while True:
                self._rx_queue.get_nowait()
                n += 1
        except queue.Empty:
            pass
        if n:
            logger.debug("UDS buffer reset — cleared %d frame(s) (%s)",
                         n, reason or "n/a")

    # ------------------------------------------------------------------
    # Request processing
    # ------------------------------------------------------------------

    def _process_request(self, req: UdsRequest) -> None:
        # P4: block Tester Present during this transaction
        self._request_active = True
        # STOP: a fresh request starts clean — clear any stale abort flag
        self._abort.clear()
        self._sniff_uds_ids.clear()

        # FIX-R2: clear the early-response slot for this transaction
        self._early_response_frame = None

        # FIX-R3: clear ONLY the per-request set; leave _tp_frame_bytes intact
        with self._tx_lock:
            self._req_tx_set.clear()

        self._flush_stale_frames(req.rx_id)

        # NEW-AUTO-KEY: store the originating request so the key-calc
        # thread can reference it when injecting the key follow-up.
        with self._pending_seed_lock:
            if (req.service_id == UdsService.SECURITY_ACCESS
                    and len(req.payload) >= 2
                    and req.payload[1] in _SA_SEED_SUBF):
                self._pending_seed_req = req
            else:
                self._pending_seed_req = None

        raw_frames: List[bytes] = []
        err: Optional[str] = None
        t_start = time.monotonic()

        for attempt in range(1 + req.max_retries):
            # STOP: bail out before (re)sending if the user aborted
            if self._abort.is_set():
                err = "Aborted by user"
                break
            if attempt > 0:
                time.sleep(req.retry_delay_ms / 1000.0)
                self.log_message.emit(
                    f"⚠  Retry {attempt}/{req.max_retries} → re-sending"
                )
                self._flush_stale_frames(req.rx_id)
                with self._tx_lock:
                    self._req_tx_set.clear()
                self._early_response_frame = None

            t_start = time.monotonic()
            frames = self._iso_tp.segment(req.payload)

            if len(frames) == 1:
                frame_data = frames[0]
                can_frame  = self._make_frame(req.channel, req.tx_id, frame_data)
                if not self._transmit(can_frame):
                    self._request_active = False
                    self._emit_error(req, 0, "TX queue full")
                    return
                self.log_message.emit(f"📤 [Tx] Single-Frame  -> {_fmt(frame_data)}")
            else:
                ok = self._send_multiframe(req, frames)
                if not ok:
                    self._request_active = False
                    return

            raw_frames, err = self._wait_for_response(req, t_start)

            if err:
                # Timeout and a genuine SN gap are retryable: re-send the whole
                # request after flushing the queue (the next loop iteration's
                # attempt>0 block re-flushes rx_id).  TX errors etc. are fatal.
                if (err.startswith("Timeout") or err.startswith("SN mismatch")) \
                        and attempt < req.max_retries:
                    continue
                break

            # Check for retryable NRC (busyRepeatRequest / requiredTimeDelayNotExpired)
            try:
                _pl = self._iso_tp.reassemble(raw_frames)
                _nrc = _pl[2] if (len(_pl) >= 3 and _pl[0] == 0x7F) else None
            except IsoTpError:
                _nrc = None

            if _nrc in _RETRYABLE_NRCS and attempt < req.max_retries:
                _delay = _NRC_RETRY_DELAY_MS.get(_nrc, req.retry_delay_ms)
                self.log_message.emit(
                    f"⚠  NRC 0x{_nrc:02X} ({UdsNrc.description(_nrc)}) — "
                    f"retry {attempt + 1}/{req.max_retries} in {_delay} ms"
                )
                time.sleep(_delay / 1000.0)
                err = f"NRC 0x{_nrc:02X} — retrying"   # treat as retry trigger
                continue

            break   # positive response or non-retryable NRC

        if err and self._sniff_uds_ids:
            ids_str = "  |  ".join(
                f"0x{cid:X} ({cnt}×)" for cid, cnt in sorted(self._sniff_uds_ids.items())
            )
            self.log_message.emit(
                f"👁  [Hint] UDS-shaped frames on: {ids_str}\n"
                f"     └─> Update Response ID in config to one of these IDs"
            )

        self._request_active = False

        if err:
            self._emit_error(req, 0, err)
            return

        elapsed = (time.monotonic() - t_start) * 1000

        try:
            payload = self._iso_tp.reassemble(raw_frames)
        except IsoTpError as e:
            self._emit_error(req, 0, f"ISO-TP error: {e}")
            return

        self._parse_uds_response(req, payload, elapsed, raw_frames)

    # ------------------------------------------------------------------
    # P2: Multi-frame TX with FC wait
    # ------------------------------------------------------------------

    def _send_multiframe(self, req: UdsRequest, frames: List[bytes]) -> bool:
        ff_data   = frames[0]
        total     = len(req.payload)
        can_frame = self._make_frame(req.channel, req.tx_id, ff_data)
        if not self._transmit(can_frame):
            self._emit_error(req, 0, "TX queue full (FF)")
            return False
        self.log_message.emit(
            f"📤 [Tx] [FF] First Frame -> {_fmt(ff_data)}  (Total: {total} bytes)"
        )

        self.log_message.emit("     └─> Waiting for ECU Flow Control...")
        fc_frame = self._wait_for_flow_control(req)
        if fc_frame is None:
            if self._early_response_frame is not None:
                self.log_message.emit(
                    "     └─> ECU sent response before FC — treating as single-frame response"
                )
                return True
            self._emit_error(req, 0, "No Flow Control from ECU — multi-frame TX aborted")
            return False

        fs    = fc_frame[0] & 0x0F
        stmin = fc_frame[2] if len(fc_frame) > 2 else 0

        if fs != 0x00:
            self._emit_error(req, 0, f"ECU FC FS={fs:#x} — not ContinueToSend")
            return False

        if stmin <= 0x7F:
            stmin_s = stmin / 1000.0
        elif 0xF1 <= stmin <= 0xF9:
            stmin_s = (stmin - 0xF0) * 0.0001
        else:
            stmin_s = 0.025

        for cf_data in frames[1:]:
            sn        = cf_data[0] & 0x0F
            can_frame = self._make_frame(req.channel, req.tx_id, cf_data)
            if not self._transmit(can_frame):
                self._emit_error(req, 0, f"TX queue full (CF SN:{sn})")
                return False
            self.log_message.emit(f"📤 [Tx] [CF] SN:{sn} -> {_fmt(cf_data)}")
            if stmin_s > 0:
                time.sleep(stmin_s)

        return True

    def _wait_for_flow_control(self, req: UdsRequest) -> Optional[bytes]:
        """
        Wait up to FC_WAIT_TIMEOUT seconds for a Flow Control frame
        from the ECU on rx_id.

        FIX-R2: Non-FC frames stored in _early_response_frame.
        Uses frame.dlc for exact-byte echo filtering (FD-safe).
        """
        deadline = time.monotonic() + self.FC_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            try:
                frame: CanFrame = self._rx_queue.get(timeout=0.02)
            except queue.Empty:
                continue

            data_raw = bytes(frame.data[:frame.dlc])

            if frame.can_id != req.rx_id:
                # Not addressed to us — discard (Trace keeps its own copy).
                continue

            if (data_raw[0] & 0xF0) == 0x30:
                self.log_message.emit(
                    f"📥 [Rx] [FC] Flow Control from ECU -> {_fmt(data_raw)}"
                )
                return data_raw

            self.log_message.emit(
                f"📥 [Rx] Early response (non-FC) on rx_id -> {_fmt(data_raw)}"
            )
            self._early_response_frame = frame
            return None

        return None

    # ------------------------------------------------------------------
    # RX poll loop
    # ------------------------------------------------------------------

    def _wait_for_response(
        self,
        req:     UdsRequest,
        t_start: float,
    ) -> tuple[List[bytes], Optional[str]]:

        effective_ms = req.timeout_ms
        if (req.service_id == UdsService.DIAGNOSTIC_SESSION_CONTROL
                and len(req.payload) >= 2
                and req.payload[1] in _SLOW_SESSIONS):
            effective_ms = max(req.timeout_ms, _SLOW_SERVICE_MIN_MS)
            if effective_ms != req.timeout_ms:
                self.log_message.emit(
                    f"⚠  Session switch — using {effective_ms}ms timeout "
                    f"(P2 was {req.timeout_ms}ms)"
                )

        p2_s     = effective_ms / 1000.0
        p2star_s = (req.p2star_ms / 1000.0) if req.p2star_ms else 5.0
        deadline = time.monotonic() + p2_s

        is_assembling   = False
        expected_length = 0
        buffer          = bytearray()
        next_sn         = 1
        raw_frames:     List[bytes] = []
        nrc78_count     = 0
        sniff_logged:   Set[int]   = set()

        seed_frames: List[CanFrame] = []
        if self._early_response_frame is not None:
            seed_frames.append(self._early_response_frame)
            self._early_response_frame = None

        def _next_frame() -> Optional[CanFrame]:
            if seed_frames:
                return seed_frames.pop(0)
            try:
                return self._rx_queue.get(timeout=0.02)
            except queue.Empty:
                return None

        while time.monotonic() < deadline:
            # STOP: abort the in-flight wait immediately (no retry)
            if self._abort.is_set():
                return [], "Aborted by user"
            frame = _next_frame()
            if frame is None:
                continue

            # Use the full DLC slice so FD frames are handled correctly
            # and echo filtering compares the exact transmitted bytes.
            data_raw  = bytes(frame.data[:frame.dlc])
            frame_hex = _fmt(data_raw)

            if frame.can_id != req.rx_id:
                if self._is_own_tx(data_raw):
                    continue

                if _is_uds_candidate(data_raw):
                    cid = frame.can_id
                    self._sniff_uds_ids[cid] = self._sniff_uds_ids.get(cid, 0) + 1
                    if cid not in sniff_logged:
                        sniff_logged.add(cid)
                        self.log_message.emit(
                            f"👁  [Sniff] ID=0x{cid:X}  {frame_hex}"
                            f"  ← not rx_id (0x{req.rx_id:X})"
                        )

                # Dedicated buffer: the Trace tab already has its own copy of
                # this frame, so simply discard non-rx_id frames.  (The old
                # shared-queue code re-queued them, which made the engine
                # re-examine the same frames in a tight loop.)
                continue

            pci        = data_raw[0]
            frame_type = (pci & 0xF0) >> 4

            # ── Single Frame ───────────────────────────────────────────
            if frame_type == 0x00:
                if pci == 0x00:
                    # CAN FD escape SF: byte 0 = 0x00, byte 1 = DL
                    if len(data_raw) < 2:
                        continue
                    length  = data_raw[1]
                    payload = data_raw[2:2 + length]
                else:
                    # Classic / FD-compat SF
                    length  = pci & 0x0F
                    payload = data_raw[1:1 + length]

                if len(payload) >= 3 and payload[0] == UdsService.NEGATIVE_RESPONSE:
                    nrc = payload[2]
                    if nrc == UdsNrc.REQUEST_CORRECTLY_RECEIVED_RESPONSE_PENDING:
                        nrc78_count += 1
                        self.log_message.emit(
                            f"📥 [Rx] NRC 78 (Response Pending) -> {frame_hex}"
                        )
                        if nrc78_count > self.MAX_NRC78:
                            return [], f"NRC 0x78 repeated >{self.MAX_NRC78}× — ECU stuck"
                        self.log_message.emit(
                            f"     └─> ECU busy — extending to P2* {p2star_s:.1f}s"
                        )
                        deadline = time.monotonic() + p2star_s
                        continue
                    nrc_desc = UdsNrc.description(nrc)
                    self.log_message.emit(
                        f"📥 [Rx] Negative Response "
                        f"(NRC 0x{nrc:02X} {nrc_desc}) -> {frame_hex}"
                    )
                    raw_frames.append(data_raw)
                    return raw_frames, None

                # Stale-frame guard: a Single Frame on rx_id whose service byte
                # is neither the expected positive response (SID + 0x40) nor a
                # negative response (0x7F, already handled above) is a leftover
                # from a previous request (e.g. a 0x50 SessionControl echo that
                # slipped past _flush_stale_frames).  Skip it and keep waiting
                # for the real response instead of returning the wrong SID.
                if (payload
                        and payload[0] != UdsService.NEGATIVE_RESPONSE
                        and payload[0] != (req.service_id + 0x40)):
                    self.log_message.emit(
                        f"⚠  [Rx] Stale SF discarded "
                        f"(SID 0x{payload[0]:02X}, expected 0x{req.service_id + 0x40:02X}) "
                        f"-> {frame_hex}"
                    )
                    continue

                self.log_message.emit(f"📥 [Rx] Single-Frame  -> {frame_hex}")
                raw_frames.append(data_raw)
                return raw_frames, None

            # ── First Frame ────────────────────────────────────────────
            elif frame_type == 0x01:
                if len(data_raw) < 2:
                    continue

                # Determine expected length and where payload data starts
                if pci == 0x10 and data_raw[1] == 0x00:
                    # CAN FD escape FF: DL in bytes 2-5 (32-bit big-endian)
                    if len(data_raw) < 6:
                        continue
                    new_expected = (
                        (data_raw[2] << 24) | (data_raw[3] << 16) |
                        (data_raw[4] <<  8) |  data_raw[5]
                    )
                    ff_payload_start = 6
                else:
                    # Classic / standard FF
                    new_expected     = ((pci & 0x0F) << 8) | data_raw[1]
                    ff_payload_start = 2

                # FIX-FC-RACE guard: if we are already assembling a multi-frame
                # response and receive another FF, it is a hardware-level echo /
                # duplicate of the ECU's First Frame (an ECU never re-sends an FF
                # on its own — once it sends an FF it waits for our FC).
                #
                # DO NOT re-send Flow Control here.  Per ISO 15765-2 the tester
                # sends exactly ONE FC after the FF (BS=0 → "send all remaining,
                # no further FC").  Re-sending a second FC while the ECU is
                # already streaming Consecutive Frames desynchronises the
                # transfer and makes the ECU re-emit CFs from SN:1, which then
                # trips the SN-mismatch abort below.  Just drop the echo.
                if is_assembling and new_expected == expected_length and raw_frames:
                    if data_raw[ff_payload_start:ff_payload_start + 4] == \
                            raw_frames[0][ff_payload_start:ff_payload_start + 4]:
                        self.log_message.emit(
                            f"⚠  [Rx] Duplicate FF discarded (echo slip-through) -> {frame_hex}"
                        )
                        deadline = time.monotonic() + p2_s
                        continue

                expected_length = new_expected
                buffer          = bytearray(data_raw[ff_payload_start:])
                is_assembling   = True
                next_sn         = 1
                raw_frames      = [data_raw]
                self.log_message.emit(
                    f"📥 [Rx] [FF] First Frame -> {frame_hex}  "
                    f"(Total: {expected_length} bytes)"
                )
                self._send_flow_control(req)
                deadline = time.monotonic() + p2_s

            # ── Consecutive Frame ──────────────────────────────────────
            elif frame_type == 0x02:
                if not is_assembling:
                    continue
                sn = pci & 0x0F
                if sn != next_sn:
                    # Duplicate of the last-accepted CF (hardware echo or an ECU
                    # re-emission after an FC slip) — benign, just ignore it and
                    # keep waiting for the next expected SN.  Only a genuine
                    # sequence gap aborts the transfer.
                    if sn == ((next_sn - 1) & 0x0F):
                        self.log_message.emit(
                            f"⚠  [Rx] Duplicate CF SN:{sn} ignored -> {frame_hex}"
                        )
                        deadline = time.monotonic() + p2_s
                        continue
                    return [], f"SN mismatch: expected {next_sn}, got {sn}"
                rem            = expected_length - len(buffer)
                # Payload per CF = DLC - 1 (PCI byte); dynamic for FD
                cf_payload_max = len(data_raw) - 1
                buffer.extend(data_raw[1:1 + min(cf_payload_max, rem)])
                next_sn    = (next_sn + 1) & 0x0F
                raw_frames.append(data_raw)
                deadline   = time.monotonic() + p2_s
                self.log_message.emit(f"📥 [Rx] [CF] SN:{sn} -> {frame_hex}")

                if len(buffer) >= expected_length:
                    raw_hex = " ".join(f"{b:02X}" for b in buffer[:expected_length])
                    self.log_message.emit(
                        f"📥 [Rx] Multi-Frame Complete — {expected_length} bytes"
                    )
                    self.log_message.emit(f"[RAW] {raw_hex}")
                    return raw_frames, None

        return [], f"Timeout after {effective_ms}ms"

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_uds_response(
        self,
        req:        UdsRequest,
        payload:    bytes,
        elapsed:    float,
        raw_frames: List[bytes],
    ) -> None:
        if len(payload) < 1:
            self._emit_error(req, 0, "Empty response")
            return

        service_byte = payload[0]

        if service_byte == UdsService.NEGATIVE_RESPONSE:
            nrc  = payload[2] if len(payload) >= 3 else 0
            self._emit_error(req, nrc,
                             f"Negative Response: {UdsNrc.description(nrc)} (0x{nrc:02X})")
            return

        expected_pos = req.service_id + 0x40
        if service_byte != expected_pos:
            self._emit_error(req, 0,
                             f"Unexpected SID: expected 0x{expected_pos:02X}, "
                             f"got 0x{service_byte:02X}")
            return

        if req.service_id in _SERVICES_WITH_SUBFUNC:
            req_sub  = req.payload[1] & 0x7F if len(req.payload) >= 2 else None
            resp_sub = payload[1]             if len(payload) >= 2      else None
            if req_sub is not None and resp_sub is not None and req_sub != resp_sub:
                self._emit_error(
                    req, 0,
                    f"Sub-function mismatch: sent 0x{req_sub:02X}, "
                    f"got echo 0x{resp_sub:02X} — stale frame discarded"
                )
                return

        # ── P3 + NEW-AUTO-KEY: Security Access seed handling ──────────────
        if (req.service_id == UdsService.SECURITY_ACCESS
                and len(req.payload) >= 2
                and req.payload[1] in _SA_SEED_SUBF
                and len(payload) >= 2):
            sub_func  = req.payload[1]
            seed_data = payload[1:]   # strip 0x67 service byte

            # Emit positive response first so the UI can show the seed data
            self.response_received.emit(UdsResponse(
                service_id = req.service_id,
                success    = True,
                data       = payload[1:],
                elapsed_ms = elapsed,
                raw_frames = raw_frames,
            ))

            # Spawn key-calculation thread — it will also handle auto-inject
            threading.Thread(
                target=self._calculate_security_key,
                args=(sub_func, seed_data, req),
                daemon=True,
            ).start()
            return  # response already emitted above

        # ── All other services ────────────────────────────────────────────
        self.response_received.emit(UdsResponse(
            service_id = req.service_id,
            success    = True,
            data       = payload[1:],
            elapsed_ms = elapsed,
            raw_frames = raw_frames,
        ))

    # ------------------------------------------------------------------
    # P3 + NEW-AUTO-KEY: Security Access key calculation + injection
    # ------------------------------------------------------------------

    def _calculate_security_key(
        self,
        sub_func:  int,
        seed_data: bytes,
        req:       UdsRequest,
    ) -> None:
        """
        Run 32-bit DLL key generator in subprocess, emit signal, then
        — when sa_auto_inject is True — enqueue a follow-up UdsRequest
        (27 <sub_func+1> <key_bytes>) directly into _req_queue so the
        engine loop processes it in the next iteration.

        Thread: daemon thread — does NOT block the engine loop.

        NEW-AUTO-KEY changes vs original:
          • After successful key calculation, if _sa_auto_inject is True,
            a new UdsRequest is enqueued. The engine then sends it,
            receives the 67 XX response and emits response_received.
          • The rx_queue is flushed of stale frames on req.rx_id before
            the injected request is processed (handled inside
            _process_request → _flush_stale_frames on the engine thread).
          • If DLL is not configured, a manual-key hint is logged and
            the function returns without injecting (same as before).
        """
        if not (self._sa_python32_path and self._sa_script_path
                and self._sa_dll_path):
            self.log_message.emit(
                "⚠  SecurityAccess: DLL path not configured — "
                "call engine.set_security_dll() to enable auto-key"
            )
            return

        # seed_data = [sub_func_echo, seed_byte0, seed_byte1, ...]
        if len(seed_data) < 2:
            return
        seed_bytes = seed_data[1:]   # skip sub_func echo byte

        level_names = {0x01: "Aftersales", 0x03: "Manufacturing"}
        level_name  = level_names.get(sub_func, f"Level {sub_func}")
        security_level = sub_func

        seed_hex_log = " ".join(f"{b:02X}" for b in seed_bytes)
        self.log_message.emit(
            f"🔐 [SecAccess] Seed received ({level_name}, "
            f"{len(seed_bytes)} bytes): [{seed_hex_log}]"
        )
        self.log_message.emit("     └─> Calculating key via 32-bit DLL...")

        # Protocol: testDLL.py <dll_path> <seed_hex> <security_level>
        # Output:   OK:<KEY_HEX>  or  ERR:<reason>  (stdout, no temp file)
        seed_hex_arg = bytes(seed_bytes).hex().upper()
        cmd = [
            self._sa_python32_path,
            self._sa_script_path,
            self._sa_dll_path,
            seed_hex_arg,
            str(security_level),
        ]

        try:
            t0     = time.time()
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", timeout=5,
                creationflags=_NO_WINDOW,
            )
            elapsed_ms = (time.time() - t0) * 1000

            key_bytes: Optional[bytes] = None
            stdout_line = result.stdout.strip()
            if stdout_line.startswith("OK:"):
                hex_str = stdout_line[3:]
                if hex_str:
                    key_bytes = bytes.fromhex(hex_str)

            if not key_bytes:
                reason = stdout_line or result.stderr.strip() or f"rc={result.returncode}"
                self.log_message.emit(
                    f"✕ [SecAccess] Key calculation failed ({reason})"
                )
                return

            key_hex   = " ".join(f"{b:02X}" for b in key_bytes)
            send_subf = sub_func + 1   # seed=0x01 → key=0x02
            suggested = f"27 {send_subf:02X} " + " ".join(f"{b:02X}" for b in key_bytes)

            self.log_message.emit(
                f"🔑 [SecAccess] Key ready in {elapsed_ms:.0f}ms: [{key_hex}]"
            )

            # Always emit the signal so the UI can display the key
            self.security_key_ready.emit(sub_func, key_bytes, suggested)

            # ── NEW-AUTO-KEY: inject key automatically ────────────────────
            if self._sa_auto_inject:
                self.log_message.emit(
                    f"⚡ [AutoInject] Injecting key automatically: {suggested}"
                )
                key_payload = bytes([UdsService.SECURITY_ACCESS, send_subf]) + key_bytes
                inject_req  = UdsRequest(
                    service_id  = UdsService.SECURITY_ACCESS,
                    payload     = key_payload,
                    channel     = req.channel,
                    tx_id       = req.tx_id,
                    rx_id       = req.rx_id,
                    timeout_ms  = req.timeout_ms,
                    description = f"[AutoInject] SA Key Level {send_subf:#04x}",
                )
                try:
                    self._req_queue.put(inject_req, timeout=1.0)
                except queue.Full:
                    self.log_message.emit(
                        "⚠  [AutoInject] Request queue full — key NOT injected. "
                        f"Send manually: {suggested}"
                    )
            else:
                # Legacy mode: hint only
                self.log_message.emit(
                    f"     └─> Send key manually:  {suggested}"
                )
            # ──────────────────────────────────────────────────────────────

        except subprocess.TimeoutExpired:
            self.log_message.emit("✕ [SecAccess] Key calculation timed out (>5s)")
        except Exception as e:
            self.log_message.emit(f"✕ [SecAccess] Error: {e}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit_error(self, req: UdsRequest, nrc: int, msg: str) -> None:
        self.response_received.emit(UdsResponse(
            service_id = req.service_id,
            success    = False,
            data       = b"",
            elapsed_ms = 0.0,
            error_code = nrc,
            error_msg  = msg,
        ))

    def _send_flow_control(self, req: UdsRequest) -> None:
        """
        Send FC during RX multi-frame assembly.

        FIX-FC-RACE: register the FC frame bytes BEFORE calling transmit().
        The previous order (transmit → register) had a race window: the TX
        echo could arrive and be processed by _wait_for_response() before
        _register_request_tx() ran, causing the echo to be misidentified as
        a second FF from the ECU and triggering a duplicate FC send.

        Correct order: register → transmit → log.

        In CAN FD mode the FC is still 3-byte content padded to the nearest
        valid discrete size (which for 3 bytes is always 8, same as classic).
        The CanFrame is tagged with FD flags when in FD mode so the hardware
        transmits it on the FD channel at the correct bitrate.
        """
        # STmin byte: 0..127 → that many ms; clamp out-of-range to 0 (fastest).
        # STmin byte: raw fc_stmin_byte if given (0xF1..0xF9 = 0.1..0.9 ms), else fc_stmin_ms.
        if getattr(req, "fc_stmin_byte", None) is not None:
            stmin_byte = int(req.fc_stmin_byte) & 0xFF          # raw byte, e.g. 0xF2 = 0.2 ms
        else:
            stmin_ms = int(round(getattr(req, "fc_stmin_ms", 0) or 0))
            stmin_byte = stmin_ms if 0 <= stmin_ms <= 0x7F else 0x00
        fc_data = self._iso_tp._pad(bytes([0x30, 0x00, stmin_byte]))
        # Register FIRST so any incoming echo is immediately filtered
        self._register_request_tx(fc_data)
        self._hw.transmit(self._make_frame(req.channel, req.tx_id, fc_data))
        self.log_message.emit(f"📤 [Tx] [FC] Flow Control -> {_fmt(fc_data)}")

    def _maybe_send_tester_present(self) -> None:
        """P4: Only send TP when no request is being processed."""
        if self._tp_tx_id is None:
            return
        if self._request_active:
            return
        now = time.monotonic()
        if (now - self._last_tp_sent) * 1000 >= self._tp_interval_ms:
            tp_data = self._build_tp_bytes()
            self._hw.transmit(self._make_frame(self._tp_channel, self._tp_tx_id, tp_data))
            self._last_tp_sent = now

    def set_tester_present(
        self,
        tx_id:       Optional[int],
        channel:     int,
        interval_ms: int = 2000,
    ) -> None:
        """
        Enable or disable the Tester Present keepalive.

        FIX-R3: _tp_frame_bytes updated atomically under _tx_lock.
        The stored bytes are the full padded TP frame so _is_own_tx()
        comparisons remain exact in both classic and FD mode.
        """
        self._tp_tx_id       = tx_id
        self._tp_channel     = channel
        self._tp_interval_ms = interval_ms

        with self._tx_lock:
            if tx_id is None:
                self._tp_frame_bytes.clear()
            else:
                self._tp_frame_bytes.clear()
                self._tp_frame_bytes.add(self._build_tp_bytes())

    @staticmethod
    def _service_name(sid: int) -> str:
        try:
            return UdsService(sid).name
        except ValueError:
            return f"SID_0x{sid:02X}"
