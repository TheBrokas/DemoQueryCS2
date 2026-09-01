import numpy as np

from demoquerycs2.ingest import tokenizer as tk


def test_token_sorted_padded():
    tok = tk.pack_token([5, 2, 9], [1])
    assert tok == bytes([2, 5, 9, 0xFF, 0xFF, 1, 0xFF, 0xFF, 0xFF, 0xFF])
    assert len(tok) == tk.TOKEN_LEN


def test_token_caps_at_five():
    tok = tk.pack_token(list(range(8)), [])
    assert tok[:5] == bytes([0, 1, 2, 3, 4])


def test_positions_roundtrip():
    slots = [None] * 10
    slots[0] = {"x": 100.5, "y": -200.25, "z": 32.0, "alive": True, "ct": True,
                "health": 87, "place_idx": 3, "node_idx": 42, "yaw": 90.0,
                "armor": 95, "money": 4750, "flash_s": 1.7,
                "primary": 4, "secondary": 1,
                "util": tk.UTIL_SMOKE | tk.UTIL_HELMET | 2}
    slots[7] = {"x": -1.0, "y": 2.0, "z": 3.0, "alive": False, "ct": False,
                "health": 0, "place_idx": 0xFF, "node_idx": 7, "yaw": -170.0}
    blob = tk.pack_positions(slots)
    assert len(blob) == 181 and blob[0] == tk.BLOB_V3
    out = tk.unpack_positions(blob)
    assert out[1] is None and out[9] is None
    p0 = out[0]
    assert p0["alive"] and p0["ct"] and p0["health"] == 87 and p0["node_idx"] == 42
    assert abs(p0["x"] - 100.5) <= 0.5 and abs(p0["y"] + 200.25) <= 0.5   # int16, 1u steps
    assert abs(p0["yaw"] - 90.0) < 1.5          # uint8 quantization ~1.4 deg
    assert p0["armor"] == 95 and p0["money"] == 4750 and abs(p0["flash_s"] - 1.7) < 0.06
    assert p0["primary"] == 4 and p0["secondary"] == 1
    assert p0["util"] & tk.UTIL_SMOKE and p0["util"] & tk.UTIL_HELMET
    assert (p0["util"] & tk.UTIL_FLASH_MASK) == 2
    p7 = out[7]
    assert not p7["alive"] and not p7["ct"] and p7["node_idx"] == 7
    assert abs(p7["yaw"] - 190.0) < 1.5         # -170 wraps to 190
    assert p7["primary"] is None and p7["secondary"] is None and p7["armor"] == 0


def test_positions_v1_v2_blobs_still_readable():
    import struct
    v1 = struct.Struct("<fffBBBB")
    blob = bytearray(160)
    v1.pack_into(blob, 0, 1.0, 2.0, 3.0, tk.FLAG_PRESENT | tk.FLAG_ALIVE, 100, 1, 5)
    out = tk.unpack_positions(bytes(blob))
    assert out[0]["alive"] and out[0]["node_idx"] == 5 and out[0]["yaw"] is None
    v2 = struct.Struct("<fffBBBBBB")
    blob = bytearray(180)
    v2.pack_into(blob, 0, 7.5, -8.0, 9.0, tk.FLAG_PRESENT | tk.FLAG_CT, 55, 2, 6, 128, 0)
    out = tk.unpack_positions(bytes(blob))
    p = out[0]
    assert p["ct"] and not p["alive"] and p["health"] == 55 and p["node_idx"] == 6
    assert abs(p["x"] - 7.5) < 1e-4 and abs(p["yaw"] - 180.7) < 1.5
    assert "armor" not in p                     # v2 has no economy fields


def test_tokens_to_matrix():
    blobs = [tk.pack_token([1], [2]), tk.pack_token([3, 4], [])]
    m = tk.tokens_to_matrix(blobs)
    assert m.shape == (2, 10)
    assert m.dtype == np.uint8
    assert m[0, 0] == 1 and m[0, 5] == 2 and m[1, 1] == 4
