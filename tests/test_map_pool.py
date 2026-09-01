from demoquerycs2 import config


def test_overpass_is_searchable_but_outside_active_duty():
    assert "de_overpass" in config.SEARCHABLE_MAPS
    assert "de_overpass" not in config.ACTIVE_DUTY_MAPS
    assert config.ACTIVE_DUTY_MAPS < config.SEARCHABLE_MAPS
