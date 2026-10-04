"""
==============================================================
core/frame_pump.py
RX bridge:  HardwareManager.rx_queue  ->  Trace frames
==============================================================

The bus threads push `core.can_backend.CanFrame` objects (channel = network NAME,
e.g. "BD", timestamp = python-can time) onto the shared rx_queue.
The Trace window wants `core.trace_types.CanFrame` (channel = database channel
NUMBER, e.g. 1, timestamp = seconds since measurement start).

FramePump drains the queue on the GUI thread every few milliseconds, converts
each frame and emits `frame_ready(trace_frame)`.  MainWindow forwards that to
`frame_received`, which every Trace / Graphic window is connected to.

  - runs only while the measurement is running (set_running)
  - keeps draining even when no window is open, so the queue never fills up
  - at most `max_per_tick` frames per tick, so a burst cannot freeze the GUI
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

from core.trace_types import CanFrame as TraceFrame

logger = logging.getLogger(__name__)

EPOCH_THRESHOLD = 1e8      # a timestamp above this is wall-clock time, below it is relative


class FramePump(QObject):
    frame_ready = pyqtSignal(object)          # core.trace_types.CanFrame

    def __init__(self, hw_manager, database, interval_ms: int = 20,
                 max_per_tick: int = 2000, parent=None) -> None:
        super().__init__(parent)
        self._hw = hw_manager
        self._db = database                   # DatabaseManager (.get(name).channel_index)
        self._max = max_per_tick
        self._chan: Dict[str, Optional[int]] = {}
        self._warned: set = set()
        self._t0_epoch = 0.0                  # wall-clock time of Start
        self._t0_rel: Optional[float] = None  # first timestamp, when timestamps are relative
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._tick)

    # ── control ───────────────────────────────────────────────

    @pyqtSlot(bool)
    def set_running(self, running: bool) -> None:
        if running:
            self._t0_epoch = time.time()
            self._t0_rel = None
            self._warned.clear()
            self._chan.clear()
            self._timer.start()
        else:
            self._timer.stop()
            while self._hw.drain_rx_queue(5000):      # discard leftovers: next Start begins clean
                pass

    @pyqtSlot()
    def invalidate(self) -> None:
        """Network list / names changed in the database: forget cached name -> channel."""
        self._chan.clear()

    # ── conversion ────────────────────────────────────────────

    def _channel_of(self, net_name: str) -> Optional[int]:
        if net_name in self._chan:
            return self._chan[net_name]
        net = self._db.get(net_name)
        ch = net.channel_index if net is not None else None
        self._chan[net_name] = ch
        return ch

    def _relative_time(self, ts: float) -> float:
        if not ts:
            return max(time.time() - self._t0_epoch, 0.0)
        if ts > EPOCH_THRESHOLD:
            return max(ts - self._t0_epoch, 0.0)
        if self._t0_rel is None:
            self._t0_rel = ts
        return max(ts - self._t0_rel, 0.0)

    def convert(self, f) -> Optional[TraceFrame]:
        """core CanFrame -> Trace CanFrame (None = drop the frame)."""
        if f.error_flags:
            return None
        ch = self._channel_of(str(f.channel))
        if ch is None:
            if f.channel not in self._warned:
                self._warned.add(f.channel)
                logger.warning("Frames from network '%s' dropped: it has no channel in the database",
                               f.channel)
            return None
        return TraceFrame(
            timestamp=self._relative_time(f.timestamp),
            channel=ch,
            can_id=f.can_id,
            data=bytes(f.data),
            is_extended=f.is_extended,
            is_fd=f.is_fd,
            brs=f.is_bitrate_switch,
            esi=f.is_error_state_indicator,
            is_tx=(f.direction == "Tx"),
        )

    # ── tick ──────────────────────────────────────────────────

    def _tick(self) -> None:
        for f in self._hw.drain_rx_queue(self._max):
            tf = self.convert(f)
            if tf is not None:
                self.frame_ready.emit(tf)