"""Version comparison for the launch-time update check."""
from demoquerycs2.updatecheck import is_newer


def test_newer_versions():
    assert is_newer("0.3.0", "0.2.2")
    assert is_newer("v0.3.0", "0.2.2")          # tag prefix tolerated
    assert is_newer("0.2.10", "0.2.9")          # numeric, not lexicographic
    assert is_newer("1.0", "0.9.9")


def test_not_newer():
    assert not is_newer("0.2.2", "0.2.2")
    assert not is_newer("0.2.1", "0.2.2")
    assert not is_newer("0.2", "0.2.0")


def test_garbage_tags_never_notify():
    assert not is_newer("latest", "0.2.2")
    assert not is_newer("", "0.2.2")
    assert not is_newer("0.3.0-rc1", "0.2.2")   # prereleases ignored by design
