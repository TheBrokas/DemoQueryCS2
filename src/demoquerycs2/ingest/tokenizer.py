"""Token and positions-blob packing.

Token (10 bytes): sorted node ids of alive CT players (5 x uint8, 0xFF pad)
followed by the same for T. Byte-equal tokens = identical node arrangement.

Positions blob v3 (1 version byte + 10 slots x 18 bytes = 181 bytes):
  int16 x, y, z (game units, 1u precision - under a radar pixel)
  uint8 flags (1=alive, 2=ct, 4=present) | uint8 health
  uint8 place_idx (0xFF unknown) | uint8 node_idx (0xFF unknown)
  uint8 yaw (0-255 maps to 0-360 deg) | uint8 armor
  uint16 money | uint8 flash (remaining blind seconds x10, capped)
  uint8 primary weapon id | uint8 secondary weapon id (global registry, 0xFF none)
  uint8 util bitmask (bits 0-1 flash count, 2 smoke, 3 he, 4 molly, 5 decoy,
                      6 defuser, 7 helmet)
Quantizing xyz float32->int16 frees exactly the six bytes the new fields need,
so a v3 slot is the same 18 bytes as v2. Legacy v2 (180 bytes, float32 xyz,
no economy fields) and v1 (160 bytes, no yaw) blobs remain readable.
"""
from __future__ import annotations

import struct

import numpy as np

TOKEN_LEN = 10
SLOT_SIZE = 18
SLOT_SIZE_V1 = 16
N_SLOTS = 10
PAD = 0xFF
BLOB_V3 = 3                 # version prefix byte of a v3 blob
PLACE_BYTE_OFFSET = 9       # place_idx byte position in a v3 blob (prefix + slot offset 8)
WEAPON_BYTE_OFFSETS = (16, 17)   # primary/secondary id positions within a v3 blob's first slot

FLAG_ALIVE = 1
FLAG_CT = 2
FLAG_PRESENT = 4

UTIL_FLASH_MASK = 0x03      # bits 0-1: flashbang count
UTIL_SMOKE = 0x04
UTIL_HE = 0x08
UTIL_MOLLY = 0x10
UTIL_DECOY = 0x20
UTIL_DEFUSER = 0x40
UTIL_HELMET = 0x80

_slot_struct = struct.Struct("<hhhBBBBBBHBBBB")
_slot_struct_v2 = struct.Struct("<fffBBBBBB")
_slot_struct_v1 = struct.Struct("<fffBBBB")
assert _slot_struct.size == SLOT_SIZE
assert _slot_struct_v2.size == SLOT_SIZE
assert _slot_struct_v1.size == SLOT_SIZE_V1


def pack_token(ct_nodes: list[int], t_nodes: list[int]) -> bytes:
    ct = sorted(ct_nodes)[:5]
    t = sorted(t_nodes)[:5]
    return bytes(ct + [PAD] * (5 - len(ct)) + t + [PAD] * (5 - len(t)))


def _encode_yaw(yaw: float | None) -> int:
    if yaw is None:
        return 0
    return int(round((float(yaw) % 360.0) / 360.0 * 255.0)) & 0xFF


def _i16(v: float) -> int:
    return max(-32768, min(32767, int(round(v))))


def _u8(v, cap: int = 255) -> int:
    return max(0, min(cap, int(v)))


def pack_positions(slots: list[dict | None]) -> bytes:
    """slots: N_SLOTS entries, each None or dict(x,y,z,alive,ct,health,place_idx,
    node_idx[,yaw,armor,money,flash_s,primary,secondary,util])."""
    out = bytearray(1 + N_SLOTS * SLOT_SIZE)
    out[0] = BLOB_V3
    for i in range(N_SLOTS):
        s = slots[i] if i < len(slots) else None
        off = 1 + i * SLOT_SIZE
        if s is None:
            _slot_struct.pack_into(out, off, 0, 0, 0, 0, 0, PAD, PAD, 0, 0, 0, 0, PAD, PAD, 0)
            continue
        flags = FLAG_PRESENT | (FLAG_ALIVE if s["alive"] else 0) | (FLAG_CT if s["ct"] else 0)
        _slot_struct.pack_into(
            out, off, _i16(s["x"]), _i16(s["y"]), _i16(s["z"]),
            flags, _u8(s["health"]),
            s.get("place_idx", PAD) & 0xFF, s.get("node_idx", PAD) & 0xFF,
            _encode_yaw(s.get("yaw")), _u8(s.get("armor", 0)),
            _u8(s.get("money", 0), 65535), _u8(round(float(s.get("flash_s", 0.0)) * 10)),
            s.get("primary", PAD) & 0xFF, s.get("secondary", PAD) & 0xFF,
            s.get("util", 0) & 0xFF)
    return bytes(out)


