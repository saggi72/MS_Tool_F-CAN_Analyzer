"""
==============================================================
core/dbc_parser.py
DBC Signal Decoder Engine
==============================================================

Extends CanNetwork (models/can_network.py) with runtime decoding.

Architecture:
  CanNetwork  → holds cantools database, node topology (your existing code)
  DbcDecoder  → wraps CanNetwork, adds fast frame→signal decoding
  DbcRegistry → manages multiple DbcDecoder instances (one per network)

Performance:
  - decode_frame() uses cached cantools message objects (O(1) lookup by ID)
  - Per-ID cache: built lazily on first decode, never rebuilt
  - cantools does bit-level decode in pure Python — for FD/high busload
    consider cpp canmatrix backend or pre-computed struct pack/unpack

Memory:
  - DecodedSignal uses __slots__ (same pattern as CanFrame)
  - Cache stores message refs, not copies
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional, Dict, List, Any, Tuple

from core.can_network import CanNetwork        # trước: from models.can_network import CanNetwork

logger = logging.getLogger(__name__)

try:
    import cantools
    _CANTOOLS_OK = True
except ImportError:
    cantools = None  # type: ignore
    _CANTOOLS_OK = False
    logger.warning("cantools not available — DBC decoding disabled")


# ==============================================================
# CAN TYPE CLASSIFICATION
# ==============================================================

def message_can_type(msg: Any, *, short: bool = False) -> str:
    """Classify a cantools message as CAN FD or classic CAN 2.0.

    A message is treated as CAN FD when cantools reports ``is_fd`` (parsed from
    the DBC ``VFrameFormat`` attribute) OR when its payload exceeds 8 bytes,
    which is only possible on CAN FD.  Some DBCs omit the FD attribute but still
    define >8-byte messages, so the length check is a robust fallback.

    Returns ``"FD"`` / ``"2.0"`` when *short*, else ``"CAN FD"`` / ``"CAN 2.0"``.
    """
    is_fd = bool(getattr(msg, "is_fd", False)) or int(getattr(msg, "length", 0) or 0) > 8
    if short:
        return "FD" if is_fd else "2.0"
    return "CAN FD" if is_fd else "CAN 2.0"


# ==============================================================
# DATA STRUCTURES
# ==============================================================

@dataclass
class DecodedSignal:
    """Decoded signal value from a CAN frame."""
    __slots__ = ("name", "value", "unit", "raw_value", "is_mux_indicator")

    name:             str
    value:            float    # Physical value (after scale/offset)
    unit:             str
    raw_value:        int      # Raw bit-level value
    is_mux_indicator: bool


@dataclass
class DecodedFrame:
    """Result of decoding one CAN frame against DBC."""
    __slots__ = (
        "can_id", "message_name", "network_name",
        "signals", "timestamp", "decode_error",
    )

    can_id:       int
    message_name: str
    network_name: str
    signals:      List[DecodedSignal]
    timestamp:    float
    decode_error: Optional[str]   # None = success


# ==============================================================
# DBC DECODER (wraps CanNetwork)
# ==============================================================

class DbcDecoder:
    """
    Runtime CAN frame decoder backed by a CanNetwork's database.

    Usage:
        network = CanNetwork("Engine", channel_index=1)
        network.load_dbc("/path/to/engine.dbc")

        decoder = DbcDecoder(network)
        decoded = decoder.decode_frame(can_id=0x100, data=b"\\x01\\x02...")
    """

    def __init__(self, network: CanNetwork):
        """
        Args:
            network: Loaded CanNetwork (must have database)
        """
        self._network = network

        # Per-ID lookup cache: CAN-ID → cantools.Message or None
        # None means ID not in DBC (unknown message)
        # Built lazily — no upfront O(n) scan needed
        self._msg_cache: Dict[int, Optional[Any]] = {}

        # Precompute ID set for fast "is this ID in DBC?" check
        self._known_ids: frozenset = frozenset()
        self._rebuild_id_set()

    def _rebuild_id_set(self) -> None:
        """Rebuild known-ID set from current database."""
        if self._network.database:
            self._known_ids = frozenset(
                m.frame_id for m in self._network.database.messages
            )
            self._msg_cache.clear()  # Invalidate on DBC reload
            logger.debug(
                f"DbcDecoder '{self._network.name}': "
                f"{len(self._known_ids)} known IDs"
            )
        else:
            self._known_ids = frozenset()

    def refresh(self) -> None:
        """Call after network.reload_dbc() to update decoder."""
        self._rebuild_id_set()

    def decode_frame(
        self,
        can_id: int,
        data:   bytes,
        timestamp: float = 0.0,
    ) -> Optional[DecodedFrame]:
        """
        Decode raw CAN frame into physical signal values.

        Returns None if:
        - No database loaded
        - CAN ID not in DBC (unknown message)

        Returns DecodedFrame with decode_error set if cantools
        raises during decode (bad data, wrong length, etc.)

        Performance notes:
        - First call per ID: O(log n) cantools lookup, cached
        - Subsequent calls: O(1) cache hit + cantools decode
        - cantools decode: O(signals_per_message) bit operations
        """
        if not self._network.database:
            return None

        # Fast path: ID not in DBC
        if can_id not in self._known_ids:
            return None

        # Lazy cache lookup
        if can_id not in self._msg_cache:
            self._msg_cache[can_id] = self._find_message(can_id)

        msg_def = self._msg_cache[can_id]
        if msg_def is None:
            return None

        # Decode scaled and raw in one pass each — no per-signal re-decode
        try:
            decoded_dict = msg_def.decode(
                data,
                decode_choices=False,
                scaling=True,
            )
            try:
                raw_dict = msg_def.decode(data, decode_choices=False, scaling=False)
            except Exception:
                raw_dict = {}

            signals = [
                DecodedSignal(
                    name=sig_name,
                    value=float(sig_value),
                    unit=self._get_signal_unit(msg_def, sig_name),
                    raw_value=int(raw_dict.get(sig_name, 0)),
                    is_mux_indicator=False,
                )
                for sig_name, sig_value in decoded_dict.items()
            ]

            return DecodedFrame(
                can_id=can_id,
                message_name=msg_def.name,
                network_name=self._network.name,
                signals=signals,
                timestamp=timestamp,
                decode_error=None,
            )

        except Exception as e:
            # Bad data (wrong DLC, mux error, etc.) — return partial result
            return DecodedFrame(
                can_id=can_id,
                message_name=msg_def.name if msg_def else "Unknown",
                network_name=self._network.name,
                signals=[],
                timestamp=timestamp,
                decode_error=str(e),
            )

    def _find_message(self, can_id: int) -> Optional[Any]:
        """Find cantools message definition by CAN ID."""
        try:
            return self._network.database.get_message_by_frame_id(can_id)
        except KeyError:
            return None

    def _get_signal_unit(self, msg_def: Any, sig_name: str) -> str:
        """Extract unit from signal definition."""
        try:
            sig = msg_def.get_signal_by_name(sig_name)
            return sig.unit or ""
        except Exception:
            return ""

    @property
    def network(self) -> CanNetwork:
        return self._network

    @property
    def message_count(self) -> int:
        return len(self._known_ids)


# ==============================================================
# DBC REGISTRY (manages multiple networks)
# ==============================================================

class DbcRegistry:
    """
    Registry of DbcDecoder instances, one per CanNetwork/channel.

    Usage:
        registry = DbcRegistry()
        registry.register(network_engine)
        registry.register(network_chassis)

        # Decode from any channel:
        decoded = registry.decode(channel=1, can_id=0x100, data=b"...")
    """

    def __init__(self):
        # channel_index → DbcDecoder
        self._decoders: Dict[int, DbcDecoder] = {}
        # network_name → DbcDecoder (alternate lookup)
        self._by_name: Dict[str, DbcDecoder] = {}

    def register(self, network: CanNetwork) -> None:
        """Register a network (creates or replaces decoder)."""
        if not network.is_loaded:
            logger.warning(
                f"Registering network '{network.name}' without DBC — "
                f"decoding disabled for this network"
            )

        decoder = DbcDecoder(network)

        if network.channel_index is not None:
            self._decoders[network.channel_index] = decoder

        self._by_name[network.name] = decoder
        logger.info(
            f"DbcRegistry: registered '{network.name}' "
            f"CH{network.channel_index} "
            f"({decoder.message_count} messages)"
        )

    def unregister(self, network_name: str) -> None:
        """Remove a network from registry."""
        decoder = self._by_name.pop(network_name, None)
        if decoder and decoder.network.channel_index in self._decoders:
            del self._decoders[decoder.network.channel_index]

    def decode(
        self,
        channel: int,
        can_id:  int,
        data:    bytes,
        timestamp: float = 0.0,
    ) -> Optional[DecodedFrame]:
        """
        Decode frame using decoder registered for this channel.

        Returns None if no decoder for channel or ID not in DBC.
        """
        decoder = self._decoders.get(channel)
        if not decoder:
            return None
        return decoder.decode_frame(can_id, data, timestamp)

    def decode_all_channels(
        self,
        can_id:  int,
        data:    bytes,
        timestamp: float = 0.0,
    ) -> List[DecodedFrame]:
        """Try decoding on all registered channels. Returns all matches."""
        results = []
        for decoder in self._decoders.values():
            result = decoder.decode_frame(can_id, data, timestamp)
            if result:
                results.append(result)
        return results

    def refresh(self, network_name: str) -> None:
        """Refresh decoder after DBC reload."""
        decoder = self._by_name.get(network_name)
        if decoder:
            decoder.refresh()

    def refresh_all(self) -> None:
        for decoder in self._decoders.values():
            decoder.refresh()

    def get_all_networks(self) -> List[CanNetwork]:
        return [d.network for d in self._by_name.values()]

    def __len__(self) -> int:
        return len(self._decoders)