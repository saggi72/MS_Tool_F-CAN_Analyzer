"""
==============================================================
core/bus_manager.py
Single CAN bus lifecycle manager
==============================================================

Encapsulates ONE python-can bus connection:
  - thread-safe state machine (DISCONNECTED/CONNECTING/CONNECTED/DISCONNECTING)
  - open/close for vector, pcan, slcan, socketcan, virtual
  - Vector-specific setup (xlSetApplConfig, force TX output, diagnostics)
  - direct one-shot send

No Qt widgets. Communicates progress through a log_fn(str) callback.
One BusManager == one bus == one connected channel.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Callable, Optional

from PyQt6.QtCore import QMutex, QMutexLocker

from core.can_backend import (
    BusConfig,
    BusState,
    EXTENDED_ID_THRESHOLD,
    HwChannel,
    get_can,
)

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]


class BusManager:
    """
    Manage the lifecycle of a single python-can Bus.

    Usage:
        mgr = BusManager(log_fn=print)
        ok = mgr.connect(cfg)          # cfg: BusConfig
        ...
        mgr.disconnect()
    """

    def __init__(self, log_fn: Optional[LogFn] = None) -> None:
        self.bus = None
        self.bus_mutex = QMutex()
        self._state = BusState.DISCONNECTED
        self._state_mtx = QMutex()
        self._log = log_fn or logger.info

    # -- State ---------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        with QMutexLocker(self._state_mtx):
            return self._state == BusState.CONNECTED

    @property
    def state(self) -> BusState:
        with QMutexLocker(self._state_mtx):
            return self._state

    def _set_state(self, new: BusState) -> None:
        with QMutexLocker(self._state_mtx):
            old = self._state
            self._state = new
            logger.debug("BusManager state: %s -> %s", old.value, new.value)

    # -- Connect / Disconnect ------------------------------------------------

    def connect(self, cfg: BusConfig) -> bool:
        """Open the bus. Returns True on success."""
        with QMutexLocker(self._state_mtx):
            if self._state != BusState.DISCONNECTED:
                self._log(f"ERROR: cannot connect while state={self._state.value}")
                return False
            self._state = BusState.CONNECTING

        can = get_can()
        if can is None:
            self._log("ERROR: python-can not installed")
            self._set_state(BusState.DISCONNECTED)
            return False

        try:
            bus = self._open_bus(can, cfg)
            if bus is None:
                raise RuntimeError("Bus object is None")
            with QMutexLocker(self.bus_mutex):
                self.bus = bus
            fd_tag = " [CAN FD]" if cfg.is_fd else ""
            self._log(f"Connected: {cfg.hw.label} @ {cfg.bitrate}{fd_tag}")
            self._set_state(BusState.CONNECTED)
            return True
        except Exception as e:
            self._log(f"Connect FAILED: {e}")
            self._safe_shutdown()
            self._set_state(BusState.DISCONNECTED)
            return False

    def disconnect(self) -> None:
        """Close the bus safely."""
        with QMutexLocker(self._state_mtx):
            if self._state == BusState.DISCONNECTED:
                return
            self._state = BusState.DISCONNECTING
        # Give worker threads a moment to observe DISCONNECTING and exit.
        time.sleep(0.05)
        self._safe_shutdown()
        self._set_state(BusState.DISCONNECTED)
        self._log("Disconnected")

    def _safe_shutdown(self) -> None:
        with QMutexLocker(self.bus_mutex):
            if self.bus is not None:
                try:
                    self.bus.shutdown()
                except Exception as e:
                    logger.warning("bus.shutdown() exception: %s", e)
                finally:
                    self.bus = None

    # -- Bus opening ---------------------------------------------------------

    def _open_bus(self, can, cfg: BusConfig):
        hw = cfg.hw
        iface = hw.interface
        ch = hw.channel
        base = {"bitrate": cfg.bitrate}
        if cfg.is_fd:
            base["fd"] = True
            base["data_bitrate"] = cfg.fd_data_bitrate

        if iface == "vector":
            return self._open_vector(can, hw, ch, base, cfg.app_name)
        elif iface == "pcan":
            return can.interface.Bus(interface="pcan", channel=str(ch), **base)
        elif iface in ("slcan", "socketcan", "virtual"):
            return can.interface.Bus(interface=iface, channel=ch, **base)
        else:
            raise ValueError(f"Unknown interface: {iface!r}")

    # -- Vector-specific -----------------------------------------------------

    def _open_vector(self, can, hw: HwChannel, ch, base: dict, app_name: str):
        vector_kwargs = {**base, "receive_own_messages": True}
        if hw.serial is not None:
            vector_kwargs["serial"] = hw.serial

        if app_name:
            self._vector_set_appl_config(app_name, hw, ch)

        bus = None
        if app_name:
            try:
                bus = can.interface.Bus(
                    interface="vector", channel=ch, app_name=app_name, **vector_kwargs,
                )
                self._log(f"Bus opened with app_name='{app_name}'")
            except Exception as e1:
                self._log(f"App '{app_name}' failed ({e1}), trying direct...")
                bus = can.interface.Bus(interface="vector", channel=ch, **vector_kwargs)
                self._log("Bus opened without app_name")
        else:
            bus = can.interface.Bus(interface="vector", channel=ch, **vector_kwargs)
            self._log("Bus opened without app_name")

        if bus:
            self.bus = bus
            self._vector_force_tx_output()
            self._vector_log_channel_info()
        return bus

    def _vector_set_appl_config(self, app_name: str, hw: HwChannel, ch) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            vxlapi = ctypes.windll.LoadLibrary("vxlapi64.dll")

            # ✅ Đảm bảo ch là int thuần, không phải c_long
            ch_int = ch.value if hasattr(ch, "value") else int(ch)

            status = vxlapi.xlSetApplConfig(
                ctypes.c_char_p(app_name.encode()),
                ctypes.c_uint(ch_int),  # app channel slot
                ctypes.c_uint(hw.hw_type),
                ctypes.c_uint(hw.hw_index),
                ctypes.c_uint(hw.hw_channel or ch_int),
                ctypes.c_uint(1),
            )
            if status == 0:
                self._log(f"xlSetApplConfig OK: Ch{ch}")
            else:
                self._log(f"xlSetApplConfig warning (status={status})")
        except Exception as e:
            self._log(f"xlSetApplConfig skipped: {e}")

    def _vector_force_tx_output(self) -> None:
        if sys.platform != "win32" or not self.bus:
            return
        try:
            import ctypes
            vxlapi = ctypes.windll.LoadLibrary("vxlapi64.dll")

            port_raw = self._get_attr("port_handle", "_port_handle", "handle")
            mask_raw = self._get_attr(
                "mask", "_channel_mask", "channel_mask",
                "permission_mask", "_permission_mask",
            )
            if port_raw is None or mask_raw is None:
                self._log("Vector handles not found")
                return

            # ✅ Unwrap ctypes value nếu cần
            port = port_raw.value if hasattr(port_raw, "value") else int(port_raw)
            mask = mask_raw.value if hasattr(mask_raw, "value") else int(mask_raw)

            perm_raw = self._get_attr("permission_mask", "_permission_mask")
            if perm_raw is not None:
                perm = perm_raw.value if hasattr(perm_raw, "value") else int(perm_raw)
                if perm == 0:
                    self._log("WARNING: TX permission NOT granted!")
                    return

            status = vxlapi.xlCanSetChannelOutput(
                ctypes.c_long(port),  # ✅ bây giờ port là int thuần
                ctypes.c_ulonglong(mask),
                ctypes.c_int(1),
            )
            if status == 0:
                self._log("Vector TX output: NORMAL mode")
            else:
                self._log(f"xlCanSetChannelOutput status={status}")
        except Exception as e:
            self._log(f"xlCanSetChannelOutput: {e}")

    def _vector_log_channel_info(self) -> None:
        if not self.bus:
            return
        try:
            parts = []
            for attr, label in [
                ("channel_info", "Info"),
                ("port_handle", "Port"), ("_port_handle", "Port"),
                ("permission_mask", "PermMask"), ("_permission_mask", "PermMask"),
            ]:
                if hasattr(self.bus, attr):
                    val = getattr(self.bus, attr)
                    if isinstance(val, int) and "Mask" in label:
                        parts.append(f"{label}=0x{val:X}")
                    else:
                        parts.append(f"{label}={val}")
            if parts:
                self._log(f"Vector: {', '.join(parts)}")
            perm = self._get_attr("permission_mask", "_permission_mask")
            if perm is not None:
                if perm == 0:
                    self._log("*** WARNING: permission_mask=0 -> NO TX! ***")
                else:
                    self._log(f"TX permission OK (mask=0x{perm:X})")
        except Exception as e:
            self._log(f"Channel info: {e}")

    def _get_attr(self, *names) -> Optional[int]:
        for name in names:
            if hasattr(self.bus, name):
                return getattr(self.bus, name)
        return None

    # -- Direct send ---------------------------------------------------------

    def send_direct(self, entry, is_fd: bool = False) -> bool:
        """Send a single message immediately (from UI thread). True on success."""
        can = get_can()
        if not self.is_connected or self.bus is None or can is None:
            return False
        try:
            use_fd = getattr(entry, "is_fd", False) or is_fd
            msg = can.Message(
                arbitration_id = entry.frame_id,
                data           = bytes(entry.data[:entry.dlc]),
                is_extended_id = entry.frame_id > EXTENDED_ID_THRESHOLD,
                is_fd          = use_fd,
                bitrate_switch = getattr(entry, "brs", False) if use_fd else False,
                dlc            = entry.dlc,
            )
            with QMutexLocker(self.bus_mutex):
                if self.bus is None:
                    return False
                self.bus.send(msg)
            entry.tx_count = getattr(entry, "tx_count", 0) + 1
            return True
        except Exception as e:
            self._log(f"[TX ERR] {getattr(entry, 'name', '?')}: {e}")
            return False
