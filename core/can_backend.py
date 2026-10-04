"""
==============================================================
core/can_backend.py
python-can backend access + shared data types & constants
==============================================================

This is the lowest layer. It owns:
  - lazy import of python-can (avoids DLL load on startup)
  - the CanFrame data structure shared across the whole app
  - shared enums (BusState, ChannelState, HardwareType, CanFrameType)
  - shared constants (timing, thresholds, queue size)

No Qt widgets. No threads. No bus logic. Pure data + backend access.

Changelog
─────────
  + APP_NAME            — application-level identifier for Vector xlSetApplConfig
  + NetworkBusState     — per-network connection state (replaces bare ChannelState
                          for network-keyed management)
  + NetworkBusConfig    — high-level per-network config that drives BusManager;
                          supersedes HardwareConfig for net_name-keyed workflows
  + CanFrameType        — frame type classification enum (CAN_20 / CAN_FD / CAN_FD_BRS)
  + CanFrame            — extended with is_bitrate_switch, is_error_state_indicator,
                          frame_type fields for mixed-bus identification
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Lazy python-can import (DLL conflicts on Windows if imported eagerly)
# ----------------------------------------------------------------------
_can_module = None
_can_checked = False

_dll_dir_handles: list = []  # giữ handle add_dll_directory() sống



def tool_root() -> Path:
    """Thư mục chứa app: cạnh file exe khi đóng gói, còn lại là thư mục gốc project."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def register_driver_dlls() -> None:
    """Cho Windows tìm vxlapi64.dll / PCANBasic.dll trong <tool>/Lib trước khi python-can nạp."""
    if sys.platform != "win32":
        return
    candidates = [tool_root() / "Lib"]
    meipass = getattr(sys, "_MEIPASS", None)          # thư mục tạm của PyInstaller one-file
    if meipass:
        candidates.append(Path(meipass))

    found_dirs = []
    for d in candidates:
        if not d.is_dir():
            continue
        try:
            _dll_dir_handles.append(os.add_dll_directory(str(d)))
        except (OSError, AttributeError):             # AttributeError: Python < 3.8
            pass
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
        found_dirs.append(d)

    is64 = sys.maxsize > 2 ** 32
    names = ("vxlapi64.dll" if is64 else "vxlapi.dll", "PCANBasic.dll")
    status = ", ".join(
        f"{n}: {'found' if any((d / n).is_file() for d in found_dirs) else 'MISSING'}"
        for n in names)
    logger.info("Driver DLLs (%d-bit Python) in %s -> %s", 64 if is64 else 32,
                ", ".join(map(str, found_dirs)) or "<no Lib folder>", status)


def get_can():
    """Lazy-import python-can. Returns the module or None."""
    global _can_module, _can_checked
    if not _can_checked:
        register_driver_dlls()          # PHẢI chạy trước `import can`
        try:
            import can
            _can_module = can
        except ImportError:
            _can_module = None
        _can_checked = True
    return _can_module


def is_can_available() -> bool:
    return get_can() is not None


# ----------------------------------------------------------------------
# Application identity
# ----------------------------------------------------------------------

#: Default Vector xlSetApplConfig application name.
#: Override per-channel via NetworkBusConfig.app_name or HardwareConfig.app_name.
APP_NAME: str = "MS_Tool"


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------
TICK_MS               = 10
UI_REFRESH_MS         = 200
EXTENDED_ID_THRESHOLD = 0x7FF
HW_READ_TIMEOUT_MS    = 50
RECONNECT_DELAY_S     = 2.0
MAX_RECONNECT         = 5
QUEUE_MAXSIZE         = 500_000
DEFAULT_FD_DATA_BITRATE = 2_000_000


# ----------------------------------------------------------------------
# Enums
# ----------------------------------------------------------------------
class BusState(Enum):
    DISCONNECTED  = "disconnected"
    CONNECTING    = "connecting"
    CONNECTED     = "connected"
    DISCONNECTING = "disconnecting"


