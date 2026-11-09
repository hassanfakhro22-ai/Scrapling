"""Lokaler Multi-Format-Exporter (JSON, CSV, XLSX, SQLite, ZIP), Ordner-Synchronisation & Backups."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from flashscore.config import (
    LEAGUES,
    ensure_storage_tree,
    resolve_data_dir,
    resolve_league_key,
)
from flashscore.database import SCHEMA_SQL, FlashscoreDatabase
from flashscore.models import utc_now_iso

VALID_SCOPES = ("all", "league", "team", "player", "match", "history")
VALID_FORMATS = ("json", "csv", "xlsx", "sqlite", "zip")


def _safe_filename(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text.strip()).strip("_")
    return cleaned or "export"


def _write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return path
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _write_xlsx(path: Path, sheets: dict[str, list[dict[str, Any]]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    default_ws = wb.active
    first_used = False

    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    for raw_title, rows in sheets.items():
        safe_title = re.sub(r"[\[\]:*?/\\]", "_", raw_title)[:31] or "Daten"
        if not first_used and default_ws is not None:
            ws = default_ws
            ws.title = safe_title
            first_used = True
        else:
            ws = wb.create_sheet(title=safe_title)

        if not rows:
            ws.append(["Hinweis"])
            ws.append(["Keine Einträge für diesen Bereich vorhanden."])
            continue

        headers = list(rows[0].keys())
        ws.append(headers)
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        for r in rows:
            ws.append(
                [
                    json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                    for v in (r.get(h) for h in headers)
                ]
            )

        for col_idx, h in enumerate(headers, start=1):
            sample_len = max(
                [len(str(h))] + [len(str(r.get(h, "") or "")) for r in rows[:100]]
            )
            ws.column_dimensions[get_column_letter(col_idx)].width = min(48, max(12, sample_len + 3))

    wb.save(str(path))
    return path


class FlashscoreExporter:
    """Verwaltet die vorgeschriebene Ordnerstruktur, manuelle Exporte und automatische Backups."""

    def __init__(self, db: FlashscoreDatabase, base_dir: str | Path | None = None) -> None:
        self.db = db
        self.base_dir = resolve_data_dir(base_dir)
        self.paths = ensure_storage_tree(self.base_dir)

    # ------------------------------------------------------------------
    # 1. Synchronisation der kompletten lokalen Ordnerstruktur
    # ------------------------------------------------------------------

    def sync_directory_snapshots(self) -> dict[str, str]:
        """Aktualisiert sämtliche Liga- und Fachordner unter `Flashscore_Daten/`."""
        written: dict[str, str] = {}

        # A) Alle 10 Liga-Ordner
        for lg in LEAGUES:
            lg_dir = self.paths[f"league:{lg.league_key}"]
            for tbl_type, fname in (
                ("overall", "gesamttabelle"),
                ("home", "heimtabelle"),
                ("away", "auswaertstabelle"),
                ("form_5", "formtabelle_5_spiele"),
                ("form_10", "formtabelle_10_spiele"),
            ):
                s_rows = self.db.fetch_dicts(
                    """
                    SELECT rank, team_id, team_name, played, wins, draws, losses,
                           goals_for, goals_against, goal_diff, points,
                           form_last_5, form_last_10, deduction_note, updated_at
                    FROM standings
                    WHERE league_key = ? AND table_type = ?
                    ORDER BY rank ASC
                    """,
                    (lg.league_key, tbl_type),
                )
                _write_json(lg_dir / f"{fname}.json", s_rows)
                _write_csv(lg_dir / f"{fname}.csv", s_rows)

            m_rows = self.db.fetch_dicts(
                """
                SELECT match_id, season, matchday, kickoff_raw, status, phase, minute,
                       home_team_id, home_team_name, away_team_id, away_team_name,
                       home_score, away_score, ht_home_score, ht_away_score,
                       referee_name, stadium_name, stadium_city, stadium_capacity, attendance, updated_at
                FROM matches
                WHERE league_key = ?
                ORDER BY kickoff_raw DESC, match_id ASC
                """,
                (lg.league_key,),
            )
            _write_json(lg_dir / "spielplan_und_ergebnisse.json", m_rows)
            _write_csv(lg_dir / "spielplan_und_ergebnisse.csv", m_rows)

            t_rows = self.db.fetch_dicts(
                "SELECT * FROM teams WHERE league_key = ? ORDER BY name ASC",
                (lg.league_key,),
            )
            _write_json(lg_dir / "mannschaften.json", t_rows)
            written[lg.league_key] = str(lg_dir)

        # B) Spieler
        players_dir = self.paths["Spieler"]
        players_rows = self.db.fetch_dicts("SELECT * FROM players ORDER BY player_name ASC")
        _write_json(players_dir / "spieler_verzeichnis.json", players_rows)
        _write_csv(players_dir / "spieler_verzeichnis.csv", players_rows)

        # C) Verletzungen & Sperren
        inj_dir = self.paths["Verletzungen"]
        inj_rows = self.db.fetch_dicts(
            "SELECT * FROM injuries ORDER BY league_key ASC, team_name ASC, player_name ASC"
        )
        _write_json(inj_dir / "aktuelle_ausfaelle_und_fragliche.json", inj_rows)
        _write_csv(inj_dir / "aktuelle_ausfaelle_und_fragliche.csv", inj_rows)
        inj_hist = self.db.fetch_dicts("SELECT * FROM injury_history ORDER BY id DESC")
        _write_json(inj_dir / "verletzungshistorie.json", inj_hist)

        # D) Live-Spiele & Ereignisse
        live_dir = self.paths["Live_Spiele"]
        live_matches = self.db.fetch_dicts(
            "SELECT * FROM matches WHERE status = 'live' ORDER BY league_key ASC, kickoff_raw ASC"
        )
        recent_events = self.db.fetch_dicts("SELECT * FROM match_events ORDER BY id DESC LIMIT 1000")
        _write_json(live_dir / "live_spiele_aktuell.json", live_matches)
        _write_csv(live_dir / "live_spiele_aktuell.csv", live_matches)
        _write_json(live_dir / "spielereignisse.json", recent_events)
        _write_csv(live_dir / "spielereignisse.csv", recent_events)

        # E) Spielstatistiken
        stats_dir = self.paths["Spielstatistiken"]
        stats_rows = self.db.fetch_dicts(
            """
            SELECT ms.match_id, m.league_key, m.home_team_name, m.away_team_name,
                   ms.period, ms.category, ms.metric_name,
                   ms.home_raw, ms.away_raw, ms.home_value, ms.away_value, ms.updated_at
            FROM match_stats ms
            JOIN matches m ON m.match_id = ms.match_id
            ORDER BY ms.match_id ASC, ms.id ASC
            """
        )
        _write_json(stats_dir / "spielstatistiken_gesamt.json", stats_rows)
        _write_csv(stats_dir / "spielstatistiken_gesamt.csv", stats_rows)

        # F) Aufstellungen
        lu_dir = self.paths["Aufstellungen"]
        lu_headers = self.db.fetch_dicts("SELECT * FROM lineups ORDER BY updated_at DESC")
        lu_players = self.db.fetch_dicts(
            "SELECT * FROM lineup_players ORDER BY match_id ASC, team_side ASC, id ASC"
        )
        _write_json(lu_dir / "aufstellungen_uebersicht.json", lu_headers)
        _write_json(lu_dir / "kader_und_ersatzbank.json", lu_players)
        _write_csv(lu_dir / "kader_und_ersatzbank.csv", lu_players)

        # G) Schiedsrichter
        ref_dir = self.paths["Schiedsrichter"]
        ref_rows = self.db.fetch_dicts("SELECT * FROM referees ORDER BY matches_officiated DESC, name ASC")
        _write_json(ref_dir / "schiedsrichter_statistik.json", ref_rows)
        _write_csv(ref_dir / "schiedsrichter_statistik.csv", ref_rows)

        # H) Stadien inkl. Spielansetzungen
        stad_dir = self.paths["Stadien"]
        stad_rows = self.db.fetch_dicts("SELECT * FROM stadiums ORDER BY name ASC")
        stad_matches = self.db.fetch_dicts(
            """
            SELECT stadium_name, stadium_city, stadium_capacity, attendance,
                   match_id, league_key, kickoff_raw, home_team_name, away_team_name,
                   home_score, away_score, status
            FROM matches
            WHERE stadium_name IS NOT NULL AND TRIM(stadium_name) != ''
            ORDER BY stadium_name ASC, kickoff_raw DESC
            """
        )
        _write_json(stad_dir / "stadien.json", stad_rows)
        _write_csv(stad_dir / "stadien.csv", stad_rows)
        _write_json(stad_dir / "stadien_spielansetzungen.json", stad_matches)

        # I) Historische Daten & Änderungsprotokolle
        hist_dir = self.paths["Historische_Daten"]
        changes_rows = self.db.fetch_dicts("SELECT * FROM change_history ORDER BY id DESC")
        stand_hist = self.db.fetch_dicts("SELECT * FROM standings_history ORDER BY id DESC")
        score_hist = self.db.fetch_dicts("SELECT * FROM score_history ORDER BY id DESC")
        lu_hist = self.db.fetch_dicts("SELECT * FROM lineup_history ORDER BY id DESC")
        stats_hist = self.db.fetch_dicts("SELECT * FROM stats_history ORDER BY id DESC")

        _write_json(hist_dir / "aenderungsprotokoll_komplett.json", changes_rows)
        _write_csv(hist_dir / "aenderungsprotokoll_komplett.csv", changes_rows)
        _write_json(hist_dir / "tabellen_historie.json", stand_hist)
        _write_json(hist_dir / "spielstand_historie.json", score_hist)
        _write_json(hist_dir / "aufstellungs_historie.json", lu_hist)
        _write_json(hist_dir / "statistik_historie.json", stats_hist)

        written["root"] = str(self.base_dir)
        return written

    # ------------------------------------------------------------------
    # 2. Gezielte Datenabfrage nach Exportumfang (Scope)
    # ------------------------------------------------------------------

    def collect_scoped_datasets(
        self, scope: str, identifier: str | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        norm_scope = scope.strip().lower()
        if norm_scope not in VALID_SCOPES:
            raise ValueError(f"Ungültiger Export-Umfang '{scope}'. Erlaubt: {VALID_SCOPES}")

        if norm_scope == "all":
            return {
                "Ligen": self.db.fetch_dicts("SELECT * FROM leagues ORDER BY country, tier"),
                "Mannschaften": self.db.fetch_dicts("SELECT * FROM teams ORDER BY league_key, name"),
                "Tabellen": self.db.fetch_dicts(
                    "SELECT * FROM standings ORDER BY league_key, table_type, rank"
                ),
                "Spiele": self.db.fetch_dicts(
                    "SELECT * FROM matches ORDER BY league_key, kickoff_raw DESC"
                ),
                "Spielereignisse": self.db.fetch_dicts("SELECT * FROM match_events ORDER BY match_id, id"),
                "Spielstatistiken": self.db.fetch_dicts("SELECT * FROM match_stats ORDER BY match_id, id"),
                "Aufstellungen": self.db.fetch_dicts("SELECT * FROM lineups ORDER BY match_id"),
                "Kader_Spieler": self.db.fetch_dicts(
                    "SELECT * FROM lineup_players ORDER BY match_id, team_side, id"
                ),
                "Spieler": self.db.fetch_dicts("SELECT * FROM players ORDER BY player_name"),
                "Verletzungen": self.db.fetch_dicts("SELECT * FROM injuries ORDER BY league_key, team_name"),
                "Schiedsrichter": self.db.fetch_dicts("SELECT * FROM referees ORDER BY name"),
                "Stadien": self.db.fetch_dicts("SELECT * FROM stadiums ORDER BY name"),
                "Aenderungsprotokoll": self.db.fetch_dicts("SELECT * FROM change_history ORDER BY id DESC"),
            }

        if norm_scope == "league":
            if not identifier:
                raise ValueError("Für scope='league' muss eine Liga angegeben werden (z. B. 'Bundesliga').")
            lkey = resolve_league_key(identifier)
            return {
                "Liga": self.db.fetch_dicts("SELECT * FROM leagues WHERE league_key = ?", (lkey,)),
                "Mannschaften": self.db.fetch_dicts(
                    "SELECT * FROM teams WHERE league_key = ? ORDER BY name", (lkey,)
                ),
                "Gesamttabelle": self.db.fetch_dicts(
                    "SELECT * FROM standings WHERE league_key = ? AND table_type = 'overall' ORDER BY rank",
                    (lkey,),
                ),
                "Heimtabelle": self.db.fetch_dicts(
                    "SELECT * FROM standings WHERE league_key = ? AND table_type = 'home' ORDER BY rank",
                    (lkey,),
                ),
                "Auswaertstabelle": self.db.fetch_dicts(
                    "SELECT * FROM standings WHERE league_key = ? AND table_type = 'away' ORDER BY rank",
                    (lkey,),
                ),
                "Form_5_Spiele": self.db.fetch_dicts(
                    "SELECT * FROM standings WHERE league_key = ? AND table_type = 'form_5' ORDER BY rank",
                    (lkey,),
                ),
                "Form_10_Spiele": self.db.fetch_dicts(
                    "SELECT * FROM standings WHERE league_key = ? AND table_type = 'form_10' ORDER BY rank",
                    (lkey,),
                ),
                "Spiele": self.db.fetch_dicts(
                    "SELECT * FROM matches WHERE league_key = ? ORDER BY kickoff_raw DESC", (lkey,)
                ),
                "Spielereignisse": self.db.fetch_dicts(
                    """
                    SELECT e.* FROM match_events e
                    JOIN matches m ON m.match_id = e.match_id
                    WHERE m.league_key = ? ORDER BY e.match_id, e.id
                    """,
                    (lkey,),
                ),
                "Spielstatistiken": self.db.fetch_dicts(
                    """
                    SELECT s.* FROM match_stats s
                    JOIN matches m ON m.match_id = s.match_id
                    WHERE m.league_key = ? ORDER BY s.match_id, s.id
                    """,
                    (lkey,),
                ),
                "Verletzungen": self.db.fetch_dicts(
                    "SELECT * FROM injuries WHERE league_key = ? ORDER BY team_name, player_name", (lkey,)
                ),
                "Aenderungsprotokoll": self.db.fetch_dicts(
                    "SELECT * FROM change_history WHERE league_key = ? ORDER BY id DESC", (lkey,)
                ),
            }

        if norm_scope == "team":
            if not identifier:
                raise ValueError("Für scope='team' muss eine Mannschafts-ID oder ein Vereinsname angegeben werden.")
            q = f"%{identifier.strip()}%"
            teams = self.db.fetch_dicts(
                "SELECT * FROM teams WHERE team_id = ? OR LOWER(name) LIKE LOWER(?)",
                (identifier.strip(), q),
            )
            if not teams:
                raise ValueError(f"Keine Mannschaft gefunden für '{identifier}'.")
            tid = teams[0]["team_id"]
            return {
                "Mannschaft": teams[:1],
                "Tabellenstaende": self.db.fetch_dicts(
                    "SELECT * FROM standings WHERE team_id = ? ORDER BY table_type", (tid,)
                ),
                "Spiele": self.db.fetch_dicts(
                    "SELECT * FROM matches WHERE home_team_id = ? OR away_team_id = ? ORDER BY kickoff_raw DESC",
                    (tid, tid),
                ),
                "Kader_Spieler": self.db.fetch_dicts(
                    "SELECT * FROM players WHERE team_id = ? ORDER BY player_name", (tid,)
                ),
                "Verletzungen_Ausfaelle": self.db.fetch_dicts(
                    "SELECT * FROM injuries WHERE team_id = ? ORDER BY player_name", (tid,)
                ),
                "Tabellen_Historie": self.db.fetch_dicts(
                    "SELECT * FROM standings_history WHERE team_id = ? ORDER BY id DESC", (tid,)
                ),
            }

        if norm_scope == "player":
            if not identifier:
                raise ValueError("Für scope='player' muss eine Spieler-ID oder ein Spielername angegeben werden.")
            q = f"%{identifier.strip()}%"
            players = self.db.fetch_dicts(
                "SELECT * FROM players WHERE player_id = ? OR LOWER(player_name) LIKE LOWER(?)",
                (identifier.strip(), q),
            )
            if not players:
                raise ValueError(f"Kein Spieler gefunden für '{identifier}'.")
            p = players[0]
            pid = p["player_id"]
            pname = p["player_name"]
            return {
                "Spielerprofil": [p],
                "Einsaetze_Aufstellungen": self.db.fetch_dicts(
                    "SELECT * FROM lineup_players WHERE player_id = ? OR player_name = ? ORDER BY id DESC",
                    (pid, pname),
                ),
                "Spielereignisse": self.db.fetch_dicts(
                    """
                    SELECT * FROM match_events
                    WHERE player_id = ? OR player_name = ? OR secondary_player_name = ?
                    ORDER BY id DESC
                    """,
                    (pid, pname, pname),
                ),
                "Aktuelle_Ausfaelle": self.db.fetch_dicts(
                    "SELECT * FROM injuries WHERE player_id = ? OR player_name = ?", (pid, pname)
                ),
                "Verletzungshistorie": self.db.fetch_dicts(
                    "SELECT * FROM injury_history WHERE player_id = ? OR player_name = ? ORDER BY id DESC",
                    (pid, pname),
                ),
            }

        if norm_scope == "match":
            if not identifier:
                raise ValueError("Für scope='match' muss eine `match_id` angegeben werden.")
            mid = identifier.strip()
            matches = self.db.fetch_dicts("SELECT * FROM matches WHERE match_id = ?", (mid,))
            if not matches:
                raise ValueError(f"Kein Spiel mit match_id='{mid}' gefunden.")
            return {
                "Spieldaten": matches,
                "Ereignisse": self.db.fetch_dicts(
                    "SELECT * FROM match_events WHERE match_id = ? ORDER BY id ASC", (mid,)
                ),
                "Statistiken": self.db.fetch_dicts(
                    "SELECT * FROM match_stats WHERE match_id = ? ORDER BY period, id ASC", (mid,)
                ),
                "Aufstellung_Header": self.db.fetch_dicts(
                    "SELECT * FROM lineups WHERE match_id = ?", (mid,)
                ),
                "Aufstellung_Spieler": self.db.fetch_dicts(
                    "SELECT * FROM lineup_players WHERE match_id = ? ORDER BY team_side, id ASC", (mid,)
                ),
                "Ausfaelle_Spiel": self.db.fetch_dicts(
                    "SELECT * FROM injuries WHERE source_match_id = ?", (mid,)
                ),
                "Spielstand_Historie": self.db.fetch_dicts(
                    "SELECT * FROM score_history WHERE match_id = ? ORDER BY id ASC", (mid,)
                ),
                "Statistik_Historie": self.db.fetch_dicts(
                    "SELECT * FROM stats_history WHERE match_id = ? ORDER BY id ASC", (mid,)
                ),
                "Aufstellungs_Historie": self.db.fetch_dicts(
                    "SELECT * FROM lineup_history WHERE match_id = ? ORDER BY id ASC", (mid,)
                ),
            }

        # norm_scope == "history"
        if identifier:
            try:
                lkey = resolve_league_key(identifier)
            except ValueError:
                lkey = identifier.strip()
            return {
                "Aenderungsprotokoll": self.db.fetch_dicts(
                    "SELECT * FROM change_history WHERE league_key = ? ORDER BY id DESC", (lkey,)
                ),
                "Tabellen_Historie": self.db.fetch_dicts(
                    "SELECT * FROM standings_history WHERE league_key = ? ORDER BY id DESC", (lkey,)
                ),
                "Spielstand_Historie": self.db.fetch_dicts(
                    "SELECT * FROM score_history WHERE league_key = ? ORDER BY id DESC", (lkey,)
                ),
                "Verletzungs_Historie": self.db.fetch_dicts(
                    "SELECT * FROM injury_history WHERE league_key = ? ORDER BY id DESC", (lkey,)
                ),
                "System_Ereignisse": self.db.fetch_dicts("SELECT * FROM system_events ORDER BY id DESC"),
            }

        return {
            "Aenderungsprotokoll": self.db.fetch_dicts("SELECT * FROM change_history ORDER BY id DESC"),
            "Tabellen_Historie": self.db.fetch_dicts("SELECT * FROM standings_history ORDER BY id DESC"),
            "Spielstand_Historie": self.db.fetch_dicts("SELECT * FROM score_history ORDER BY id DESC"),
            "Statistik_Historie": self.db.fetch_dicts("SELECT * FROM stats_history ORDER BY id DESC"),
            "Aufstellungs_Historie": self.db.fetch_dicts("SELECT * FROM lineup_history ORDER BY id DESC"),
            "Verletzungs_Historie": self.db.fetch_dicts("SELECT * FROM injury_history ORDER BY id DESC"),
            "System_Ereignisse": self.db.fetch_dicts("SELECT * FROM system_events ORDER BY id DESC"),
        }

    # ------------------------------------------------------------------
    # 3. Manueller On-Demand-Export in JSON, CSV, XLSX, SQLite oder ZIP
    # ------------------------------------------------------------------

    def export_dataset(
        self,
        *,
        scope: str = "all",
        export_format: str = "json",
        identifier: str | None = None,
        output_path: str | Path | None = None,
    ) -> Path:
        """Exportiert den gewünschten Datenumfang in das gewählte Format."""
        fmt = export_format.strip().lower()
        if fmt not in VALID_FORMATS:
            raise ValueError(f"Ungültiges Format '{export_format}'. Erlaubt: {VALID_FORMATS}")

        datasets = self.collect_scoped_datasets(scope, identifier)
        ts_slug = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        id_part = f"_{_safe_filename(identifier)}" if identifier else ""
        stem = f"flashscore_{scope.lower()}{id_part}_{ts_slug}"

        default_dir = self.base_dir / "Exporte"
        default_dir.mkdir(parents=True, exist_ok=True)

        if fmt == "json":
            dest = Path(output_path).resolve() if output_path else (default_dir / f"{stem}.json")
            payload = {
                "meta": {
                    "exported_at": utc_now_iso(),
                    "scope": scope,
                    "identifier": identifier,
                    "record_counts": {k: len(v) for k, v in datasets.items()},
                },
                "data": datasets,
            }
            return _write_json(dest, payload)

        if fmt == "xlsx":
            dest = Path(output_path).resolve() if output_path else (default_dir / f"{stem}.xlsx")
            return _write_xlsx(dest, datasets)

        if fmt == "csv":
            dest_dir = Path(output_path).resolve() if output_path else (default_dir / f"{stem}_csv")
            dest_dir.mkdir(parents=True, exist_ok=True)
            for sheet_name, rows in datasets.items():
                _write_csv(dest_dir / f"{_safe_filename(sheet_name)}.csv", rows)
            _write_json(
                dest_dir / "manifest.json",
                {
                    "exported_at": utc_now_iso(),
                    "scope": scope,
                    "identifier": identifier,
                    "files": [f"{_safe_filename(k)}.csv" for k in datasets],
                },
            )
            return dest_dir

        if fmt == "sqlite":
            dest = Path(output_path).resolve() if output_path else (default_dir / f"{stem}.sqlite")
            if scope.lower() == "all":
                return self.db.create_sqlite_backup(dest)
            return self._write_scoped_sqlite(dest, datasets)

        # fmt == "zip"
        dest = Path(output_path).resolve() if output_path else (default_dir / f"{stem}.zip")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            json_file = self.export_dataset(
                scope=scope, export_format="json", identifier=identifier, output_path=tmp / f"{stem}.json"
            )
            xlsx_file = self.export_dataset(
                scope=scope, export_format="xlsx", identifier=identifier, output_path=tmp / f"{stem}.xlsx"
            )
            sqlite_file = self.export_dataset(
                scope=scope, export_format="sqlite", identifier=identifier, output_path=tmp / f"{stem}.sqlite"
            )
            csv_dir = self.export_dataset(
                scope=scope, export_format="csv", identifier=identifier, output_path=tmp / "csv"
            )

            with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(json_file, arcname=json_file.name)
                zf.write(xlsx_file, arcname=xlsx_file.name)
                zf.write(sqlite_file, arcname=sqlite_file.name)
                for cf in sorted(csv_dir.glob("*")):
                    if cf.is_file():
                        zf.write(cf, arcname=f"csv/{cf.name}")
        return dest

    def _write_scoped_sqlite(
        self, dest: Path, datasets: dict[str, list[dict[str, Any]]]
    ) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        conn = sqlite3.connect(str(dest))
        try:
            conn.executescript(SCHEMA_SQL)
            for name, rows in datasets.items():
                tbl = _safe_filename(name).lower()
                if not rows:
                    conn.execute(f'CREATE TABLE IF NOT EXISTS "export_{tbl}" (info TEXT)')
                    continue
                cols = list(rows[0].keys())
                col_defs = ", ".join(f'"{c}" TEXT' for c in cols)
                conn.execute(f'CREATE TABLE IF NOT EXISTS "export_{tbl}" ({col_defs})')
                placeholders = ", ".join("?" for _ in cols)
                for r in rows:
                    vals = [
                        json.dumps(r[c], ensure_ascii=False) if isinstance(r[c], (dict, list)) else r[c]
                        for c in cols
                    ]
                    conn.execute(f'INSERT INTO "export_{tbl}" VALUES ({placeholders})', vals)
            conn.commit()
        finally:
            conn.close()
        return dest

    # ------------------------------------------------------------------
    # 4. Automatische regelmäßige Backups
    # ------------------------------------------------------------------

    def create_backup(self, max_backups: int = 168) -> dict[str, str]:
        """Erstellt ein vollständiges SQLite- und ZIP-Backup unter `Flashscore_Daten/Backups/`."""
        backup_dir = self.paths["Backups"]
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        sqlite_backup = backup_dir / f"Gesamtdatenbank_Backup_{ts}.sqlite"
        zip_backup = backup_dir / f"Flashscore_Komplettbackup_{ts}.zip"

        self.db.create_sqlite_backup(sqlite_backup)
        with zipfile.ZipFile(zip_backup, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(sqlite_backup, arcname=sqlite_backup.name)
            hist_log = self.paths["Historische_Daten"] / "aenderungsprotokoll_komplett.json"
            if hist_log.exists():
                zf.write(hist_log, arcname="Historische_Daten/aenderungsprotokoll_komplett.json")

        # Aufbewahrung älterer Backups bis `max_backups`
        for pattern in ("Gesamtdatenbank_Backup_*.sqlite", "Flashscore_Komplettbackup_*.zip"):
            existing = sorted(backup_dir.glob(pattern))
            while len(existing) > max_backups:
                oldest = existing.pop(0)
                oldest.unlink(missing_ok=True)

        self.db.log_system_event(
            "INFO",
            "FlashscoreExporter",
            "BACKUP_CREATED",
            f"Automatisches Backup erstellt: {zip_backup.name}",
            {"sqlite_backup": str(sqlite_backup), "zip_backup": str(zip_backup)},
        )
        return {"sqlite": str(sqlite_backup), "zip": str(zip_backup)}
