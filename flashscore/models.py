"""Datenmodelle für Ligen, Tabellen, Live-Spiele, Ereignisse, Kader, Verletzungen, Schiedsrichter & Stadien."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    """Aktueller UTC-Zeitstempel im ISO-8601-Format (Sekundenpräzision + Mikrosekunden)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def compute_payload_hash(payload: Any) -> str:
    """Deterministischer SHA-256-Hash zur Deduplizierung und Änderungserkennung."""
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class TeamRecord:
    team_id: str
    name: str
    slug: str
    country: str
    league_key: str
    stadium_name: str | None = None
    coach_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class StandingRow:
    """Tabellenzeile (unterstützt Gesamttabelle, Heimtabelle, Auswärtstabelle, Form 5 & 10)."""

    league_key: str
    season: str
    table_type: str  # 'overall' | 'home' | 'away' | 'form_5' | 'form_10'
    rank: int
    team_id: str
    team_name: str
    played: int
    wins: int
    draws: int
    losses: int
    goals_for: int
    goals_against: int
    goal_diff: int
    points: int
    form_last_5: str = ""
    form_last_10: str = ""
    deduction_note: str | None = None

    def state_fingerprint(self) -> str:
        return compute_payload_hash(
            {
                "rank": self.rank,
                "played": self.played,
                "wins": self.wins,
                "draws": self.draws,
                "losses": self.losses,
                "gf": self.goals_for,
                "ga": self.goals_against,
                "gd": self.goal_diff,
                "pts": self.points,
                "f5": self.form_last_5,
                "f10": self.form_last_10,
                "note": self.deduction_note,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MatchEvent:
    """Chronologisches Spielereignis (Tor, Vorlage, Karte, Elfmeter, Wechsel, VAR)."""

    match_id: str
    half: str  # '1. Hälfte' | '2. Hälfte' | 'Verlängerung' | 'Elfmeterschießen'
    minute: str  # z.B. "3'", "45+2'", "90+6'"
    event_type: str  # 'goal' | 'penalty_goal' | 'own_goal' | 'penalty_missed' | 'yellow_card' | 'yellow_red_card' | 'red_card' | 'substitution' | 'var'
    team_side: str  # 'home' | 'away' | 'unknown'
    player_name: str
    player_id: str | None = None
    secondary_player_name: str | None = None  # Assistgeber oder ausgewechselter Spieler
    secondary_player_id: str | None = None
    score_snapshot: str | None = None  # z.B. "1 - 0"
    detail: str | None = None  # z.B. "Elfmeter", "Halten", "VAR - Kein Tor"

    @property
    def event_key(self) -> str:
        return compute_payload_hash(
            {
                "m": self.match_id,
                "h": self.half,
                "min": self.minute,
                "t": self.event_type,
                "p": self.player_name,
                "s": self.secondary_player_name,
                "sc": self.score_snapshot,
                "d": self.detail,
            }
        )[:24]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["event_key"] = self.event_key
        return data


@dataclass(slots=True)
class MatchStatItem:
    """Einzelne Spielstatistik-Metrik für Gesamtspiel, 1. Hälfte oder 2. Hälfte."""

    match_id: str
    period: str  # 'gesamt' | '1-halfte' | '2-halfte'
    category: str  # 'Top-Statistiken' | 'Schüsse' | 'Angriff' | 'Pässe' | 'Verteidigung' | 'Torhüter'
    metric_name: str
    home_raw: str
    away_raw: str
    home_value: float | None = None
    away_value: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LineupPlayer:
    """Spieler in Startelf (bestätigt/voraussichtlich) oder auf der Ersatzbank."""

    match_id: str
    team_side: str  # 'home' | 'away'
    team_id: str
    lineup_status: str  # 'confirmed' | 'probable'
    role: str  # 'starter' | 'bench' | 'substituted_in'
    player_name: str
    player_id: str | None = None
    shirt_number: int | None = None
    position: str | None = None  # 'TW' | 'ABW' | 'MIT' | 'STU' | None
    nationality: str | None = None
    is_captain: bool = False
    is_goalkeeper: bool = False
    rating: float | None = None
    sub_minute: str | None = None
    replaced_player: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MatchLineupSummary:
    """Gesamtaufstellung eines Spiels inkl. Formationen, Kapitänen, Bank und Trainern."""

    match_id: str
    lineup_status: str  # 'confirmed' | 'probable'
    home_formation: str | None = None
    away_formation: str | None = None
    home_rating: float | None = None
    away_rating: float | None = None
    home_coach: str | None = None
    away_coach: str | None = None
    players: list[LineupPlayer] = field(default_factory=list)

    def state_fingerprint(self) -> str:
        return compute_payload_hash(
            {
                "status": self.lineup_status,
                "hf": self.home_formation,
                "af": self.away_formation,
                "hc": self.home_coach,
                "ac": self.away_coach,
                "players": [
                    (
                        p.team_side,
                        p.role,
                        p.shirt_number,
                        p.player_name,
                        p.is_captain,
                        p.sub_minute,
                        p.replaced_player,
                    )
                    for p in self.players
                ],
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "lineup_status": self.lineup_status,
            "home_formation": self.home_formation,
            "away_formation": self.away_formation,
            "home_rating": self.home_rating,
            "away_rating": self.away_rating,
            "home_coach": self.home_coach,
            "away_coach": self.away_coach,
            "players": [p.to_dict() for p in self.players],
        }


@dataclass(slots=True)
class InjuryRecord:
    """Verletzungs-, Sperren-, Krankheits- oder Fraglich-Eintrag eines Spielers."""

    player_id: str
    player_name: str
    team_id: str
    team_name: str
    league_key: str
    availability_status: str  # 'verletzt' | 'gesperrt' | 'krank' | 'fraglich' | 'abwesend' | 'sicher_ausfallend'
    reason: str
    expected_return: str | None = None
    since_date: str | None = None
    nationality: str | None = None
    source_match_id: str | None = None

    def state_fingerprint(self) -> str:
        return compute_payload_hash(
            {
                "pid": self.player_id,
                "tid": self.team_id,
                "status": self.availability_status,
                "reason": self.reason,
                "ret": self.expected_return,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PlayerProfile:
    """Spielerprofil inkl. Status, Position, Vertragsdaten und Leistungsdaten."""

    player_id: str
    player_name: str
    slug: str
    team_id: str | None = None
    team_name: str | None = None
    league_key: str | None = None
    position: str | None = None
    shirt_number: int | None = None
    nationality: str | None = None
    birth_date: str | None = None
    age: int | None = None
    market_value: str | None = None
    contract_until: str | None = None
    current_status: str = "fit"
    status_reason: str | None = None
    expected_return: str | None = None
    season_matches: int | None = None
    season_goals: int | None = None
    season_assists: int | None = None
    season_yellow_cards: int | None = None
    season_red_cards: int | None = None
    season_rating: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RefereeRecord:
    """Hauptschiedsrichter inkl. Verband und Einsatzstatistiken."""

    referee_id: str
    name: str
    country_code: str | None = None
    matches_officiated: int = 0
    yellow_cards_total: int = 0
    red_cards_total: int = 0
    penalties_awarded: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class StadiumRecord:
    """Stadionstammdaten inkl. Stadt, Land, Kapazität und Heimteams."""

    stadium_id: str
    name: str
    city: str | None = None
    country: str | None = None
    capacity: int | None = None
    home_teams: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MatchRecord:
    """Vollständiger Spieldatensatz (Spielplan, Live-Spiel oder Ergebnis)."""

    match_id: str
    league_key: str
    season: str
    matchday: str | None
    kickoff_raw: str
    status: str  # 'scheduled' | 'live' | 'finished' | 'postponed' | 'cancelled'
    phase: str  # 'Nicht begonnen' | '1. Halbzeit' | 'Halbzeit' | '2. Halbzeit' | 'Nachspielzeit' | 'Verlängerung' | 'Elfmeterschießen' | 'Beendet' | 'Verschoben' | 'Abgesagt'
    minute: str | None
    home_team_id: str
    home_team_name: str
    away_team_id: str
    away_team_name: str
    home_score: int | None = None
    away_score: int | None = None
    ht_home_score: int | None = None
    ht_away_score: int | None = None
    second_half_home_score: int | None = None
    second_half_away_score: int | None = None
    referee_name: str | None = None
    referee_country: str | None = None
    stadium_name: str | None = None
    stadium_city: str | None = None
    stadium_capacity: int | None = None
    attendance: int | None = None
    match_url: str | None = None

    @property
    def is_live(self) -> bool:
        return self.status == "live"

    def score_fingerprint(self) -> str:
        return compute_payload_hash(
            {
                "status": self.status,
                "phase": self.phase,
                "minute": self.minute,
                "hs": self.home_score,
                "as": self.away_score,
                "ht_h": self.ht_home_score,
                "ht_a": self.ht_away_score,
                "kickoff": self.kickoff_raw,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ChangeLogEntry:
    """Unveränderlicher Historieneintrag für jede erkannte Datenänderung."""

    detected_at: str
    league_key: str | None
    entity_type: str  # 'match' | 'standing' | 'lineup' | 'injury' | 'stats' | 'schedule'
    entity_id: str
    change_type: str
    summary: str
    old_value: dict[str, Any] | None
    new_value: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