class ChannelState(Enum):
    DISCONNECTED = auto()
    CONNECTING   = auto()
    CONNECTED    = auto()
    ERROR        = auto()
    RECONNECTING = auto()


class HardwareType(Enum):
    VECTOR  = auto()
    PEAK    = auto()
    VIRTUAL = auto()


class CanFrameType(Enum):
    """
    Frame type classification for mixed CAN 2.0 / CAN FD buses.

    Determined at RX time from the python-can Message flags:
        CAN_20      — classical CAN 2.0 frame  (is_fd=False)
        CAN_FD      — CAN FD frame, no bitrate switch  (is_fd=True, BRS=False)
        CAN_FD_BRS  — CAN FD frame with bitrate switch  (is_fd=True, BRS=True)

    Usage in downstream consumers (trace, logger, DBC decode):
        if frame.frame_type == CanFrameType.CAN_20:
            ...
        label = frame.frame_type.value   # → "CAN2.0" / "CANFD" / "CANFD_BRS"
    """
    CAN_20     = "CAN2.0"
    CAN_FD     = "CANFD"
    CAN_FD_BRS = "CANFD_BRS"

    @property
    def is_fd(self) -> bool:
        """True for any CAN FD variant."""
        return self in (CanFrameType.CAN_FD, CanFrameType.CAN_FD_BRS)

    @property
    def display_label(self) -> str:
        """Short label suitable for trace table column."""
        return self.value


class NetworkBusState(Enum):
    """
    Per-network connection state for net_name-keyed management.

    Unlike ChannelState (which is indexed by channel_index int),
    NetworkBusState is carried alongside a net_name key so that
    HardwareManager and UI layers can track individual named networks
    independently of their physical channel assignment.

    Mapping to legacy ChannelState:
        IDLE         → (no ChannelState equivalent — pre-registration)
        CONNECTING   → CONNECTING
        CONNECTED    → CONNECTED
        DISCONNECTED → DISCONNECTED
        ERROR        → ERROR
        RECONNECTING → RECONNECTING
    """
    IDLE         = "idle"          # Registered but not yet started
    CONNECTING   = "connecting"
    CONNECTED    = "connected"
    DISCONNECTED = "disconnected"
    ERROR        = "error"
    RECONNECTING = "reconnecting"

    @classmethod
    def from_channel_state(cls, cs: ChannelState) -> "NetworkBusState":
        """Convert legacy ChannelState → NetworkBusState."""
        _map = {
            ChannelState.DISCONNECTED: cls.DISCONNECTED,
            ChannelState.CONNECTING:   cls.CONNECTING,
            ChannelState.CONNECTED:    cls.CONNECTED,
            ChannelState.ERROR:        cls.ERROR,
            ChannelState.RECONNECTING: cls.RECONNECTING,
        }
        return _map.get(cs, cls.IDLE)

    def to_channel_state(self) -> Optional[ChannelState]:
        """Convert back to ChannelState (returns None for IDLE)."""
        _map = {
            NetworkBusState.CONNECTING:   ChannelState.CONNECTING,
            NetworkBusState.CONNECTED:    ChannelState.CONNECTED,
            NetworkBusState.DISCONNECTED: ChannelState.DISCONNECTED,
            NetworkBusState.ERROR:        ChannelState.ERROR,
            NetworkBusState.RECONNECTING: ChannelState.RECONNECTING,
        }
        return _map.get(self)


