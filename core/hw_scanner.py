"""
==============================================================
core/hw_scanner.py
Hardware interface scanner — discovery ONLY
==============================================================

Responsibility: find available CAN interfaces and return HwChannel
objects.  Nothing here connects, opens, or sends on a bus.

Backends: Vector, PEAK, CAN-Analyzer (slcan/serial), SocketCAN, Virtual.

Changelog
─────────
  + HardwareConfigHelper  — converts a scanned HwChannel into a
                            NetworkBusConfig (preferred) or the legacy
                            HardwareConfig, ready for HardwareManager.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import List, Optional

from core.can_backend import (
    APP_NAME,
    DEFAULT_FD_DATA_BITRATE,
    HardwareConfig,
    HardwareType,
    HwChannel,
    NetworkBusConfig,
    get_can,
)

logger = logging.getLogger(__name__)


# ==============================================================
# HW SCANNER
# ==============================================================

class HwScanner:
    """Synchronous scan of available CAN hardware channels."""

    BACKENDS = ["VECTOR", "PEAK", "CAN_ANALYZER", "SOCKETCAN", "VIRTUAL"]

    @staticmethod
    def scan(backends: Optional[List[str]] = None) -> List[HwChannel]:
        if backends is None:
            backends = HwScanner.BACKENDS
        channels: List[HwChannel] = []
        if "VECTOR" in backends:
            channels.extend(HwScanner._scan_vector())
        if "PEAK" in backends:
            channels.extend(HwScanner._scan_peak())
        if "CAN_ANALYZER" in backends:
            channels.extend(HwScanner._scan_can_analyzer())
        if "SOCKETCAN" in backends and sys.platform != "win32":
            channels.extend(HwScanner._scan_socketcan())
        if "VIRTUAL" in backends:
            channels.extend(HwScanner._scan_virtual())
        return channels

    # -- Vector --------------------------------------------------------------
    @staticmethod
    def _scan_vector() -> List[HwChannel]:
        can = get_can()
        if can is None:
            return []
        results: List[HwChannel] = []
        try:
            XL = None
            try:
                from can.interfaces.vector.xldefine import XL_HardwareType as XL
            except ImportError:
                pass
            for hw in can.detect_available_configs(interfaces=["vector"]):
                ch     = int(hw.get("channel", 0))
                ht     = hw.get("hw_type", 0)
                serial = hw.get("serial", None)
                model  = str(ht)
                if XL:
                    try:
                        model = XL(ht).name.replace("XL_HWTYPE_", "")
                    except (ValueError, AttributeError):
                        pass
                results.append(HwChannel(
                    label       = f"Vector {model} Ch{ch + 1}",
                    interface   = "vector",
                    channel     = ch,
                    supports_fd = True,
                    hw_type     = ht,
                    hw_index    = hw.get("hw_index", 0),
                    hw_channel  = hw.get("hw_channel", ch),
                    serial      = serial,
                    model       = model,
                ))
        except Exception as e:
            logger.debug("Vector scan skipped: %s", e)
        return results

    # -- PEAK ----------------------------------------------------------------
    @staticmethod
    def _scan_peak() -> List[HwChannel]:
        can = get_can()
        if can is None:
            return []
        results: List[HwChannel] = []
        try:
            for hw in can.detect_available_configs(interfaces=["pcan"]):
                ch = hw.get("channel", "")
                results.append(HwChannel(
                    label     = f"PEAK {ch}",
                    interface = "pcan",
                    channel   = str(ch),
                    model     = "PCAN",
                ))
        except Exception as e:
            logger.debug("PEAK scan skipped: %s", e)
        return results

    # -- CAN Analyzer (slcan + serial autodetect) ----------------------------
    @staticmethod
    def _scan_can_analyzer() -> List[HwChannel]:
        can = get_can()
        results: List[HwChannel] = []
        if can:
            try:
                for hw in can.detect_available_configs(interfaces=["slcan"]):
                    ch = hw.get("channel", "")
                    results.append(HwChannel(
                        label     = f"CAN Analyzer (slcan) {ch}",
                        interface = "slcan",
                        channel   = str(ch),
                        model     = "CAN Analyzer",
                    ))
            except Exception:
                pass
        try:
            import serial.tools.list_ports
            keywords = ["can", "canable", "cantact", "usb2can",
                        "gs_usb", "candlelight", "innomaker"]
            for port in serial.tools.list_ports.comports():
                desc = port.description.lower()
                if any(kw in desc for kw in keywords):
                    results.append(HwChannel(
                        label     = f"CAN Analyzer {port.device} ({port.description})",
                        interface = "slcan",
                        channel   = port.device,
                        model     = port.description,
                    ))
        except ImportError:
            pass
        except Exception as e:
            logger.debug("Serial CAN Analyzer scan skipped: %s", e)
        return results

    # -- SocketCAN (Linux) ---------------------------------------------------
    @staticmethod
    def _scan_socketcan() -> List[HwChannel]:
        results: List[HwChannel] = []
        sys_net = "/sys/class/net"
        try:
            if os.path.isdir(sys_net):
                for name in os.listdir(sys_net):
                    if name.startswith(("can", "vcan")):
                        results.append(HwChannel(
                            label     = f"SocketCAN {name}",
                            interface = "socketcan",
                            channel   = name,
                            model     = "SocketCAN",
                        ))
        except Exception:
            pass
        if not results:
            try:
                proc = subprocess.run(
                    ["ip", "link", "show", "type", "can"],
                    capture_output=True, text=True, timeout=3,
                )
                for line in proc.stdout.splitlines():
                    parts = line.split(":")
                    if len(parts) >= 2:
                        iface = parts[1].strip()
                        if iface:
                            results.append(HwChannel(
                                label     = f"SocketCAN {iface}",
                                interface = "socketcan",
                                channel   = iface,
                                model     = "SocketCAN",
                            ))
            except Exception as e:
                logger.debug("SocketCAN ip-link scan skipped: %s", e)
        return results

    # -- Virtual -------------------------------------------------------------
    @staticmethod
    def _scan_virtual(count: int = 3) -> List[HwChannel]:
        return [
            HwChannel(
                label     = f"Virtual Ch{i}",
                interface = "virtual",
                channel   = i,
                model     = "Virtual",
            )
            for i in range(count)
        ]


# ==============================================================
# HARDWARE CONFIG HELPER
# ==============================================================

class HardwareConfigHelper:
    """
    Converts a scanned HwChannel into a ready-to-use config object.

    Two output formats are supported:

    1. to_network_config()  → NetworkBusConfig   (preferred, net_name-keyed)
    2. to_hardware_config() → HardwareConfig      (legacy, channel_index-keyed)

    Usage
    ─────
        channels = HwScanner.scan()
        ch = channels[0]   # e.g. "Vector VN1640 Ch1"

        # Modern path (net_name-keyed HardwareManager)
        cfg = HardwareConfigHelper.to_network_config(
            hw_channel = ch,
            net_name   = "Engine",
            bitrate    = 500_000,
        )
        hw_manager.add_network(cfg)

        # Legacy path (channel_index-keyed HardwareManager)
        cfg = HardwareConfigHelper.to_hardware_config(
            hw_channel    = ch,
            channel_index = 0,
            bitrate       = 500_000,
        )
        hw_manager.add_channel(cfg)
    """

    @staticmethod
    def to_network_config(
        hw_channel:      HwChannel,
        net_name:        str,
        bitrate:         int  = 500_000,
        is_fd:           bool = False,
        fd_data_bitrate: int  = DEFAULT_FD_DATA_BITRATE,
        app_name:        str  = APP_NAME,
    ) -> NetworkBusConfig:
        """
        Build a NetworkBusConfig from a scanned HwChannel.

        Parameters
        ──────────
        hw_channel      : HwChannel returned by HwScanner.scan()
        net_name        : logical network name (matches CanNetwork.name)
        bitrate         : nominal CAN bitrate in bit/s
        is_fd           : enable CAN FD
        fd_data_bitrate : FD data-phase bitrate (ignored if is_fd=False)
        app_name        : Vector xlSetApplConfig application name
        """
        return NetworkBusConfig(
            net_name        = net_name,
            hw_channel      = hw_channel,
            bitrate         = bitrate,
            is_fd           = is_fd,
            fd_data_bitrate = fd_data_bitrate,
            app_name        = app_name,
        )

    @staticmethod
    def to_hardware_config(
        hw_channel:    HwChannel,
        channel_index: int  = 0,
        bitrate:       int  = 500_000,
        is_fd:         bool = False,
        fd_bitrate:    int  = DEFAULT_FD_DATA_BITRATE,
        app_name:      str  = APP_NAME,
    ) -> HardwareConfig:
        """
        Build a legacy HardwareConfig from a scanned HwChannel.

        Infers HardwareType from hw_channel.interface:
            "vector"    → HardwareType.VECTOR
            "pcan"      → HardwareType.PEAK
            "virtual"   → HardwareType.VIRTUAL
            anything else → HardwareType.VIRTUAL (safe default)

        Parameters
        ──────────
        hw_channel    : HwChannel returned by HwScanner.scan()
        channel_index : integer channel slot in HardwareManager
        bitrate       : nominal CAN bitrate in bit/s
        is_fd         : enable CAN FD
        fd_bitrate    : FD data-phase bitrate
        app_name      : Vector xlSetApplConfig application name

        Note: HardwareConfig.to_bus_config() rebuilds the HwChannel from
        channel_index + HardwareType, so detailed fields (serial, hw_index)
        from the original hw_channel are not preserved.  Use
        to_network_config() when those details matter.
        """
        _iface_to_hwtype = {
            "vector":    HardwareType.VECTOR,
            "pcan":      HardwareType.PEAK,
            "virtual":   HardwareType.VIRTUAL,
            "socketcan": HardwareType.VIRTUAL,   # closest available
            "slcan":     HardwareType.VIRTUAL,
        }
        hw_type = _iface_to_hwtype.get(
            hw_channel.interface.lower(), HardwareType.VIRTUAL
        )
        return HardwareConfig(
            hw_type       = hw_type,
            channel_index = channel_index,
            bitrate       = bitrate,
            is_fd         = is_fd,
            fd_bitrate    = fd_bitrate,
            app_name      = app_name,
        )