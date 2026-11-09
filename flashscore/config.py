"""Konfiguration aller 10 Ziel-Ligen, Abfrageintervalle und Ordnerstrukturen."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

FLASHSCORE_BASE_URL: Final[str] = "https://www.flashscore.de"


@dataclass(frozen=True, slots=True)
class LeagueConfig:
    """Metadaten und URLs einer überwachten Fußballliga auf Flashscore.de."""

    league_key: str
    country: str
    country_folder: str
    name: str
    league_folder: str
    flashscore_slug: str
    stage_id: str
    season: str = "2026/2027"
    tier: int = 1

    @property
    def base_url(self) -> str:
        return f"https://www.flashscore.de/fussball/{self.flashscore_slug}/"

    @property
    def results_url(self) -> str:
        return f"{self.base_url}ergebnisse/"

    @property
    def fixtures_url(self) -> str:
        return f"{self.base_url}begegnungen/"

    @property
    def standings_url(self) -> str:
        return f"{self.base_url}tabelle/"

    @property
    def relative_folder(self) -> Path:
        return Path(self.country_folder) / self.league_folder


LEAGUES: Final[tuple[LeagueConfig, ...]] = (
    # Deutschland (1.–3. Liga)
    LeagueConfig(
        league_key="de_bundesliga",
        country="Deutschland",
        country_folder="Deutschland",
        name="1. Bundesliga",
        league_folder="Bundesliga",
        flashscore_slug="deutschland/bundesliga",
        stage_id="jg0MwVuC",
        tier=1,
    ),
    LeagueConfig(
        league_key="de_2_bundesliga",
        country="Deutschland",
        country_folder="Deutschland",
        name="2. Bundesliga",
        league_folder="2_Bundesliga",
        flashscore_slug="deutschland/2-bundesliga",
        stage_id="6khmdCet",
        tier=2,
    ),
    LeagueConfig(
        league_key="de_3_liga",
        country="Deutschland",
        country_folder="Deutschland",
        name="3. Liga",
        league_folder="3_Liga",
        flashscore_slug="deutschland/3-liga",
        stage_id="xzz5hUf5",
        tier=3,
    ),
    # England (1.–3. Liga)
    LeagueConfig(
        league_key="en_premier_league",
        country="England",
        country_folder="England",
        name="Premier League",
        league_folder="Premier_League",
        flashscore_slug="england/premier-league",
        stage_id="CfoA8Dmm",
        tier=1,
    ),
    LeagueConfig(
        league_key="en_championship",
        country="England",
        country_folder="England",
        name="Championship",
        league_folder="Championship",
        flashscore_slug="england/championship",
        stage_id="40zsPsC5",
        tier=2,
    ),
    LeagueConfig(
        league_key="en_league_one",
        country="England",
        country_folder="England",
        name="League One",
        league_folder="League_One",
        flashscore_slug="england/league-one",
        stage_id="fBE45JPc",
        tier=3,
    ),
    # Frankreich (1.–2. Liga)
    LeagueConfig(
        league_key="fr_ligue_1",
        country="Frankreich",
        country_folder="Frankreich",
        name="Ligue 1",
        league_folder="Ligue_1",
        flashscore_slug="frankreich/ligue-1",
        stage_id="CQF28KxR",
        tier=1,
    ),
    LeagueConfig(
        league_key="fr_ligue_2",
        country="Frankreich",
        country_folder="Frankreich",
        name="Ligue 2",
        league_folder="Ligue_2",
        flashscore_slug="frankreich/ligue-2",
        stage_id="zX0tEFE6",
        tier=2,
    ),
    # Spanien (1.–2. Liga)
    LeagueConfig(
        league_key="es_laliga",
        country="Spanien",
        country_folder="Spanien",
        name="LaLiga",
        league_folder="LaLiga",
        flashscore_slug="spanien/la-liga",
        stage_id="dWdJXP6U",
        tier=1,
    ),
    LeagueConfig(
        league_key="es_segunda_division",
        country="Spanien",
        country_folder="Spanien",
        name="Segunda División",
        league_folder="Segunda_Division",
        flashscore_slug="spanien/la-liga2",
        stage_id="42oxhTin",
        tier=2,
    ),
)

LEAGUE_BY_KEY: Final[dict[str, LeagueConfig]] = {lg.league_key: lg for lg in LEAGUES}

LEAGUE_ALIASES: Final[dict[str, str]] = {
    "de_bundesliga": "de_bundesliga",
    "de_1_bundesliga": "de_bundesliga",
    "bundesliga": "de_bundesliga",
    "1. bundesliga": "de_bundesliga",
    "1_bundesliga": "de_bundesliga",
    "de_2_bundesliga": "de_2_bundesliga",
    "2. bundesliga": "de_2_bundesliga",
    "2_bundesliga": "de_2_bundesliga",
    "2-bundesliga": "de_2_bundesliga",
    "de_3_liga": "de_3_liga",
    "3. liga": "de_3_liga",
    "3_liga": "de_3_liga",
    "3-liga": "de_3_liga",
    "en_premier_league": "en_premier_league",
    "premier league": "en_premier_league",
    "premier_league": "en_premier_league",
    "premier-league": "en_premier_league",
    "en_championship": "en_championship",
    "championship": "en_championship",
    "en_league_one": "en_league_one",
    "league one": "en_league_one",
    "league_one": "en_league_one",
    "league-one": "en_league_one",
    "fr_ligue_1": "fr_ligue_1",
    "ligue 1": "fr_ligue_1",
    "ligue_1": "fr_ligue_1",
    "ligue-1": "fr_ligue_1",
    "fr_ligue_2": "fr_ligue_2",
    "ligue 2": "fr_ligue_2",
    "ligue_2": "fr_ligue_2",
    "ligue-2": "fr_ligue_2",
    "es_laliga": "es_laliga",
    "laliga": "es_laliga",
    "la liga": "es_laliga",
    "la-liga": "es_laliga",
    "es_segunda_division": "es_segunda_division",
    "segunda division": "es_segunda_division",
    "segunda división": "es_segunda_division",
    "segunda_division": "es_segunda_division",
    "la liga2": "es_segunda_division",
    "la-liga2": "es_segunda_division",
    "laliga2": "es_segunda_division",
}


def resolve_league_key(identifier: str) -> str:
    """Löst einen Liganamen, Alias oder Schlüssel eindeutig auf `league_key` auf."""
    normalized = identifier.strip().lower()
    if normalized in LEAGUE_ALIASES:
        return LEAGUE_ALIASES[normalized]
    raise ValueError(
        f"Unbekannte Liga '{identifier}'. Verfügbar: {', '.join(LEAGUE_BY_KEY.keys())}"
    )


@dataclass(frozen=True, slots=True)
class PollingIntervals:
    """Vorgeschriebene Überwachungs- und Synchronisationsintervalle in Sekunden."""

    live_scores_sec: float = 15.0
    live_events_sec: float = 15.0
    live_stats_sec: float = 30.0
    injuries_sec: float = 900.0          # 15 Minuten
    prematch_lineups_sec: float = 180.0  # alle 3 Minuten ab 90 Min vor Anpfiff
    prematch_window_min: int = 90        # Intensivfenster vor Spielbeginn
    schedules_sec: float = 1800.0        # Spielpläne alle 30 Minuten prüfen
    standings_fallback_sec: float = 900.0  # zusätzlich sofort nach jedem beendeten Spiel
    auto_backup_sec: float = 3600.0      # automatische Sicherung jede Stunde
    min_request_delay_sec: float = 0.6   # Schutz vor Ratenbegrenzung


TOP_LEVEL_SUBFOLDERS: Final[tuple[str, ...]] = (
    "Spieler",
    "Verletzungen",
    "Live_Spiele",
    "Spielstatistiken",
    "Aufstellungen",
    "Schiedsrichter",
    "Stadien",
    "Historische_Daten",
    "Backups",
)

DATABASE_FILENAME: Final[str] = "Gesamtdatenbank.sqlite"


def resolve_data_dir(custom_dir: str | Path | None = None) -> Path:
    """Bestimmt das Zielverzeichnis für alle Daten und Exporte (Standard: ./Flashscore_Daten)."""
    if custom_dir is not None:
        return Path(custom_dir).expanduser().resolve()
    env_dir = os.environ.get("FLASHSCORE_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    repo_root = Path(__file__).resolve().parent.parent
    return (repo_root / "Flashscore_Daten").resolve()


def ensure_storage_tree(base_dir: Path) -> dict[str, Path]:
    """Erstellt die vollständig vorgeschriebene lokale Ordnerstruktur."""
    base_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {"root": base_dir}

    for league in LEAGUES:
        lg_dir = base_dir / league.relative_folder
        lg_dir.mkdir(parents=True, exist_ok=True)
        paths[league.league_key] = lg_dir
        paths[f"league:{league.league_key}"] = lg_dir

    for folder_name in TOP_LEVEL_SUBFOLDERS:
        sub_dir = base_dir / folder_name
        sub_dir.mkdir(parents=True, exist_ok=True)
        paths[folder_name] = sub_dir

    logs_dir = base_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    paths["logs"] = logs_dir
    paths["db"] = base_dir / DATABASE_FILENAME
    paths["database"] = paths["db"]
    return paths
