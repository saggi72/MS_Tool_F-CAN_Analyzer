"""
==============================================================
core/hardware.py
Compatibility facade — re-exports từ các module đã tách
==============================================================

hardware.py đã được tách thành các module theo trách nhiệm:

    core/can_backend.py       CanFrame, enums, constants, configs
    core/hw_scanner.py        HwScanner — chỉ scan HW interface
    core/bus_manager.py       BusManager — vòng đời MỘT bus
    core/threads.py           TxWorkerThread, RxReaderThread, QueueFeedRxThread
    core/hardware_manager.py  HardwareManager — điều phối nhiều channel/network

File này giữ nguyên tất cả `from core.hardware import X` cũ
không cần sửa ở bất kỳ file nào khác.

Changelog
─────────
  + APP_NAME              — application-level Vector app identifier
  + NetworkBusState       — per-network connection state enum
  + NetworkBusConfig      — net_name-keyed bus config (preferred over HardwareConfig)
  + HardwareConfigHelper  — HwChannel → NetworkBusConfig / HardwareConfig converter
  + CanFrameType          — frame type enum (CAN_20 / CAN_FD / CAN_FD_BRS)
  + classify_can_frame    — classify a python-can Message → CanFrameType
  + HwScanThread          — background hardware scan thread (moved from ui/)
"""

from __future__ import annotations

# -- Data / enums / constants ------------------------------------------------
from core.can_backend import (
    APP_NAME,
    BusConfig,
    BusState,
    CanFrame,
    CanFrameType,
    ChannelState,
    DEFAULT_FD_DATA_BITRATE,
    EXTENDED_ID_THRESHOLD,
    HW_READ_TIMEOUT_MS,
    HardwareConfig,
    HardwareType,
    HwChannel,
    MAX_RECONNECT,
    NetworkBusConfig,
    NetworkBusState,
    QUEUE_MAXSIZE,
    RECONNECT_DELAY_S,
    TICK_MS,
    UI_REFRESH_MS,
    classify_can_frame,
    get_can,
    is_can_available,
)

# -- Scanner + config helper -------------------------------------------------
from core.hw_scanner import HwScanner, HardwareConfigHelper

# -- Single-bus lifecycle ----------------------------------------------------
from core.bus_manager import BusManager

# -- Threads -----------------------------------------------------------------
from core.threads import (
    HwScanThread,        # background hardware scan
    QueueFeedRxThread,
    RxReaderThread,
    TxWorkerThread,
)

# -- Multi-channel / multi-network coordinator --------------------------------
from core.hardware_manager import HardwareManager

# -- Backward-compat aliases -------------------------------------------------
_QueueFeedRxThread = QueueFeedRxThread
_get_can           = get_can

__all__ = [
    # ── Application identity ─────────────────────────────────
    "APP_NAME",

    # ── Data structures ──────────────────────────────────────
    "CanFrame",
    "HwChannel",
    "BusConfig",
    "HardwareConfig",
    "NetworkBusConfig",

    # ── Enums ────────────────────────────────────────────────
    "BusState",
    "CanFrameType",             # NEW: CAN_20 / CAN_FD / CAN_FD_BRS
    "ChannelState",
    "NetworkBusState",
    "HardwareType",

    # ── Constants ────────────────────────────────────────────
    "TICK_MS",
    "UI_REFRESH_MS",
    "EXTENDED_ID_THRESHOLD",
    "HW_READ_TIMEOUT_MS",
    "RECONNECT_DELAY_S",
    "MAX_RECONNECT",
    "QUEUE_MAXSIZE",
    "DEFAULT_FD_DATA_BITRATE",

    # ── Functions ────────────────────────────────────────────
    "classify_can_frame",       # NEW: classify python-can Message → CanFrameType
    "get_can",
    "is_can_available",

    # ── Classes ──────────────────────────────────────────────
    "HwScanner",
    "HardwareConfigHelper",
    "BusManager",
    "TxWorkerThread",
    "RxReaderThread",
    "QueueFeedRxThread",
    "HwScanThread",      # NEW: background hardware scan (Channel Mapping dialog)
    "HardwareManager",

    # ── Backward-compat aliases ──────────────────────────────
    "_QueueFeedRxThread",
    "_get_can",


]