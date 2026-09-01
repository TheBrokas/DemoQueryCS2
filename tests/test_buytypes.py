from demoquerycs2.ingest.buytypes import classify
from demoquerycs2.ingest.demo_parser import TICK_PROPS


def test_thresholds_hltv():
    assert classify(0) == "eco"
    assert classify(5000) == "eco"
    assert classify(5001) == "semi"
    assert classify(16250) == "semi"
    assert classify(16251) == "full"
    assert classify(30000) == "full"


def test_parser_classifies_carried_equipment_including_saved_weapons():
    assert "current_equip_value" in TICK_PROPS
    assert "cash_spent_this_round" not in TICK_PROPS
