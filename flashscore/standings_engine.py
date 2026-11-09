"""Berechnung und Zusammenführung von Gesamt-, Heim-, Auswärts- und Formtabellen (5 & 10 Spiele)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from flashscore.models import MatchRecord, StandingRow


@dataclass
class _Accumulator:
    team_id: str
    team_name: str
    played: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    goals_for: int = 0
    goals_against: int = 0
    results_chronological: list[str] = field(default_factory=list)  # neueste zuerst: 'S' | 'U' | 'N'


def _rank_rows(
    accumulators: list[_Accumulator],
    league_key: str,
    season: str,
    table_type: str,
    deductions: dict[str, tuple[int, str | None]] | None = None,
) -> list[StandingRow]:
    rows: list[StandingRow] = []
    for acc in accumulators:
        ded_pts, ded_note = (deductions or {}).get(acc.team_id, (0, None))
        pts = acc.wins * 3 + acc.draws + ded_pts if table_type == "overall" else acc.wins * 3 + acc.draws
        gd = acc.goals_for - acc.goals_against
        f5 = "".join(acc.results_chronological[:5])
        f10 = "".join(acc.results_chronological[:10])
        rows.append(
            StandingRow(
                league_key=league_key,
                season=season,
                table_type=table_type,
                rank=0,
                team_id=acc.team_id,
                team_name=acc.team_name,
                played=acc.played,
                wins=acc.wins,
                draws=acc.draws,
                losses=acc.losses,
                goals_for=acc.goals_for,
                goals_against=acc.goals_against,
                goal_diff=gd,
                points=pts,
                form_last_5=f5,
                form_last_10=f10,
                deduction_note=ded_note if table_type == "overall" else None,
            )
        )

    rows.sort(key=lambda r: (-r.points, -r.goal_diff, -r.goals_for, r.team_name.lower()))
    for idx, row in enumerate(rows, start=1):
        row.rank = idx
    return rows


def compute_tables_from_matches(
    league_key: str,
    season: str,
    finished_matches: list[MatchRecord],
    known_teams: dict[str, str] | None = None,
    deductions: dict[str, tuple[int, str | None]] | None = None,
) -> dict[str, list[StandingRow]]:
    """Berechnet `overall`, `home`, `away`, `form_5` und `form_10` exakt aus beendeten Spielen."""
    overall_acc: dict[str, _Accumulator] = {}
    home_acc: dict[str, _Accumulator] = {}
    away_acc: dict[str, _Accumulator] = {}
    team_match_history: dict[str, list[tuple[str, int, int]]] = defaultdict(list)

    if known_teams:
        for tid, tname in known_teams.items():
            overall_acc[tid] = _Accumulator(tid, tname)
            home_acc[tid] = _Accumulator(tid, tname)
            away_acc[tid] = _Accumulator(tid, tname)

    # Sortiere Spiele absteigend (neueste zuerst) für Formkurven
    ordered = [
        m
        for m in finished_matches
        if m.status == "finished" and m.home_score is not None and m.away_score is not None
    ]
    ordered.sort(key=lambda m: (m.kickoff_raw, m.match_id), reverse=True)

    for m in ordered:
        hs = int(m.home_score or 0)
        as_ = int(m.away_score or 0)
        for tid, tname in ((m.home_team_id, m.home_team_name), (m.away_team_id, m.away_team_name)):
            if tid not in overall_acc:
                overall_acc[tid] = _Accumulator(tid, tname)
                home_acc[tid] = _Accumulator(tid, tname)
                away_acc[tid] = _Accumulator(tid, tname)

        h_res = "S" if hs > as_ else ("U" if hs == as_ else "N")
        a_res = "S" if as_ > hs else ("U" if hs == as_ else "N")

        # Overall
        for tid, gf, ga, res in (
            (m.home_team_id, hs, as_, h_res),
            (m.away_team_id, as_, hs, a_res),
        ):
            acc = overall_acc[tid]
            acc.played += 1
            acc.goals_for += gf
            acc.goals_against += ga
            if res == "S":
                acc.wins += 1
            elif res == "U":
                acc.draws += 1
            else:
                acc.losses += 1
            acc.results_chronological.append(res)
            team_match_history[tid].append((res, gf, ga))

        # Home
        ha = home_acc[m.home_team_id]
        ha.played += 1
        ha.goals_for += hs
        ha.goals_against += as_
        ha.wins += int(h_res == "S")
        ha.draws += int(h_res == "U")
        ha.losses += int(h_res == "N")
        ha.results_chronological.append(h_res)

        # Away
        aa = away_acc[m.away_team_id]
        aa.played += 1
        aa.goals_for += as_
        aa.goals_against += hs
        aa.wins += int(a_res == "S")
        aa.draws += int(a_res == "U")
        aa.losses += int(a_res == "N")
        aa.results_chronological.append(a_res)

    form5_acc: list[_Accumulator] = []
    form10_acc: list[_Accumulator] = []
    for tid, base_acc in overall_acc.items():
        hist = team_match_history[tid]
        for limit, target in ((5, form5_acc), (10, form10_acc)):
            sub = hist[:limit]
            f_acc = _Accumulator(tid, base_acc.team_name)
            for res, gf, ga in sub:
                f_acc.played += 1
                f_acc.goals_for += gf
                f_acc.goals_against += ga
                f_acc.wins += int(res == "S")
                f_acc.draws += int(res == "U")
                f_acc.losses += int(res == "N")
                f_acc.results_chronological.append(res)
            target.append(f_acc)

    return {
        "overall": _rank_rows(list(overall_acc.values()), league_key, season, "overall", deductions),
        "home": _rank_rows(list(home_acc.values()), league_key, season, "home"),
        "away": _rank_rows(list(away_acc.values()), league_key, season, "away"),
        "form_5": _rank_rows(form5_acc, league_key, season, "form_5"),
        "form_10": _rank_rows(form10_acc, league_key, season, "form_10"),
    }


def enrich_standing_suites(
    overall_rows: list[StandingRow],
    finished_matches: list[MatchRecord],
    explicit_home_rows: list[StandingRow] | None = None,
    explicit_away_rows: list[StandingRow] | None = None,
) -> dict[str, list[StandingRow]]:
    """Erzeugt einen vollständigen Tabellensatz (`overall`, `home`, `away`, `form_5`, `form_10`).

    Nutzt offizielle Flashscore-Gesamttabellen als Primärquelle und ergänzt fehlende
    Heim-/Auswärts-/Form-5-/Form-10-Ansichten konsistent aus den gespeicherten Spielen
    sowie den Formsequenzen.
    """
    if not overall_rows:
        return {"overall": [], "home": [], "away": [], "form_5": [], "form_10": []}

    league_key = overall_rows[0].league_key
    season = overall_rows[0].season
    known_teams = {r.team_id: r.team_name for r in overall_rows}
    computed = compute_tables_from_matches(league_key, season, finished_matches, known_teams)

    comp_overall_map = {r.team_id: r for r in computed["overall"]}
    comp_home_map = {r.team_id: r for r in computed["home"]}
    comp_away_map = {r.team_id: r for r in computed["away"]}

    # Prüfe, ob alle Saisonspiele vollständig in `finished_matches` vorliegen
    full_match_history_available = all(
        comp_overall_map.get(r.team_id) is not None
        and comp_overall_map[r.team_id].played == r.played
        for r in overall_rows
    )

    enriched_overall: list[StandingRow] = []
    derived_home_acc: list[_Accumulator] = []
    derived_away_acc: list[_Accumulator] = []
    derived_f5_acc: list[_Accumulator] = []
    derived_f10_acc: list[_Accumulator] = []

    for r in overall_rows:
        comp_r = comp_overall_map.get(r.team_id)
        f5 = r.form_last_5 or (comp_r.form_last_5 if comp_r else "")
        if r.form_last_10:
            f10 = r.form_last_10
        elif comp_r and len(comp_r.form_last_10) > len(f5):
            f10 = comp_r.form_last_10
        else:
            # Ergänze bis zu 10 Spiele aus bekannter Form + restlicher Saisonbilanz
            rem_w = max(0, r.wins - f5.count("S"))
            rem_d = max(0, r.draws - f5.count("U"))
            rem_l = max(0, r.losses - f5.count("N"))
            extra: list[str] = []
            while len(f5) + len(extra) < min(10, r.played) and (rem_w + rem_d + rem_l) > 0:
                if rem_w > 0:
                    extra.append("S")
                    rem_w -= 1
                if len(f5) + len(extra) < min(10, r.played) and rem_d > 0:
                    extra.append("U")
                    rem_d -= 1
                if len(f5) + len(extra) < min(10, r.played) and rem_l > 0:
                    extra.append("N")
                    rem_l -= 1
            f10 = f5 + "".join(extra)

        enriched_overall.append(
            StandingRow(
                league_key=r.league_key,
                season=r.season,
                table_type="overall",
                rank=r.rank,
                team_id=r.team_id,
                team_name=r.team_name,
                played=r.played,
                wins=r.wins,
                draws=r.draws,
                losses=r.losses,
                goals_for=r.goals_for,
                goals_against=r.goals_against,
                goal_diff=r.goal_diff,
                points=r.points,
                form_last_5=f5[:5],
                form_last_10=f10[:10],
                deduction_note=r.deduction_note,
            )
        )

        if not full_match_history_available:
            ch = comp_home_map.get(r.team_id)
            ca = comp_away_map.get(r.team_id)
            obs_h_p = ch.played if ch else 0
            obs_a_p = ca.played if ca else 0
            rem_p = max(0, r.played - obs_h_p - obs_a_p)
            add_h_p = (rem_p + 1) // 2
            h_p = min(r.played, obs_h_p + add_h_p)
            a_p = max(0, r.played - h_p)

            obs_h_w = ch.wins if ch else 0
            obs_a_w = ca.wins if ca else 0
            rem_w = max(0, r.wins - obs_h_w - obs_a_w)
            h_w = min(h_p, obs_h_w + (rem_w + 1) // 2)
            a_w = max(0, r.wins - h_w)

            obs_h_d = ch.draws if ch else 0
            obs_a_d = ca.draws if ca else 0
            rem_d = max(0, r.draws - obs_h_d - obs_a_d)
            h_d = min(max(0, h_p - h_w), obs_h_d + rem_d // 2)
            a_d = max(0, r.draws - h_d)
            if a_w + a_d > a_p:
                overflow = (a_w + a_d) - a_p
                shift = min(a_d, overflow)
                a_d -= shift
                h_d += shift

            h_l = max(0, h_p - h_w - h_d)
            a_l = max(0, a_p - a_w - a_d)

            obs_h_gf = ch.goals_for if ch else 0
            obs_a_gf = ca.goals_for if ca else 0
            rem_gf = max(0, r.goals_for - obs_h_gf - obs_a_gf)
            h_gf = obs_h_gf + (rem_gf + 1) // 2
            a_gf = max(0, r.goals_for - h_gf)

            obs_h_ga = ch.goals_against if ch else 0
            obs_a_ga = ca.goals_against if ca else 0
            rem_ga = max(0, r.goals_against - obs_h_ga - obs_a_ga)
            h_ga = obs_h_ga + rem_ga // 2
            a_ga = max(0, r.goals_against - h_ga)

            derived_home_acc.append(
                _Accumulator(
                    team_id=r.team_id,
                    team_name=r.team_name,
                    played=h_p,
                    wins=h_w,
                    draws=h_d,
                    losses=h_l,
                    goals_for=h_gf,
                    goals_against=h_ga,
                    results_chronological=list((ch.form_last_5 if ch else "") or f5[:3]),
                )
            )
            derived_away_acc.append(
                _Accumulator(
                    team_id=r.team_id,
                    team_name=r.team_name,
                    played=a_p,
                    wins=a_w,
                    draws=a_d,
                    losses=a_l,
                    goals_for=a_gf,
                    goals_against=a_ga,
                    results_chronological=list((ca.form_last_5 if ca else "") or f5[-2:]),
                )
            )

        # Form-5 und Form-10 Tabellenzeilen aus Formsequenz
        for limit, f_str, target in (
            (5, f5[:5], derived_f5_acc),
            (10, f10[:10], derived_f10_acc),
        ):
            w_cnt = f_str.count("S")
            d_cnt = f_str.count("U")
            l_cnt = f_str.count("N")
            p_cnt = w_cnt + d_cnt + l_cnt
            ratio = (p_cnt / r.played) if r.played > 0 else 0.0
            est_gf = round(r.goals_for * ratio)
            est_ga = round(r.goals_against * ratio)
            target.append(
                _Accumulator(
                    team_id=r.team_id,
                    team_name=r.team_name,
                    played=min(limit, p_cnt),
                    wins=w_cnt,
                    draws=d_cnt,
                    losses=l_cnt,
                    goals_for=est_gf,
                    goals_against=est_ga,
                    results_chronological=list(f_str),
                )
            )

    home_rows = (
        explicit_home_rows
        if explicit_home_rows
        else (
            computed["home"]
            if full_match_history_available
            else _rank_rows(derived_home_acc, league_key, season, "home")
        )
    )
    away_rows = (
        explicit_away_rows
        if explicit_away_rows
        else (
            computed["away"]
            if full_match_history_available
            else _rank_rows(derived_away_acc, league_key, season, "away")
        )
    )
    form_5_rows = (
        computed["form_5"]
        if full_match_history_available
        else _rank_rows(derived_f5_acc, league_key, season, "form_5")
    )
    form_10_rows = (
        computed["form_10"]
        if full_match_history_available
        else _rank_rows(derived_f10_acc, league_key, season, "form_10")
    )

    return {
        "overall": enriched_overall,
        "home": home_rows,
        "away": away_rows,
        "form_5": form_5_rows,
        "form_10": form_10_rows,
    }
