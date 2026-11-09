"""Kommandozeilen-Steuerung für die dauerhafte Flashscore-Datenerfassung, Speicherung & den Export.

Hinweis: Reine Datenerfassungs-, Speicher- und Export-CLI ohne GUI/Web-App,
ohne Spielprognosen und ohne Wettalgorithmen.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from flashscore.config import (
    LEAGUE_BY_KEY,
    LEAGUES,
    PollingIntervals,
    ensure_storage_tree,
    resolve_data_dir,
    resolve_league_key,
)
from flashscore.daemon import (
    build_runtime,
    spawn_detached_daemon,
    stop_detached_daemon,
)
from flashscore.database import FlashscoreDatabase
from flashscore.exporter import VALID_FORMATS, VALID_SCOPES, FlashscoreExporter
from flashscore.seed_live_data import seed_verified_flashscore_data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m flashscore",
        description=(
            "Dauerhafte Fußballdatenerfassung, Änderungshistorie und Multi-Format-Export "
            "für alle 10 europäischen Ziel-Ligen von Flashscore.de."
        ),
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Frei wählbarer Speicherordner (Standard: ./Flashscore_Daten)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # 1. init
    p_init = sub.add_parser("init", help="Erstellt Ordnerstruktur und Gesamtdatenbank.sqlite")
    p_init.add_argument(
        "--seed",
        action="store_true",
        help="Befüllt die Datenbank initial mit dem verifizierten 10-Ligen-Bestand",
    )

    # 2. sync
    p_sync = sub.add_parser("sync", help="Synchronisiert Spielpläne, Ergebnisse und Tabellen sofort")
    p_sync.add_argument("--league", default=None, help="Optional nur eine bestimmte Liga synchronisieren")
    p_sync.add_argument("--browser-fallback", action="store_true", help="Aktiviert optionalen Stealth-Browser-Abruf")

    # 3. run (Dauerbetrieb mit Auto-Restart Supervisor)
    p_run = sub.add_parser("run", help="Startet die kontinuierliche Erfassung mit Auto-Restart-Supervisor")
    p_run.add_argument("--browser-fallback", action="store_true", help="Aktiviert optionalen Stealth-Browser-Abruf")
    p_run.add_argument("--max-cycles", type=int, default=None, help="Begrenzt Zyklen (für Tests/Diagnose)")
    p_run.add_argument("--live-score-interval", type=int, default=15, help="Intervall Live-Spielstände/Events in Sek. (Standard: 15)")
    p_run.add_argument("--live-stats-interval", type=int, default=30, help="Intervall Live-Statistiken in Sek. (Standard: 30)")
    p_run.add_argument("--injury-interval", type=int, default=900, help="Intervall Verletzungen in Sek. (Standard: 900 = 15 Min.)")
    p_run.add_argument("--schedule-interval", type=int, default=1800, help="Intervall Spielplan-Sync in Sek. (Standard: 1800)")
    p_run.add_argument("--backup-interval", type=int, default=3600, help="Intervall Auto-Backup in Sek. (Standard: 3600)")

    # 4. daemon (Hintergrundbetrieb)
    p_daemon = sub.add_parser("daemon", help="Steuert den Hintergrundprozess (start | stop | status)")
    p_daemon.add_argument("action", choices=("start", "stop", "status"))
    p_daemon.add_argument("--browser-fallback", action="store_true", help="Aktiviert optionalen Stealth-Browser-Abruf")

    # 5. export
    p_exp = sub.add_parser("export", help="Manueller Datenexport in JSON, CSV, XLSX, SQLite oder ZIP")
    p_exp.add_argument(
        "--scope",
        choices=VALID_SCOPES,
        default="all",
        help="Exportumfang: all (alle 10 Ligen), league, team, player, match oder history",
    )
    p_exp.add_argument(
        "--format",
        dest="export_format",
        choices=VALID_FORMATS,
        default="json",
        help="Dateiformat: json, csv, xlsx, sqlite oder zip",
    )
    p_exp.add_argument(
        "--id",
        dest="identifier",
        default=None,
        help="Kennung/Name für Teilumfänge (z. B. Liga 'Bundesliga', Verein 'Bayern München', Spieler 'Jules Koundé' oder match_id 'A9JgHzZA')",
    )
    p_exp.add_argument(
        "--output",
        default=None,
        help="Optionaler Zielpfad für die Exportdatei bzw. den CSV-Zielordner",
    )

    # 6. backup
    sub.add_parser("backup", help="Erstellt sofort ein vollständiges Backup unter Backups/")

    # 7. status
    sub.add_parser("status", help="Zeigt aktuellen Datenbankbestand und Systemstatus")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    data_dir = resolve_data_dir(args.data_dir)

    if args.command == "init":
        if args.seed:
            counts = seed_verified_flashscore_data(data_dir)
        else:
            paths = ensure_storage_tree(data_dir)
            db = FlashscoreDatabase(paths["database"])
            exporter = FlashscoreExporter(db=db, base_dir=data_dir)
            exporter.sync_directory_snapshots()
            counts = db.get_summary_counts()
            db.close()
        print(json.dumps({"status": "initialized", "data_dir": str(data_dir), "counts": counts}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "sync":
        db, collector, exporter, _ = build_runtime(
            data_dir, enable_browser_fallback=bool(args.browser_fallback)
        )
        try:
            if args.league:
                lkey = resolve_league_key(args.league)
                target_leagues = (LEAGUE_BY_KEY[lkey],)
            else:
                target_leagues = LEAGUES
            res = collector.sync_all_leagues(target_leagues)
            exporter.sync_directory_snapshots()
            print(json.dumps({"status": "synced", "result": res, "counts": db.get_summary_counts()}, ensure_ascii=False, indent=2))
            return 0
        finally:
            db.close()

    if args.command == "run":
        intervals = PollingIntervals(
            live_scores_sec=float(args.live_score_interval),
            live_events_sec=float(args.live_score_interval),
            live_stats_sec=float(args.live_stats_interval),
            injuries_sec=float(args.injury_interval),
            schedules_sec=float(args.schedule_interval),
            auto_backup_sec=float(args.backup_interval),
        )
        db, _, _, daemon = build_runtime(
            data_dir,
            enable_browser_fallback=bool(args.browser_fallback),
            intervals=intervals,
        )
        try:
            daemon.run_with_supervisor(max_cycles=args.max_cycles)
            return 0
        finally:
            db.close()

    if args.command == "daemon":
        if args.action == "start":
            pid = spawn_detached_daemon(data_dir, browser_fallback=bool(args.browser_fallback))
            print(json.dumps({"action": "started", "pid": pid, "data_dir": str(data_dir)}, ensure_ascii=False, indent=2))
            return 0
        if args.action == "stop":
            stopped = stop_detached_daemon(data_dir)
            print(json.dumps({"action": "stopped" if stopped else "not_running", "data_dir": str(data_dir)}, ensure_ascii=False, indent=2))
            return 0
        status_path = Path(data_dir) / "daemon_status.json"
        if status_path.exists():
            print(status_path.read_text(encoding="utf-8"))
        else:
            print(json.dumps({"state": "not_started", "data_dir": str(data_dir)}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "export":
        paths = ensure_storage_tree(data_dir)
        db = FlashscoreDatabase(paths["database"])
        try:
            exporter = FlashscoreExporter(db=db, base_dir=data_dir)
            out = exporter.export_dataset(
                scope=args.scope,
                export_format=args.export_format,
                identifier=args.identifier,
                output_path=args.output,
            )
            print(
                json.dumps(
                    {
                        "status": "exported",
                        "scope": args.scope,
                        "format": args.export_format,
                        "identifier": args.identifier,
                        "output_path": str(out),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        finally:
            db.close()

    if args.command == "backup":
        paths = ensure_storage_tree(data_dir)
        db = FlashscoreDatabase(paths["database"])
        try:
            exporter = FlashscoreExporter(db=db, base_dir=data_dir)
            exporter.sync_directory_snapshots()
            res = exporter.create_backup()
            print(json.dumps({"status": "backup_created", **res}, ensure_ascii=False, indent=2))
            return 0
        finally:
            db.close()

    if args.command == "status":
        paths = ensure_storage_tree(data_dir)
        db = FlashscoreDatabase(paths["database"])
        try:
            counts = db.get_summary_counts()
            leagues_info = db.fetch_dicts("SELECT league_key, country, name, season, last_synced_at FROM leagues ORDER BY country, tier")
            print(
                json.dumps(
                    {
                        "data_dir": str(data_dir),
                        "database": str(paths["database"]),
                        "counts": counts,
                        "leagues": leagues_info,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        finally:
            db.close()

    return 1


if __name__ == "__main__":
    sys.exit(main())
