"""Nạp file DBC trong thread nền (parse là bước chậm)."""
from __future__ import annotations

import os
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

try:
    import cantools
    CANTOOLS_OK = True
except ImportError:                                        # pragma: no cover
    cantools = None
    CANTOOLS_OK = False

_ENCODINGS = ("utf-8", "cp1252", "latin-1")


def load_dbc_file(path: str) -> tuple[Any, str]:
    """Trả về (database, "") hoặc (None, thông báo lỗi)."""
    if not CANTOOLS_OK:
        return None, "cantools is not installed (pip install cantools)"
    if not os.path.isfile(path):
        return None, f"File not found: {path}"

    last: Exception | None = None
    for enc in _ENCODINGS:
        try:
            return cantools.database.load_file(
                path, database_format="dbc", encoding=enc, strict=False), ""
        except Exception as e:                             # noqa: BLE001
            last = e
    try:                                                   # phương án cuối: đọc tay
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return cantools.database.load_string(
                fh.read(), database_format="dbc", strict=False), ""
    except Exception as e:                                 # noqa: BLE001
        last = e
    return None, str(last) if last else "Unknown error"


class DbcLoadWorker(QObject):
    finished = pyqtSignal(str, object, str)                # (network, database|None, lỗi)

    def __init__(self, name: str, path: str):
        super().__init__()
        self._name = name
        self._path = path

    @pyqtSlot()
    def run(self) -> None:
        db, err = load_dbc_file(self._path)
        self.finished.emit(self._name, db, err)