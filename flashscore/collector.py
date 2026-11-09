"""Datenerfassungs-Koordinator für alle 10 Ligen, Live-Spiele, Statistiken, Kader & Verletzungen."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from flashscore.client import HtmlTransport
from flashscore.config import FLASHSCORE_BASE_URL, LEAGUE_BY_KEY, LEAGUES, LeagueConfig
from flashscore.database import FlashscoreDatabase
from flashscore.models import MatchRecord
from flashscore.parsers import (
    parse_league_page,
    parse_match_detail_page,
    parse_match_lineups_page,
    parse_match_stats_page,
    parse_player_profile_and_injuries,
)
from flashscore.standings_engine import compute_tables_from_matches, enrich_standing_suites

logger = logging.getLogger("flashscore.collector")


def build_match_subroute_url(match_url: str, subroute: str, match_id: str) -> str:
    """Baut Flashscore-2026-Subrouten wie `/bericht/stats/?mid=...` oder `/bericht/kader/?mid=...`."""
    parsed = urlparse(match_url)
    base_path = parsed.path.rstrip("/")
    for suffix in ("/bericht/stats", "/bericht/kader", "/bericht/spieler-stats", "/tabelle"):
        if base_path.endswith(suffix):
            base_path = base_path[: -len(suffix)]
    sub = subroute.strip("/")
    scheme_netloc = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else FLASHSCORE_BASE_URL
    return f"{scheme_netloc}{base_path}/{sub}/?mid={match_id}"


def is_within_prematch_window(kickoff_raw: str, window_minutes: int = 90, now_utc: datetime | None = None) -> bool:
    """Prüft, ob ein geplantes Spiel innerhalb des Vorlauf-Fensters vor Anpfiff liegt."""
    raw = kickoff_raw.strip()
    if not raw:
        return False
    ref = now_utc or datetime.now(timezone.utc)
    try:
        if len(raw) == 16 and raw[2] == "." and raw[5] == ".":
            dt = datetime.strptime(raw, "%d.%m.%Y %H:%M").replace(tzinfo=timezone.utc)
        elif len(raw) == 12 and raw[2] == "." and raw[5] == ".":
            dt = datetime.strptime(f"{raw[:6]}{ref.year} {raw[7:]}", "%d.%m.%Y %H:%M").replace(tzinfo=timezone.utc)
        else:
            return False
        delta_minutes = (dt - ref).total_seconds() / 60.0
        return -15.0 <= delta_minutes <= float(window_minutes)
    except ValueError:
        return False


class FlashscoreCollector:
    """Orchestriert Abruf, Parsing, Änderungserkennung und Speicherung über alle 10 Zielligen."""

    def __init__(self, db: FlashscoreDatabase, client: HtmlTransport) -> None:
        self.db = db
        self.client = client

    def sync_all_leagues(self, leagues: tuple[LeagueConfig, ...] = LEAGUES) -> dict[str, int]:
        """Synchronisiert Spielpläne, Ergebnisse und Tabellen aller 10 Ligen."""
        stats = {"leagues": 0, "matches": 0, "changes": 0}
        for lg in leagues:
            m_cnt, c_cnt = self.sync_league(lg)
            stats["leagues"] += 1
            stats["matches"] += m_cnt
            stats["changes"] += c_cnt
        self.db.recalculate_referee_aggregates()
        return stats

    def sync_league(self, league: LeagueConfig) -> tuple[int, int]:
        """Synchronisiert Übersicht, Ergebnisse, Spielplan und Tabellen einer Liga."""
        total_matches = 0
        total_changes = 0
        overall_standings = []
        anchor_match_url: str | None = None
        anchor_match_id: str | None = None

        for url in (league.base_url, league.results_url, league.fixtures_url):
            try:
                html = self.client.fetch_html(url)
            except Exception as exc:
                logger.warning("Liga-Abruf fehlgeschlagen (%s - %s): %s", league.name, url, exc)
                continue

            parsed = parse_league_page(html, league)
            for team in parsed.teams:
                self.db.upsert_team(team)

            for match in parsed.matches:
                total_matches += 1
                if anchor_match_url is None and match.match_url:
                    anchor_match_url = match.match_url
                    anchor_match_id = match.match_id
                m_changes, _ = self.db.upsert_match(match)
                total_changes += len(m_changes)

            if parsed.standings and not overall_standings:
                overall_standings = parsed.standings

        # Falls Ligaseite keine SSR-Tabelle enthielt, lade sie über eine Spiel-Detailseite
        if not overall_standings and anchor_match_url and anchor_match_id:
            try:
                detail_html = self.client.fetch_html(anchor_match_url)
                detail = parse_match_detail_page(detail_html, league, anchor_match_id, anchor_match_url)
                for team in detail.teams:
                    self.db.upsert_team(team)
                if detail.match:
                    mc, _ = self.db.upsert_match(detail.match)
                    total_changes += len(mc)
                if detail.events:
                    ec = self.db.insert_match_events(anchor_match_id, league.league_key, detail.events)
                    total_changes += len(ec)
                for inj in detail.injuries:
                    ic = self.db.upsert_injury(inj)
                    total_changes += len(ic)
                overall_standings = detail.embedded_standings
            except Exception as exc:
                logger.warning("Tabellen-Fallback für %s fehlgeschlagen: %s", league.name, exc)

        total_changes += self.recompute_and_store_standings(league, overall_standings)
        self.db.mark_league_synced(league.league_key)
        return total_matches, total_changes

    def recompute_and_store_standings(
        self,
        league: LeagueConfig,
        explicit_overall: list | None = None,
    ) -> int:
        """Berechnet und speichert Gesamt-, Heim-, Auswärts-, Form-5- und Form-10-Tabellen."""
        finished_rows = self.db.fetch_dicts(
            "SELECT * FROM matches WHERE league_key = ? AND status = 'finished'",
            (league.league_key,),
        )
        finished_matches = [
            MatchRecord(
                match_id=r["match_id"],
                league_key=r["league_key"],
                season=r["season"],
                matchday=r["matchday"],
                kickoff_raw=r["kickoff_raw"],
                status=r["status"],
                phase=r["phase"],
                minute=r["minute"],
                home_team_id=r["home_team_id"],
                home_team_name=r["home_team_name"],
                away_team_id=r["away_team_id"],
                away_team_name=r["away_team_name"],
                home_score=r["home_score"],
                away_score=r["away_score"],
                ht_home_score=r["ht_home_score"],
                ht_away_score=r["ht_away_score"],
            )
            for r in finished_rows
        ]

        if explicit_overall:
            suites = enrich_standing_suites(explicit_overall, finished_matches)
        elif finished_matches:
            team_rows = self.db.fetch_dicts(
                "SELECT team_id, name FROM teams WHERE league_key = ?", (league.league_key,)
            )
            known_teams = {r["team_id"]: r["name"] for r in team_rows}
            suites = compute_tables_from_matches(league.league_key, league.season, finished_matches, known_teams)
        else:
            return 0

        change_cnt = 0
        for tbl_rows in suites.values():
            if tbl_rows:
                change_cnt += len(self.db.upsert_standings(tbl_rows))
        return change_cnt

    def poll_live_scores_and_events(
        self, leagues: tuple[LeagueConfig, ...] = LEAGUES
    ) -> int:
        """15-Sekunden-Zyklus: Aktuelle Spielstände, Minuten, Spielphasen & alle Live-Events."""
        placeholders = ",".join("?" for _ in leagues)
        lkeys = tuple(lg.league_key for lg in leagues)
        candidates = self.db.fetch_dicts(
            f"""
            SELECT match_id, league_key, match_url, kickoff_raw, status
            FROM matches
            WHERE league_key IN ({placeholders})
              AND (status = 'live' OR status = 'scheduled')
            """,
            lkeys,
        )

        changes_total = 0
        leagues_needing_table_refresh: set[str] = set()

        for row in candidates:
            if row["status"] == "scheduled" and not is_within_prematch_window(row["kickoff_raw"], window_minutes=15):
                continue
            murl = row["match_url"]
            if not murl:
                continue
            lg = LEAGUE_BY_KEY[row["league_key"]]
            try:
                html = self.client.fetch_html(murl)
                detail = parse_match_detail_page(html, lg, row["match_id"], murl)
                if detail.match:
                    mc, newly_finished = self.db.upsert_match(detail.match)
                    changes_total += len(mc)
                    if newly_finished:
                        leagues_needing_table_refresh.add(lg.league_key)
                if detail.events:
                    ec = self.db.insert_match_events(row["match_id"], lg.league_key, detail.events)
                    changes_total += len(ec)
            except Exception as exc:
                logger.warning("Live-Score-Abruf fehlgeschlagen (%s): %s", row["match_id"], exc)

        # Vorgabe: Tabellen-Aktualisierung sofort nach jedem beendeten Spiel
        for lkey in leagues_needing_table_refresh:
            lg = LEAGUE_BY_KEY[lkey]
            logger.info("Spiel beendet in %s -> Aktualisiere Tabellen sofort.", lg.name)
            changes_total += self.recompute_and_store_standings(lg)
            self.db.recalculate_referee_aggregates()

        return changes_total

    def poll_live_statistics(
        self, leagues: tuple[LeagueConfig, ...] = LEAGUES
    ) -> int:
        """30-Sekunden-Zyklus: Live- & Spielstatistiken (Ballbesitz, Schüsse, xG, Pässe, Zweikämpfe usw.)."""
        placeholders = ",".join("?" for _ in leagues)
        lkeys = tuple(lg.league_key for lg in leagues)
        live_rows = self.db.fetch_dicts(
            f"""
            SELECT match_id, league_key, match_url
            FROM matches
            WHERE league_key IN ({placeholders}) AND status = 'live'
            """,
            lkeys,
        )
        changes_total = 0
        for row in live_rows:
            if not row["match_url"]:
                continue
            stats_url = build_match_subroute_url(row["match_url"], "bericht/stats", row["match_id"])
            try:
                html = self.client.fetch_html(stats_url)
                items = parse_match_stats_page(html, row["match_id"], period="gesamt")
                sc = self.db.upsert_match_stats(row["match_id"], row["league_key"], "gesamt", items)
                changes_total += len(sc)
            except Exception as exc:
                logger.warning("Live-Statistik-Abruf fehlgeschlagen (%s): %s", row["match_id"], exc)
        return changes_total

    def poll_lineups(
        self,
        leagues: tuple[LeagueConfig, ...] = LEAGUES,
        prematch_window_minutes: int = 90,
    ) -> int:
        """Aufstellungs-Zyklus vor Spielbeginn & bei Änderungen (Voraussichtlich -> Bestätigt -> Wechsel)."""
        placeholders = ",".join("?" for _ in leagues)
        lkeys = tuple(lg.league_key for lg in leagues)
        rows = self.db.fetch_dicts(
            f"""
            SELECT match_id, league_key, match_url, kickoff_raw, status,
                   home_team_id, home_team_name, away_team_id, away_team_name
            FROM matches
            WHERE league_key IN ({placeholders})
              AND status IN ('scheduled', 'live')
            """,
            lkeys,
        )
        changes_total = 0
        for r in rows:
            if r["status"] == "scheduled" and not is_within_prematch_window(
                r["kickoff_raw"], window_minutes=prematch_window_minutes
            ):
                continue
            if not r["match_url"]:
                continue
            kader_url = build_match_subroute_url(r["match_url"], "bericht/kader", r["match_id"])
            try:
                html = self.client.fetch_html(kader_url)
                summary, absences = parse_match_lineups_page(
                    html,
                    r["match_id"],
                    r["league_key"],
                    r["home_team_id"],
                    r["home_team_name"],
                    r["away_team_id"],
                    r["away_team_name"],
                )
                if summary is not None:
                    lc = self.db.upsert_lineup(r["league_key"], summary)
                    changes_total += len(lc)
                for inj in absences:
                    ic = self.db.upsert_injury(inj, is_prematch_late_dropout=True)
                    changes_total += len(ic)
            except Exception as exc:
                logger.warning("Kader-Abruf fehlgeschlagen (%s): %s", r["match_id"], exc)
        return changes_total

    def poll_injuries_and_absences(
        self, leagues: tuple[LeagueConfig, ...] = LEAGUES
    ) -> int:
        """15-Minuten-Zyklus: Verletzungen, Sperren, Krankheiten & fragliche Spieler."""
        placeholders = ",".join("?" for _ in leagues)
        lkeys = tuple(lg.league_key for lg in leagues)
        upcoming = self.db.fetch_dicts(
            f"""
            SELECT match_id, league_key, match_url
            FROM matches
            WHERE league_key IN ({placeholders}) AND status = 'scheduled'
            ORDER BY kickoff_raw ASC
            LIMIT 40
            """,
            lkeys,
        )
        changes_total = 0
        for r in upcoming:
            if not r["match_url"]:
                continue
            lg = LEAGUE_BY_KEY[r["league_key"]]
            try:
                html = self.client.fetch_html(r["match_url"])
                detail = parse_match_detail_page(html, lg, r["match_id"], r["match_url"])
                for inj in detail.injuries:
                    ic = self.db.upsert_injury(inj)
                    changes_total += len(ic)
            except Exception as exc:
                logger.warning("Verletzungs-Check fehlgeschlagen (%s): %s", r["match_id"], exc)
        return changes_total

    def ingest_full_match_bundle(self, league: LeagueConfig, match_id: str, match_url: str) -> int:
        """Lädt Bericht, Statistiken und Aufstellung eines Spiels vollständig in die Datenbank."""
        changes_total = 0
        detail_html = self.client.fetch_html(match_url)
        detail = parse_match_detail_page(detail_html, league, match_id, match_url)
        for t in detail.teams:
            self.db.upsert_team(t)
        if detail.match:
            mc, _ = self.db.upsert_match(detail.match)
            changes_total += len(mc)
        if detail.events:
            ec = self.db.insert_match_events(match_id, league.league_key, detail.events)
            changes_total += len(ec)
        for inj in detail.injuries:
            ic = self.db.upsert_injury(inj)
            changes_total += len(ic)
        if detail.embedded_standings:
            changes_total += self.recompute_and_store_standings(league, detail.embedded_standings)

        stats_url = build_match_subroute_url(match_url, "bericht/stats", match_id)
        try:
            stats_html = self.client.fetch_html(stats_url)
            stats_items = parse_match_stats_page(stats_html, match_id, period="gesamt")
            sc = self.db.upsert_match_stats(match_id, league.league_key, "gesamt", stats_items)
            changes_total += len(sc)
        except Exception as exc:
            logger.debug("Keine Zusatzstatistiken für %s: %s", match_id, exc)

        if detail.match:
            kader_url = build_match_subroute_url(match_url, "bericht/kader", match_id)
            try:
                kader_html = self.client.fetch_html(kader_url)
                summary, kader_inj = parse_match_lineups_page(
                    kader_html,
                    match_id,
                    league.league_key,
                    detail.match.home_team_id,
                    detail.match.home_team_name,
                    detail.match.away_team_id,
                    detail.match.away_team_name,
                )
                if summary:
                    changes_total += len(self.db.upsert_lineup(league.league_key, summary))
                for inj in kader_inj:
                    changes_total += len(self.db.upsert_injury(inj))
            except Exception as exc:
                logger.debug("Keine Aufstellung für %s: %s", match_id, exc)

        self.db.recalculate_referee_aggregates()
        return changes_total

    def ingest_player_injury_history(
        self, player_id: str, slug: str, league_key: str = ""
    ) -> int:
        url = f"{FLASHSCORE_BASE_URL}/spieler/{slug}/{player_id}/verletzungshistorie/"
        html = self.client.fetch_html(url)
        profile, records = parse_player_profile_and_injuries(html, player_id, slug, league_key)
        self.db.upsert_player_profile(profile)
        changes = 0
        for rec in records:
            changes += len(self.db.upsert_injury(rec))
        return changes
