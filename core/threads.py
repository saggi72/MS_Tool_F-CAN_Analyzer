"""
==============================================================
core/threads.py
CAN worker threads — TX engine, RX reader, queue-feed RX
==============================================================

  TxWorkerThread      — cyclic-send tick engine (one per active bus)
  RxReaderThread      — continuous recv loop (one per active bus)
  QueueFeedRxThread   — RxReaderThread variant that converts each
                        message to a CanFrame and pushes it onto a
                        shared queue, tagged with its channel index.

No Qt widgets. Threads operate on a bus + its bus_mutex (shared with
the owning BusManager).

Changelog
─────────
  + QueueFeedRxThread._on_message  — populate is_bitrate_switch,
                                     is_error_state_indicator, frame_type
                                     using classify_can_frame() from can_backend.
    This allows correct identification of CAN 2.0 vs CAN FD frames
    on a mixed bus when the controller runs in CAN FD mode.
"""

from __future__ import annotations

import logging
import queue
import time
from typing import Any

from PyQt6.QtCore import QMutex, QMutexLocker, QThread, pyqtSignal

from core.can_backend import (
    CanFrame,
    CanFrameType,
    classify_can_frame,
    EXTENDED_ID_THRESHOLD,
    HW_READ_TIMEOUT_MS,
    TICK_MS,
    UI_REFRESH_MS,
    get_can,
)

logger = logging.getLogger(__name__)


