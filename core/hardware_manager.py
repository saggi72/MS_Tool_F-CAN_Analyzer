"""
==============================================================
core/hardware_manager.py
Multi-channel CAN coordinator — net_name-keyed
==============================================================

Manages N named networks.  Each network that connects successfully
becomes one live bus (one BusManager) with its own QueueFeedRxThread
feeding the shared rx_queue.

  number of active buses == number of networks currently CONNECTED

Design change from v1
─────────────────────
  v1 keyed everything by channel_index (int).
  v2 keys everything by net_name (str), which is the logical network
  identifier defined in AddDbcTab (matches CanNetwork.name).

  The physical channel assignment lives inside NetworkBusConfig.hw_channel,
  so the manager does not care which integer channel a network is on.

Backward-compat shim
────────────────────
  add_channel(config: HardwareConfig)  → internally promoted to net_name
  start_channel(idx) / stop_channel(idx) still work if the net_name
  happens to be str(channel_index), for legacy callers.

Public surface
──────────────
  add_network(cfg: NetworkBusConfig)       register a network (no connect)
  remove_network(net_name)                 unregister + stop if running
  start_network(net_name) -> bool          connect + start RX thread
  stop_network(net_name)                   disconnect + stop RX thread
  start_all() / stop_all()
  transmit(frame) -> bool                  route CanFrame to its bus
  drain_rx_queue(max) -> List[CanFrame]
  get_active_buses() -> Dict[str, BusManager]   {net_name: mgr} CONNECTED only
  get_active_channel_count() -> int
  is_network_active(net_name) -> bool
  get_network_names() -> List[str]

  # Legacy / compat
  add_channel(config: HardwareConfig)      maps to add_network()
  start_channel(idx) / stop_channel(idx)   maps to start/stop_network(str(idx))
  is_channel_active(idx)                   maps to is_network_active(str(idx))
  get_channel_count()                      alias for len(get_network_names())

Signals (own — also forwarded to SignalBus when injected)
─────────────────────────────────────────────────────────
  network_state_changed(str, str)     (net_name, NetworkBusState.value)
  network_error(str, str)             (net_name, error_message)
  network_stats_updated(str, float, int)  (net_name, busload, fps)
  buses_changed(dict)                 {net_name: BusManager} on every change

  # Legacy aliases (still emitted for backward compat with existing UI slots)
  channel_state_changed(int, str)     emitted with hash(net_name) & 0xFFFF
  channel_error(int, str)
  channel_stats_updated(int, float, int)
"""

from __future__ import annotations

import logging
import queue
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QMutexLocker, QObject, pyqtSignal

from core.can_backend import (
    APP_NAME,
    CanFrame,
    ChannelState,
    HardwareConfig,
    HwChannel,
    NetworkBusConfig,
    NetworkBusState,
    get_can,
)
from core.bus_manager import BusManager
from core.threads import QueueFeedRxThread

logger = logging.getLogger(__name__)


