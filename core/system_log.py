"""
==============================================================
core/system_log.py
System-wide log bridge:  python `logging`  ->  Qt signal
==============================================================

Every `logger.info/warning/error(...)` call anywhere in the app (core,
models, ui) is forwarded to ONE Qt signal, which SystemMessageWidget
listens to.  No existing file needs to change to appear in the widget.

Thread safety
─────────────
Log records may be produced on any thread (BusManager, RX thread, scan
thread).  The handler only emits a Qt signal; because the SystemLog
object lives in the GUI thread, Qt delivers it as a queued call, so the
widget is always updated on the GUI thread.

Usage (MainWindow.__init__, as early as possible)
─────────────────────────────────────────────────
    from core import system_log
    self.system_log = system_log.install()                 # INFO and above
    ...
    self.system_log.message.connect(self.sysMsgWidget.append_message)
    ...
    system_log.uninstall()                                 # in closeEvent
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal


class SystemLog(QObject):
    #            timestamp(epoch s), level name, source, text
    message = pyqtSignal(float, str, str, str)


class _QtLogHandler(logging.Handler):
    def __init__(self, emitter: SystemLog, level: int) -> None:
        super().__init__(level)
        self._emitter = emitter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            source = record.name.rsplit(".", 1)[-1]      # core.hardware_manager -> hardware_manager
            self._emitter.message.emit(
                record.created, record.levelname, source, record.getMessage()
            )
        except RuntimeError:
            # Emitter already deleted (app shutting down) — drop silently.
            pass
        except Exception:
            self.handleError(record)


_log: Optional[SystemLog] = None
_handler: Optional[_QtLogHandler] = None


def install(level: int = logging.INFO) -> SystemLog:
    """Attach the Qt handler to the root logger (idempotent)."""
    global _log, _handler
    if _log is not None:
        return _log

    _log = SystemLog()
    _handler = _QtLogHandler(_log, level)

    root = logging.getLogger()
    # A logger drops records below its own level BEFORE handlers see them,
    # so the root level must be at least as verbose as the handler level.
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    root.addHandler(_handler)
    return _log


def uninstall() -> None:
    global _log, _handler
    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
    _handler = None
    _log = None


def get() -> Optional[SystemLog]:
    return _log