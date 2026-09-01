"""Database schema and migrations."""

from demoquerycs2 import db as dbmod


def test_token_column_survives_removal_of_legacy_token_index(tmp_path):
    path = tmp_path / "legacy-token-index.sqlite3"
    token = bytes([1, 2, 3, 0xFF, 0xFF, 4, 5, 6, 0xFF, 0xFF])

    conn = dbmod.connect(path)
    conn.execute("CREATE INDEX idx_states_map_token ON states(map_name, token)")
    conn.execute(
        "INSERT INTO demos (demo_id, filename, file_size, content_key, map_name, tickrate, "
        "parsed_at, tokenizer_version) VALUES (1, 'test.dem', 1, 'key', 'de_mirage', 64, 'now', 1)"
    )
    conn.execute(
        "INSERT INTO rounds (round_id, demo_id, round_num, freeze_end_tick, end_tick, ct_buy, t_buy) "
        "VALUES (1, 1, 1, 0, 64, 'full', 'full')"
    )
    conn.execute(
        "INSERT INTO states (state_id, round_id, demo_id, map_name, tick, round_time_s, token, "
        "bomb_planted, alive_ct, alive_t, positions) "
        "VALUES (1, 1, 1, 'de_mirage', 64, 1, ?, 0, 3, 3, ?)",
        (token, bytes(181)),
    )
    conn.commit()
    conn.close()

    # Reopening an existing database runs the migration that drops only the
    # unused compound index.
    conn = dbmod.connect(path)
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(states)")}
    assert "idx_states_map_token" not in indexes
    assert "idx_states_map" in indexes
    assert conn.execute(
        "SELECT token FROM states WHERE map_name=? AND token=?",
        ("de_mirage", token),
    ).fetchone()[0] == token
    assert conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0] == str(dbmod.SCHEMA_VERSION)
    conn.close()
