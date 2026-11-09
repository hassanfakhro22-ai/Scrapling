"""Persistente SQLite-Datenbank mit Änderungserkennung, Deduplizierung und Append-Only-Historie."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from flashscore.config import LEAGUES, LeagueConfig
from flashscore.models import (
    ChangeLogEntry,
    InjuryRecord,
    MatchEvent,
    MatchLineupSummary,
    MatchRecord,
    MatchStatItem,
    PlayerProfile,
    RefereeRecord,
    StadiumRecord,
    StandingRow,
    TeamRecord,
    compute_payload_hash,
    utc_now_iso,
)


def _slugify_id(prefix: str, text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return f"{prefix}_{cleaned or 'unknown'}"


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS leagues (
    league_key TEXT PRIMARY KEY,
    country TEXT NOT NULL,
    name TEXT NOT NULL,
    tier INTEGER NOT NULL,
    season TEXT NOT NULL,
    flashscore_slug TEXT NOT NULL,
    stage_id TEXT NOT NULL,
    base_url TEXT NOT NULL,
    last_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS teams (
    team_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    country TEXT NOT NULL,
    league_key TEXT NOT NULL,
    stadium_name TEXT,
    coach_name TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS standings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    league_key TEXT NOT NULL,
    season TEXT NOT NULL,
    table_type TEXT NOT NULL,
    rank INTEGER NOT NULL,
    team_id TEXT NOT NULL,
    team_name TEXT NOT NULL,
    played INTEGER NOT NULL,
    wins INTEGER NOT NULL,
    draws INTEGER NOT NULL,
    losses INTEGER NOT NULL,
    goals_for INTEGER NOT NULL,
    goals_against INTEGER NOT NULL,
    goal_diff INTEGER NOT NULL,
    points INTEGER NOT NULL,
    form_last_5 TEXT NOT NULL DEFAULT '',
    form_last_10 TEXT NOT NULL DEFAULT '',
    deduction_note TEXT,
    state_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(league_key, season, table_type, team_id)
);

CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    league_key TEXT NOT NULL,
    season TEXT NOT NULL,
    matchday TEXT,
    kickoff_raw TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    minute TEXT,
    home_team_id TEXT NOT NULL,
    home_team_name TEXT NOT NULL,
    away_team_id TEXT NOT NULL,
    away_team_name TEXT NOT NULL,
    home_score INTEGER,
    away_score INTEGER,
    ht_home_score INTEGER,
    ht_away_score INTEGER,
    second_half_home_score INTEGER,
    second_half_away_score INTEGER,
    referee_name TEXT,
    referee_country TEXT,
    stadium_name TEXT,
    stadium_city TEXT,
    stadium_capacity INTEGER,
    attendance INTEGER,
    match_url TEXT,
    score_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS match_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,
    event_key TEXT NOT NULL,
    half TEXT NOT NULL,
    minute TEXT NOT NULL,
    event_type TEXT NOT NULL,
    team_side TEXT NOT NULL,
    player_name TEXT NOT NULL,
    player_id TEXT,
    secondary_player_name TEXT,
    secondary_player_id TEXT,
    score_snapshot TEXT,
    detail TEXT,
    first_detected_at TEXT NOT NULL,
    UNIQUE(match_id, event_key)
);

CREATE TABLE IF NOT EXISTS match_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,
    period TEXT NOT NULL,
    category TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    home_raw TEXT NOT NULL,
    away_raw TEXT NOT NULL,
    home_value REAL,
    away_value REAL,
    updated_at TEXT NOT NULL,
    UNIQUE(match_id, period, metric_name)
);

CREATE TABLE IF NOT EXISTS lineups (
    match_id TEXT PRIMARY KEY,
    lineup_status TEXT NOT NULL,
    home_formation TEXT,
    away_formation TEXT,
    home_rating REAL,
    away_rating REAL,
    home_coach TEXT,
    away_coach TEXT,
    state_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lineup_players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,
    team_side TEXT NOT NULL,
    team_id TEXT NOT NULL,
    lineup_status TEXT NOT NULL,
    role TEXT NOT NULL,
    player_name TEXT NOT NULL,
    player_id TEXT,
    shirt_number INTEGER,
    position TEXT,
    nationality TEXT,
    is_captain INTEGER NOT NULL DEFAULT 0,
    is_goalkeeper INTEGER NOT NULL DEFAULT 0,
    rating REAL,
    sub_minute TEXT,
    replaced_player TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(match_id, team_side, role, player_name)
);

CREATE TABLE IF NOT EXISTS players (
    player_id TEXT PRIMARY KEY,
    player_name TEXT NOT NULL,
    slug TEXT NOT NULL,
    team_id TEXT,
    team_name TEXT,
    league_key TEXT,
    position TEXT,
    shirt_number INTEGER,
    nationality TEXT,
    birth_date TEXT,
    age INTEGER,
    market_value TEXT,
    contract_until TEXT,
    current_status TEXT NOT NULL DEFAULT 'fit',
    status_reason TEXT,
    expected_return TEXT,
    season_matches INTEGER,
    season_goals INTEGER,
    season_assists INTEGER,
    season_yellow_cards INTEGER,
    season_red_cards INTEGER,
    season_rating REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS injuries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    team_id TEXT NOT NULL,
    team_name TEXT NOT NULL,
    league_key TEXT NOT NULL,
    availability_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    expected_return TEXT,
    since_date TEXT,
    nationality TEXT,
    source_match_id TEXT,
    state_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(player_id, team_id)
);

CREATE TABLE IF NOT EXISTS referees (
    referee_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    country_code TEXT,
    matches_officiated INTEGER NOT NULL DEFAULT 0,
    yellow_cards_total INTEGER NOT NULL DEFAULT 0,
    red_cards_total INTEGER NOT NULL DEFAULT 0,
    penalties_awarded INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stadiums (
    stadium_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT,
    country TEXT,
    capacity INTEGER,
    home_teams TEXT,
    updated_at TEXT NOT NULL
);

-- =====================================================================
-- UNVERÄNDERLICHE HISTORISCHE TABELLEN (NIEMALS ÜBERSCHREIBEN!)
-- =====================================================================

CREATE TABLE IF NOT EXISTS change_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    league_key TEXT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    change_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    old_value_json TEXT,
    new_value_json TEXT NOT NULL,
    transition_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS standings_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    league_key TEXT NOT NULL,
    season TEXT NOT NULL,
    table_type TEXT NOT NULL,
    rank INTEGER NOT NULL,
    team_id TEXT NOT NULL,
    team_name TEXT NOT NULL,
    played INTEGER NOT NULL,
    wins INTEGER NOT NULL,
    draws INTEGER NOT NULL,
    losses INTEGER NOT NULL,
    goals_for INTEGER NOT NULL,
    goals_against INTEGER NOT NULL,
    goal_diff INTEGER NOT NULL,
    points INTEGER NOT NULL,
    form_last_5 TEXT NOT NULL,
    form_last_10 TEXT NOT NULL,
    deduction_note TEXT,
    state_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS score_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    match_id TEXT NOT NULL,
    league_key TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    minute TEXT,
    home_score INTEGER,
    away_score INTEGER,
    ht_home_score INTEGER,
    ht_away_score INTEGER,
    kickoff_raw TEXT NOT NULL,
    score_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stats_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    match_id TEXT NOT NULL,
    period TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    stats_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lineup_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    match_id TEXT NOT NULL,
    lineup_status TEXT NOT NULL,
    home_formation TEXT,
    away_formation TEXT,
    lineup_json TEXT NOT NULL,
    state_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS injury_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    player_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    team_id TEXT NOT NULL,
    team_name TEXT NOT NULL,
    league_key TEXT NOT NULL,
    availability_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    expected_return TEXT,
    since_date TEXT,
    state_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL,
    level TEXT NOT NULL,
    component TEXT NOT NULL,
    event_code TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_matches_league_status ON matches(league_key, status);
CREATE INDEX IF NOT EXISTS idx_standings_league_type ON standings(league_key, table_type, rank);
CREATE INDEX IF NOT EXISTS idx_injuries_league_team ON injuries(league_key, team_id);
CREATE INDEX IF NOT EXISTS idx_change_history_entity ON change_history(entity_type, entity_id, id);
CREATE INDEX IF NOT EXISTS idx_change_history_league ON change_history(league_key, detected_at);
"""


