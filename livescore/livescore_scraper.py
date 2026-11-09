#!/usr/bin/env python3
"""
Scrapling-Scraper für https://www.livescore.com/de/
Extrahiert Spiele (heute / Ergebnisse / kommende Spielpläne) sowie Tabellenstände für:
  - Deutschland: 1. Bundesliga, 2. Bundesliga, 3. Liga
  - England:     Premier League (1. Liga), Championship (2. Liga)
  - Spanien:     LaLiga (1. Liga), LaLiga 2 (2. Liga)
  - Frankreich:  Ligue 1 (1. Liga), Ligue 2 (2. Liga)

Nutzung (lokal mit Internetzugang):
  python livescore/livescore_scraper.py
  python livescore/livescore_scraper.py --date 2026-11-09 --tz 1 --output livescore/livescore_de_extrakt.json
  python livescore/livescore_scraper.py --browser
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import orjson
from scrapling.fetchers import FetcherSession, StealthySession
from scrapling.parser import Selector


@dataclass(frozen=True)
class LeagueConfig:
    key: str
    country_de: str
    country_api: str
    name_de: str
    tier: int
    slug: str

    @property
    def overview_url(self) -> str:
        return f"https://www.livescore.com/de/fussball/{self.country_de}/{self.slug}/"

    @property
    def standings_url(self) -> str:
        return f"{self.overview_url}standings/"

    def stage_api_url(self, tz_offset: int = 1, locale: str = "de") -> str:
        return (
            f"https://prod-public-api.livescore.com/v1/api/app/stage/soccer/"
            f"{self.country_api}/{self.slug}/{tz_offset}?locale={locale}"
        )


TARGET_LEAGUES: tuple[LeagueConfig, ...] = (
    LeagueConfig("de_1", "deutschland", "germany", "1. Bundesliga", 1, "bundesliga"),
    LeagueConfig("de_2", "deutschland", "germany", "2. Bundesliga", 2, "2-bundesliga"),
    LeagueConfig("de_3", "deutschland", "germany", "3. Liga", 3, "3-liga"),
    LeagueConfig("en_1", "england", "england", "Premier League", 1, "premier-league"),
    LeagueConfig("en_2", "england", "england", "Championship", 2, "championship"),
    LeagueConfig("es_1", "spanien", "spain", "LaLiga", 1, "laliga"),
    LeagueConfig("es_2", "spanien", "spain", "LaLiga 2", 2, "laliga-2"),
    LeagueConfig("fr_1", "frankreich", "france", "Ligue 1", 1, "ligue-1"),
    LeagueConfig("fr_2", "frankreich", "france", "Ligue 2", 2, "ligue-2"),
)

TARGET_STAGE_KEYS: frozenset[tuple[str, str]] = frozenset(
    (league.country_api, league.slug) for league in TARGET_LEAGUES
)


def format_esd(esd: int | str | None) -> str | None:
    """Formatiert LiveScore-Zeitstempel (z. B. 20261109203000 -> '2026-11-09 20:30')."""
    if not esd:
        return None
    raw = str(esd)
    if len(raw) >= 12:
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}"
    return raw


def parse_event(event: dict[str, Any]) -> dict[str, Any]:
    """Extrahiert ein einzelnes Spiel aus dem LiveScore-Event-Objekt."""
    t1 = (event.get("T1") or [{}])[0]
    t2 = (event.get("T2") or [{}])[0]
    return {
        "event_id": event.get("Eid"),
        "kickoff": format_esd(event.get("Esd")),
        "status": event.get("Eps"),
        "round": event.get("ErnInf"),
        "home_team": t1.get("Nm"),
        "away_team": t2.get("Nm"),
        "home_score": event.get("Tr1"),
        "away_score": event.get("Tr2"),
        "ht_home_score": event.get("Trh1"),
        "ht_away_score": event.get("Trh2"),
    }


def parse_daily_json(payload: bytes | str) -> list[dict[str, Any]]:
    """Filtert die Tagesansicht von LiveScore auf die 9 gewünschten Ligen."""
    data = orjson.loads(payload)
    matched_stages: list[dict[str, Any]] = []
    for stage in data.get("Stages", []):
        country = stage.get("Ccd", "")
        comp_slug = stage.get("CompUrlName") or stage.get("Scd") or ""
        stage_slug = stage.get("Scd") or ""
        if (country, comp_slug) in TARGET_STAGE_KEYS or (country, stage_slug) in TARGET_STAGE_KEYS:
            matched_stages.append(
                {
                    "country": stage.get("Cnm") or country,
                    "league": stage.get("CompN") or stage.get("Snm"),
                    "slug": comp_slug or stage_slug,
                    "matches": [parse_event(ev) for ev in stage.get("Events", [])],
                }
            )
    return matched_stages


def parse_standings_html(html: str | bytes, url: str = "") -> list[dict[str, Any]]:
    """
    Parst die LiveScore-Tabellenseite (/de/fussball/<land>/<liga>/standings/)
    mithilfe von Scraplings `Selector`.
    """
    page = Selector(html, url=url)

    # Falls __NEXT_DATA__ eingebettet ist, nutze bevorzugt die strukturierten Tabellendaten
    next_data_text = page.css("script#__NEXT_DATA__::text").get()
    if next_data_text:
        try:
            next_data = orjson.loads(str(next_data_text))
            tables = _extract_tables_from_next_data(next_data)
            if tables:
                return tables
        except orjson.JSONDecodeError:
            pass

    # Fallback: Direktes HTML-Tabellen-Parsing über Scrapling CSS-Selektoren
    rows: list[dict[str, Any]] = []
    for idx, tr in enumerate(page.css("table tbody tr"), start=1):
        cells = [c.strip() for c in tr.css("td *::text, td::text").getall() if c.strip()]
        team_link = tr.css('a[href*="/fussball/team/"]')
        team_name = team_link.css("::text").get("").strip() if team_link else ""
        if team_name and cells:
            rows.append({"rank": idx, "team": team_name, "raw_cells": cells})
    return rows


def _extract_tables_from_next_data(obj: Any) -> list[dict[str, Any]]:
    """Sucht rekursiv nach LiveScore-LeagueTable-Einträgen ('team' / 'L' Struktur)."""
    if isinstance(obj, dict):
        if "LeagueTable" in obj and isinstance(obj["LeagueTable"], dict):
            stages = obj["LeagueTable"].get("L", [])
            for st in stages:
                for table in st.get("Tables", []):
                    if table.get("LTT") in (1, None):  # 1 = Gesamt-Tabelle
                        return [
                            {
                                "rank": team.get("rnk"),
                                "team": team.get("Tnm"),
                                "played": team.get("pld"),
                                "wins": team.get("win"),
                                "draws": team.get("drw"),
                                "losses": team.get("lst"),
                                "goals_for": team.get("gf"),
                                "goals_against": team.get("ga"),
                                "goal_diff": team.get("gd"),
                                "points": team.get("pts"),
                            }
                            for team in table.get("team", [])
                        ]
        for value in obj.values():
            found = _extract_tables_from_next_data(value)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _extract_tables_from_next_data(item)
            if found:
                return found
    return []


def scrape_livescore(date_str: str, tz_offset: int = 1, use_browser: bool = False) -> dict[str, Any]:
    """Führt den vollständigen Scrape für das gewünschte Datum und alle 9 Ligen aus."""
    ymd = date_str.replace("-", "")
    daily_api_url = f"https://prod-public-api.livescore.com/v1/api/app/date/soccer/{ymd}/{tz_offset}?locale=de"

    result: dict[str, Any] = {
        "source": "https://www.livescore.com/de/",
        "date": date_str,
        "timezone_offset_hours": tz_offset,
        "daily_matches": [],
        "leagues": {},
    }

    with FetcherSession(impersonate="chrome", stealthy_headers=True, timeout=30) as session:
        daily_resp = session.get(daily_api_url)
        if daily_resp.status == 200:
            result["daily_matches"] = parse_daily_json(daily_resp.body)

        for league in TARGET_LEAGUES:
            stage_resp = session.get(league.stage_api_url(tz_offset=tz_offset))
            events: list[dict[str, Any]] = []
            standings: list[dict[str, Any]] = []
            if stage_resp.status == 200:
                stage_json = orjson.loads(stage_resp.body)
                for st in stage_json.get("Stages", []):
                    events.extend(parse_event(ev) for ev in st.get("Events", []))
                standings = _extract_tables_from_next_data(stage_json)

            finished = [e for e in events if e.get("status") == "FT"][-10:]
            upcoming = [e for e in events if e.get("status") == "NS"][:10]
            live = [e for e in events if e.get("status") not in ("FT", "NS", "Postp.", "Canc.")]

            if not standings and not use_browser:
                st_resp = session.get(league.standings_url)
                if st_resp.status == 200:
                    standings = parse_standings_html(st_resp.body, url=league.standings_url)

            result["leagues"][league.key] = {
                "meta": asdict(league) | {"url": league.overview_url, "standings_url": league.standings_url},
                "live_matches": live,
                "recent_results": finished,
                "upcoming_fixtures": upcoming,
                "standings": standings,
            }

    if use_browser:
        with StealthySession(headless=True, block_ads=True) as browser_session:
            for league in TARGET_LEAGUES:
                page = browser_session.fetch(league.standings_url, network_idle=True)
                result["leagues"][league.key]["standings"] = parse_standings_html(
                    page.body, url=league.standings_url
                )

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrapling LiveScore.com/de Extraktor (9 Top-Ligen)")
    parser.add_argument(
        "--date",
        default=datetime.now().strftime("%Y-%m-%d"),
        help="Datum im Format YYYY-MM-DD (Standard: heute)",
    )
    parser.add_argument(
        "--tz",
        type=int,
        default=1,
        help="Zeitzonen-Offset in Stunden zu UTC (Standard: 1 = MEZ)",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Nutzt StealthySession (Headless Chromium) für die Tabellenseiten",
    )
    parser.add_argument(
        "--output",
        default="livescore/livescore_de_extrakt.json",
        help="Ausgabepfad für die JSON-Datei",
    )
    args = parser.parse_args()

    data = scrape_livescore(date_str=args.date, tz_offset=args.tz, use_browser=args.browser)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(orjson.dumps(data, option=orjson.OPT_INDENT_2))
    print(f"Erfolgreich gespeichert unter: {out_path} ({len(data['leagues'])} Ligen)")


if __name__ == "__main__":
    main()
