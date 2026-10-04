"""Mô hình một CAN network: tên, channel, file DBC, database cantools."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")


def is_valid_network_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


@dataclass(eq=False)
class CanNetwork:
    name: str
    channel_index: int
    dbc_file: str = ""
    database: Any = None                                   # cantools Database
    node_cache: dict[str, dict[str, list]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not is_valid_network_name(self.name):
            raise ValueError("Network name: only letters, digits, _ and - are allowed.")
        if self.channel_index < 1:
            raise ValueError("Channel index must be >= 1.")

    @property
    def is_loaded(self) -> bool:
        return self.database is not None

    @property
    def message_count(self) -> int:
        return len(self.database.messages) if self.database else 0

    @property
    def node_count(self) -> int:
        return len(self.node_cache)

    def get_channel_display(self) -> str:
        return f"CH{self.channel_index}"

    def get_all_nodes(self) -> list[str]:
        return sorted(self.node_cache)

    def clear_dbc(self) -> None:
        """Bỏ database nhưng giữ đường dẫn để nạp lại được."""
        self.database = None
        self.node_cache = {}


class NodeCacheBuilder:
    """Dựng {node: {"tx_messages": [...], "rx_messages": [...]}} từ database."""

    @staticmethod
    def build(db: Any) -> dict[str, dict[str, list]]:
        cache: dict[str, dict[str, list]] = {}

        def node(name: str) -> dict[str, list]:
            return cache.setdefault(name, {"tx_messages": [], "rx_messages": []})

        for n in getattr(db, "nodes", None) or []:
            node(n.name)

        for msg in db.messages:
            senders = [s for s in (getattr(msg, "senders", None) or [])
                       if s != "Vector__XXX"]
            receivers: set[str] = set()
            for sig in msg.signals:
                receivers.update(r for r in (getattr(sig, "receivers", None) or [])
                                 if r != "Vector__XXX")
            for s in senders:
                node(s)["tx_messages"].append(msg)
            for r in receivers - set(senders):
                node(r)["rx_messages"].append(msg)
        return cache