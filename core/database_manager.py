"""DatabaseManager: nguồn sự thật duy nhất về các CAN network và DBC của chúng."""
from __future__ import annotations

import os
from typing import Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from core.can_network import CanNetwork, NodeCacheBuilder, is_valid_network_name
from core.dbc_loader import DbcLoadWorker
from core.dbc_parser import DbcRegistry
from core.trace_types import DecodedMessage, Decoder, cantools_decoder


class DatabaseManager(QObject):
    networks_changed = pyqtSignal()               # thêm / xóa network
    network_renamed = pyqtSignal(str, str)        # (tên cũ, tên mới)
    network_updated = pyqtSignal(str)             # đổi đường dẫn DBC...
    dbc_loading = pyqtSignal(str)
    dbc_loaded = pyqtSignal(str, int)             # (network, số message)
    dbc_failed = pyqtSignal(str, str)
    dbc_cleared = pyqtSignal(str)
    decoders_changed = pyqtSignal()               # DBC nạp / xóa -> Trace nên decode lại
    lock_changed = pyqtSignal(bool)               # True: không được đổi tên / xóa network (đang đo)

    LOCKED_MSG = "Stop the measurement before renaming or removing a network."

    def __init__(self, parent=None):
        super().__init__(parent)
        self.registry = DbcRegistry()
        self._networks: dict[str, CanNetwork] = {}
        self._jobs: dict[str, tuple[QThread, DbcLoadWorker, CanNetwork]] = {}
        self._errors: dict[str, str] = {}
        self._trace_cache: dict[int, Decoder] = {}      # channel -> decoder
        self._locked = False

    # ---------- khóa khi đang đo ----------
    @property
    def locked(self) -> bool:
        return self._locked

    def set_locked(self, locked: bool) -> None:
        """Bus đang chạy dưới tên network: đổi tên / xóa sẽ làm Trace mất khung và hỏng mapping."""
        locked = bool(locked)
        if locked != self._locked:
            self._locked = locked
            self.lock_changed.emit(locked)

    # ---------- truy vấn ----------
    @property
    def networks(self) -> dict[str, CanNetwork]:
        return self._networks

    def get(self, name: str) -> Optional[CanNetwork]:
        return self._networks.get(name)

    def is_loading(self, name: str) -> bool:
        return name in self._jobs

    def last_error(self, name: str) -> str:
        return self._errors.get(name, "")

    def network_for_channel(self, channel: int) -> Optional[CanNetwork]:
        for net in self._networks.values():
            if net.channel_index == channel:
                return net
        return None

    def next_channel(self) -> int:
        used = {n.channel_index for n in self._networks.values()}
        ch = 1
        while ch in used:
            ch += 1
        return ch

    # ---------- quản lý network ----------
    def add_network(self, name: str, channel: int | None = None) -> CanNetwork:
        name = name.strip()
        if not is_valid_network_name(name):
            raise ValueError("Only letters, digits, _ and - are allowed.")
        if name in self._networks:
            raise ValueError(f"Network '{name}' already exists.")
        channel = channel or self.next_channel()
        if self.network_for_channel(channel):
            raise ValueError(f"Channel {channel} is already used.")
        net = CanNetwork(name=name, channel_index=channel)
        self._networks[name] = net
        self.networks_changed.emit()
        return net

    def remove_network(self, name: str) -> None:
        if self._locked:
            raise ValueError(self.LOCKED_MSG)
        if self._networks.pop(name, None) is None:
            return
        self.registry.unregister(name)
        self._errors.pop(name, None)
        self._trace_cache.clear()
        self.networks_changed.emit()
        self.decoders_changed.emit()

    def rename_network(self, old: str, new: str) -> None:
        new = new.strip()
        if old == new:
            return
        if self._locked:
            raise ValueError(self.LOCKED_MSG)
        if not is_valid_network_name(new):
            raise ValueError("Only letters, digits, _ and - are allowed.")
        if new in self._networks:
            raise ValueError(f"'{new}' already exists.")
        if old in self._jobs:
            raise ValueError("Cannot rename while the DBC is loading.")
        net = self._networks.pop(old, None)
        if net is None:
            return
        net.name = new
        self._networks[new] = net
        self.registry.unregister(old)
        if net.is_loaded:
            self.registry.register(net)
        self.network_renamed.emit(old, new)

    def set_dbc_path(self, name: str, path: str) -> None:
        if net := self._networks.get(name):
            net.dbc_file = path
            self.network_updated.emit(name)

    # ---------- nạp / xóa DBC ----------
    def load_dbc(self, name: str, path: str | None = None) -> None:
        net = self._networks.get(name)
        if net is None or name in self._jobs:
            return
        path = path or net.dbc_file
        if not path:
            self.dbc_failed.emit(name, "No DBC file selected.")
            return
        net.dbc_file = path
        self._errors.pop(name, None)

        thread = QThread(self)
        worker = DbcLoadWorker(name, path)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_worker_finished)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._jobs[name] = (thread, worker, net)

        self.dbc_loading.emit(name)
        thread.start()

    def _on_worker_finished(self, name: str, db, err: str) -> None:
        job = self._jobs.pop(name, None)
        net = self._networks.get(name)
        if job is None or net is None or job[2] is not net:
            return                                  # network đã bị xóa / thay trong lúc nạp
        if db is None:
            self._errors[name] = err
            self.dbc_failed.emit(name, err)
            return
        net.database = db
        net.node_cache = NodeCacheBuilder.build(db)
        self.registry.register(net)
        self._trace_cache.clear()
        self.dbc_loaded.emit(name, net.message_count)
        self.decoders_changed.emit()

    def clear_dbc(self, name: str) -> None:
        net = self._networks.get(name)
        if net is None:
            return
        net.clear_dbc()
        self.registry.unregister(name)
        self._trace_cache.clear()
        self.dbc_cleared.emit(name)
        self.decoders_changed.emit()

    def shutdown(self) -> None:
        for thread, _worker, _net in list(self._jobs.values()):
            thread.quit()
            thread.wait(2000)
        self._jobs.clear()

    # ---------- decoder cho Trace (có value table dạng chữ) ----------
    def trace_decoder(self, channel: int, can_id: int,
                      data: bytes) -> Optional[DecodedMessage]:
        net = self.network_for_channel(channel)
        if net is None or net.database is None:
            return None
        dec = self._trace_cache.get(channel)
        if dec is None:
            dec = self._trace_cache[channel] = cantools_decoder(net.database)
        return dec(channel, can_id, data)

    # ---------- lưu / khôi phục (dùng cho File > Save/Open Project sau này) ----------
    def to_dict(self) -> dict:
        return {"networks": [
            {"name": n.name, "channel": n.channel_index, "dbc_path": n.dbc_file}
            for n in self._networks.values()]}

    def load_dict(self, data: dict) -> None:
        if self._locked:
            raise ValueError(self.LOCKED_MSG)
        for name in list(self._networks):
            self.remove_network(name)
        for entry in data.get("networks", []):
            try:
                net = self.add_network(entry["name"], entry.get("channel"))
            except (KeyError, ValueError):
                continue
            path = entry.get("dbc_path", "")
            if path:
                net.dbc_file = path
                if os.path.isfile(path):
                    self.load_dbc(net.name, path)