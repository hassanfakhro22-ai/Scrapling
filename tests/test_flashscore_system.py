"""Umfassende Unit- und Integrationstests für das Flashscore-Datenerfassungs-, Historie- & Exportsystem."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

from openpyxl import load_workbook

from flashscore.cli import main as cli_main
from flashscore.client import ConnectionMonitor
from flashscore.collector import FlashscoreCollector
from flashscore.config import (
    LEAGUE_BY_KEY,
    LEAGUES,
    PollingIntervals,
    ensure_storage_tree,
    resolve_league_key,
)
from flashscore.daemon import FlashscoreDaemon
from flashscore.database import FlashscoreDatabase
from flashscore.exporter import FlashscoreExporter
from flashscore.models import InjuryRecord, MatchRecord
from flashscore.parsers import (
    parse_league_page,
    parse_match_detail_page,
    parse_match_lineups_page,
    parse_match_stats_page,
    parse_player_profile_and_injuries,
)
from flashscore.seed_live_data import LIVE_STANDINGS_2026_11_09, seed_verified_flashscore_data
from flashscore.standings_engine import compute_tables_from_matches


class FakeHtmlTransport:
    """Deterministischer HTML-Provider für Ende-zu-Ende-Tests des Collectors und Daemons."""

    def __init__(self, routes: dict[str, str] | None = None) -> None:
        self.routes = routes or {}
        self.fail_next = False

    def fetch_html(self, url: str, *, require_js: bool = False) -> str:
        _ = require_js
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("Simulierter Netzwerkausfall")
        return self.routes.get(url, "<html><body>Leer</body></html>")

    def close(self) -> None:
        return None


def test_ten_leagues_and_storage_tree(tmp_path: Path) -> None:
    assert len(LEAGUES) == 10
    total_seeded_clubs = sum(len(v) for v in LIVE_STANDINGS_2026_11_09.values())
    assert total_seeded_clubs == 202

    assert resolve_league_key("1. Bundesliga") == "de_bundesliga"
    assert resolve_league_key("Championship") == "en_championship"
    assert resolve_league_key("League One") == "en_league_one"
    assert resolve_league_key("Ligue 2") == "fr_ligue_2"
    assert resolve_league_key("Segunda División") == "es_segunda_division"

    paths = ensure_storage_tree(tmp_path / "Flashscore_Daten")
    expected_dirs = [
        tmp_path / "Flashscore_Daten" / "Deutschland" / "Bundesliga",
        tmp_path / "Flashscore_Daten" / "Deutschland" / "2_Bundesliga",
        tmp_path / "Flashscore_Daten" / "Deutschland" / "3_Liga",
        tmp_path / "Flashscore_Daten" / "England" / "Premier_League",
        tmp_path / "Flashscore_Daten" / "England" / "Championship",
        tmp_path / "Flashscore_Daten" / "England" / "League_One",
        tmp_path / "Flashscore_Daten" / "Frankreich" / "Ligue_1",
        tmp_path / "Flashscore_Daten" / "Frankreich" / "Ligue_2",
        tmp_path / "Flashscore_Daten" / "Spanien" / "LaLiga",
        tmp_path / "Flashscore_Daten" / "Spanien" / "Segunda_Division",
        tmp_path / "Flashscore_Daten" / "Spieler",
        tmp_path / "Flashscore_Daten" / "Verletzungen",
        tmp_path / "Flashscore_Daten" / "Live_Spiele",
        tmp_path / "Flashscore_Daten" / "Spielstatistiken",
        tmp_path / "Flashscore_Daten" / "Aufstellungen",
        tmp_path / "Flashscore_Daten" / "Schiedsrichter",
        tmp_path / "Flashscore_Daten" / "Stadien",
        tmp_path / "Flashscore_Daten" / "Historische_Daten",
        tmp_path / "Flashscore_Daten" / "Backups",
    ]
    for d in expected_dirs:
        assert d.is_dir(), f"Ordner fehlt: {d}"
    assert paths["database"].name == "Gesamtdatenbank.sqlite"


def test_parsers_strip_odds_and_extract_all_entities() -> None:
    lg_championship = LEAGUE_BY_KEY["en_championship"]
    match_html = """
    <html><body>
      <div>Quoten</div>
      <div>bet365</div>
      <div>21.11.2026 07:30</div>
      <div>16. Spieltag</div>
      <a href="/mannschaft/fc-millwall/6uz2cJBL/">FC Millwall</a>
      <span>2</span><span>-</span><span>1</span>
      <a href="/mannschaft/queens-park-rangers/lrMHUHDc/">Queens Park Rangers</a>
      <div>Beendet</div>
      <div>Bericht</div>
      <div>1. Halbzeit</div><div>1</div><div>-</div><div>0</div>
      <div>18'</div><a href="/spieler/watmore-duncan/abc12345/">Duncan Watmore</a><div>1 - 0</div><div>George Honeyman</div>
      <div>34'</div><div>Jake Cooper</div><div>Foul</div>
      <div>2. Halbzeit</div><div>1</div><div>-</div><div>1</div>
      <div>61'</div><div>Michael Frey</div><div>Kein Tor - VAR (Abseits)</div>
      <div>75'</div><div>Ilias Chair</div><div>1 - 1</div><div>(Elfmeter)</div>
      <div>82'</div><div>Mihailo Ivanovic</div><div>Duncan Watmore</div>
      <div>88'</div><div>Mihailo Ivanovic</div><div>2 - 1</div>
      <div>Wird nicht spielen</div>
      <a href="/spieler/bryan-joe/joe98765/">Joe Bryan</a><div>Rote Karte</div>
      <div>Fraglich</div>
      <a href="/spieler/leonard-ryan/rya54321/">Ryan Leonard</a><div>Knieverletzung</div>
      <div>Spielinformationen</div>
      <div>Schiedsrichter:</div><div>Andrew Madley (Eng)</div>
      <div>Stadion:</div><div>The Den (London)</div>
      <div>Anzahl Plätze:</div><div>20 146</div>
      <div>Zuschauer:</div><div>18 900</div>
      <div>Tabelle</div>
      <div>#</div><div>Mannschaft</div>
      <div>7.</div><div>-4 Punkte (Verbandsentscheidung)</div>
      <a href="/mannschaft/fc-southampton/WdKOwxDM/">FC Southampton</a>
      <div>15</div><div>8</div><div>4</div><div>3</div><div>29:15</div><div>14</div><div>24</div>
      <div>?</div><div>N</div><div>U</div><div>S</div><div>S</div><div>U</div>
    </body></html>
    """
    detail = parse_match_detail_page(
        match_html,
        lg_championship,
        "Gt5z5AvB",
        "https://www.flashscore.de/spiel/fussball/fc-millwall-6uz2cJBL/queens-park-rangers-lrMHUHDc/?mid=Gt5z5AvB",
    )
    assert detail.match is not None
    assert detail.match.status == "finished"
    assert detail.match.home_score == 2 and detail.match.away_score == 1
    assert detail.match.ht_home_score == 1 and detail.match.ht_away_score == 0
    assert detail.match.referee_name == "Andrew Madley"
    assert detail.match.referee_country == "Eng"
    assert detail.match.stadium_name == "The Den"
    assert detail.match.stadium_city == "London"
    assert detail.match.stadium_capacity == 20146
    assert detail.match.attendance == 18900

    ev_types = [e.event_type for e in detail.events]
    assert ev_types == ["goal", "yellow_card", "var", "penalty_goal", "substitution", "goal"]
    assert detail.events[0].secondary_player_name == "George Honeyman"

    statuses = {i.player_name: i.availability_status for i in detail.injuries}
    assert statuses["Joe Bryan"] == "gesperrt"
    assert statuses["Ryan Leonard"] == "fraglich"

    assert len(detail.embedded_standings) == 1
    soton = detail.embedded_standings[0]
    assert soton.team_name == "FC Southampton"
    assert soton.points == 24
    assert soton.form_last_5 == "NUSSU"
    assert soton.deduction_note == "-4 Punkte (Verbandsentscheidung)"


def test_stats_lineup_and_player_injury_parsers() -> None:
    stats_html = """
    <html><body>
      <div>Top-Statistiken</div>
      <div>1.89</div><div>Expected Goals (xG)</div><div>2.45</div>
      <div>42%</div><div>Ballbesitz</div><div>58%</div>
      <div>Pässe</div>
      <div>81% (324/400)</div><div>Pässe</div><div>89% (490/550)</div>
      <div>Torhüter</div>
      <div>5</div><div>Paraden</div><div>2</div>
    </body></html>
    """
    stats = parse_match_stats_page(stats_html, "A9JgHzZA", "gesamt")
    assert len(stats) == 4
    assert stats[0].metric_name == "Expected Goals (xG)"
    assert stats[0].home_value == 1.89 and stats[0].away_value == 2.45
    assert stats[2].home_raw == "81% (324/400)" and stats[2].home_value == 81.0

    kader_html = """
    <html><body>
      <div>Startaufstellungen</div>
      <div>4-2-3-1</div><div>6.80</div><div>4-3-3</div><div>7.20</div>
      <div>1</div><div>Manuel Neuer (TW) (C)</div><div>7.1</div>
      <div>9</div><div>Harry Kane</div><div>8.2</div>
      <div>Auswechslungen</div>
      <div>68'</div><div>7</div><div>Serge Gnabry</div><div>Harry Kane</div><div>6.9</div>
      <div>Ersatzspieler</div>
      <div>7</div><div>Serge Gnabry</div><div>6.9</div>
      <div>26</div><div>Sven Ulreich (TW)</div>
      <div>Trainer</div>
      <div>Vincent Kompany</div><div>Lukas Kwasniok</div>
    </body></html>
    """
    lu, _ = parse_match_lineups_page(
        kader_html, "zZPQYkxo", "de_bundesliga", "nVp0wiqd", "Bayern München", "WG9pOTse", "1. FC Köln"
    )
    assert lu is not None
    assert lu.lineup_status == "confirmed"
    assert lu.home_formation == "4-2-3-1" and lu.away_formation == "4-3-3"
    assert lu.home_coach == "Vincent Kompany"
    neuer = next(p for p in lu.players if "Neuer" in p.player_name)
    assert neuer.is_captain and neuer.is_goalkeeper
    gnabry = next(p for p in lu.players if "Gnabry" in p.player_name)
    assert gnabry.role == "bench" and gnabry.sub_minute == "68'" and gnabry.replaced_player == "Harry Kane"

    player_html = """
    <html><body>
      <h2>Jules Koundé</h2>
      <a href="/mannschaft/fc-barcelona/SKbpVP5K/">FC Barcelona</a>
      <div>Abwehr (FC Barcelona 23)</div>
      <div>Alter: 27 (12.11.1998)</div>
      <div>Marktwert</div><div>60.0 Mio. €</div>
      <div>Vertrag läuft bis 30.06.2030</div>
      <div>(Sprunggelenksverletzung - Rückkehr Ende November 2026)</div>
      <div>Verletzungshistorie</div>
      <div>Von</div><div>Bis</div><div>Verletzung</div>
      <div>18.10.2026</div><div>?</div><div>Sprunggelenksverletzung</div>
      <div>04.03.2025</div><div>19.03.2025</div><div>Muskelverletzung</div>
    </body></html>
    """
    prof, inj_list = parse_player_profile_and_injuries(player_html, "v36QVRPu", "kounde-jules", "es_laliga")
    assert prof.player_name == "Jules Koundé"
    assert prof.position == "Abwehr" and prof.shirt_number == 23
    assert prof.current_status == "verletzt"
    assert prof.expected_return == "Ende November 2026"
    assert len(inj_list) == 2
    assert inj_list[0].availability_status == "verletzt"
    assert inj_list[1].availability_status == "genesen"


def test_standings_engine_computes_overall_home_away_form5_form10() -> None:
    matches = [
        MatchRecord("m1", "de_bundesliga", "2026/2027", "1. Spieltag", "01.09.2026 15:30", "finished", "FT", "90'", "tA", "Team A", "tB", "Team B", 3, 0, 1, 0),
        MatchRecord("m2", "de_bundesliga", "2026/2027", "2. Spieltag", "08.09.2026 15:30", "finished", "FT", "90'", "tB", "Team B", "tA", "Team A", 1, 1, 0, 1),
    ]
    tables = compute_tables_from_matches("de_bundesliga", "2026/2027", matches)
    assert set(tables.keys()) == {"overall", "home", "away", "form_5", "form_10"}
    top = tables["overall"][0]
    assert top.team_id == "tA" and top.points == 4 and top.form_last_5 == "US"
    home_a = next(r for r in tables["home"] if r.team_id == "tA")
    away_a = next(r for r in tables["away"] if r.team_id == "tA")
    assert home_a.wins == 1 and away_a.draws == 1


def test_non_destructive_history_dedup_and_post_match_table_update(tmp_path: Path) -> None:
    db = FlashscoreDatabase(tmp_path / "test.sqlite")
    lg = LEAGUE_BY_KEY["de_bundesliga"]

    # 1. Geplantes Spiel -> Verschoben -> Live -> Tor -> Beendet
    m_sched = MatchRecord("M100", lg.league_key, lg.season, "10. Spieltag", "20.11.2026 15:30", "scheduled", "NS", None, "T1", "Bayern München", "T2", "Borussia Dortmund", None, None, None, None)
    db.upsert_match(m_sched, detected_at="2026-11-09T10:00:00Z")
    # Identischer Zweitabruf darf KEIN Duplikat erzeugen
    dup_changes, _ = db.upsert_match(m_sched, detected_at="2026-11-09T10:00:15Z")
    assert dup_changes == []

    # Spielverschiebung
    m_post = MatchRecord("M100", lg.league_key, lg.season, "10. Spieltag", "21.11.2026 18:30", "postponed", "POSTP", None, "T1", "Bayern München", "T2", "Borussia Dortmund", None, None, None, None)
    c_post, _ = db.upsert_match(m_post, detected_at="2026-11-09T11:00:00Z")
    assert any(c.change_type == "MATCH_POSTPONED" for c in c_post)

    # Live 1:0 -> Beendet 2:1
    m_live = MatchRecord("M100", lg.league_key, lg.season, "10. Spieltag", "21.11.2026 18:30", "live", "1H", "23'", "T1", "Bayern München", "T2", "Borussia Dortmund", 1, 0, None, None)
    c_live, fin1 = db.upsert_match(m_live, detected_at="2026-11-21T18:53:00Z")
    assert not fin1
    assert any(c.change_type == "SCORE_CHANGED" for c in c_live)

    m_ft = MatchRecord("M100", lg.league_key, lg.season, "10. Spieltag", "21.11.2026 18:30", "finished", "FT", "90'", "T1", "Bayern München", "T2", "Borussia Dortmund", 2, 1, 1, 0)
    c_ft, fin2 = db.upsert_match(m_ft, detected_at="2026-11-21T20:20:00Z")
    assert fin2 is True
    assert any(c.change_type == "MATCH_FINISHED" for c in c_ft)

    # Prüfe, dass ALLE 4 Spielzustände unverändert in score_history erhalten geblieben sind
    score_hist = db.fetch_dicts("SELECT status, home_score, away_score FROM score_history WHERE match_id = 'M100' ORDER BY id ASC")
    assert len(score_hist) == 4
    assert score_hist[0]["status"] == "scheduled"
    assert score_hist[1]["status"] == "postponed"
    assert score_hist[2]["home_score"] == 1
    assert score_hist[3]["home_score"] == 2

    # Verletzungs-Historie ohne Überschreiben
    inj1 = InjuryRecord("P1", "Jamal Musiala", "T1", "Bayern München", lg.league_key, "fraglich", "Muskelverhärtung")
    inj2 = InjuryRecord("P1", "Jamal Musiala", "T1", "Bayern München", lg.league_key, "verletzt", "Muskelfaserriss", "15.12.2026")
    db.upsert_injury(inj1, detected_at="2026-11-09T09:00:00Z", is_prematch_late_dropout=True)
    db.upsert_injury(inj2, detected_at="2026-11-09T12:00:00Z")
    inj_hist = db.fetch_dicts("SELECT availability_status, reason FROM injury_history WHERE player_id = 'P1' ORDER BY id ASC")
    assert [r["availability_status"] for r in inj_hist] == ["fraglich", "verletzt"]
    db.close()


def test_connection_monitor_and_daemon_auto_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "Flashscore_Daten"
    paths = ensure_storage_tree(data_dir)
    db = FlashscoreDatabase(paths["database"])

    # 1. Verbindungsabbruch & Wiederherstellung
    mon = ConnectionMonitor(db=db)
    backoff = mon.record_failure("https://www.flashscore.de/", RuntimeError("Timeout"))
    assert backoff >= 2.0 and not mon.health.is_online
    mon.record_success("https://www.flashscore.de/")
    assert mon.health.is_online and mon.health.consecutive_failures == 0

    events = db.fetch_dicts("SELECT event_code FROM system_events ORDER BY id ASC")
    codes = [e["event_code"] for e in events]
    assert "CONNECTION_LOST" in codes and "CONNECTION_RESTORED" in codes

    # 2. Daemon-Supervisor Auto-Restart bei Fehler
    transport = FakeHtmlTransport()
    collector = FlashscoreCollector(db=db, client=transport)
    exporter = FlashscoreExporter(db=db, base_dir=data_dir)
    daemon = FlashscoreDaemon(
        db=db,
        collector=collector,
        exporter=exporter,
        intervals=PollingIntervals(),
        leagues=(LEAGUE_BY_KEY["de_bundesliga"],),
    )

    calls = {"n": 0}
    orig_tick = daemon.run_single_tick

    def flaky_tick(now_mono: float | None = None) -> dict[str, int]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Simulierter Crash im ersten Zyklus")
        return orig_tick(now_mono)

    daemon.run_single_tick = flaky_tick  # type: ignore[method-assign]
    daemon.run_with_supervisor(max_cycles=1)
    assert daemon.restart_count == 1
    assert daemon.cycles_completed == 1
    sys_codes = [r["event_code"] for r in db.fetch_dicts("SELECT event_code FROM system_events")]
    assert "DAEMON_AUTO_RESTART" in sys_codes
    db.close()


def test_league_page_parser_and_all_five_export_formats_across_all_scopes(tmp_path: Path) -> None:
    lg_html = """
    <html><body>
      <div>10. Spieltag</div>
      <div>20.11. 14:30</div>
      <a href="/spiel/fussball/1-fc-koln-WG9pOTse/bayern-munchen-nVp0wiqd/?mid=zZPQYkxo">Details</a>
      <a href="/mannschaft/bayern-munchen/nVp0wiqd/">Bayern München</a>
      <a href="/mannschaft/1-fc-koln/WG9pOTse/">1. FC Köln</a>
      <div>-</div>
    </body></html>
    """
    parsed_lg = parse_league_page(lg_html, LEAGUE_BY_KEY["de_bundesliga"])
    assert len(parsed_lg.matches) == 1
    assert parsed_lg.matches[0].match_id == "zZPQYkxo"

    # Initialisiere kompletten Bestand über CLI und prüfe alle 5 Exportformate & alle 6 Scopes
    custom_dir = tmp_path / "Custom_Flashscore_Export"
    rc = cli_main(["--data-dir", str(custom_dir), "init", "--seed"])
    assert rc == 0
    assert (custom_dir / "Gesamtdatenbank.sqlite").is_file()

    # 1. JSON (scope=team)
    json_out = custom_dir / "bayern.json"
    assert cli_main(["--data-dir", str(custom_dir), "export", "--scope", "team", "--id", "Bayern München", "--format", "json", "--output", str(json_out)]) == 0
    bayern_data = json.loads(json_out.read_text(encoding="utf-8"))
    assert bayern_data["data"]["Mannschaft"][0]["name"] == "Bayern München"
    assert len(bayern_data["data"]["Tabellenstaende"]) == 5

    # 2. CSV (scope=player)
    csv_out = custom_dir / "kounde_csv"
    assert cli_main(["--data-dir", str(custom_dir), "export", "--scope", "player", "--id", "Jules Koundé", "--format", "csv", "--output", str(csv_out)]) == 0
    assert (csv_out / "Spielerprofil.csv").is_file()
    assert (csv_out / "Verletzungshistorie.csv").is_file()

    # 3. XLSX (scope=league)
    xlsx_out = custom_dir / "bundesliga.xlsx"
    assert cli_main(["--data-dir", str(custom_dir), "export", "--scope", "league", "--id", "Bundesliga", "--format", "xlsx", "--output", str(xlsx_out)]) == 0
    wb = load_workbook(xlsx_out)
    assert "Gesamttabelle" in wb.sheetnames and "Heimtabelle" in wb.sheetnames and "Auswaertstabelle" in wb.sheetnames

    # 4. SQLite (scope=match)
    sqlite_out = custom_dir / "match_barca.sqlite"
    assert cli_main(["--data-dir", str(custom_dir), "export", "--scope", "match", "--id", "A9JgHzZA", "--format", "sqlite", "--output", str(sqlite_out)]) == 0
    conn = sqlite3.connect(sqlite_out)
    stat_cnt = conn.execute('SELECT COUNT(*) FROM "export_statistiken"').fetchone()[0]
    conn.close()
    assert stat_cnt == 20

    # 5. ZIP (scope=all & scope=history)
    zip_out = custom_dir / "komplett.zip"
    assert cli_main(["--data-dir", str(custom_dir), "export", "--scope", "all", "--format", "zip", "--output", str(zip_out)]) == 0
    with zipfile.ZipFile(zip_out, "r") as zf:
        names = zf.namelist()
        assert any(n.endswith(".json") for n in names)
        assert any(n.endswith(".xlsx") for n in names)
        assert any(n.endswith(".sqlite") for n in names)
        assert any(n.startswith("csv/") for n in names)

    hist_out = custom_dir / "historie.json"
    assert cli_main(["--data-dir", str(custom_dir), "export", "--scope", "history", "--format", "json", "--output", str(hist_out)]) == 0
    hist_payload = json.loads(hist_out.read_text(encoding="utf-8"))
    assert len(hist_payload["data"]["Tabellen_Historie"]) == 202 * 5

    # Seed-Direktaufruf-Prüfung
    counts = seed_verified_flashscore_data(custom_dir)
    assert counts["leagues"] == 10
    assert counts["teams"] == 202
    assert counts["standings"] == 1010
