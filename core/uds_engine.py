"""
==============================================================
core/uds_engine.py
UDS Diagnostic Engine — ISO 14229
==============================================================

Xử lý các UDS service theo ISO 14229-1:
  0x10 DiagnosticSessionControl
  0x11 ECUReset
  0x27 SecurityAccess (Seed/Key)
  0x22 ReadDataByIdentifier (ReadDID)
  0x2E WriteDataByIdentifier
  0x14 ClearDiagnosticInformation
  0x19 ReadDTCInformation
  0x31 RoutineControl
  0x34/36/37 RequestDownload / TransferData / RequestTransferExit

Architecture:
  UdsEngine (QObject, chạy trong UdsWorkerThread)
    → nhận request từ UI qua request_queue
    → gửi CAN frame qua hw_manager.transmit()
    → nhận response từ rx_queue (filtered by UDS IDs)
    → emit response signal → UI

ISO 15765-2 (CAN Transport Layer):
  Single Frame (SF):   0x0N dd dd...      (N bytes, N≤7)
  First Frame (FF):    0x1H HL dd dd...   (HL = length high+low)
  Consecutive Frame:   0x2N dd dd...      (N = sequence 1-F)
  Flow Control (FC):   0x30 FS BS STmin   (sent by receiver)
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional, List, Tuple, Dict, Callable

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from core.hardware import HardwareManager, CanFrame

logger = logging.getLogger(__name__)


# ==============================================================
# UDS CONSTANTS (ISO 14229-1)
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
    DEFAULT    = 0x01
    EXTENDED   = 0x03
    PROGRAMMING = 0x02


class UdsNrc(IntEnum):
    """Negative Response Codes (ISO 14229-1 Table A-1)."""
    GENERAL_REJECT                     = 0x10
    SERVICE_NOT_SUPPORTED              = 0x11
    SUBFUNCTION_NOT_SUPPORTED          = 0x12
    INCORRECT_LENGTH                   = 0x13
    CONDITIONS_NOT_CORRECT             = 0x22
    REQUEST_SEQUENCE_ERROR             = 0x24
    NO_RESPONSE_FROM_SUBNET_COMPONENT  = 0x25
    UPLOAD_DOWNLOAD_NOT_ACCEPTED       = 0x70
    TRANSFER_DATA_SUSPENDED            = 0x71
    GENERAL_PROGRAMMING_FAILURE        = 0x72
    WRONG_BLOCK_SEQUENCE_COUNTER       = 0x73
    REQUEST_CORRECTLY_RECEIVED_RESPONSE_PENDING = 0x78
    SUBFUNCTION_NOT_SUPPORTED_IN_SESSION = 0x7E
    SERVICE_NOT_SUPPORTED_IN_SESSION   = 0x7F
    SECURITY_ACCESS_DENIED             = 0x33
    INVALID_KEY                        = 0x35
    EXCEEDED_NUMBER_OF_ATTEMPTS        = 0x36
    REQUIRED_TIME_DELAY_NOT_EXPIRED    = 0x37

    @classmethod
    def description(cls, code: int) -> str:
        try:
            return cls(code).name.replace("_", " ").title()
        except ValueError:
            return f"Unknown NRC 0x{code:02X}"


# ==============================================================
# ISO 15765-2 TRANSPORT LAYER
# ==============================================================

class IsoTpError(Exception):
    pass


class IsoTpLayer:
    """
    ISO 15765-2 CAN Transport Protocol.

    Segments UDS messages >7 bytes across multiple CAN frames.
    Handles:
      - Single Frame (SF): payload ≤ 7 bytes
      - First Frame (FF) + Consecutive Frames (CF): payload > 7 bytes
      - Flow Control (FC): sent by receiver to pace CF
    """

    SF_MAX_PAYLOAD = 7
    FF_FIRST_BYTES = 6    # First 6 data bytes in FF
    CF_MAX_PAYLOAD = 7
    FC_CONTINUE    = 0x00
    FC_WAIT        = 0x01
    FC_OVERFLOW    = 0x02

    def segment(self, data: bytes) -> List[bytes]:
        """
        Segment payload into ISO-TP CAN frames.

        Returns list of 8-byte CAN frame payloads.
        """
        if len(data) <= self.SF_MAX_PAYLOAD:
            # Single Frame: [0x0N, d0, d1, ..., 0x00 padding]
            frame = bytes([len(data)]) + data
            frame = frame.ljust(8, b"\x00")
            return [frame]

        frames = []
        total  = len(data)

        # First Frame: [0x1H, 0xLL, d0..d5]  (HL = total length 12 bits)
        high    = (total >> 8) & 0x0F
        low     = total & 0xFF
        ff_data = data[:self.FF_FIRST_BYTES]
        ff      = bytes([0x10 | high, low]) + ff_data
        ff      = ff.ljust(8, b"\x00")
        frames.append(ff)

        # Consecutive Frames: [0x2N, d0..d6]
        remaining = data[self.FF_FIRST_BYTES:]
        sn = 1
        while remaining:
            chunk  = remaining[:self.CF_MAX_PAYLOAD]
            remaining = remaining[self.CF_MAX_PAYLOAD:]
            cf = bytes([0x20 | (sn & 0x0F)]) + chunk
            cf = cf.ljust(8, b"\x00")
            frames.append(cf)
            sn = (sn + 1) & 0x0F

        return frames

    def reassemble(self, frames: List[bytes]) -> bytes:
        """
        Reassemble ISO-TP frames into payload bytes.

        Raises IsoTpError on protocol violation.
        """
        if not frames:
            raise IsoTpError("No frames to reassemble")

        first = frames[0]
        frame_type = (first[0] & 0xF0) >> 4

        if frame_type == 0x00:
            # Single Frame
            length = first[0] & 0x0F
            return first[1:1 + length]

        elif frame_type == 0x01:
            # First Frame
            length = ((first[0] & 0x0F) << 8) | first[1]
            payload = bytearray(first[2:8])

            for cf in frames[1:]:
                cf_type = (cf[0] & 0xF0) >> 4
                if cf_type != 0x02:
                    raise IsoTpError(f"Expected CF (0x2x), got {cf[0]:#x}")
                payload.extend(cf[1:8])

            return bytes(payload[:length])

        else:
            raise IsoTpError(f"Unexpected frame type: {frame_type:#x}")


# ==============================================================
# UDS REQUEST / RESPONSE
# ==============================================================

@dataclass
class UdsRequest:
    """Một UDS request từ UI → Engine."""
    service_id:  int
    payload:     bytes
    channel:     int
    tx_id:       int   # CAN ID của request (tester → ECU)
    rx_id:       int   # CAN ID của response (ECU → tester)
    timeout_ms:  int = 1000
    description: str = ""


@dataclass
class UdsResponse:
    """Kết quả UDS response trả về UI."""
    service_id:   int
    success:      bool
    data:         bytes    # Response payload (stripped of service header)
    elapsed_ms:   float
    error_code:   int = 0  # NRC code nếu negative response
    error_msg:    str = ""
    raw_frames:   List[bytes] = field(default_factory=list)


# ==============================================================
# UDS ENGINE (runs in worker thread)
# ==============================================================

class UdsEngine(QObject):
    """
    UDS request-response engine.

    Chạy trong UdsWorkerThread (QObject + moveToThread).
    Nhận request từ request_queue, gửi CAN frame, đợi response.

    Blocking wait pattern:
      1. Gửi request frames qua hw_manager.transmit()
      2. Poll response_event (threading.Event) với timeout
      3. Khi CanFrame với rx_id đến → feed vào reassembler
      4. Parse UDS response → emit signal
    """

    response_received = pyqtSignal(object)   # UdsResponse
    log_message       = pyqtSignal(str)       # Debug log

    def __init__(
        self,
        hw_manager:    HardwareManager,
        request_queue: queue.Queue,
        stop_event:    threading.Event,
    ):
        super().__init__()
        self._hw        = hw_manager
        self._req_queue = request_queue
        self._stop      = stop_event
        self._iso_tp    = IsoTpLayer()

        # Response accumulation: rx_id → accumulated frames
        self._pending_rx_id:    Optional[int] = None
        self._rx_frames:        List[bytes]   = []
        self._rx_total_len:     int  = 0
        self._response_event:   threading.Event = threading.Event()

        # Tester Present timer
        self._tp_interval_ms  = 2000
        self._last_tp_sent    = 0.0
        self._tp_tx_id: Optional[int] = None
        self._tp_channel: int = 1

    @pyqtSlot()
    def run(self) -> None:
        """Main loop: process requests from queue."""
        logger.info("UdsEngine started")

        while not self._stop.is_set():
            try:
                # Get request (timeout allows checking stop_event)
                req: UdsRequest = self._req_queue.get(timeout=0.1)
                self._process_request(req)
            except queue.Empty:
                # Send Tester Present if session active
                self._maybe_send_tester_present()

        logger.info("UdsEngine stopped")

    def feed_frame(self, frame: CanFrame) -> None:
        """
        Called from UI timer when RX frame matches rx_id.
        Thread-safe: called from main thread, sets event.
        """
        if self._pending_rx_id is None:
            return
        if frame.can_id != self._pending_rx_id:
            return

        data = frame.data[:8]
        frame_type = (data[0] & 0xF0) >> 4

        if frame_type == 0x00:
            # Single Frame → complete
            self._rx_frames = [bytes(data)]
            self._response_event.set()

        elif frame_type == 0x01:
            # First Frame → start accumulation
            total = ((data[0] & 0x0F) << 8) | data[1]
            self._rx_total_len = total
            self._rx_frames    = [bytes(data)]
            # Send Flow Control: Continue, BS=0, STmin=0
            self._send_flow_control(frame.channel)

        elif frame_type == 0x02:
            # Consecutive Frame
            self._rx_frames.append(bytes(data))
            received = 6 + (len(self._rx_frames) - 1) * 7
            if received >= self._rx_total_len:
                self._response_event.set()

    def _process_request(self, req: UdsRequest) -> None:
        t_start = time.monotonic()
        self.log_message.emit(
            f"→ {req.description or UdsService(req.service_id).name} "
            f"[{req.payload.hex(' ').upper()}]"
        )

        # Segment request
        frames_data = self._iso_tp.segment(req.payload)

        # Setup response listener
        self._pending_rx_id   = req.rx_id
        self._rx_frames       = []
        self._rx_total_len    = 0
        self._response_event.clear()

        # Transmit request frames
        for frame_data in frames_data:
            can_frame = CanFrame(
                timestamp=time.monotonic(),
                channel=req.channel,
                can_id=req.tx_id,
                dlc=len(frame_data),
                data=frame_data,
            )
            if not self._hw.transmit(can_frame):
                self._emit_error(req, 0, "TX queue full")
                return
            time.sleep(0.0005)  # 0.5ms inter-frame gap

        # Wait for response
        received = self._response_event.wait(timeout=req.timeout_ms / 1000.0)
        elapsed  = (time.monotonic() - t_start) * 1000

        self._pending_rx_id = None

        if not received:
            self._emit_error(req, 0, f"Timeout after {elapsed:.0f}ms")
            return

        # Reassemble
        try:
            payload = self._iso_tp.reassemble(self._rx_frames)
        except IsoTpError as e:
            self._emit_error(req, 0, f"ISO-TP error: {e}")
            return

        self._parse_uds_response(req, payload, elapsed)

    def _parse_uds_response(
        self, req: UdsRequest, payload: bytes, elapsed: float
    ) -> None:
        if len(payload) < 1:
            self._emit_error(req, 0, "Empty response")
            return

        service_byte = payload[0]

        # Negative Response: 0x7F SID NRC
        if service_byte == UdsService.NEGATIVE_RESPONSE:
            nrc = payload[2] if len(payload) >= 3 else 0
            # Handle Response Pending (0x78) — keep waiting
            if nrc == UdsNrc.REQUEST_CORRECTLY_RECEIVED_RESPONSE_PENDING:
                self.log_message.emit(f"⏳ Response pending (NRC 0x78)...")
                # Re-arm wait
                self._pending_rx_id = req.rx_id
                self._rx_frames     = []
                self._response_event.clear()
                received = self._response_event.wait(timeout=5.0)
                if received:
                    try:
                        payload = self._iso_tp.reassemble(self._rx_frames)
                        self._pending_rx_id = None
                        self._parse_uds_response(req, payload, elapsed)
                    except IsoTpError as e:
                        self._emit_error(req, nrc, str(e))
                    return
                else:
                    self._emit_error(req, nrc, "Timeout after pending")
                    return

            desc = UdsNrc.description(nrc)
            self._emit_error(req, nrc, f"Negative Response: {desc} (0x{nrc:02X})")
            return

        # Positive Response: 0x(SID+0x40) ...
        expected_pos = req.service_id + 0x40
        if service_byte != expected_pos:
            self._emit_error(
                req, 0,
                f"Unexpected response: expected 0x{expected_pos:02X}, "
                f"got 0x{service_byte:02X}"
            )
            return

        response = UdsResponse(
            service_id=req.service_id,
            success=True,
            data=payload[1:],   # Strip service byte
            elapsed_ms=elapsed,
            raw_frames=list(self._rx_frames),
        )

        self.log_message.emit(
            f"← OK [{payload[1:].hex(' ').upper()}] "
            f"({elapsed:.1f}ms)"
        )
        self.response_received.emit(response)

    def _emit_error(self, req: UdsRequest, nrc: int, msg: str) -> None:
        resp = UdsResponse(
            service_id=req.service_id,
            success=False,
            data=b"",
            elapsed_ms=0,
            error_code=nrc,
            error_msg=msg,
        )
        self.log_message.emit(f"✕ {msg}")
        self.response_received.emit(resp)

    def _send_flow_control(self, channel: int) -> None:
        """Send ISO-TP Flow Control: Continue."""
        if self._pending_rx_id is None:
            return
        fc = CanFrame(
            timestamp=time.monotonic(),
            channel=channel,
            can_id=self._pending_rx_id - 8,   # Approximate: tester CAN ID
            dlc=8,
            data=b"\x30\x00\x00\x00\x00\x00\x00\x00",
        )
        self._hw.transmit(fc)

    def _maybe_send_tester_present(self) -> None:
        """Send 0x3E 0x80 periodically to keep session alive."""
        if self._tp_tx_id is None:
            return
        now = time.monotonic()
        if (now - self._last_tp_sent) * 1000 >= self._tp_interval_ms:
            frame = CanFrame(
                timestamp=now,
                channel=self._tp_channel,
                can_id=self._tp_tx_id,
                dlc=8,
                data=b"\x02\x3E\x80\x00\x00\x00\x00\x00",
            )
            self._hw.transmit(frame)
            self._last_tp_sent = now

    def set_tester_present(
        self, tx_id: Optional[int], channel: int, interval_ms: int = 2000
    ) -> None:
        """Enable/disable automatic Tester Present."""
        self._tp_tx_id      = tx_id
        self._tp_channel    = channel
        self._tp_interval_ms = interval_ms


# ==============================================================
# UDS SERVICE HELPERS (high-level API)
# ==============================================================

class UdsServiceBuilder:
    """Build UDS request payloads for common services."""

    @staticmethod
    def diagnostic_session(session: UdsSession) -> bytes:
        return bytes([UdsService.DIAGNOSTIC_SESSION_CONTROL, session])

    @staticmethod
    def ecu_reset(reset_type: int = 0x01) -> bytes:
        return bytes([UdsService.ECU_RESET, reset_type])

    @staticmethod
    def security_access_request_seed(level: int) -> bytes:
        """level: 0x01 = Level1, 0x03 = Level2, etc."""
        return bytes([UdsService.SECURITY_ACCESS, level])

    @staticmethod
    def security_access_send_key(level: int, key: bytes) -> bytes:
        """level must be seed_level + 1."""
        return bytes([UdsService.SECURITY_ACCESS, level + 1]) + key

    @staticmethod
    def read_did(did: int) -> bytes:
        return bytes([
            UdsService.READ_DATA_BY_ID,
            (did >> 8) & 0xFF,
            did & 0xFF,
        ])

    @staticmethod
    def write_did(did: int, data: bytes) -> bytes:
        return bytes([
            UdsService.WRITE_DATA_BY_ID,
            (did >> 8) & 0xFF,
            did & 0xFF,
        ]) + data

    @staticmethod
    def clear_dtc(group: int = 0xFFFFFF) -> bytes:
        return bytes([
            UdsService.CLEAR_DTC,
            (group >> 16) & 0xFF,
            (group >> 8)  & 0xFF,
            group & 0xFF,
        ])

    @staticmethod
    def read_dtc_by_status(status_mask: int = 0xFF) -> bytes:
        return bytes([
            UdsService.READ_DTC,
            0x02,            # reportDTCByStatusMask
            status_mask,
        ])

    @staticmethod
    def tester_present(suppress: bool = True) -> bytes:
        sub = 0x80 if suppress else 0x00
        return bytes([UdsService.TESTER_PRESENT, sub])