# ======================================================================
# TX WORKER
# ======================================================================
class TxWorkerThread(QThread):
    """
    Tick engine that sends cyclic CAN messages from a tx_queue.

    Signals:
        stats_updated(int, int):  (tx_batch, err_batch)
        error_occurred(str)
        message_sent(int, bytes): (frame_id, data)
    """

    stats_updated  = pyqtSignal(int, int)
    error_occurred = pyqtSignal(str)
    message_sent   = pyqtSignal(int, bytes)

    def __init__(self, bus, bus_mutex: QMutex, tx_queue, queue_mutex: QMutex,
                 is_fd: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._bus         = bus
        self._bus_mutex   = bus_mutex
        self._tx_queue    = tx_queue
        self._queue_mutex = queue_mutex
        self._global_fd   = is_fd
        self._running     = False
        self._tx_batch    = 0
        self._err_batch   = 0

    def run(self) -> None:
        self._running = True
        tick_s = TICK_MS / 1000.0
        stats_interval = UI_REFRESH_MS / 1000.0
        last_stats = time.perf_counter()
        logger.debug("TxWorkerThread started")

        while self._running:
            t0 = time.perf_counter()
            if self._bus is None:
                self._running = False
                break

            with QMutexLocker(self._queue_mutex):
                entries = list(self._tx_queue.values())

            for entry in entries:
                if not self._running:
                    break
                if not getattr(entry, "active", True):
                    continue
                if entry.should_send(TICK_MS):
                    entry.tick_generators()
                    self._send_one(entry)

            now = time.perf_counter()
            if now - last_stats >= stats_interval:
                if self._tx_batch or self._err_batch:
                    self.stats_updated.emit(self._tx_batch, self._err_batch)
                    self._tx_batch = self._err_batch = 0
                last_stats = now

            remaining = tick_s - (time.perf_counter() - t0)
            if remaining > 0.001:
                time.sleep(remaining)

        logger.debug("TxWorkerThread stopped")

    def _send_one(self, entry) -> None:
        try:
            if self._bus is None:
                return
            can = get_can()
            if can is None:
                return
            use_fd = getattr(entry, "is_fd", False) or self._global_fd
            msg = can.Message(
                arbitration_id = entry.frame_id,
                data           = bytes(entry.data[:entry.dlc]),
                is_extended_id = entry.frame_id > EXTENDED_ID_THRESHOLD,
                is_fd          = use_fd,
                bitrate_switch = getattr(entry, "brs", False) if use_fd else False,
                dlc            = entry.dlc,
            )
            with QMutexLocker(self._bus_mutex):
                if self._bus is None:
                    return
                self._bus.send(msg)
            entry.tx_count = getattr(entry, "tx_count", 0) + 1
            self._tx_batch += 1
            self.message_sent.emit(entry.frame_id, bytes(entry.data[:entry.dlc]))
        except Exception as e:
            self._err_batch += 1
            self.error_occurred.emit(f"{getattr(entry, 'name', '?')}: {e}")

    def stop(self) -> None:
        self._running = False
        if not self.wait(3000):
            logger.warning("TxWorkerThread did not finish in 3s, terminating")
            self.terminate()
            self.wait(1000)


# ======================================================================
# RX READER
# ======================================================================
class RxReaderThread(QThread):
    """
    Continuous recv loop. Emits the raw python-can Message via
    message_received(object). Does NOT auto-start; call start().

    Signals:
        message_received(object)
        error_occurred(str)
    """

    message_received = pyqtSignal(object)
    error_occurred   = pyqtSignal(str)

    def __init__(self, bus, bus_mutex: QMutex, parent=None) -> None:
        super().__init__(parent)
        self._bus = bus
        self._mutex = bus_mutex
        self._running = False

    def run(self) -> None:
        self._running = True
        logger.debug("RxReaderThread started")
        while self._running:
            try:
                if self._bus is None:
                    self._running = False
                    break
                with QMutexLocker(self._mutex):
                    msg = self._bus.recv(timeout=HW_READ_TIMEOUT_MS / 1000.0)
                if msg is not None:
                    self.message_received.emit(msg)
            except Exception as e:
                if self._running:
                    self.error_occurred.emit(str(e))
                time.sleep(0.1)
        logger.debug("RxReaderThread stopped")

    def stop(self) -> None:
        self._running = False
        if not self.wait(3000):
            logger.warning("RxReaderThread did not finish in 3s, terminating")
            self.terminate()
            self.wait(1000)

    def set_bus(self, bus) -> None:
        with QMutexLocker(self._mutex):
            self._bus = bus


# ======================================================================
# QUEUE-FEED RX  (one per active channel; pushes CanFrame to a shared queue)
# ======================================================================
class QueueFeedRxThread(RxReaderThread):
    """
    RX reader that converts each message into a CanFrame tagged with its
    channel index and pushes it onto a shared queue.  Multiple instances
    (one per connected bus) feed the SAME queue, giving multi-channel
    trace from a single consumer.

    Mixed CAN 2.0 / CAN FD identification
    ──────────────────────────────────────
    When the controller runs in CAN FD mode it receives both classical
    CAN 2.0 frames (FDF=0) and CAN FD frames (FDF=1) on the same bus.
    _on_message() extracts the python-can flags and delegates to
    classify_can_frame() (defined in can_backend) to set:

        frame.frame_type               — CanFrameType.CAN_20 / CAN_FD / CAN_FD_BRS
        frame.is_bitrate_switch        — True if BRS bit was set
        frame.is_error_state_indicator — True if ESI bit was set

    python-can attribute name mapping
    ─────────────────────────────────
    python-can exposes these attributes on can.Message after recv():

        msg.is_fd                   — True for any CAN FD frame
        msg.bitrate_switch          — True when BRS bit set   (python-can naming)
        msg.error_state_indicator   — True when ESI bit set   (python-can naming)

    Some older / patched builds may use is_bitrate_switch / is_error_state_indicator.
    Both names are checked via getattr with fallback to False so the code
    is robust across python-can versions.
    """

    def __init__(self, bus, bus_mutex: QMutex, rx_queue: "queue.Queue",
                 channel_index: Any, parent=None) -> None:
        super().__init__(bus, bus_mutex, parent)
        self._rx_queue = rx_queue
        self._channel_index = channel_index
        self.message_received.connect(self._on_message)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_brs(msg) -> bool:
        """
        Extract the BRS (Bit Rate Switch) flag from a python-can Message.

        python-can uses 'bitrate_switch' as the attribute name.
        Older builds may use 'is_bitrate_switch'.  Falls back to False.
        """
        return bool(
            getattr(msg, "bitrate_switch", False)
            or getattr(msg, "is_bitrate_switch", False)
        )

    @staticmethod
    def _extract_esi(msg) -> bool:
        """
        Extract the ESI (Error State Indicator) flag from a python-can Message.

        python-can uses 'error_state_indicator' as the attribute name.
        Older builds may use 'is_error_state_indicator'.  Falls back to False.
        """
        return bool(
            getattr(msg, "error_state_indicator", False)
            or getattr(msg, "is_error_state_indicator", False)
        )

    # ------------------------------------------------------------------
    # Message handler
    # ------------------------------------------------------------------

    def _on_message(self, msg) -> None:
        """
        Convert a raw python-can Message to a CanFrame and push to the queue.

        Frame type identification strategy
        ───────────────────────────────────
        1. classify_can_frame(msg) → CanFrameType  (from can_backend)
           reads msg.is_fd + BRS flag → CAN_20 / CAN_FD / CAN_FD_BRS
        2. _extract_brs(msg)  → bool  (BRS flag, version-safe)
        3. _extract_esi(msg)  → bool  (ESI flag, version-safe)

        All three results are stored on the CanFrame so downstream
        consumers (trace table, logger, DBC decoder) have both the
        compact enum label and the raw boolean flags available.
        """
        # --- classify frame type (CAN 2.0 vs CAN FD vs CAN FD BRS) ---
        frame_type = classify_can_frame(msg)

        # --- extract CAN FD flags (False for CAN 2.0 frames) ----------
        brs = self._extract_brs(msg)
        esi = self._extract_esi(msg)

        # --- build CanFrame -------------------------------------------
        frame = CanFrame(
            timestamp                = msg.timestamp if msg.timestamp else time.monotonic(),
            channel                  = str(self._channel_index),
            can_id                   = msg.arbitration_id,
            dlc                      = msg.dlc,
            data                     = bytes(msg.data),
            is_extended              = msg.is_extended_id,
            is_fd                    = getattr(msg, "is_fd", False),
            is_remote                = getattr(msg, "is_remote_frame", False),
            # CAN FD / mixed-bus identification
            is_bitrate_switch        = brs,
            is_error_state_indicator = esi,
            frame_type               = frame_type,
        )

        # --- push to shared queue (drop oldest on overflow) -----------
        try:
            self._rx_queue.put_nowait(frame)
        except queue.Full:
            # Drop the oldest frame to make room; keep newest.
            try:
                self._rx_queue.get_nowait()
                self._rx_queue.put_nowait(frame)
            except (queue.Empty, queue.Full):
                pass