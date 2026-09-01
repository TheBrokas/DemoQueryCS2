"""Kill parsing at round boundaries and halftime side changes."""
import pandas as pd

from demoquerycs2.ingest.demo_parser import (
    ParsedRound,
    _parse_kills,
    _round_player_sides,
)


def test_terminal_kill_uses_sampled_round_sides_not_event_sides():
    rounds = [ParsedRound(round_num=12, freeze_end_tick=1000, end_tick=5000,
                          winner="CT", win_reason="t_killed")]
    ticks = pd.DataFrame([
        {"tick": 1000, "name": "winner", "team_num": 3},
        {"tick": 1000, "name": "victim", "team_num": 2},
        {"tick": 4900, "name": "winner", "team_num": 3},
        {"tick": 4900, "name": "victim", "team_num": 2},
    ])
    deaths = pd.DataFrame([{
        "tick": 5000,
        "attacker_name": "winner",
        "attacker_team_num": 2,  # parser has already exposed next-half sides
        "user_name": "victim",
        "user_team_num": 3,
        "weapon": "ak47",
        "headshot": False,
        "assister_name": None,
    }])

    sides = _round_player_sides(ticks, rounds)
    kill = _parse_kills(deaths, rounds, sides)[0]

    assert kill.round_index == 0
    assert kill.attacker_side == "CT"
    assert kill.victim_side == "T"