# ----------------------------------------------------------------------
# Data structures
# ----------------------------------------------------------------------
@dataclass(slots=True)
class CanFrame:
    """
    Immutable CAN / CAN-FD frame.  slots=True keeps it ~96 B/frame.

    Frame type identification (mixed CAN 2.0 + CAN FD bus)
    ────────────────────────────────────────────────────────
    When a controller operates in CAN FD mode, it receives both classical
    CAN 2.0 frames and CAN FD frames on the same bus.  The three fields
    below allow downstream consumers (trace table, logger, DBC decoder)
    to identify each frame unambiguously without re-inspecting raw flags:

        frame_type               — CanFrameType enum (CAN_20 / CAN_FD / CAN_FD_BRS)
        is_bitrate_switch        — True when CAN FD BRS bit is set (data phase at
                                   higher bitrate, e.g. 2 Mbit/s)
        is_error_state_indicator — True when CAN FD ESI bit is set (transmitter
                                   was error-passive at send time)

    Quick reference:
        is_fd=False                          → CAN 2.0  classical frame
        is_fd=True,  is_bitrate_switch=False → CAN FD   (same bitrate throughout)
        is_fd=True,  is_bitrate_switch=True  → CAN FD   with bitrate switch (BRS)

    The frame_type field is the single authoritative label; is_fd,
    is_bitrate_switch, and is_error_state_indicator are kept as separate
    booleans for fast filtering without enum comparison.
    """
    timestamp:   float
    channel:     str
    can_id:      int
    dlc:         int
    data:        bytes
    is_extended: bool         = False
    is_fd:       bool         = False
    is_remote:   bool         = False
    error_flags: int          = 0

    #: "Rx" for received frames (default); "Tx" for frames this tool transmitted.
    direction:   str          = "Rx"

    # ------------------------------------------------------------------
    # CAN FD / mixed-bus identification fields
    # ------------------------------------------------------------------
    #: True when the CAN FD BRS (Bit Rate Switch) bit is set.
    #: Always False for CAN 2.0 frames.
    is_bitrate_switch: bool = False

    #: True when the CAN FD ESI (Error State Indicator) bit is set.
    #: Always False for CAN 2.0 frames.
    is_error_state_indicator: bool = False

    #: Authoritative frame type label derived from is_fd + BRS at RX time.
    #: Default is CAN_20; set by classify_can_frame() in threads.py.
    frame_type: CanFrameType = field(default=CanFrameType.CAN_20)


@dataclass
class HwChannel:
    """One physical CAN channel discovered by HwScanner."""
    label:       str
    interface:   str
    channel:     object
    supports_fd: bool = True
    hw_type:     int  = 0
    hw_index:    int  = 0
    hw_channel:  int  = 0
    serial:      Optional[str] = None
    model:       str  = ""

    def as_dict(self) -> Dict[str, Any]:
        """Backward-compat: some scanners/consumers expect a plain dict."""
        return {
            "label":       self.label,
            "interface":   self.interface,
            "channel":     self.channel,
            "supports_fd": self.supports_fd,
            "hw_type":     self.hw_type,
            "hw_index":    self.hw_index,
            "hw_channel":  self.hw_channel,
            "serial":      self.serial,
            "model":       self.model,
        }


@dataclass
class BusConfig:
    """Everything needed to open one python-can bus."""
    hw:              HwChannel
    bitrate:         int  = 500_000
    is_fd:           bool = False
    fd_data_bitrate: int  = DEFAULT_FD_DATA_BITRATE
    app_name:        str  = ""