def unpack_positions(blob: bytes) -> list[dict | None]:
    if len(blob) == 1 + N_SLOTS * SLOT_SIZE and blob[0] == BLOB_V3:
        return _unpack_v3(blob)
    v1 = len(blob) == N_SLOTS * SLOT_SIZE_V1
    st = _slot_struct_v1 if v1 else _slot_struct_v2
    size = SLOT_SIZE_V1 if v1 else SLOT_SIZE
    out: list[dict | None] = []
    for i in range(N_SLOTS):
        vals = st.unpack_from(blob, i * size)
        x, y, z, flags, health, place_idx, node_idx = vals[:7]
        if not flags & FLAG_PRESENT:
            out.append(None)
            continue
        out.append({
            "x": x, "y": y, "z": z,
            "alive": bool(flags & FLAG_ALIVE),
            "ct": bool(flags & FLAG_CT),
            "health": health,
            "place_idx": place_idx,
            "node_idx": node_idx,
            "yaw": (vals[7] / 255.0 * 360.0) if not v1 else None,
        })
    return out


def _unpack_v3(blob: bytes) -> list[dict | None]:
    out: list[dict | None] = []
    for i in range(N_SLOTS):
        (x, y, z, flags, health, place_idx, node_idx, yaw, armor,
         money, flash, primary, secondary, util) = _slot_struct.unpack_from(blob, 1 + i * SLOT_SIZE)
        if not flags & FLAG_PRESENT:
            out.append(None)
            continue
        out.append({
            "x": float(x), "y": float(y), "z": float(z),
            "alive": bool(flags & FLAG_ALIVE),
            "ct": bool(flags & FLAG_CT),
            "health": health,
            "place_idx": place_idx,
            "node_idx": node_idx,
            "yaw": yaw / 255.0 * 360.0,
            "armor": armor,
            "money": money,
            "flash_s": flash / 10.0,
            "primary": None if primary == PAD else primary,
            "secondary": None if secondary == PAD else secondary,
            "util": util,
        })
    return out


# Grenade travel paths. v2 (1 + 8 bytes/sample): version byte 2, then
# uint16 centiseconds since freeze end + int16 x, y, z per sample - half the
# size of v1's float32 quadruples at a precision (1 unit, 10 ms) far below
# what a dashed line on a radar can show. v1 blobs stay readable: they are
# 16 bytes/sample, so their length is even while every v2 blob's is odd.
PATH_V2 = 2
_path_sample = struct.Struct("<Hhhh")
assert _path_sample.size == 8


def pack_path(samples) -> bytes:
    """samples: iterable of (t_rel_s, x, y, z)."""
    out = bytearray(b"\x02")
    for t, x, y, z in samples:
        out += _path_sample.pack(
            max(0, min(65535, int(round(float(t) * 100)))),
            _i16(x), _i16(y), _i16(z))
    return bytes(out)


def unpack_path(blob: bytes) -> list[list[float]]:
    """[(t_rel_s, x, y, z), ...] from either path format."""
    if blob and blob[0] == PATH_V2 and (len(blob) - 1) % 8 == 0:
        return [[t / 100.0, float(x), float(y), float(z)]
                for t, x, y, z in _path_sample.iter_unpack(blob[1:])]
    arr = np.frombuffer(blob, dtype=np.float32).reshape(-1, 4)
    return [[round(float(v), 1) for v in p] for p in arr]


def tokens_to_matrix(token_blobs: list[bytes]) -> np.ndarray:
    """(N, 10) uint8 matrix from raw token blobs."""
    return np.frombuffer(b"".join(token_blobs), dtype=np.uint8).reshape(-1, TOKEN_LEN)
