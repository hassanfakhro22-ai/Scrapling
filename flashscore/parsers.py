"""Scrapling-HTML-Parser für Flashscore.de (Ligen, Spiele, Live-Events, Statistiken, Kader, Verletzungen).

Wichtiger Hinweis gemäß Projektspezifikation:
Jegliche Buchmacher-/Wettquoten-Blöcke (`Quoten`, `bet365`, `bwin` usw.) werden aktiv ausgefiltert
und niemals extrahiert oder ausgewertet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from scrapling.parser import Selector

from flashscore.config import FLASHSCORE_BASE_URL, LeagueConfig
from flashscore.models import (
    InjuryRecord,
    LineupPlayer,
    MatchEvent,
    MatchLineupSummary,
    MatchRecord,
    MatchStatItem,
    PlayerProfile,
    StandingRow,
    TeamRecord,
)

_ODDS_NOISE_RE = re.compile(
    r"(?i)^(quoten|bet365|bwin|tipico|unibet|betano|1\s*x\s*2|glücksspiel|18\+|ggl)\b"
)
_TEAM_URL_RE = re.compile(r"/mannschaft/([^/]+)/([A-Za-z0-9]{6,12})/?")
_PLAYER_URL_RE = re.compile(r"/spieler/([^/]+)/([A-Za-z0-9]{6,12})/?")
_MATCH_URL_RE = re.compile(
    r"/spiel/fussball/([^/]+)-([A-Za-z0-9]{8})/([^/]+)-([A-Za-z0-9]{8})/\?mid=([A-Za-z0-9]{8})"
)
_MINUTE_RE = re.compile(r"^(\d{1,3}(?:\+\d{1,2})?'?)$")
_FORMATION_RE = re.compile(r"^\d(?:-\d){2,4}$")
_SCORE_SNAPSHOT_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_FORM_CHAR_MAP = {"S": "S", "W": "S", "U": "U", "D": "U", "N": "N", "L": "N"}


def _clean_lines(sel: Selector) -> list[str]:
    raw_text = sel.get_all_text(separator="\n", strip=True)
    lines: list[str] = []
    for line in str(raw_text).splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        if not cleaned or _ODDS_NOISE_RE.search(cleaned):
            continue
        lines.append(cleaned)
    return lines


def _parse_numeric(raw: str) -> float | None:
    s = raw.strip()
    m = re.match(r"^([+-]?\d+(?:[.,]\d+)?)\s*%?", s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _parse_spaced_int(raw: str) -> int | None:
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits else None


def classify_absence_status(reason: str, is_questionable_section: bool = False) -> str:
    """Klassifiziert Ausfälle in `verletzt`, `gesperrt`, `krank`, `fraglich` oder `sicher_ausfallend`."""
    if is_questionable_section:
        return "fraglich"
    low = reason.lower()
    if any(k in low for k in ("rote karte", "gelbsperre", "gesperrt", "sperre", "gelb-rot")):
        return "gesperrt"
    if any(k in low for k in ("krank", "grippe", "virus", "infekt", "fieber")):
        return "krank"
    if any(k in low for k in ("inaktiv", "nicht im kader", "trainerentscheidung", "persönliche", "nationalteam")):
        return "sicher_ausfallend"
    if "fraglich" in low:
        return "fraglich"
    return "verletzt"


def _extract_team_links(sel: Selector) -> dict[str, tuple[str, str]]:
    """Gibt `team_name -> (team_id, slug)` für alle Mannschaftslinks auf der Seite zurück."""
    mapping: dict[str, tuple[str, str]] = {}
    for a in sel.css('a[href*="/mannschaft/"]'):
        href = str(a.attrib.get("href", ""))
        m = _TEAM_URL_RE.search(href)
        if not m:
            continue
        slug, team_id = m.group(1), m.group(2)
        name = re.sub(r"\s+", " ", str(a.get_all_text(strip=True))).strip()
        if name and len(name) >= 2 and not name.isdigit():
            mapping[name] = (team_id, slug)
    return mapping


def _extract_player_links(sel: Selector) -> dict[str, tuple[str, str]]:
    """Gibt `player_name -> (player_id, slug)` für alle Spielerlinks auf der Seite zurück."""
    mapping: dict[str, tuple[str, str]] = {}
    for a in sel.css('a[href*="/spieler/"]'):
        href = str(a.attrib.get("href", ""))
        m = _PLAYER_URL_RE.search(href)
        if not m:
            continue
        slug, pid = m.group(1), m.group(2)
        name = re.sub(r"\s+", " ", str(a.get_all_text(strip=True))).strip()
        if name and len(name) >= 2:
            mapping[name] = (pid, slug)
    return mapping


@dataclass
class LeaguePageParseResult:
    matches: list[MatchRecord] = field(default_factory=list)
    teams: list[TeamRecord] = field(default_factory=list)
    standings: list[StandingRow] = field(default_factory=list)


@dataclass
class MatchDetailParseResult:
    match: MatchRecord | None = None
    events: list[MatchEvent] = field(default_factory=list)
    injuries: list[InjuryRecord] = field(default_factory=list)
    embedded_standings: list[StandingRow] = field(default_factory=list)
    teams: list[TeamRecord] = field(default_factory=list)


def parse_embedded_standings_from_lines(
    lines: list[str],
    team_links: dict[str, tuple[str, str]],
    league: LeagueConfig,
    table_type: str = "overall",
) -> list[StandingRow]:
    """Extrahiert Flashscore-SSR-Tabellen inklusive Punktabzugs-Hinweisen (z. B. Southampton -4)."""
    rows: list[StandingRow] = []
    seen_teams: set[str] = set()

    # Suche Start der Tabelle ("#" gefolgt von "Mannschaft" oder "Team")
    start_indices = [
        idx
        for idx, tok in enumerate(lines)
        if tok == "#" and idx + 2 < len(lines) and lines[idx + 1] in ("Mannschaft", "Team")
    ]
    if not start_indices:
        return rows

    i = start_indices[0] + 2
    n = len(lines)
    while i < n:
        tok = lines[i]
        if tok in ("Begegnungen", "Form", "Über/Unter", "Torschützen", "Fehlende Spieler", "Vorschau"):
            break

        rank_match = re.match(r"^(\d{1,2})\.(?:\s*(.*))?$", tok)
        if not rank_match:
            i += 1
            continue

        rank = int(rank_match.group(1))
        inline_note = (rank_match.group(2) or "").strip() or None
        deduction_note = inline_note
        i += 1

        # Überspringe eventuelle Zusatznotizen zwischen Rang und Vereinsname
        while i < n and (
            lines[i].startswith("-")
            or "Punkte" in lines[i]
            or lines[i] in ("Aufstieg", "Relegation", "Abstieg", "Champions League")
        ):
            if "Punkte" in lines[i] and not deduction_note:
                deduction_note = lines[i]
            i += 1

        if i >= n:
            break
        team_name = lines[i].strip()
        i += 1

        # Erwarte: Sp, S, U, N, Tore(GF:GA), TD, Pkt (7 Token)
        if i + 6 >= n:
            break
        try:
            played = int(lines[i])
            wins = int(lines[i + 1])
            draws = int(lines[i + 2])
            losses = int(lines[i + 3])
            goals_tok = lines[i + 4]
            gm = re.match(r"^(\d+)\s*:\s*(\d+)$", goals_tok)
            if not gm:
                continue
            gf, ga = int(gm.group(1)), int(gm.group(2))
            gd = int(lines[i + 5])
            pts = int(lines[i + 6])
            i += 7
        except ValueError:
            continue

        form_chars: list[str] = []
        while i < n and lines[i] in ("?", "S", "U", "N", "W", "D", "L"):
            if lines[i] != "?":
                form_chars.append(_FORM_CHAR_MAP[lines[i]])
            i += 1

        tid, _slug = team_links.get(
            team_name,
            (re.sub(r"[^a-z0-9]+", "", team_name.lower())[:12] or f"team{rank}", ""),
        )
        if tid in seen_teams:
            continue
        seen_teams.add(tid)

        rows.append(
            StandingRow(
                league_key=league.league_key,
                season=league.season,
                table_type=table_type,
                rank=rank,
                team_id=tid,
                team_name=team_name,
                played=played,
                wins=wins,
                draws=draws,
                losses=losses,
                goals_for=gf,
                goals_against=ga,
                goal_diff=gd,
                points=pts,
                form_last_5="".join(form_chars[:5]),
                form_last_10="".join(form_chars[:10]),
                deduction_note=deduction_note,
            )
        )

    return rows


def parse_league_page(html: str, league: LeagueConfig) -> LeaguePageParseResult:
    """Parst eine Flashscore-Ligaseite (Übersicht, Ergebnisse oder Spielplan)."""
    sel = Selector(html)
    lines = _clean_lines(sel)
    team_links = _extract_team_links(sel)
    result = LeaguePageParseResult()

    teams_by_id: dict[str, TeamRecord] = {}
    for tname, (tid, tslug) in team_links.items():
        teams_by_id[tid] = TeamRecord(
            team_id=tid,
            name=tname,
            slug=tslug,
            country=league.country,
            league_key=league.league_key,
        )

    # 1. Sammle alle Spiel-Links mit Reihenfolge aus dem DOM
    match_anchors: list[tuple[str, str, str, str, str, str]] = []
    seen_mids: set[str] = set()
    for a in sel.css('a[href*="/spiel/fussball/"]'):
        href = str(a.attrib.get("href", ""))
        m = _MATCH_URL_RE.search(href)
        if not m:
            parsed = urlparse(href)
            qs = parse_qs(parsed.query)
            mid_list = qs.get("mid", [])
            if not mid_list:
                continue
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) < 4:
                continue
            h_part, a_part, mid = parts[-2], parts[-1], mid_list[0]
            hm = re.match(r"^(.+)-([A-Za-z0-9]{8})$", h_part)
            am = re.match(r"^(.+)-([A-Za-z0-9]{8})$", a_part)
            if not (hm and am):
                continue
            h_slug, h_id = hm.group(1), hm.group(2)
            a_slug, a_id = am.group(1), am.group(2)
        else:
            h_slug, h_id, a_slug, a_id, mid = m.groups()

        if mid in seen_mids:
            continue
        seen_mids.add(mid)
        full_url = href if href.startswith("http") else f"{FLASHSCORE_BASE_URL}{href}"
        match_anchors.append((mid, h_slug, h_id, a_slug, a_id, full_url))

    # Map team_id -> kanonischer Anzeigename
    id_to_name: dict[str, str] = {tid: rec.name for tid, rec in teams_by_id.items()}
    for _, h_slug, h_id, a_slug, a_id, _ in match_anchors:
        if h_id not in id_to_name:
            pretty_h = h_slug.replace("-", " ").title()
            id_to_name[h_id] = pretty_h
            teams_by_id[h_id] = TeamRecord(h_id, pretty_h, h_slug, league.country, league.league_key)
        if a_id not in id_to_name:
            pretty_a = a_slug.replace("-", " ").title()
            id_to_name[a_id] = pretty_a
            teams_by_id[a_id] = TeamRecord(a_id, pretty_a, a_slug, league.country, league.league_key)

    # 2. Durchlaufe Textzeilen für Spieltag, Anstoßzeit, Live-Minute und Spielstände
    matchday_by_idx: dict[int, str] = {}
    current_md: str | None = None
    for idx, line in enumerate(lines):
        if re.match(r"^\d{1,2}\.\s*Spieltag$", line) or "Runde" in line:
            current_md = line
        matchday_by_idx[idx] = current_md or ""

    search_cursor = 0
    date_time_re = re.compile(r"^\d{2}\.\d{2}\.(?:\d{4})?\s+\d{2}:\d{2}$|^\d{2}:\d{2}$")

    for mid, _h_slug, h_id, _a_slug, a_id, full_url in match_anchors:
        h_name = id_to_name[h_id]
        a_name = id_to_name[a_id]
        found_idx = -1
        for idx in range(search_cursor, len(lines)):
            if lines[idx] == h_name:
                window = lines[idx : min(len(lines), idx + 8)]
                if a_name in window:
                    found_idx = idx
                    search_cursor = idx + 1
                    break
        if found_idx == -1:
            for idx in range(0, len(lines)):
                if lines[idx] == h_name and a_name in lines[idx : min(len(lines), idx + 8)]:
                    found_idx = idx
                    break

        kickoff_raw = ""
        status = "scheduled"
        phase = "NS"
        minute: str | None = None
        h_score: int | None = None
        a_score: int | None = None
        ht_h: int | None = None
        ht_a: int | None = None
        md = matchday_by_idx.get(found_idx, None) or None

        if found_idx != -1:
            prev_tokens = lines[max(0, found_idx - 3) : found_idx]
            for ptok in reversed(prev_tokens):
                if date_time_re.match(ptok):
                    kickoff_raw = ptok
                    break
                if ptok in ("Verlegt", "Verschoben"):
                    status, phase = "postponed", "POSTP"
                elif ptok in ("Abgesagt", "Abbruch"):
                    status, phase = "cancelled", "CANC"
                elif ptok in ("Halbzeit", "Pause"):
                    status, phase, minute = "live", "HT", "HT"
                elif _MINUTE_RE.match(ptok):
                    status = "live"
                    minute = ptok if ptok.endswith("'") else f"{ptok}'"
                    min_num = int(re.match(r"^\d+", ptok).group(0))  # type: ignore[union-attr]
                    phase = "1H" if min_num <= 45 else ("2H" if min_num <= 90 else "ET")
                elif ptok in ("Beendet", "Nach Verlängerung", "Nach Elfmeterschießen"):
                    status = "finished"
                    phase = "ET" if "Verlängerung" in ptok else "FT"

            after = lines[found_idx + 1 : min(len(lines), found_idx + 9)]
            if a_name in after:
                a_pos = after.index(a_name)
                tail = after[a_pos + 1 :]
            else:
                tail = after

            nums: list[int] = []
            for t in tail[:4]:
                if re.match(r"^\d+$", t):
                    nums.append(int(t))
                elif re.match(r"^\(\d+\)$", t):
                    nums.append(int(t.strip("()")))
                elif t in ("-", "Vorschau"):
                    break
            if len(nums) >= 2:
                h_score, a_score = nums[0], nums[1]
                if status == "scheduled":
                    status, phase = "finished", "FT"
                if len(nums) >= 4:
                    ht_h, ht_a = nums[2], nums[3]

        result.matches.append(
            MatchRecord(
                match_id=mid,
                league_key=league.league_key,
                season=league.season,
                matchday=md,
                kickoff_raw=kickoff_raw,
                status=status,
                phase=phase,
                minute=minute,
                home_team_id=h_id,
                home_team_name=h_name,
                away_team_id=a_id,
                away_team_name=a_name,
                home_score=h_score,
                away_score=a_score,
                ht_home_score=ht_h,
                ht_away_score=ht_a,
                match_url=full_url,
            )
        )

    result.standings = parse_embedded_standings_from_lines(lines, team_links, league, "overall")
    result.teams = list(teams_by_id.values())
    return result


def parse_match_detail_page(
    html: str,
    league: LeagueConfig,
    match_id: str,
    match_url: str | None = None,
) -> MatchDetailParseResult:
    """Parst eine Spiel-Detailseite (`/?mid=...`) inkl. Live-/Endstand, Events, Ausfälle, Schiri, Stadion & Tabelle."""
    sel = Selector(html)
    lines = _clean_lines(sel)
    team_links = _extract_team_links(sel)
    player_links = _extract_player_links(sel)
    out = MatchDetailParseResult()

    # Bestimme Heim- und Auswärtsteam aus den ersten beiden Mannschaftslinks im Header
    header_teams: list[tuple[str, str, str]] = []
    for a in sel.css('a[href*="/mannschaft/"]'):
        href = str(a.attrib.get("href", ""))
        m = _TEAM_URL_RE.search(href)
        if not m:
            continue
        slug, tid = m.group(1), m.group(2)
        name = re.sub(r"\s+", " ", str(a.get_all_text(strip=True))).strip()
        if name and (not header_teams or header_teams[-1][1] != tid):
            header_teams.append((name, tid, slug))
        if len(header_teams) >= 2:
            break

    if len(header_teams) < 2 and match_url:
        um = _MATCH_URL_RE.search(match_url)
        if um:
            hs, hid, as_, aid, _ = um.groups()
            header_teams = [
                (hs.replace("-", " ").title(), hid, hs),
                (as_.replace("-", " ").title(), aid, as_),
            ]

    if len(header_teams) >= 2:
        (h_name, h_id, h_slug), (a_name, a_id, a_slug) = header_teams[0], header_teams[1]
    else:
        h_name, h_id, h_slug = ("Heimteam", "home_unknown", "heimteam")
        a_name, a_id, a_slug = ("Auswärtsteam", "away_unknown", "auswaertsteam")

    out.teams.extend(
        [
            TeamRecord(h_id, h_name, h_slug, league.country, league.league_key),
            TeamRecord(a_id, a_name, a_slug, league.country, league.league_key),
        ]
    )

    kickoff_raw = ""
    matchday: str | None = None
    status = "scheduled"
    phase = "NS"
    minute: str | None = None
    h_score: int | None = None
    a_score: int | None = None
    ht_h: int | None = None
    ht_a: int | None = None
    h2_h: int | None = None
    h2_a: int | None = None

    # Extrahiere Spielstand & Status ausschließlich aus dem Header vor "Bericht"/"Übersicht"/"1. Halbzeit"
    header_end = next(
        (
            idx
            for idx, tok in enumerate(lines[:40])
            if tok in ("Bericht", "Übersicht", "Spielinfo", "Vorschau", "1. Halbzeit")
        ),
        min(len(lines), 25),
    )
    header_slice = lines[:header_end]

    for line in header_slice:
        if not kickoff_raw and re.match(r"^\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}$", line):
            kickoff_raw = line
        if not matchday and ("Spieltag" in line or "Runde" in line):
            md_m = re.search(r"(\d{1,2}\.\s*Spieltag|Runde\s*\d+)", line)
            if md_m:
                matchday = md_m.group(1)
        if line in ("Beendet", "Nach Verlängerung", "Nach Elfmeterschießen"):
            status = "finished"
            phase = "ET" if "Verlängerung" in line else "FT"
        elif line in ("Verlegt", "Verschoben"):
            status, phase = "postponed", "POSTP"
        elif line in ("Abgesagt", "Abbruch"):
            status, phase = "cancelled", "CANC"
        elif line in ("Halbzeit", "Pause") and status != "finished":
            status, phase, minute = "live", "HT", "HT"
        elif re.match(r"^\d{1,3}(?:\+\d{1,2})?'$", line) and status != "finished":
            status = "live"
            minute = line
            mnum = int(re.match(r"^\d+", line).group(0))  # type: ignore[union-attr]
            phase = "1H" if mnum <= 45 else ("2H" if mnum <= 90 else "ET")

    for idx in range(len(header_slice) - 2):
        if (
            re.match(r"^\d+$", header_slice[idx])
            and header_slice[idx + 1] == "-"
            and re.match(r"^\d+$", header_slice[idx + 2])
        ):
            h_score = int(header_slice[idx])
            a_score = int(header_slice[idx + 2])
            if status == "scheduled":
                status, phase = "finished", "FT"
            break

    # Halbzeitstände & Ereignisse ("1. Halbzeit", "2. Halbzeit", "Verlängerung")
    current_half = ""
    last_score = (0, 0)
    idx = header_end
    n = len(lines)
    stop_event_sections = {
        "Spielinformationen",
        "Wird nicht spielen",
        "Fehlende Spieler",
        "Fraglich",
        "Vorschau",
        "Tabelle",
    }

    while idx < n:
        tok = lines[idx]
        if tok in stop_event_sections:
            break
        if tok in ("1. Halbzeit", "2. Halbzeit", "Verlängerung", "Elfmeterschießen"):
            current_half = tok
            if idx + 3 < n and re.match(r"^\d+$", lines[idx + 1]) and lines[idx + 2] == "-" and re.match(r"^\d+$", lines[idx + 3]):
                s1, s2 = int(lines[idx + 1]), int(lines[idx + 3])
                if tok == "1. Halbzeit":
                    ht_h, ht_a = s1, s2
                elif tok == "2. Halbzeit":
                    h2_h, h2_a = s1, s2
                idx += 4
                continue
            idx += 1
            continue

        if current_half and re.match(r"^\d{1,3}(?:\+\d{1,2})?'$", tok):
            ev_min = tok
            chunk: list[str] = []
            j = idx + 1
            while j < n:
                nt = lines[j]
                if (
                    re.match(r"^\d{1,3}(?:\+\d{1,2})?'$", nt)
                    or nt in ("1. Halbzeit", "2. Halbzeit", "Verlängerung", "Elfmeterschießen")
                    or nt in stop_event_sections
                ):
                    break
                chunk.append(nt)
                j += 1

            if chunk:
                ev = _classify_event_chunk(match_id, ev_min, current_half, chunk, last_score, player_links)
                if ev is not None:
                    if ev.score_snapshot:
                        sm = _SCORE_SNAPSHOT_RE.match(ev.score_snapshot)
                        if sm:
                            last_score = (int(sm.group(1)), int(sm.group(2)))
                    out.events.append(ev)
            idx = j
            continue

        idx += 1

    # Extrahiere Ausfälle ("Wird nicht spielen" / "Fehlende Spieler") und "Fraglich"
    out.injuries.extend(
        _parse_match_absences(lines, player_links, league.league_key, h_id, h_name, a_id, a_name, match_id)
    )

    # Extrahiere Spielinformationen: Schiedsrichter, Stadion, Kapazität, Zuschauer
    ref_name: str | None = None
    ref_country: str | None = None
    stad_name: str | None = None
    stad_city: str | None = None
    stad_cap: int | None = None
    attendance: int | None = None

    for i, tok in enumerate(lines):
        if tok.startswith("Schiedsrichter") and i + 1 < n:
            raw_ref = lines[i + 1]
            cm = re.match(r"^(.+?)\s*\(([A-Za-z]{2,3})\)$", raw_ref)
            if cm:
                ref_name, ref_country = cm.group(1).strip(), cm.group(2).strip()
            else:
                ref_name = raw_ref.strip()
                if i + 2 < n and re.match(r"^\([A-Za-z]{2,3}\)$", lines[i + 2]):
                    ref_country = lines[i + 2].strip("()")
        elif tok.startswith("Stadion") and i + 1 < n and stad_name is None:
            cand = lines[i + 1]
            if cand not in ("Anzahl Plätze", "Zuschauer") and not cand.isdigit():
                sm = re.match(r"^(.+?)\s*\(([^)]+)\)$", cand)
                if sm:
                    stad_name, stad_city = sm.group(1).strip(), sm.group(2).strip()
                else:
                    stad_name = cand.strip()
                    if i + 2 < n and lines[i + 2].startswith("(") and lines[i + 2].endswith(")"):
                        stad_city = lines[i + 2].strip("()")
        elif tok.startswith("Anzahl Plätze") and i + 1 < n:
            stad_cap = _parse_spaced_int(lines[i + 1])
        elif tok.startswith("Zuschauer") and i + 1 < n:
            attendance = _parse_spaced_int(lines[i + 1])

    out.match = MatchRecord(
        match_id=match_id,
        league_key=league.league_key,
        season=league.season,
        matchday=matchday,
        kickoff_raw=kickoff_raw,
        status=status,
        phase=phase,
        minute=minute,
        home_team_id=h_id,
        home_team_name=h_name,
        away_team_id=a_id,
        away_team_name=a_name,
        home_score=h_score,
        away_score=a_score,
        ht_home_score=ht_h,
        ht_away_score=ht_a,
        second_half_home_score=h2_h,
        second_half_away_score=h2_a,
        referee_name=ref_name,
        referee_country=ref_country,
        stadium_name=stad_name,
        stadium_city=stad_city,
        stadium_capacity=stad_cap,
        attendance=attendance,
        match_url=match_url,
    )

    out.embedded_standings = parse_embedded_standings_from_lines(lines, team_links, league, "overall")
    return out


def _classify_event_chunk(
    match_id: str,
    minute: str,
    half: str,
    chunk: list[str],
    prev_score: tuple[int, int],
    player_links: dict[str, tuple[str, str]],
) -> MatchEvent | None:
    cleaned = [c.strip("()") for c in chunk if c.strip()]
    if not cleaned:
        return None

    joined = " | ".join(chunk)
    score_tok: str | None = None
    team_side = "unknown"
    for c in chunk:
        sm = _SCORE_SNAPSHOT_RE.match(c)
        if sm:
            nh, na = int(sm.group(1)), int(sm.group(2))
            score_tok = f"{nh} - {na}"
            if nh > prev_score[0] and na == prev_score[1]:
                team_side = "home"
            elif na > prev_score[1] and nh == prev_score[0]:
                team_side = "away"
            break

    non_score = [c for c in chunk if not _SCORE_SNAPSHOT_RE.match(c)]
    if not non_score:
        return None

    # 1. Tor / Elfmetertor / Eigentor
    if score_tok is not None:
        ev_type = "goal"
        detail: str | None = None
        names: list[str] = []
        for item in non_score:
            stripped = item.strip("()")
            low = stripped.lower()
            if "elfmeter" in low:
                ev_type = "penalty_goal"
                detail = stripped
            elif "eigentor" in low:
                ev_type = "own_goal"
                detail = stripped
            else:
                names.append(stripped)
        scorer = names[0] if names else non_score[0]
        assist = names[1] if len(names) >= 2 else None
        pid = player_links.get(scorer, (None, ""))[0]
        aid = player_links.get(assist, (None, ""))[0] if assist else None
        return MatchEvent(
            match_id=match_id,
            half=half,
            minute=minute,
            event_type=ev_type,
            team_side=team_side,
            player_name=scorer,
            player_id=pid,
            secondary_player_name=assist,
            secondary_player_id=aid,
            score_snapshot=score_tok,
            detail=detail,
        )

    low_joined = joined.lower()
    # 2. VAR-Entscheidung
    if "var" in low_joined or "kein tor" in low_joined or "kein elfmeter" in low_joined or "tor bestätigt" in low_joined:
        p_name = non_score[0].strip("()")
        det = ", ".join(x.strip("()") for x in non_score[1:]) or "VAR-Überprüfung"
        return MatchEvent(
            match_id=match_id,
            half=half,
            minute=minute,
            event_type="var",
            team_side=team_side,
            player_name=p_name,
            player_id=player_links.get(p_name, (None, ""))[0],
            detail=det,
        )

    # 3. Verschossener Elfmeter
    if "elfmeter verschossen" in low_joined:
        p_name = non_score[0].strip("()")
        return MatchEvent(
            match_id=match_id,
            half=half,
            minute=minute,
            event_type="penalty_missed",
            team_side=team_side,
            player_name=p_name,
            player_id=player_links.get(p_name, (None, ""))[0],
            detail="Elfmeter verschossen",
        )

    # 4. Karten (Rot, Gelb-Rot, Gelb)
    card_reasons = (
        "foul",
        "unsportliches verhalten",
        "handspiel",
        "meckern",
        "spielverzögerung",
        "zeitspiel",
        "schwalbe",
        "notbremse",
        "grobes spiel",
        "tätlichkeit",
        "halten",
        "beinstellen",
        "gelb-rot",
        "rote karte",
    )
    if any(r in low_joined for r in card_reasons):
        p_name = non_score[0].strip("()")
        det = ", ".join(x.strip("()") for x in non_score[1:]) or None
        if "gelb-rot" in low_joined:
            ctype = "yellow_red_card"
        elif any(k in low_joined for k in ("notbremse", "tätlichkeit", "grobes spiel", "rote karte")):
            ctype = "red_card"
        else:
            ctype = "yellow_card"
        return MatchEvent(
            match_id=match_id,
            half=half,
            minute=minute,
            event_type=ctype,
            team_side=team_side,
            player_name=p_name,
            player_id=player_links.get(p_name, (None, ""))[0],
            detail=det,
        )

    # 5. Auswechslung (2 Spielernamen hintereinander, optional "(Verletzung)")
    if len(non_score) >= 2:
        p_in = non_score[0].strip("()")
        p_out = non_score[1].strip("()")
        sub_detail = ", ".join(x.strip("()") for x in non_score[2:]) or None
        return MatchEvent(
            match_id=match_id,
            half=half,
            minute=minute,
            event_type="substitution",
            team_side=team_side,
            player_name=p_in,
            player_id=player_links.get(p_in, (None, ""))[0],
            secondary_player_name=p_out,
            secondary_player_id=player_links.get(p_out, (None, ""))[0],
            detail=sub_detail,
        )

    p_single = non_score[0].strip("()")
    return MatchEvent(
        match_id=match_id,
        half=half,
        minute=minute,
        event_type="yellow_card",
        team_side=team_side,
        player_name=p_single,
        player_id=player_links.get(p_single, (None, ""))[0],
    )


def _parse_match_absences(
    lines: list[str],
    player_links: dict[str, tuple[str, str]],
    league_key: str,
    home_team_id: str,
    home_team_name: str,
    away_team_id: str,
    away_team_name: str,
    match_id: str,
) -> list[InjuryRecord]:
    """Parst `Wird nicht spielen` / `Fehlende Spieler` sowie `Fraglich` aus Spielbericht oder Kaderseite."""
    injuries: list[InjuryRecord] = []
    stop_headers = {
        "Fraglich",
        "Spielinformationen",
        "Vorschau",
        "Tabelle",
        "Trainer",
        "Schiedsrichter",
        "Ersatzspieler",
        "Startaufstellungen",
    }

    for section_title, is_q in (
        ("Wird nicht spielen", False),
        ("Fehlende Spieler", False),
        ("Fraglich", True),
    ):
        for idx, tok in enumerate(lines):
            if tok != section_title:
                continue
            entries: list[tuple[str, str]] = []
            j = idx + 1
            while j + 1 < len(lines):
                p_cand = lines[j]
                if p_cand in stop_headers or p_cand.startswith("Schiedsrichter") or p_cand.startswith("Stadion"):
                    break
                reason_cand = lines[j + 1]
                if reason_cand in stop_headers:
                    break
                # Überspringe reine Nationalitäts-Labels, falls vorhanden
                if len(p_cand) >= 2 and not p_cand.isdigit():
                    entries.append((p_cand, reason_cand.strip("()")))
                j += 2

            midpoint = max(1, (len(entries) + 1) // 2)
            for pos_idx, (pname, reason) in enumerate(entries):
                t_id = home_team_id if pos_idx < midpoint else away_team_id
                t_name = home_team_name if pos_idx < midpoint else away_team_name
                pid = player_links.get(
                    pname,
                    (re.sub(r"[^a-z0-9]+", "_", f"pl_{t_id}_{pname.lower()}").strip("_"), ""),
                )[0]
                injuries.append(
                    InjuryRecord(
                        player_id=pid,
                        player_name=pname,
                        team_id=t_id,
                        team_name=t_name,
                        league_key=league_key,
                        availability_status=classify_absence_status(reason, is_questionable_section=is_q),
                        reason=reason,
                        source_match_id=match_id,
                    )
                )
    return injuries


def parse_match_stats_page(
    html: str, match_id: str, period: str = "gesamt"
) -> list[MatchStatItem]:
    """Parst `/bericht/stats/?mid=...` (Gesamt, 1. Hälfte, 2. Hälfte) mit allen Kategorien."""
    sel = Selector(html)
    lines = _clean_lines(sel)
    categories = {
        "Top-Statistiken",
        "Schüsse",
        "Angriff",
        "Pässe",
        "Verteidigung",
        "Torhüter",
        "Disziplin",
    }
    val_re = re.compile(r"^[+-]?\d+(?:[.,]\d+)?%?(?:\s*\(\d+/\d+\))?$")

    start_idx = next((i for i, t in enumerate(lines) if t in categories), -1)
    if start_idx == -1:
        return []

    items: list[MatchStatItem] = []
    seen_metrics: set[str] = set()
    current_cat = "Top-Statistiken"
    i = start_idx
    n = len(lines)

    while i < n:
        tok = lines[i]
        if tok in ("Spielinformationen", "Wird nicht spielen", "Tabelle", "Folge uns"):
            break
        if tok in categories:
            current_cat = tok
            i += 1
            continue

        if i + 2 < n and val_re.match(tok) and not val_re.match(lines[i + 1]) and val_re.match(lines[i + 2]):
            h_raw = tok
            metric = lines[i + 1]
            a_raw = lines[i + 2]
            if metric not in seen_metrics:
                seen_metrics.add(metric)
                items.append(
                    MatchStatItem(
                        match_id=match_id,
                        period=period,
                        category=current_cat,
                        metric_name=metric,
                        home_raw=h_raw,
                        away_raw=a_raw,
                        home_value=_parse_numeric(h_raw),
                        away_value=_parse_numeric(a_raw),
                    )
                )
            i += 3
            continue
        i += 1

    return items


def parse_match_lineups_page(
    html: str,
    match_id: str,
    league_key: str,
    home_team_id: str,
    home_team_name: str,
    away_team_id: str,
    away_team_name: str,
) -> tuple[MatchLineupSummary | None, list[InjuryRecord]]:
    """Parst `/bericht/kader/?mid=...` (bestätigte & voraussichtliche Aufstellung, Bank, Wechsel, Ausfälle)."""
    sel = Selector(html)
    lines = _clean_lines(sel)
    player_links = _extract_player_links(sel)

    lineup_status: str | None = None
    start_idx = -1
    for i, tok in enumerate(lines):
        if tok == "Startaufstellungen":
            lineup_status = "confirmed"
            start_idx = i
            break
        if tok == "Voraussichtliche Aufstellungen":
            lineup_status = "probable"
            start_idx = i
            break

    injuries = _parse_match_absences(
        lines,
        player_links,
        league_key,
        home_team_id,
        home_team_name,
        away_team_id,
        away_team_name,
        match_id,
    )

    if start_idx == -1 or lineup_status is None:
        return None, injuries

    formations: list[str] = []
    ratings: list[float] = []
    j = start_idx + 1
    while j < min(len(lines), start_idx + 10):
        t = lines[j]
        if _FORMATION_RE.match(t):
            formations.append(t)
        elif re.match(r"^\d\.\d{1,2}$", t):
            ratings.append(float(t))
        elif re.match(r"^\d{1,2}$", t):
            break
        j += 1

    home_formation = formations[0] if len(formations) >= 1 else None
    away_formation = formations[1] if len(formations) >= 2 else (formations[0] if formations else None)
    home_rating = ratings[0] if len(ratings) >= 1 else None
    away_rating = ratings[1] if len(ratings) >= 2 else None

    # Finde Sektionsgrenzen
    subs_idx = next((i for i in range(j, len(lines)) if lines[i] == "Auswechslungen"), -1)
    bench_idx = next((i for i in range(j, len(lines)) if lines[i] in ("Ersatzspieler", "Ersatzbank")), -1)
    missing_idx = next(
        (i for i in range(j, len(lines)) if lines[i] in ("Fehlende Spieler", "Wird nicht spielen", "Fraglich")),
        -1,
    )
    coach_idx = next((i for i in range(j, len(lines)) if lines[i] == "Trainer"), -1)

    starters_end = min(
        x for x in (subs_idx, bench_idx, missing_idx, coach_idx, len(lines)) if x != -1
    )
    starters_raw = _parse_player_block(lines[j:starters_end], player_links)
    players: list[LineupPlayer] = []

    half_split = 11 if len(starters_raw) >= 22 else max(1, (len(starters_raw) + 1) // 2)
    for idx_p, item in enumerate(starters_raw):
        side = "home" if idx_p < half_split else "away"
        tid = home_team_id if side == "home" else away_team_id
        players.append(
            LineupPlayer(
                match_id=match_id,
                team_side=side,
                team_id=tid,
                lineup_status=lineup_status,
                role="starter",
                player_name=item.name,
                player_id=item.player_id,
                shirt_number=item.shirt,
                position="TW" if item.is_gk else None,
                is_captain=item.is_captain,
                is_goalkeeper=item.is_gk,
                rating=item.rating,
            )
        )

    # Auswechslungen mit Minute & ersetztem Spieler
    sub_minutes: dict[str, tuple[str, str | None]] = {}
    if subs_idx != -1:
        subs_end = min(x for x in (bench_idx, missing_idx, coach_idx, len(lines)) if x != -1 and x > subs_idx)
        s_lines = lines[subs_idx + 1 : subs_end]
        k = 0
        while k < len(s_lines):
            if re.match(r"^\d{1,3}(?:\+\d{1,2})?'$", s_lines[k]):
                smin = s_lines[k]
                k += 1
                if k < len(s_lines) and re.match(r"^\d{1,2}$", s_lines[k]):
                    k += 1
                p_in = s_lines[k] if k < len(s_lines) else ""
                k += 1
                p_out: str | None = None
                if k < len(s_lines) and not re.match(r"^\d", s_lines[k]):
                    p_out = s_lines[k]
                    k += 1
                if k < len(s_lines) and re.match(r"^\d\.\d$", s_lines[k]):
                    k += 1
                if p_in:
                    sub_minutes[p_in] = (smin, p_out)
                continue
            k += 1

    # Ersatzbank
    if bench_idx != -1:
        bench_end = min(x for x in (missing_idx, coach_idx, len(lines)) if x != -1 and x > bench_idx)
        bench_raw = _parse_player_block(lines[bench_idx + 1 : bench_end], player_links)
        b_split = max(1, (len(bench_raw) + 1) // 2)
        for idx_b, item in enumerate(bench_raw):
            side = "home" if idx_b < b_split else "away"
            tid = home_team_id if side == "home" else away_team_id
            s_info = sub_minutes.get(item.name)
            players.append(
                LineupPlayer(
                    match_id=match_id,
                    team_side=side,
                    team_id=tid,
                    lineup_status=lineup_status,
                    role="bench",
                    player_name=item.name,
                    player_id=item.player_id,
                    shirt_number=item.shirt,
                    position="TW" if item.is_gk else None,
                    is_captain=item.is_captain,
                    is_goalkeeper=item.is_gk,
                    rating=item.rating,
                    sub_minute=s_info[0] if s_info else None,
                    replaced_player=s_info[1] if s_info else None,
                )
            )

    home_coach: str | None = None
    away_coach: str | None = None
    if coach_idx != -1:
        c_candidates = [
            c
            for c in lines[coach_idx + 1 : min(len(lines), coach_idx + 6)]
            if len(c) >= 3 and not c.isdigit() and c not in ("Vorschau", "Tabelle", "Spielinformationen")
        ]
        if len(c_candidates) >= 1:
            home_coach = c_candidates[0]
        if len(c_candidates) >= 2:
            away_coach = c_candidates[1]

    summary = MatchLineupSummary(
        match_id=match_id,
        lineup_status=lineup_status,
        home_formation=home_formation,
        away_formation=away_formation,
        home_rating=home_rating,
        away_rating=away_rating,
        home_coach=home_coach,
        away_coach=away_coach,
        players=players,
    )
    return summary, injuries


@dataclass(slots=True)
class _ParsedPlayerItem:
    name: str
    player_id: str | None
    shirt: int | None
    is_gk: bool
    is_captain: bool
    rating: float | None


def _parse_player_block(
    block_lines: list[str], player_links: dict[str, tuple[str, str]]
) -> list[_ParsedPlayerItem]:
    parsed: list[_ParsedPlayerItem] = []
    i = 0
    n = len(block_lines)
    while i < n:
        tok = block_lines[i]
        if re.match(r"^\d{1,2}$", tok) and i + 1 < n:
            shirt = int(tok)
            raw_name = block_lines[i + 1]
            i += 2
        elif not re.match(r"^\d\.\d$", tok) and tok not in ("(TW)", "(C)", "(TW) (C)", "*"):
            shirt = None
            raw_name = tok
            i += 1
        else:
            i += 1
            continue

        is_gk = "(TW)" in raw_name or "(G)" in raw_name
        is_cap = "(C)" in raw_name
        clean_name = re.sub(r"\s*\((?:TW|G|C)\)\s*", " ", raw_name).strip()

        while i < n and block_lines[i] in ("(TW)", "(C)", "(G)", "(TW) (C)", "*"):
            if "TW" in block_lines[i] or "G" in block_lines[i]:
                is_gk = True
            if "C" in block_lines[i]:
                is_cap = True
            i += 1

        rating: float | None = None
        if i < n and re.match(r"^\d\.\d{1,2}$", block_lines[i]):
            rating = float(block_lines[i])
            i += 1

        if clean_name and len(clean_name) >= 2:
            pid = player_links.get(clean_name, (None, ""))[0]
            parsed.append(
                _ParsedPlayerItem(
                    name=clean_name,
                    player_id=pid,
                    shirt=shirt,
                    is_gk=is_gk,
                    is_captain=is_cap,
                    rating=rating,
                )
            )
    return parsed


def parse_player_profile_and_injuries(
    html: str,
    player_id: str,
    slug: str,
    league_key: str = "",
) -> tuple[PlayerProfile, list[InjuryRecord]]:
    """Parst `/spieler/{slug}/{id}/` bzw. `/verletzungshistorie/` inkl. Rückkehrdatum & Verletzungschronik."""
    sel = Selector(html)
    lines = _clean_lines(sel)
    team_links = _extract_team_links(sel)

    player_name = slug.replace("-", " ").title()
    for h_el in sel.css("h2, h1"):
        txt = re.sub(r"\s+", " ", str(h_el.get_all_text(strip=True))).strip()
        if txt and len(txt) >= 3 and "Flashscore" not in txt:
            player_name = txt
            break

    position: str | None = None
    shirt_number: int | None = None
    team_name: str | None = None
    team_id: str | None = None
    age: int | None = None
    birth_date: str | None = None
    market_value: str | None = None
    contract_until: str | None = None
    current_status = "fit"
    status_reason: str | None = None
    expected_return: str | None = None

    for idx, tok in enumerate(lines[:50]):
        pos_m = re.match(r"^(Torwart|Abwehr|Mittelfeld|Sturm)(?:\s*\((.+?)\s*(\d{1,2})?\))?$", tok)
        if pos_m:
            position = pos_m.group(1)
            if pos_m.group(2):
                team_name = pos_m.group(2).strip()
            if pos_m.group(3):
                shirt_number = int(pos_m.group(3))
        if tok.startswith("Alter:") and idx + 1 < len(lines):
            am = re.search(r"(\d{2})", lines[idx + 1])
            bm = re.search(r"\((\d{2}\.\d{2}\.\d{4})\)", lines[idx + 1])
            if am:
                age = int(am.group(1))
            if bm:
                birth_date = bm.group(1)
        elif "Alter:" in tok:
            am = re.search(r"Alter:\s*(\d{2})", tok)
            bm = re.search(r"\((\d{2}\.\d{2}\.\d{4})\)", tok)
            if am:
                age = int(am.group(1))
            if bm:
                birth_date = bm.group(1)
        if "Marktwert" in tok and idx + 1 < len(lines) and "€" in lines[idx + 1]:
            market_value = lines[idx + 1]
        if "Vertrag läuft bis" in tok:
            cm = re.search(r"(\d{2}\.\d{2}\.\d{4})", tok)
            if cm:
                contract_until = cm.group(1)
            elif idx + 1 < len(lines):
                cm2 = re.search(r"(\d{2}\.\d{2}\.\d{4})", lines[idx + 1])
                if cm2:
                    contract_until = cm2.group(1)
        if "Rückkehr" in tok:
            rm = re.search(r"Rückkehr\s+([^)]+)", tok)
            if rm:
                expected_return = rm.group(1).strip()

    if team_name and team_name in team_links:
        team_id = team_links[team_name][0]
    elif team_links:
        first_tname, (first_tid, _) = next(iter(team_links.items()))
        team_name = team_name or first_tname
        team_id = first_tid

    injury_records: list[InjuryRecord] = []
    vh_idx = next((i for i, t in enumerate(lines) if t == "Verletzungshistorie"), -1)
    if vh_idx != -1:
        j = vh_idx + 1
        while j < len(lines) and lines[j] in ("Von", "Bis", "Verletzung"):
            j += 1
        date_re = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
        while j + 2 < len(lines) and date_re.match(lines[j]):
            since_d = lines[j]
            until_d = lines[j + 1]
            inj_reason = lines[j + 2]
            is_open = until_d == "?"
            avail = classify_absence_status(inj_reason) if is_open else "genesen"
            if is_open and current_status == "fit":
                current_status = avail
                status_reason = inj_reason
            injury_records.append(
                InjuryRecord(
                    player_id=player_id,
                    player_name=player_name,
                    team_id=team_id or "unknown_team",
                    team_name=team_name or "Unbekannt",
                    league_key=league_key,
                    availability_status=avail,
                    reason=inj_reason,
                    expected_return=expected_return if is_open else until_d,
                    since_date=since_d,
                )
            )
            j += 3

    profile = PlayerProfile(
        player_id=player_id,
        player_name=player_name,
        slug=slug,
        team_id=team_id,
        team_name=team_name,
        league_key=league_key or None,
        position=position,
        shirt_number=shirt_number,
        birth_date=birth_date,
        age=age,
        market_value=market_value,
        contract_until=contract_until,
        current_status=current_status,
        status_reason=status_reason,
        expected_return=expected_return,
    )
    return profile, injury_records