@dataclass
class NetworkBusConfig:
    """
    Per-network bus configuration, keyed by net_name.

    This is the preferred replacement for HardwareConfig in workflows
    that manage channels by logical network name rather than integer
    channel_index.  HardwareManager.add_network() accepts this type
    directly.

    Fields
    ──────
    net_name     : logical network identifier (matches CanNetwork.name)
    hw_channel   : the physical HwChannel to bind (from HwScanner)
    bitrate      : nominal CAN bitrate in bit/s  (default 500 kbit/s)
    is_fd        : enable CAN FD mode
    fd_data_bitrate : FD data-phase bitrate (ignored when is_fd=False)
    app_name     : Vector xlSetApplConfig name; defaults to APP_NAME
    extra        : pass-through kwargs forwarded to python-can Bus()

    Usage
    ─────
        cfg = NetworkBusConfig(
            net_name   = "Engine",
            hw_channel = scanned_channels[0],
            bitrate    = 500_000,
        )
        hw_manager.add_network(cfg)
        hw_manager.start_network("Engine")
    """
    net_name:        str
    hw_channel:      HwChannel
    bitrate:         int            = 500_000
    is_fd:           bool           = False
    fd_data_bitrate: int            = DEFAULT_FD_DATA_BITRATE
    app_name:        str            = field(default_factory=lambda: APP_NAME)
    extra:           Dict[str, Any] = field(default_factory=dict)

    def to_bus_config(self) -> BusConfig:
        """Convert to the BusConfig that BusManager.connect() expects."""
        return BusConfig(
            hw              = self.hw_channel,
            bitrate         = self.bitrate,
            is_fd           = self.is_fd,
            fd_data_bitrate = self.fd_data_bitrate,
            app_name        = self.app_name,
        )


@dataclass
class HardwareConfig:
    """
    High-level per-channel config (what the UI / main.py builds).
    Converts to a BusConfig that BusManager understands.

    NOTE: Prefer NetworkBusConfig for new code.  HardwareConfig is kept
    for backward compatibility with channel_index-based callers.
    """
    hw_type:       HardwareType
    channel_index: int            = 0
    bitrate:       int            = 500_000
    fd_bitrate:    int            = DEFAULT_FD_DATA_BITRATE
    app_name:      str            = field(default_factory=lambda: APP_NAME)
    is_fd:         bool           = False
    extra:         Dict[str, Any] = field(default_factory=dict)

    def to_bus_config(self) -> BusConfig:
        iface_map = {
            HardwareType.VECTOR:  "vector",
            HardwareType.PEAK:    "pcan",
            HardwareType.VIRTUAL: "virtual",
        }
        label_map = {
            HardwareType.VECTOR:  f"Vector Ch{self.channel_index + 1}",
            HardwareType.PEAK:    f"PEAK USBBUS{self.channel_index}",
            HardwareType.VIRTUAL: f"Virtual Ch{self.channel_index}",
        }
        hw = HwChannel(
            label     = label_map[self.hw_type],
            interface = iface_map[self.hw_type],
            channel   = self.channel_index,
            model     = self.hw_type.name.capitalize(),
        )
        return BusConfig(
            hw              = hw,
            bitrate         = self.bitrate,
            is_fd           = self.is_fd,
            fd_data_bitrate = self.fd_bitrate,
            app_name        = self.app_name,
        )


# ----------------------------------------------------------------------
# Frame classification helper
# ----------------------------------------------------------------------
def classify_can_frame(msg) -> CanFrameType:
    """
    Classify a python-can Message as CAN 2.0, CAN FD, or CAN FD with BRS.

    This is the single authoritative classification function used by
    QueueFeedRxThread._on_message() to populate CanFrame.frame_type.
    Keeping the logic here (in can_backend) rather than in threads.py
    ensures any other consumer (e.g. a sniffer, replay tool) can reuse
    the same rule without importing threads.

    Rules
    ─────
        msg.is_fd == False                          → CAN_20
        msg.is_fd == True, bitrate_switch == False  → CAN_FD
        msg.is_fd == True, bitrate_switch == True   → CAN_FD_BRS

    python-can attribute name note:
        python-can < 4.0  uses  msg.bitrate_switch
        python-can >= 4.0 uses  msg.bitrate_switch  (unchanged)
        Some older builds may expose is_bitrate_switch — both are checked.
    """
    if not getattr(msg, "is_fd", False):
        return CanFrameType.CAN_20

    brs = (
        getattr(msg, "bitrate_switch", False)
        or getattr(msg, "is_bitrate_switch", False)
    )
    return CanFrameType.CAN_FD_BRS if brs else CanFrameType.CAN_FD