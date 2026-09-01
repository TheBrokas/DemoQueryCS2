"""Roster selection: alive-based ranking must exclude FACEIT-style coaches."""
import pandas as pd

from demoquerycs2.ingest.demo_parser import select_roster


def _df(players: dict[str, tuple[int, int]]) -> pd.DataFrame:
    """players: steamid -> (total_rows, alive_rows)."""
    sids, alive = [], []
    for sid, (rows, alive_rows) in players.items():
        sids.extend([sid] * rows)
        alive.extend([True] * alive_rows + [False] * (rows - alive_rows))
    return pd.DataFrame({"steamid": sids, "is_alive": alive})


def test_coach_with_full_presence_is_excluded():
    # 10 players + a coach, all with identical presence (the real FACEIT case:
    # every candidate had 1519 rows and the coach 0 alive samples)
    players = {f"7656119800000000{i:02d}": (1519, 1100 + i) for i in range(10)}
    players["76561198999999999"] = (1519, 0)          # coach: never alive
    roster = select_roster(_df(players))
    assert len(roster) == 10
    assert "76561198999999999" not in roster
    assert set(roster) == set(players) - {"76561198999999999"}


def test_coach_excluded_even_with_room_to_spare():
    players = {f"sid{i:02d}": (500, 400) for i in range(9)}
    players["coach"] = (500, 0)
    roster = select_roster(_df(players))
    assert len(roster) == 9 and "coach" not in roster


def test_substitute_beats_coach_but_not_regulars():
    players = {f"sid{i:02d}": (1500, 1200) for i in range(10)}
    players["sub"] = (300, 250)                       # played a few rounds
    players["coach"] = (1500, 0)
    roster = select_roster(_df(players))
    assert "coach" not in roster and "sub" not in roster
    assert len(roster) == 10


def test_roster_is_sorted_and_deterministic():
    players = {f"sid{i:02d}": (100, 50) for i in range(12)}   # all tied
    r1 = select_roster(_df(players))
    r2 = select_roster(_df(dict(reversed(players.items()))))
    assert r1 == r2 == sorted(r1)


def test_nan_is_alive_treated_as_dead():
    df = pd.DataFrame({
        "steamid": ["a"] * 10 + ["b"] * 10,
        "is_alive": pd.Series([True] * 5 + [None] * 5 + [False] * 10, dtype=object),
    })
    roster = select_roster(df)
    assert roster == ["a"]
