"""
==============================================================
core/measurement_session.py
Start / Stop of the whole measurement (HOME > Start, HOME > Stop)
==============================================================

Owns the *running* state of the application.

    Start : build one bus per ACTIVE row of
              - the Measure Bus mapping     -> hw_manager       (Vector app "MS_Tool")
              - the Diagnostic Bus mapping  -> diag_hw_manager  (Vector app "MS_Tool_Diagnostic")
            and connect them.  Measure buses are opened first so they hold the
            channel's init access (bitrate); the Diagnostic port then joins with
            the SAME bitrate / FD settings.
    Stop  : disconnect Diagnostic buses first, then Measure buses.

The Channel Mapping dialog never connects anything; it only edits the mapping
stored in HwSettingsStore.  Everything that must follow the run state
(dialogs, Trace, Graphic, ...) listens to `running_changed(bool)`.

The diagnostic HardwareManager has its own (dummy) primary RX queue, so
diagnostic RX frames reach ONLY its subscribers (UdsEngine) and never the Trace.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QObject, pyqtSignal

from core.hardware import (
    HardwareConfigHelper,
    HardwareManager,
    HwScanner,
    is_can_available,
)
from models.hw_settings import (
    KIND_BUS, KIND_DIAG, HwSettingsStore, allows_shared_apps, build_display_labels, interface_of,
)

logger = logging.getLogger(__name__)

ERROR_REPEAT_S = 5.0     # the same bus error is logged at most once per 5 s

_TITLE = {KIND_BUS: "Measure", KIND_DIAG: "Diagnostic"}


class MeasurementSession(QObject):
    running_changed = pyqtSignal(bool)

    def __init__(self, store: HwSettingsStore, database, hw_manager: HardwareManager,
                 diag_hw_manager: HardwareManager, parent=None) -> None:
        super().__init__(parent)
        self._store = store
        self._db = database                  # DatabaseManager (needs .networks)
        self._hw = hw_manager                # Trace / Send buses
        self._diag = diag_hw_manager         # Diagnostic buses
        self._running = False
        self._last_error: Dict[Tuple[str, str, str], float] = {}
        self._hw.network_error.connect(lambda n, m: self._on_network_error(n, m, "NET"))
        self._diag.network_error.connect(lambda n, m: self._on_network_error(n, m, "DIAG"))

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Start ─────────────────────────────────────────────────

    def _jobs(self, kind: str) -> List[Tuple[str, str]]:
        """[(net_name, hw_key)] of active rows whose network still exists, in channel order."""
        wanted = self._store.active_mapping(kind)
        nets = sorted(self._db.networks.values(), key=lambda n: n.channel_index or 0)
        return [(n.name, wanted[n.name]) for n in nets if n.name in wanted]

    def start(self) -> bool:
        """Connect every active Measure + Diagnostic network.  True if at least one bus is up."""
        if self._running:
            return True

        bus_jobs = self._jobs(KIND_BUS)
        diag_jobs = self._jobs(KIND_DIAG)
        if not bus_jobs and not diag_jobs:
            logger.warning("Start: no active network in Channel Mapping")
            return False

        # Scan results are not persisted: after an app restart the hardware list is empty.
        if any(self._store.resolve(k) is None for _, k in bus_jobs + diag_jobs):
            self._scan_now()

        s = self._store
        bus_used = self._start_group(KIND_BUS, self._hw, bus_jobs, s.app_name)
        diag_used = self._start_group(KIND_DIAG, self._diag, diag_jobs, s.diag.app_name,
                                      taken=bus_used)
        n_bus, n_diag = len(bus_used), len(diag_used)

        if n_bus + n_diag == 0:
            logger.warning("Start failed: no network could be started")
            return False

        self._running = True
        logger.info("Measurement started: measure %d/%d, diagnostic %d/%d",
                    n_bus, len(bus_jobs), n_diag, len(diag_jobs))
        self.running_changed.emit(True)
        return True

    def _start_group(self, kind: str, manager: HardwareManager,
                     jobs: List[Tuple[str, str]], app_name: str,
                     taken: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """
        Open one bus per job on *manager*.  Returns {hw_key: net_name} of the buses that came up.

        *taken* = channels already opened by the OTHER role.  Only Vector can share a channel
        between Measure and Diagnostic; for any other interface the job is skipped.
        """
        s = self._store
        labels = build_display_labels(s.channels)
        title = _TITLE[kind]
        used: Dict[str, str] = {}

        for net, key in jobs:
            hw = s.resolve(key)
            if hw is None:
                logger.warning("Start: %s '%s' skipped — mapped hardware not found (not plugged in?)",
                               title.lower(), net)
                continue
            if key in used:
                logger.warning("Start: %s '%s' skipped — %s is already used by '%s'",
                               title.lower(), net, labels[key], used[key])
                continue

            if taken and key in taken and not allows_shared_apps(key):
                logger.warning(
                    "Start: %s '%s' skipped — %s allows only one of Measure / Diagnostic per device "
                    "(%s is in use by '%s')", title.lower(), net,
                    interface_of(key).upper(), labels[key], taken[key])
                continue

            # The Diagnostic port shares the channel with the Measure port: the Vector
            # driver only accepts identical bit-rate / FD settings, so both use the same.
            cfg = HardwareConfigHelper.to_network_config(
                hw_channel=hw, net_name=net, bitrate=s.bitrate, is_fd=s.is_fd,
                fd_data_bitrate=s.fd_data_bitrate, app_name=app_name,
            )
            if net in manager.get_network_names():
                manager.remove_network(net)
            manager.add_network(cfg)

            if manager.start_network(net):
                used[key] = net
                logger.info("Started %s '%s' on %s @ %d kbit/s%s (app '%s')", title.lower(), net,
                            labels[key], s.bitrate // 1000,
                            f" FD {s.fd_data_bitrate // 1000} kbit/s" if s.is_fd else "",
                            app_name)
            else:
                manager.remove_network(net)
                logger.error("Start: %s '%s' FAILED to connect on %s", title.lower(), net, labels[key])
        return used

    def _scan_now(self) -> None:
        if not is_can_available():
            logger.error("python-can is not installed (pip install python-can)")
            return
        logger.info("Hardware list is empty — scanning before Start")
        try:
            self._store.set_channels(HwScanner.scan(self._store.backends))
        except Exception as e:
            logger.error("Scan before Start failed: %s", e)

    # ── Stop ──────────────────────────────────────────────────

    def stop(self) -> None:
        """Disconnect every bus (Diagnostic first, then Measure)."""
        diag_names = list(self._diag.get_network_names())
        bus_names = list(self._hw.get_network_names())
        if not self._running and not diag_names and not bus_names:
            return
        for nn in diag_names:
            self._diag.remove_network(nn)           # stops the RX thread + closes the bus
        for nn in bus_names:
            self._hw.remove_network(nn)
        self._running = False
        self._last_error.clear()
        logger.info("Measurement stopped (%d measure + %d diagnostic network(s) closed)",
                    len(bus_names), len(diag_names))
        self.running_changed.emit(False)

    # ── bus errors ────────────────────────────────────────────

    def _on_network_error(self, net: str, msg: str, tag: str) -> None:
        """RX threads can report the same error every 100 ms (e.g. cable pulled): throttle it."""
        now = time.monotonic()
        key = (tag, net, msg)
        if now - self._last_error.get(key, -1e9) < ERROR_REPEAT_S:
            return
        if len(self._last_error) > 200:
            self._last_error.clear()
        self._last_error[key] = now
        logger.error("%s[%s]: %s", tag, net, msg)