class HardwareManager(QObject):
    """Coordinates multiple named CAN networks; one live bus per connected network."""

    # ── New net_name-keyed signals ──────────────────────────────────────────
    network_state_changed  = pyqtSignal(str, str)         # (net_name, state_value)
    network_error          = pyqtSignal(str, str)          # (net_name, message)
    network_stats_updated  = pyqtSignal(str, float, int)   # (net_name, busload, fps)

    # Emits {net_name: BusManager} whenever connected-bus set changes
    buses_changed          = pyqtSignal(dict)

    # ── Legacy channel_index-keyed signals (backward compat) ───────────────
    # UI slots connected to these still work; we emit both sets.
    channel_state_changed  = pyqtSignal(int, str)
    channel_error          = pyqtSignal(int, str)
    channel_stats_updated  = pyqtSignal(int, float, int)

    def __init__(
        self,
        rx_queue:   "queue.Queue",
        signal_bus: Any = None,          # middleware.signals.SignalBus (optional)
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._rx_queue    = rx_queue

        # net_name → NetworkBusConfig
        self._configs:    Dict[str, NetworkBusConfig]  = {}
        # net_name → BusManager
        self._managers:   Dict[str, BusManager]        = {}
        # net_name → QueueFeedRxThread
        self._rx_threads: Dict[str, QueueFeedRxThread] = {}
        # net_name → NetworkBusState
        self._states:     Dict[str, NetworkBusState]   = {}

        if signal_bus is not None:
            self._wire_signal_bus(signal_bus)

    # ── SignalBus forwarding ────────────────────────────────────────────────

    def _wire_signal_bus(self, bus: Any) -> None:
        """Forward HardwareManager signals to the shared SignalBus."""
        try:
            # Prefer new net_name signals if bus has them
            if hasattr(bus, "network_state_changed"):
                self.network_state_changed.connect(bus.network_state_changed)
            if hasattr(bus, "network_error"):
                self.network_error.connect(bus.network_error)
            if hasattr(bus, "network_stats_updated"):
                self.network_stats_updated.connect(bus.network_stats_updated)

            # Legacy signals — always forward for existing UI compatibility
            self.channel_state_changed.connect(bus.channel_state_changed)
            self.channel_error.connect(bus.channel_error)
            self.channel_stats_updated.connect(bus.channel_stats_updated)

            if hasattr(bus, "buses_changed"):
                self.buses_changed.connect(bus.buses_changed)

            logger.debug("HardwareManager wired to SignalBus")
        except Exception as e:
            logger.warning("Could not wire HardwareManager to SignalBus: %s", e)

    # ── Internal helpers ────────────────────────────────────────────────────

    def _net_to_legacy_ch(self, net_name: str) -> int:
        """
        Derive a stable int channel key from net_name for legacy signals.

        Strategy (in order):
          1. If NetworkBusConfig.hw_channel.channel is an int → use it
          2. Fall back to a small hash (keeps value in 0-255 range for readability)
        """
        cfg = self._configs.get(net_name)
        if cfg is not None:
            ch = getattr(cfg.hw_channel, "channel", None)
            if isinstance(ch, int):
                return ch
        return abs(hash(net_name)) % 256

    def _set_network_state(self, net_name: str, state: NetworkBusState) -> None:
        self._states[net_name] = state
        # Emit new signal
        self.network_state_changed.emit(net_name, state.value)
        # Emit legacy signal
        cs = state.to_channel_state()
        if cs is not None:
            self.channel_state_changed.emit(
                self._net_to_legacy_ch(net_name), cs.name
            )

    def _on_log(self, net_name: str, msg: str) -> None:
        logger.info("NET[%s]: %s", net_name, msg)
        upper = msg.upper()
        if "ERROR" in upper or "FAILED" in upper:
            self.network_error.emit(net_name, msg)
            self.channel_error.emit(self._net_to_legacy_ch(net_name), msg)

    # ── Network registration (new API) ──────────────────────────────────────

    def add_network(self, config: NetworkBusConfig) -> None:
        """Register a named network. Replaces any existing entry with same name."""
        nn = config.net_name
        if nn in self._configs:
            self.stop_network(nn)
        self._configs[nn] = config
        self._managers[nn] = BusManager(
            log_fn=lambda msg, _n=nn: self._on_log(_n, msg)
        )
        self._states[nn] = NetworkBusState.IDLE
        logger.debug(
            "Network '%s' registered (iface=%s channel=%s)",
            nn, config.hw_channel.interface, config.hw_channel.channel,
        )

    def remove_network(self, net_name: str) -> None:
        """Unregister + stop a named network."""
        self.stop_network(net_name)
        self._configs.pop(net_name, None)
        self._managers.pop(net_name, None)
        self._states.pop(net_name, None)
        logger.debug("Network '%s' removed", net_name)

    # ── Start / Stop (new API) ──────────────────────────────────────────────

    def start_all(self) -> None:
        for nn in list(self._configs.keys()):
            self.start_network(nn)

    def start_network(self, net_name: str) -> bool:
        """Connect bus + start RX thread for a named network. Returns True on success."""
        config = self._configs.get(net_name)
        mgr    = self._managers.get(net_name)
        if config is None or mgr is None:
            logger.warning("start_network('%s'): not registered", net_name)
            return False

        self._set_network_state(net_name, NetworkBusState.CONNECTING)
        ok = mgr.connect(config.to_bus_config())

        if ok:
            rx = QueueFeedRxThread(
                mgr.bus, mgr.bus_mutex, self._rx_queue, net_name,
            )
            rx.error_occurred.connect(
                lambda m, nn=net_name: (
                    self.network_error.emit(nn, m),
                    self.channel_error.emit(self._net_to_legacy_ch(nn), m),
                )
            )
            self._rx_threads[net_name] = rx
            rx.start()
            self._set_network_state(net_name, NetworkBusState.CONNECTED)
            self._emit_buses_changed()
            logger.info("Network '%s' started (bus connected, RX running)", net_name)
        else:
            self._set_network_state(net_name, NetworkBusState.ERROR)

        return ok

    def stop_network(self, net_name: str, timeout_ms: int = 3000) -> None:
        """Stop RX thread + disconnect bus for a named network."""
        rx = self._rx_threads.pop(net_name, None)
        if rx:
            rx.stop()

        mgr = self._managers.get(net_name)
        was_connected = bool(mgr and mgr.is_connected)
        if was_connected:
            mgr.disconnect()

        self._set_network_state(net_name, NetworkBusState.DISCONNECTED)
        if was_connected:
            self._emit_buses_changed()
        logger.info("Network '%s' stopped", net_name)

    def stop_all(self, timeout_ms: int = 3000) -> None:
        for nn in list(self._configs.keys()):
            self.stop_network(nn, timeout_ms)

    # ── Introspection (new API) ─────────────────────────────────────────────

    def get_active_buses(self) -> Dict[str, BusManager]:
        """{net_name: BusManager} for every CONNECTED network."""
        return {
            nn: mgr
            for nn, mgr in self._managers.items()
            if mgr.is_connected
        }

    def _emit_buses_changed(self) -> None:
        self.buses_changed.emit(self.get_active_buses())

    def get_network_names(self) -> List[str]:
        return list(self._configs.keys())

    def get_network_state(self, net_name: str) -> NetworkBusState:
        return self._states.get(net_name, NetworkBusState.IDLE)

    def is_network_active(self, net_name: str) -> bool:
        mgr = self._managers.get(net_name)
        return mgr is not None and mgr.is_connected

    def get_active_channel_count(self) -> int:
        return sum(1 for m in self._managers.values() if m.is_connected)

    # ── TX ──────────────────────────────────────────────────────────────────

    def transmit(self, frame: "CanFrame") -> bool:
        """
        Route a CanFrame to its network's bus.

        Routing order:
          1. frame.channel matches a net_name exactly
          2. frame.channel (str) matches str(hw_channel.channel) for any network
          3. No match → return False
        """
        mgr = self._managers.get(str(frame.channel))
        if mgr is None:
            # Try matching against the underlying hw channel value
            for nn, cfg in self._configs.items():
                if str(cfg.hw_channel.channel) == str(frame.channel):
                    mgr = self._managers.get(nn)
                    break

        if mgr is None:
            logger.debug("transmit: no network matching channel %r", frame.channel)
            return False

        if not mgr.is_connected:
            return False

        can = get_can()
        if can is None:
            return False

        try:
            msg = can.Message(
                arbitration_id = frame.can_id,
                data           = bytes(frame.data[:frame.dlc]),
                is_extended_id = frame.is_extended,
                is_fd          = frame.is_fd,
                dlc            = frame.dlc,
            )
            with QMutexLocker(mgr.bus_mutex):
                if mgr.bus is None:
                    return False
                mgr.bus.send(msg)
            return True
        except Exception as e:
            logger.error("transmit error channel=%r: %s", frame.channel, e)
            return False

    # ── RX ──────────────────────────────────────────────────────────────────

    def drain_rx_queue(self, max_frames: int = 500) -> List["CanFrame"]:
        """Pull up to max_frames from the shared RX queue (non-blocking)."""
        frames: List[CanFrame] = []
        try:
            for _ in range(max_frames):
                frames.append(self._rx_queue.get_nowait())
        except queue.Empty:
            pass
        return frames

    # ── Legacy / backward-compat API ────────────────────────────────────────
    #
    # These methods translate old channel_index calls to net_name calls.
    # They allow existing callers (main.py, tabs) to keep working unchanged.

    def add_channel(self, config: HardwareConfig) -> None:
        """
        Backward-compat: register a channel by HardwareConfig.

        Derives net_name = str(config.channel_index) and promotes
        HardwareConfig → NetworkBusConfig internally.
        """
        net_name = str(config.channel_index)
        bus_cfg  = config.to_bus_config()
        net_cfg  = NetworkBusConfig(
            net_name        = net_name,
            hw_channel      = bus_cfg.hw,
            bitrate         = config.bitrate,
            is_fd           = config.is_fd,
            fd_data_bitrate = config.fd_bitrate,
            app_name        = config.app_name or APP_NAME,
        )
        self.add_network(net_cfg)

    def remove_channel(self, channel_index: int) -> None:
        """Backward-compat: remove by integer channel index."""
        self.remove_network(str(channel_index))

    def start_channel(self, channel_index: int) -> bool:
        """Backward-compat: start by integer channel index."""
        return self.start_network(str(channel_index))

    def stop_channel(self, channel_index: int, timeout_ms: int = 3000) -> None:
        """Backward-compat: stop by integer channel index."""
        self.stop_network(str(channel_index), timeout_ms)

    def is_channel_active(self, channel_index: int) -> bool:
        """Backward-compat: check activity by integer channel index."""
        return self.is_network_active(str(channel_index))

    def get_channel_count(self) -> int:
        """Backward-compat: total registered network count."""
        return len(self._configs)