class FlashscoreDatabase:
    """Thread-sichere, dauerhafte SQLite-Datenbank mit vollständiger Historisierung."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA synchronous = NORMAL;")
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.execute("PRAGMA busy_timeout = 10000;")
            self._conn.executescript(SCHEMA_SQL)
            self._register_leagues(LEAGUES)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _register_leagues(self, leagues: tuple[LeagueConfig, ...]) -> None:
        for lg in leagues:
            self._conn.execute(
                """
                INSERT INTO leagues (league_key, country, name, tier, season, flashscore_slug, stage_id, base_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(league_key) DO UPDATE SET
                    country = excluded.country,
                    name = excluded.name,
                    tier = excluded.tier,
                    season = excluded.season,
                    flashscore_slug = excluded.flashscore_slug,
                    stage_id = excluded.stage_id,
                    base_url = excluded.base_url
                """,
                (
                    lg.league_key,
                    lg.country,
                    lg.name,
                    lg.tier,
                    lg.season,
                    lg.flashscore_slug,
                    lg.stage_id,
                    lg.base_url,
                ),
            )

    def mark_league_synced(self, league_key: str, synced_at: str | None = None) -> None:
        ts = synced_at or utc_now_iso()
        with self._lock:
            self._conn.execute(
                "UPDATE leagues SET last_synced_at = ? WHERE league_key = ?",
                (ts, league_key),
            )
            self._conn.commit()

    def log_system_event(
        self,
        level: str,
        component: str,
        event_code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO system_events (logged_at, level, component, event_code, message, details_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now_iso(),
                    level.upper(),
                    component,
                    event_code,
                    message,
                    json.dumps(details, ensure_ascii=False) if details else None,
                ),
            )
            self._conn.commit()

    def _record_change(
        self,
        *,
        detected_at: str,
        league_key: str | None,
        entity_type: str,
        entity_id: str,
        change_type: str,
        summary: str,
        old_value: dict[str, Any] | None,
        new_value: dict[str, Any],
    ) -> ChangeLogEntry | None:
        transition_hash = compute_payload_hash(
            {
                "et": entity_type,
                "eid": entity_id,
                "ct": change_type,
                "old": old_value,
                "new": new_value,
            }
        )
        last_row = self._conn.execute(
            """
            SELECT transition_hash FROM change_history
            WHERE entity_type = ? AND entity_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (entity_type, entity_id),
        ).fetchone()
        if last_row and last_row["transition_hash"] == transition_hash:
            return None

        self._conn.execute(
            """
            INSERT INTO change_history (
                detected_at, league_key, entity_type, entity_id,
                change_type, summary, old_value_json, new_value_json, transition_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                detected_at,
                league_key,
                entity_type,
                entity_id,
                change_type,
                summary,
                json.dumps(old_value, ensure_ascii=False) if old_value is not None else None,
                json.dumps(new_value, ensure_ascii=False),
                transition_hash,
            ),
        )
        return ChangeLogEntry(
            detected_at=detected_at,
            league_key=league_key,
            entity_type=entity_type,
            entity_id=entity_id,
            change_type=change_type,
            summary=summary,
            old_value=old_value,
            new_value=new_value,
        )

    # ------------------------------------------------------------------
    # Teams, Stadien & Schiedsrichter
    # ------------------------------------------------------------------

    def upsert_team(self, team: TeamRecord, detected_at: str | None = None) -> None:
        ts = detected_at or utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO teams (team_id, name, slug, country, league_key, stadium_name, coach_name, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id) DO UPDATE SET
                    name = excluded.name,
                    slug = CASE WHEN excluded.slug != '' THEN excluded.slug ELSE teams.slug END,
                    country = excluded.country,
                    league_key = excluded.league_key,
                    stadium_name = COALESCE(excluded.stadium_name, teams.stadium_name),
                    coach_name = COALESCE(excluded.coach_name, teams.coach_name),
                    updated_at = excluded.updated_at
                """,
                (
                    team.team_id,
                    team.name,
                    team.slug,
                    team.country,
                    team.league_key,
                    team.stadium_name,
                    team.coach_name,
                    ts,
                ),
            )
            self._conn.commit()

    def upsert_stadium(self, stadium: StadiumRecord, detected_at: str | None = None) -> None:
        ts = detected_at or utc_now_iso()
        with self._lock:
            existing = self._conn.execute(
                "SELECT home_teams, capacity, city, country FROM stadiums WHERE stadium_id = ?",
                (stadium.stadium_id,),
            ).fetchone()
            merged_teams = stadium.home_teams
            if existing and existing["home_teams"] and stadium.home_teams:
                known = {t.strip() for t in existing["home_teams"].split(",") if t.strip()}
                for t in stadium.home_teams.split(","):
                    if t.strip():
                        known.add(t.strip())
                merged_teams = ", ".join(sorted(known))

            self._conn.execute(
                """
                INSERT INTO stadiums (stadium_id, name, city, country, capacity, home_teams, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stadium_id) DO UPDATE SET
                    name = excluded.name,
                    city = COALESCE(excluded.city, stadiums.city),
                    country = COALESCE(excluded.country, stadiums.country),
                    capacity = COALESCE(excluded.capacity, stadiums.capacity),
                    home_teams = COALESCE(excluded.home_teams, stadiums.home_teams),
                    updated_at = excluded.updated_at
                """,
                (
                    stadium.stadium_id,
                    stadium.name,
                    stadium.city,
                    stadium.country,
                    stadium.capacity,
                    merged_teams,
                    ts,
                ),
            )
            self._conn.commit()

    def upsert_referee(self, referee: RefereeRecord, detected_at: str | None = None) -> None:
        ts = detected_at or utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO referees (
                    referee_id, name, country_code, matches_officiated,
                    yellow_cards_total, red_cards_total, penalties_awarded, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(referee_id) DO UPDATE SET
                    name = excluded.name,
                    country_code = COALESCE(excluded.country_code, referees.country_code),
                    matches_officiated = MAX(excluded.matches_officiated, referees.matches_officiated),
                    yellow_cards_total = MAX(excluded.yellow_cards_total, referees.yellow_cards_total),
                    red_cards_total = MAX(excluded.red_cards_total, referees.red_cards_total),
                    penalties_awarded = MAX(excluded.penalties_awarded, referees.penalties_awarded),
                    updated_at = excluded.updated_at
                """,
                (
                    referee.referee_id,
                    referee.name,
                    referee.country_code,
                    referee.matches_officiated,
                    referee.yellow_cards_total,
                    referee.red_cards_total,
                    referee.penalties_awarded,
                    ts,
                ),
            )
            self._conn.commit()

    def recalculate_referee_aggregates(self) -> None:
        """Berechnet geleitete Spiele, Karten und Elfmeter je Schiedsrichter aus den Spieldaten."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    m.referee_name,
                    MAX(m.referee_country) AS referee_country,
                    COUNT(DISTINCT m.match_id) AS matches_cnt,
                    SUM(CASE WHEN e.event_type = 'yellow_card' THEN 1 ELSE 0 END) AS yellow_cnt,
                    SUM(CASE WHEN e.event_type IN ('red_card', 'yellow_red_card') THEN 1 ELSE 0 END) AS red_cnt,
                    SUM(CASE WHEN e.event_type IN ('penalty_goal', 'penalty_missed') THEN 1 ELSE 0 END) AS pen_cnt
                FROM matches m
                LEFT JOIN match_events e ON e.match_id = m.match_id
                WHERE m.referee_name IS NOT NULL AND TRIM(m.referee_name) != ''
                GROUP BY m.referee_name
                """
            ).fetchall()
            ts = utc_now_iso()
            for r in rows:
                ref_id = _slugify_id("ref", r["referee_name"])
                self._conn.execute(
                    """
                    INSERT INTO referees (
                        referee_id, name, country_code, matches_officiated,
                        yellow_cards_total, red_cards_total, penalties_awarded, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(referee_id) DO UPDATE SET
                        country_code = COALESCE(excluded.country_code, referees.country_code),
                        matches_officiated = MAX(excluded.matches_officiated, referees.matches_officiated),
                        yellow_cards_total = MAX(excluded.yellow_cards_total, referees.yellow_cards_total),
                        red_cards_total = MAX(excluded.red_cards_total, referees.red_cards_total),
                        penalties_awarded = MAX(excluded.penalties_awarded, referees.penalties_awarded),
                        updated_at = excluded.updated_at
                    """,
                    (
                        ref_id,
                        r["referee_name"],
                        r["referee_country"],
                        int(r["matches_cnt"] or 0),
                        int(r["yellow_cnt"] or 0),
                        int(r["red_cnt"] or 0),
                        int(r["pen_cnt"] or 0),
                        ts,
                    ),
                )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Tabellen & Tabellenhistorie
    # ------------------------------------------------------------------

    def upsert_standings(
        self, rows: list[StandingRow], detected_at: str | None = None
    ) -> list[ChangeLogEntry]:
        ts = detected_at or utc_now_iso()
        changes: list[ChangeLogEntry] = []
        with self._lock:
            for row in rows:
                s_hash = row.state_fingerprint()
                existing = self._conn.execute(
                    """
                    SELECT rank, played, wins, draws, losses, goals_for, goals_against,
                           goal_diff, points, form_last_5, form_last_10, deduction_note, state_hash
                    FROM standings
                    WHERE league_key = ? AND season = ? AND table_type = ? AND team_id = ?
                    """,
                    (row.league_key, row.season, row.table_type, row.team_id),
                ).fetchone()

                if existing and existing["state_hash"] == s_hash:
                    continue

                old_dict = dict(existing) if existing else None
                if old_dict is not None:
                    old_dict.pop("state_hash", None)

                self._conn.execute(
                    """
                    INSERT INTO standings (
                        league_key, season, table_type, rank, team_id, team_name,
                        played, wins, draws, losses, goals_for, goals_against,
                        goal_diff, points, form_last_5, form_last_10, deduction_note,
                        state_hash, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(league_key, season, table_type, team_id) DO UPDATE SET
                        rank = excluded.rank,
                        team_name = excluded.team_name,
                        played = excluded.played,
                        wins = excluded.wins,
                        draws = excluded.draws,
                        losses = excluded.losses,
                        goals_for = excluded.goals_for,
                        goals_against = excluded.goals_against,
                        goal_diff = excluded.goal_diff,
                        points = excluded.points,
                        form_last_5 = excluded.form_last_5,
                        form_last_10 = excluded.form_last_10,
                        deduction_note = excluded.deduction_note,
                        state_hash = excluded.state_hash,
                        updated_at = excluded.updated_at
                    """,
                    (
                        row.league_key,
                        row.season,
                        row.table_type,
                        row.rank,
                        row.team_id,
                        row.team_name,
                        row.played,
                        row.wins,
                        row.draws,
                        row.losses,
                        row.goals_for,
                        row.goals_against,
                        row.goal_diff,
                        row.points,
                        row.form_last_5,
                        row.form_last_10,
                        row.deduction_note,
                        s_hash,
                        ts,
                    ),
                )

                self._conn.execute(
                    """
                    INSERT INTO standings_history (
                        detected_at, league_key, season, table_type, rank, team_id, team_name,
                        played, wins, draws, losses, goals_for, goals_against,
                        goal_diff, points, form_last_5, form_last_10, deduction_note, state_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        row.league_key,
                        row.season,
                        row.table_type,
                        row.rank,
                        row.team_id,
                        row.team_name,
                        row.played,
                        row.wins,
                        row.draws,
                        row.losses,
                        row.goals_for,
                        row.goals_against,
                        row.goal_diff,
                        row.points,
                        row.form_last_5,
                        row.form_last_10,
                        row.deduction_note,
                        s_hash,
                    ),
                )

                if old_dict is not None:
                    entry = self._record_change(
                        detected_at=ts,
                        league_key=row.league_key,
                        entity_type="standing",
                        entity_id=f"{row.league_key}:{row.table_type}:{row.team_id}",
                        change_type="STANDINGS_UPDATED",
                        summary=(
                            f"Tabelle ({row.table_type}) aktualisiert: {row.team_name} "
                            f"Platz {old_dict['rank']}->{row.rank}, Punkte {old_dict['points']}->{row.points}"
                        ),
                        old_value=old_dict,
                        new_value=row.to_dict(),
                    )
                    if entry:
                        changes.append(entry)

            self._conn.commit()
        return changes

    # ------------------------------------------------------------------
    # Spiele, Spielstände, Verschiebungen & Ereignisse
    # ------------------------------------------------------------------

    def upsert_match(
        self, match: MatchRecord, detected_at: str | None = None
    ) -> tuple[list[ChangeLogEntry], bool]:
        """Speichert oder aktualisiert ein Spiel und erkennt alle Status-/Tor-/Terminänderungen.

        Gibt `(changes, newly_finished)` zurück, damit nach jedem beendeten Spiel sofort
        die Ligentabelle neu synchronisiert wird.
        """
        ts = detected_at or utc_now_iso()
        changes: list[ChangeLogEntry] = []
        newly_finished = False
        s_hash = match.score_fingerprint()

        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM matches WHERE match_id = ?", (match.match_id,)
            ).fetchone()

            if existing:
                old_status = existing["status"]
                old_hs = existing["home_score"]
                old_as = existing["away_score"]
                old_phase = existing["phase"]
                old_kickoff = existing["kickoff_raw"]
                old_dict = {
                    k: existing[k]
                    for k in (
                        "status",
                        "phase",
                        "minute",
                        "home_score",
                        "away_score",
                        "ht_home_score",
                        "ht_away_score",
                        "kickoff_raw",
                    )
                }
                new_dict = {
                    "status": match.status,
                    "phase": match.phase,
                    "minute": match.minute,
                    "home_score": match.home_score,
                    "away_score": match.away_score,
                    "ht_home_score": match.ht_home_score,
                    "ht_away_score": match.ht_away_score,
                    "kickoff_raw": match.kickoff_raw,
                }

                if match.status == "postponed" and old_status != "postponed":
                    entry = self._record_change(
                        detected_at=ts,
                        league_key=match.league_key,
                        entity_type="schedule",
                        entity_id=match.match_id,
                        change_type="MATCH_POSTPONED",
                        summary=f"Spiel verschoben: {match.home_team_name} - {match.away_team_name}",
                        old_value=old_dict,
                        new_value=new_dict,
                    )
                    if entry:
                        changes.append(entry)
                elif match.status == "cancelled" and old_status != "cancelled":
                    entry = self._record_change(
                        detected_at=ts,
                        league_key=match.league_key,
                        entity_type="schedule",
                        entity_id=match.match_id,
                        change_type="MATCH_CANCELLED",
                        summary=f"Spiel abgesagt: {match.home_team_name} - {match.away_team_name}",
                        old_value=old_dict,
                        new_value=new_dict,
                    )
                    if entry:
                        changes.append(entry)
                elif old_kickoff != match.kickoff_raw and match.status == "scheduled":
                    entry = self._record_change(
                        detected_at=ts,
                        league_key=match.league_key,
                        entity_type="schedule",
                        entity_id=match.match_id,
                        change_type="KICKOFF_CHANGED",
                        summary=(
                            f"Anstoßzeit geändert ({match.home_team_name} - {match.away_team_name}): "
                            f"{old_kickoff} -> {match.kickoff_raw}"
                        ),
                        old_value=old_dict,
                        new_value=new_dict,
                    )
                    if entry:
                        changes.append(entry)

                if (old_hs, old_as) != (match.home_score, match.away_score) and (
                    match.home_score is not None or match.away_score is not None
                ):
                    entry = self._record_change(
                        detected_at=ts,
                        league_key=match.league_key,
                        entity_type="match",
                        entity_id=match.match_id,
                        change_type="SCORE_CHANGED",
                        summary=(
                            f"Spielstand geändert ({match.home_team_name} - {match.away_team_name}): "
                            f"{old_hs}:{old_as} -> {match.home_score}:{match.away_score} ({match.minute or match.phase})"
                        ),
                        old_value=old_dict,
                        new_value=new_dict,
                    )
                    if entry:
                        changes.append(entry)

                if match.status == "finished" and old_status != "finished":
                    newly_finished = True
                    entry = self._record_change(
                        detected_at=ts,
                        league_key=match.league_key,
                        entity_type="match",
                        entity_id=match.match_id,
                        change_type="MATCH_FINISHED",
                        summary=(
                            f"Spiel beendet: {match.home_team_name} {match.home_score}:{match.away_score} "
                            f"{match.away_team_name}"
                        ),
                        old_value=old_dict,
                        new_value=new_dict,
                    )
                    if entry:
                        changes.append(entry)
                elif old_phase != match.phase and match.status == "live":
                    entry = self._record_change(
                        detected_at=ts,
                        league_key=match.league_key,
                        entity_type="match",
                        entity_id=match.match_id,
                        change_type="PHASE_CHANGED",
                        summary=(
                            f"Spielphase ({match.home_team_name} - {match.away_team_name}): "
                            f"{old_phase} -> {match.phase}"
                        ),
                        old_value=old_dict,
                        new_value=new_dict,
                    )
                    if entry:
                        changes.append(entry)

            score_changed_for_hist = (not existing) or (existing["score_hash"] != s_hash)

            self._conn.execute(
                """
                INSERT INTO matches (
                    match_id, league_key, season, matchday, kickoff_raw, status, phase, minute,
                    home_team_id, home_team_name, away_team_id, away_team_name,
                    home_score, away_score, ht_home_score, ht_away_score,
                    second_half_home_score, second_half_away_score,
                    referee_name, referee_country, stadium_name, stadium_city,
                    stadium_capacity, attendance, match_url, score_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(match_id) DO UPDATE SET
                    matchday = COALESCE(excluded.matchday, matches.matchday),
                    kickoff_raw = excluded.kickoff_raw,
                    status = excluded.status,
                    phase = excluded.phase,
                    minute = excluded.minute,
                    home_team_name = excluded.home_team_name,
                    away_team_name = excluded.away_team_name,
                    home_score = COALESCE(excluded.home_score, matches.home_score),
                    away_score = COALESCE(excluded.away_score, matches.away_score),
                    ht_home_score = COALESCE(excluded.ht_home_score, matches.ht_home_score),
                    ht_away_score = COALESCE(excluded.ht_away_score, matches.ht_away_score),
                    second_half_home_score = COALESCE(excluded.second_half_home_score, matches.second_half_home_score),
                    second_half_away_score = COALESCE(excluded.second_half_away_score, matches.second_half_away_score),
                    referee_name = COALESCE(excluded.referee_name, matches.referee_name),
                    referee_country = COALESCE(excluded.referee_country, matches.referee_country),
                    stadium_name = COALESCE(excluded.stadium_name, matches.stadium_name),
                    stadium_city = COALESCE(excluded.stadium_city, matches.stadium_city),
                    stadium_capacity = COALESCE(excluded.stadium_capacity, matches.stadium_capacity),
                    attendance = COALESCE(excluded.attendance, matches.attendance),
                    match_url = COALESCE(excluded.match_url, matches.match_url),
                    score_hash = excluded.score_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    match.match_id,
                    match.league_key,
                    match.season,
                    match.matchday,
                    match.kickoff_raw,
                    match.status,
                    match.phase,
                    match.minute,
                    match.home_team_id,
                    match.home_team_name,
                    match.away_team_id,
                    match.away_team_name,
                    match.home_score,
                    match.away_score,
                    match.ht_home_score,
                    match.ht_away_score,
                    match.second_half_home_score,
                    match.second_half_away_score,
                    match.referee_name,
                    match.referee_country,
                    match.stadium_name,
                    match.stadium_city,
                    match.stadium_capacity,
                    match.attendance,
                    match.match_url,
                    s_hash,
                    ts,
                ),
            )

            if score_changed_for_hist:
                self._conn.execute(
                    """
                    INSERT INTO score_history (
                        detected_at, match_id, league_key, status, phase, minute,
                        home_score, away_score, ht_home_score, ht_away_score, kickoff_raw, score_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        match.match_id,
                        match.league_key,
                        match.status,
                        match.phase,
                        match.minute,
                        match.home_score,
                        match.away_score,
                        match.ht_home_score,
                        match.ht_away_score,
                        match.kickoff_raw,
                        s_hash,
                    ),
                )

            if match.stadium_name:
                lg_row = self._conn.execute(
                    "SELECT country FROM leagues WHERE league_key = ?", (match.league_key,)
                ).fetchone()
                country = lg_row["country"] if lg_row else ""
                st_id = _slugify_id("stad", f"{match.stadium_name}_{match.stadium_city or ''}")
                self._conn.execute(
                    """
                    INSERT INTO stadiums (stadium_id, name, city, country, capacity, home_teams, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stadium_id) DO UPDATE SET
                        city = COALESCE(excluded.city, stadiums.city),
                        country = COALESCE(excluded.country, stadiums.country),
                        capacity = COALESCE(excluded.capacity, stadiums.capacity),
                        updated_at = excluded.updated_at
                    """,
                    (
                        st_id,
                        match.stadium_name,
                        match.stadium_city,
                        country,
                        match.stadium_capacity,
                        match.home_team_name,
                        ts,
                    ),
                )

            if match.referee_name:
                ref_id = _slugify_id("ref", match.referee_name)
                self._conn.execute(
                    """
                    INSERT INTO referees (referee_id, name, country_code, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(referee_id) DO UPDATE SET
                        country_code = COALESCE(excluded.country_code, referees.country_code),
                        updated_at = excluded.updated_at
                    """,
                    (ref_id, match.referee_name, match.referee_country, ts),
                )

            self._conn.commit()
        return changes, newly_finished

    def insert_match_events(
        self,
        match_id: str,
        league_key: str,
        events: list[MatchEvent],
        detected_at: str | None = None,
        emit_change_log: bool = True,
    ) -> list[ChangeLogEntry]:
        ts = detected_at or utc_now_iso()
        changes: list[ChangeLogEntry] = []
        event_type_to_change = {
            "goal": "GOAL",
            "penalty_goal": "PENALTY",
            "own_goal": "GOAL",
            "penalty_missed": "PENALTY",
            "yellow_card": "CARD_YELLOW",
            "yellow_red_card": "CARD_YELLOW_RED",
            "red_card": "CARD_RED",
            "substitution": "SUBSTITUTION",
            "var": "VAR_DECISION",
        }
        with self._lock:
            for ev in events:
                ekey = ev.event_key
                exists = self._conn.execute(
                    "SELECT 1 FROM match_events WHERE match_id = ? AND event_key = ?",
                    (match_id, ekey),
                ).fetchone()
                if exists:
                    continue

                self._conn.execute(
                    """
                    INSERT INTO match_events (
                        match_id, event_key, half, minute, event_type, team_side,
                        player_name, player_id, secondary_player_name, secondary_player_id,
                        score_snapshot, detail, first_detected_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        match_id,
                        ekey,
                        ev.half,
                        ev.minute,
                        ev.event_type,
                        ev.team_side,
                        ev.player_name,
                        ev.player_id,
                        ev.secondary_player_name,
                        ev.secondary_player_id,
                        ev.score_snapshot,
                        ev.detail,
                        ts,
                    ),
                )

                if emit_change_log:
                    c_type = event_type_to_change.get(ev.event_type, "MATCH_EVENT")
                    entry = self._record_change(
                        detected_at=ts,
                        league_key=league_key,
                        entity_type="match_event",
                        entity_id=f"{match_id}:{ekey}",
                        change_type=c_type,
                        summary=(
                            f"Spielereignis ({match_id}) {ev.minute} [{ev.event_type}]: "
                            f"{ev.player_name}"
                            + (f" ({ev.secondary_player_name})" if ev.secondary_player_name else "")
                            + (f" [{ev.score_snapshot}]" if ev.score_snapshot else "")
                        ),
                        old_value=None,
                        new_value=ev.to_dict(),
                    )
                    if entry:
                        changes.append(entry)
            self._conn.commit()
        return changes

    # ------------------------------------------------------------------
    # Spielstatistiken & Statistik-Historie
    # ------------------------------------------------------------------

    def upsert_match_stats(
        self,
        match_id: str,
        league_key: str,
        period: str,
        stats: list[MatchStatItem],
        detected_at: str | None = None,
    ) -> list[ChangeLogEntry]:
        if not stats:
            return []
        ts = detected_at or utc_now_iso()
        changes: list[ChangeLogEntry] = []
        payload = [s.to_dict() for s in stats]
        s_hash = compute_payload_hash(payload)

        with self._lock:
            last_hist = self._conn.execute(
                """
                SELECT stats_hash, stats_json FROM stats_history
                WHERE match_id = ? AND period = ?
                ORDER BY id DESC LIMIT 1
                """,
                (match_id, period),
            ).fetchone()

            for item in stats:
                self._conn.execute(
                    """
                    INSERT INTO match_stats (
                        match_id, period, category, metric_name,
                        home_raw, away_raw, home_value, away_value, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(match_id, period, metric_name) DO UPDATE SET
                        category = excluded.category,
                        home_raw = excluded.home_raw,
                        away_raw = excluded.away_raw,
                        home_value = excluded.home_value,
                        away_value = excluded.away_value,
                        updated_at = excluded.updated_at
                    """,
                    (
                        match_id,
                        period,
                        item.category,
                        item.metric_name,
                        item.home_raw,
                        item.away_raw,
                        item.home_value,
                        item.away_value,
                        ts,
                    ),
                )

            if not last_hist or last_hist["stats_hash"] != s_hash:
                self._conn.execute(
                    """
                    INSERT INTO stats_history (detected_at, match_id, period, stats_json, stats_hash)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (ts, match_id, period, json.dumps(payload, ensure_ascii=False), s_hash),
                )
                if last_hist is not None:
                    entry = self._record_change(
                        detected_at=ts,
                        league_key=league_key,
                        entity_type="stats",
                        entity_id=f"{match_id}:{period}",
                        change_type="STATS_UPDATED",
                        summary=f"Spielstatistiken aktualisiert ({match_id}, Abschnitt: {period}, {len(stats)} Werte)",
                        old_value={"stats_count": len(json.loads(last_hist["stats_json"]))},
                        new_value={"stats_count": len(stats), "period": period},
                    )
                    if entry:
                        changes.append(entry)

            self._conn.commit()
        return changes

    # ------------------------------------------------------------------
    # Aufstellungen, Startelf-Änderungen & Kaderhistorie
    # ------------------------------------------------------------------

    def upsert_lineup(
        self,
        league_key: str,
        lineup: MatchLineupSummary,
        detected_at: str | None = None,
    ) -> list[ChangeLogEntry]:
        ts = detected_at or utc_now_iso()
        changes: list[ChangeLogEntry] = []
        s_hash = lineup.state_fingerprint()

        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM lineups WHERE match_id = ?", (lineup.match_id,)
            ).fetchone()

            if existing and existing["state_hash"] == s_hash:
                return []

            old_players_rows = self._conn.execute(
                "SELECT * FROM lineup_players WHERE match_id = ?", (lineup.match_id,)
            ).fetchall()
            old_starters = {
                (r["team_side"], r["player_name"])
                for r in old_players_rows
                if r["role"] == "starter"
            }
            new_starters = {
                (p.team_side, p.player_name)
                for p in lineup.players
                if p.role == "starter"
            }

            if existing:
                old_status = existing["lineup_status"]
                if old_status == "probable" and lineup.lineup_status == "confirmed":
                    entry = self._record_change(
                        detected_at=ts,
                        league_key=league_key,
                        entity_type="lineup",
                        entity_id=lineup.match_id,
                        change_type="LINEUP_CONFIRMED_PUBLISHED",
                        summary=(
                            f"Offizielle Startaufstellung veröffentlicht ({lineup.match_id}): "
                            f"{lineup.home_formation or '?'} vs {lineup.away_formation or '?'}"
                        ),
                        old_value={"lineup_status": old_status, "starters": sorted(p for _, p in old_starters)},
                        new_value={
                            "lineup_status": lineup.lineup_status,
                            "home_formation": lineup.home_formation,
                            "away_formation": lineup.away_formation,
                            "starters": sorted(p for _, p in new_starters),
                        },
                    )
                    if entry:
                        changes.append(entry)
                elif old_starters != new_starters and old_starters:
                    dropped = sorted(p for _, p in (old_starters - new_starters))
                    added = sorted(p for _, p in (new_starters - old_starters))
                    entry = self._record_change(
                        detected_at=ts,
                        league_key=league_key,
                        entity_type="lineup",
                        entity_id=lineup.match_id,
                        change_type="STARTING_XI_CHANGED",
                        summary=(
                            f"Kurzfristige Startelf-Änderung ({lineup.match_id}): "
                            f"Raus: {', '.join(dropped) or '-'} | Rein: {', '.join(added) or '-'}"
                        ),
                        old_value={"starters": sorted(p for _, p in old_starters)},
                        new_value={"starters": sorted(p for _, p in new_starters), "dropped": dropped, "added": added},
                    )
                    if entry:
                        changes.append(entry)

            self._conn.execute(
                """
                INSERT INTO lineups (
                    match_id, lineup_status, home_formation, away_formation,
                    home_rating, away_rating, home_coach, away_coach, state_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(match_id) DO UPDATE SET
                    lineup_status = excluded.lineup_status,
                    home_formation = COALESCE(excluded.home_formation, lineups.home_formation),
                    away_formation = COALESCE(excluded.away_formation, lineups.away_formation),
                    home_rating = COALESCE(excluded.home_rating, lineups.home_rating),
                    away_rating = COALESCE(excluded.away_rating, lineups.away_rating),
                    home_coach = COALESCE(excluded.home_coach, lineups.home_coach),
                    away_coach = COALESCE(excluded.away_coach, lineups.away_coach),
                    state_hash = excluded.state_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    lineup.match_id,
                    lineup.lineup_status,
                    lineup.home_formation,
                    lineup.away_formation,
                    lineup.home_rating,
                    lineup.away_rating,
                    lineup.home_coach,
                    lineup.away_coach,
                    s_hash,
                    ts,
                ),
            )

            self._conn.execute("DELETE FROM lineup_players WHERE match_id = ?", (lineup.match_id,))
            for p in lineup.players:
                self._conn.execute(
                    """
                    INSERT INTO lineup_players (
                        match_id, team_side, team_id, lineup_status, role, player_name,
                        player_id, shirt_number, position, nationality,
                        is_captain, is_goalkeeper, rating, sub_minute, replaced_player, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lineup.match_id,
                        p.team_side,
                        p.team_id,
                        lineup.lineup_status,
                        p.role,
                        p.player_name,
                        p.player_id,
                        p.shirt_number,
                        p.position,
                        p.nationality,
                        1 if p.is_captain else 0,
                        1 if p.is_goalkeeper else 0,
                        p.rating,
                        p.sub_minute,
                        p.replaced_player,
                        ts,
                    ),
                )

                # Registriere Spieler ebenfalls im Spieler-Stammverzeichnis
                pid = p.player_id or _slugify_id("pl", f"{p.team_id}_{p.player_name}")
                self._conn.execute(
                    """
                    INSERT INTO players (
                        player_id, player_name, slug, team_id, league_key,
                        position, shirt_number, nationality, current_status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'fit', ?)
                    ON CONFLICT(player_id) DO UPDATE SET
                        team_id = COALESCE(excluded.team_id, players.team_id),
                        league_key = COALESCE(excluded.league_key, players.league_key),
                        position = COALESCE(excluded.position, players.position),
                        shirt_number = COALESCE(excluded.shirt_number, players.shirt_number),
                        nationality = COALESCE(excluded.nationality, players.nationality),
                        updated_at = excluded.updated_at
                    """,
                    (
                        pid,
                        p.player_name,
                        _slugify_id("s", p.player_name),
                        p.team_id,
                        league_key,
                        p.position or ("Torwart" if p.is_goalkeeper else None),
                        p.shirt_number,
                        p.nationality,
                        ts,
                    ),
                )

            self._conn.execute(
                """
                INSERT INTO lineup_history (
                    detected_at, match_id, lineup_status, home_formation, away_formation, lineup_json, state_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    lineup.match_id,
                    lineup.lineup_status,
                    lineup.home_formation,
                    lineup.away_formation,
                    json.dumps(lineup.to_dict(), ensure_ascii=False),
                    s_hash,
                ),
            )
            self._conn.commit()
        return changes

    # ------------------------------------------------------------------
    # Spielerprofile, Verletzungen, Sperren & Ausfallhistorie
    # ------------------------------------------------------------------

    def upsert_player_profile(
        self, profile: PlayerProfile, detected_at: str | None = None
    ) -> None:
        ts = detected_at or utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO players (
                    player_id, player_name, slug, team_id, team_name, league_key,
                    position, shirt_number, nationality, birth_date, age,
                    market_value, contract_until, current_status, status_reason,
                    expected_return, season_matches, season_goals, season_assists,
                    season_yellow_cards, season_red_cards, season_rating, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(player_id) DO UPDATE SET
                    player_name = excluded.player_name,
                    slug = CASE WHEN excluded.slug != '' THEN excluded.slug ELSE players.slug END,
                    team_id = COALESCE(excluded.team_id, players.team_id),
                    team_name = COALESCE(excluded.team_name, players.team_name),
                    league_key = COALESCE(excluded.league_key, players.league_key),
                    position = COALESCE(excluded.position, players.position),
                    shirt_number = COALESCE(excluded.shirt_number, players.shirt_number),
                    nationality = COALESCE(excluded.nationality, players.nationality),
                    birth_date = COALESCE(excluded.birth_date, players.birth_date),
                    age = COALESCE(excluded.age, players.age),
                    market_value = COALESCE(excluded.market_value, players.market_value),
                    contract_until = COALESCE(excluded.contract_until, players.contract_until),
                    current_status = excluded.current_status,
                    status_reason = COALESCE(excluded.status_reason, players.status_reason),
                    expected_return = COALESCE(excluded.expected_return, players.expected_return),
                    season_matches = COALESCE(excluded.season_matches, players.season_matches),
                    season_goals = COALESCE(excluded.season_goals, players.season_goals),
                    season_assists = COALESCE(excluded.season_assists, players.season_assists),
                    season_yellow_cards = COALESCE(excluded.season_yellow_cards, players.season_yellow_cards),
                    season_red_cards = COALESCE(excluded.season_red_cards, players.season_red_cards),
                    season_rating = COALESCE(excluded.season_rating, players.season_rating),
                    updated_at = excluded.updated_at
                """,
                (
                    profile.player_id,
                    profile.player_name,
                    profile.slug,
                    profile.team_id,
                    profile.team_name,
                    profile.league_key,
                    profile.position,
                    profile.shirt_number,
                    profile.nationality,
                    profile.birth_date,
                    profile.age,
                    profile.market_value,
                    profile.contract_until,
                    profile.current_status,
                    profile.status_reason,
                    profile.expected_return,
                    profile.season_matches,
                    profile.season_goals,
                    profile.season_assists,
                    profile.season_yellow_cards,
                    profile.season_red_cards,
                    profile.season_rating,
                    ts,
                ),
            )
            self._conn.commit()

    def upsert_injury(
        self,
        injury: InjuryRecord,
        detected_at: str | None = None,
        is_prematch_late_dropout: bool = False,
    ) -> list[ChangeLogEntry]:
        ts = detected_at or utc_now_iso()
        changes: list[ChangeLogEntry] = []
        s_hash = injury.state_fingerprint()

        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM injuries WHERE player_id = ? AND team_id = ?",
                (injury.player_id, injury.team_id),
            ).fetchone()

            if existing and existing["state_hash"] == s_hash:
                return []

            old_dict = (
                {
                    "availability_status": existing["availability_status"],
                    "reason": existing["reason"],
                    "expected_return": existing["expected_return"],
                }
                if existing
                else None
            )

            if existing is None:
                c_type = "LATE_DROPOUT_PREMATCH" if is_prematch_late_dropout else "INJURY_NEW"
                summary = (
                    f"{'Kurzfristiger Ausfall vor Spielbeginn' if is_prematch_late_dropout else 'Neuer Ausfall/Verletzung'}: "
                    f"{injury.player_name} ({injury.team_name}) – {injury.availability_status} ({injury.reason})"
                )
            elif injury.availability_status == "genesen":
                c_type = "INJURY_RESOLVED"
                summary = f"Spieler zurückgekehrt/genesen: {injury.player_name} ({injury.team_name})"
            else:
                c_type = "INJURY_STATUS_CHANGED"
                summary = (
                    f"Ausfallstatus geändert: {injury.player_name} ({injury.team_name}) – "
                    f"{existing['availability_status']} ({existing['reason']}) -> "
                    f"{injury.availability_status} ({injury.reason})"
                )

            self._conn.execute(
                """
                INSERT INTO injuries (
                    player_id, player_name, team_id, team_name, league_key,
                    availability_status, reason, expected_return, since_date,
                    nationality, source_match_id, state_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(player_id, team_id) DO UPDATE SET
                    player_name = excluded.player_name,
                    team_name = excluded.team_name,
                    league_key = excluded.league_key,
                    availability_status = excluded.availability_status,
                    reason = excluded.reason,
                    expected_return = COALESCE(excluded.expected_return, injuries.expected_return),
                    since_date = COALESCE(excluded.since_date, injuries.since_date),
                    nationality = COALESCE(excluded.nationality, injuries.nationality),
                    source_match_id = COALESCE(excluded.source_match_id, injuries.source_match_id),
                    state_hash = excluded.state_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    injury.player_id,
                    injury.player_name,
                    injury.team_id,
                    injury.team_name,
                    injury.league_key,
                    injury.availability_status,
                    injury.reason,
                    injury.expected_return,
                    injury.since_date,
                    injury.nationality,
                    injury.source_match_id,
                    s_hash,
                    ts,
                ),
            )

            self._conn.execute(
                """
                INSERT INTO injury_history (
                    detected_at, player_id, player_name, team_id, team_name, league_key,
                    availability_status, reason, expected_return, since_date, state_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    injury.player_id,
                    injury.player_name,
                    injury.team_id,
                    injury.team_name,
                    injury.league_key,
                    injury.availability_status,
                    injury.reason,
                    injury.expected_return,
                    injury.since_date,
                    s_hash,
                ),
            )

            self._conn.execute(
                """
                INSERT INTO players (
                    player_id, player_name, slug, team_id, team_name, league_key,
                    nationality, current_status, status_reason, expected_return, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(player_id) DO UPDATE SET
                    team_id = excluded.team_id,
                    team_name = excluded.team_name,
                    league_key = excluded.league_key,
                    nationality = COALESCE(excluded.nationality, players.nationality),
                    current_status = excluded.current_status,
                    status_reason = excluded.status_reason,
                    expected_return = COALESCE(excluded.expected_return, players.expected_return),
                    updated_at = excluded.updated_at
                """,
                (
                    injury.player_id,
                    injury.player_name,
                    _slugify_id("s", injury.player_name),
                    injury.team_id,
                    injury.team_name,
                    injury.league_key,
                    injury.nationality,
                    injury.availability_status,
                    injury.reason,
                    injury.expected_return,
                    ts,
                ),
            )

            entry = self._record_change(
                detected_at=ts,
                league_key=injury.league_key,
                entity_type="injury",
                entity_id=f"{injury.team_id}:{injury.player_id}",
                change_type=c_type,
                summary=summary,
                old_value=old_dict,
                new_value=injury.to_dict(),
            )
            if entry:
                changes.append(entry)

            self._conn.commit()
        return changes

    # ------------------------------------------------------------------
    # Abfrage-Methoden für Exporte & Monitoring
    # ------------------------------------------------------------------

    def fetch_dicts(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def get_summary_counts(self) -> dict[str, int]:
        tables = (
            "leagues",
            "teams",
            "standings",
            "matches",
            "match_events",
            "match_stats",
            "lineups",
            "lineup_players",
            "players",
            "injuries",
            "referees",
            "stadiums",
            "change_history",
            "standings_history",
            "score_history",
            "stats_history",
            "lineup_history",
            "injury_history",
            "system_events",
        )
        counts: dict[str, int] = {}
        with self._lock:
            for tbl in tables:
                row = self._conn.execute(f"SELECT COUNT(*) AS c FROM {tbl}").fetchone()
                counts[tbl] = int(row["c"]) if row else 0
        return counts

    def create_sqlite_backup(self, target_path: str | Path) -> Path:
        """Erstellt ein konsistentes Online-Backup der gesamten SQLite-Datenbank."""
        dest = Path(target_path).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest != self.db_path:
            dest.unlink()
        with self._lock:
            backup_conn = sqlite3.connect(str(dest))
            try:
                self._conn.backup(backup_conn)
            finally:
                backup_conn.close()
        return dest
