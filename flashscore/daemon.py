"""Dauerhafter Hintergrund-Daemon mit Multi-Intervall-Scheduler, Fehlerprotokollierung & Auto-Restart."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from flashscore.client import HtmlTransport, ScraplingFlashscoreClient
from flashscore.collector import FlashscoreCollector
from flashscore.config import (
    LEAGUES,
    LeagueConfig,
    PollingIntervals,
    ensure_storage_tree,
    resolve_data_dir,
)
from flashscore.database import FlashscoreDatabase
from flashscore.exporter import FlashscoreExporter
from flashscore.models import utc_now_iso

logger = logging.getLogger("flashscore.daemon")


def configure_logging(data_dir: Path) -> tuple[Path, Path]:
    """Richtet rotierende System- und Fehler-Logdateien im Datenverzeichnis ein."""
    data_dir.mkdir(parents=True, exist_ok=True)
    sys_log = data_dir / "flashscore_system.log"
    err_log = data_dir / "flashscore_errors.log"

    root_logger = logging.getLogger("flashscore")
    root_logger.setLevel(logging.INFO)
    if not root_logger.handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

        sh = RotatingFileHandler(sys_log, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        sh.setLevel(logging.INFO)
        sh.setFormatter(fmt)

        eh = RotatingFileHandler(err_log, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        eh.setLevel(logging.WARNING)
        eh.setFormatter(fmt)

        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)

        root_logger.addHandler(sh)
        root_logger.addHandler(eh)
        root_logger.addHandler(ch)

    return sys_log, err_log


class FlashscoreDaemon:
    """Kontinuierlich laufender Scheduler für alle 10 Ligen mit exakten Abfrageintervallen."""

    def __init__(
        self,
        db: FlashscoreDatabase,
        collector: FlashscoreCollector,
        exporter: FlashscoreExporter,
        intervals: PollingIntervals | None = None,
        leagues: tuple[LeagueConfig, ...] = LEAGUES,
    ) -> None:
        self.db = db
        self.collector = collector
        self.exporter = exporter
        self.intervals = intervals or PollingIntervals()
        self.leagues = leagues
        self._stop_event = threading.Event()
        self.cycles_completed = 0
        self.restart_count = 0
        self.started_at = utc_now_iso()

        self._next_live_scores = 0.0
        self._next_live_stats = 0.0
        self._next_lineups = 0.0
        self._next_injuries = 0.0
        self._next_schedule_sync = 0.0
        self._next_backup = 0.0

    def request_stop(self) -> None:
        self._stop_event.set()

    @property
    def status_file(self) -> Path:
        return self.exporter.base_dir / "daemon_status.json"

    @property
    def pid_file(self) -> Path:
        return self.exporter.base_dir / "daemon.pid"

    def write_status_heartbeat(self, state: str = "running", extra: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "state": state,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "heartbeat_at": utc_now_iso(),
            "cycles_completed": self.cycles_completed,
            "restart_count": self.restart_count,
            "intervals": {
                "live_scores_sec": self.intervals.live_scores_sec,
                "live_events_sec": self.intervals.live_events_sec,
                "live_stats_sec": self.intervals.live_stats_sec,
                "prematch_lineups_sec": self.intervals.prematch_lineups_sec,
                "injuries_sec": self.intervals.injuries_sec,
                "schedules_sec": self.intervals.schedules_sec,
                "auto_backup_sec": self.intervals.auto_backup_sec,
            },
            "database_counts": self.db.get_summary_counts(),
        }
        if extra:
            payload.update(extra)
        self.status_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def run_single_tick(self, now_mono: float | None = None) -> dict[str, int]:
        """Führt alle zum Zeitpunkt `now_mono` fälligen Teilaufgaben aus."""
        now = time.monotonic() if now_mono is None else now_mono
        executed: dict[str, int] = {}

        # 1. Regelmäßige 10-Ligen-Spielplan- & Tabellen-Synchronisation
        if now >= self._next_schedule_sync:
            res = self.collector.sync_all_leagues(self.leagues)
            self.exporter.sync_directory_snapshots()
            executed["schedule_sync_changes"] = res["changes"]
            self._next_schedule_sync = now + self.intervals.schedules_sec

        # 2. Live-Spielstände & Live-Ereignisse (alle 15 Sekunden)
        if now >= self._next_live_scores:
            c_live = self.collector.poll_live_scores_and_events(self.leagues)
            executed["live_scores_and_events_changes"] = c_live
            if c_live > 0:
                self.exporter.sync_directory_snapshots()
            self._next_live_scores = now + self.intervals.live_scores_sec

        # 3. Live-Statistiken (alle 30 Sekunden)
        if now >= self._next_live_stats:
            c_stats = self.collector.poll_live_statistics(self.leagues)
            executed["live_stats_changes"] = c_stats
            self._next_live_stats = now + self.intervals.live_stats_sec

        # 4. Aufstellungen vor Spielbeginn & bei Änderungen
        if now >= self._next_lineups:
            c_lu = self.collector.poll_lineups(self.leagues, prematch_window_minutes=self.intervals.prematch_window_min)
            executed["lineup_changes"] = c_lu
            self._next_lineups = now + self.intervals.prematch_lineups_sec

        # 5. Verletzungen & Ausfälle (alle 15 Minuten)
        if now >= self._next_injuries:
            c_inj = self.collector.poll_injuries_and_absences(self.leagues)
            executed["injury_changes"] = c_inj
            self._next_injuries = now + self.intervals.injuries_sec

        # 6. Automatische regelmäßige Backups
        if now >= self._next_backup:
            self.exporter.sync_directory_snapshots()
            self.exporter.create_backup()
            executed["backup_created"] = 1
            self._next_backup = now + self.intervals.auto_backup_sec

        self.cycles_completed += 1
        self.write_status_heartbeat("running", {"last_tick_results": executed})
        return executed

    def run_with_supervisor(self, max_cycles: int | None = None) -> None:
        """Startet die Endlosschleife inklusive automatischem Neustart bei Fehlern."""
        self.pid_file.write_text(str(os.getpid()), encoding="utf-8")
        self.db.log_system_event(
            "INFO",
            "FlashscoreDaemon",
            "DAEMON_STARTED",
            f"Hintergrund-Datenerfassung gestartet (PID {os.getpid()}) für {len(self.leagues)} Ligen.",
        )

        def _handle_sig(signum: int, _frame: object) -> None:
            logger.info("Stopp-Signal (%s) empfangen – fahre Daemon kontrolliert herunter.", signum)
            self.request_stop()

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, _handle_sig)
            signal.signal(signal.SIGINT, _handle_sig)

        try:
            while not self._stop_event.is_set():
                try:
                    self.run_single_tick()
                    if max_cycles is not None and self.cycles_completed >= max_cycles:
                        break
                    self._stop_event.wait(timeout=1.0)
                except Exception as exc:
                    self.restart_count += 1
                    tb = traceback.format_exc()
                    wait_secs = min(30.0, float(2 ** min(self.restart_count, 5)))
                    logger.error(
                        "Unerwarteter Fehler im Erfassungszyklus (#%d) – automatischer Neustart in %.0fs: %s\n%s",
                        self.restart_count,
                        wait_secs,
                        exc,
                        tb,
                    )
                    self.db.log_system_event(
                        "ERROR",
                        "FlashscoreDaemon",
                        "DAEMON_AUTO_RESTART",
                        f"Automatischer Neustart #{self.restart_count} nach Fehler: {exc}",
                        {"traceback": tb, "wait_seconds": wait_secs},
                    )
                    self.write_status_heartbeat(
                        "restarting",
                        {"last_error": str(exc), "next_retry_seconds": wait_secs},
                    )
                    if max_cycles is not None and self.restart_count >= max(5, max_cycles * 5):
                        break
                    self._stop_event.wait(timeout=min(wait_secs, 0.05 if max_cycles is not None else wait_secs))
        finally:
            self.exporter.sync_directory_snapshots()
            self.write_status_heartbeat("stopped")
            self.pid_file.unlink(missing_ok=True)
            self.db.log_system_event(
                "INFO",
                "FlashscoreDaemon",
                "DAEMON_STOPPED",
                f"Hintergrund-Datenerfassung kontrolliert beendet nach {self.cycles_completed} Zyklen.",
            )


def build_runtime(
    data_dir: str | Path | None = None,
    *,
    client: HtmlTransport | None = None,
    enable_browser_fallback: bool = False,
    intervals: PollingIntervals | None = None,
) -> tuple[FlashscoreDatabase, FlashscoreCollector, FlashscoreExporter, FlashscoreDaemon]:
    """Erstellt die komplette Laufzeitumgebung (Ordnerbaum, SQLite-DB, Client, Collector, Exporter, Daemon)."""
    resolved = resolve_data_dir(data_dir)
    paths = ensure_storage_tree(resolved)
    configure_logging(resolved)
    db = FlashscoreDatabase(paths["database"])
    active_client = client or ScraplingFlashscoreClient(
        db=db, enable_browser_fallback=enable_browser_fallback
    )
    collector = FlashscoreCollector(db=db, client=active_client)
    exporter = FlashscoreExporter(db=db, base_dir=resolved)
    daemon = FlashscoreDaemon(
        db=db,
        collector=collector,
        exporter=exporter,
        intervals=intervals,
    )
    return db, collector, exporter, daemon


def spawn_detached_daemon(data_dir: str | Path | None = None, browser_fallback: bool = False) -> int:
    """Startet den Daemon als unabhängigen Hintergrundprozess (`start_new_session=True`)."""
    resolved = resolve_data_dir(data_dir)
    ensure_storage_tree(resolved)
    cmd = [
        sys.executable,
        "-m",
        "flashscore.cli",
        "--data-dir",
        str(resolved),
        "run",
    ]
    if browser_fallback:
        cmd.append("--browser-fallback")

    stdout_log = (resolved / "daemon_stdout.log").open("a", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=stdout_log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return int(proc.pid)


def stop_detached_daemon(data_dir: str | Path | None = None) -> bool:
    resolved = resolve_data_dir(data_dir)
    pid_path = resolved / "daemon.pid"
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        os.kill(pid, signal.SIGTERM)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        pid_path.unlink(missing_ok=True)
        return False
