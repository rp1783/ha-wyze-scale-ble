"""Protocol for the original Wyze Scale (WHSCL1) — framing, checksum, auth, decoders.

Frame format (verified against all 216 frames in the public HCI captures,
openScale issue #853): [A0|B0] len_le16 seq cmd_be16 data cs8 where
len = 1 + 2 + len(data) + 1 and cs = (seq + cmd_hi + cmd_lo + sum(data)) & 0xFF.
A0 = from scale, B0 = from phone. Phone sequence starts at 1 and increments by 2.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum


class MessageType(IntEnum):
    """Known scale-to-client message types."""

    GET_BATTERY = 0xA911
    DEVICE_INFO = 0xA910
    GET_USER_INFO = 0xA920
    ONLINE_USER_ACK = 0xA921
    CREATE_USER_ACK = 0xA923
    OFFLINE_DATA = 0xA931
    ONLINE_USER_QUERY = 0xA924
    LIVE_WEIGHT = 0xA940
    STABLE_WEIGHT = 0xA941
    MEASUREMENT = 0xA942
    AUTH_R1 = 0xA9FA
    AUTH_R2 = 0xA9FB
    AUTH_R1E = 0xA9FC
    AUTH_OK = 0xA9FD
    AUTH_FAIL = 0xA9FE


class CMD:
    """Phone-to-scale command ids (0xA9 family)."""

    GET_DEVICE_INFO = 0xA910
    GET_BATTERY = 0xA911
    SET_UNIT = 0xA912
    GET_USER_INFO = 0xA920
    ONLINE_USER = 0xA921
    CREATE_USER = 0xA923
    GET_OFFLINE_DATA = 0xA931
    LIGHT_UP = 0xA916
    LIVE_WEIGHT = 0xA940
    ONLINE_RESULT = 0xA941
    BODY_FAT_RESULT = 0xA942
    AUTH_R1 = 0xA9FA
    AUTH_R2 = 0xA9FB
    AUTH_R1E = 0xA9FC
    AUTH_OK = 0xA9FD
    AUTH_FAIL = 0xA9FE


@dataclass(frozen=True, slots=True)
class ScaleMessage:
    """A decoded message from the scale."""

    message_type: MessageType | None
    raw: bytes
    seq: int = 0
    data: bytes = b""
    weight_kg: float | None = None
    stable: bool = False


def checksum(seq: int, cmd: int, data: bytes) -> int:
    """Compute the frame checksum: (seq + cmd bytes + sum(data)) & 0xFF."""
    hi, lo = (cmd >> 8) & 0xFF, cmd & 0xFF
    return (seq + hi + lo + sum(data)) & 0xFF


def build_frame(cmd: int, data: bytes = b"", seq: int = 1) -> bytes:
    """Build a phone-to-scale request frame (0xB0)."""
    length = 1 + 2 + len(data) + 1
    frame = bytes(
        [0xB0, length & 0xFF, (length >> 8) & 0xFF, seq & 0xFF, (cmd >> 8) & 0xFF, cmd & 0xFF]
    ) + data
    return frame + bytes([checksum(seq, cmd, data)])


class FrameAssembler:
    """Reassemble proprietary frames split across BLE notifications."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        """Feed a notification and return any complete frames."""
        if not chunk:
            return []

        if not self._buffer:
            try:
                marker = chunk.index(0xA0)
            except ValueError:
                return []
            self._buffer.extend(chunk[marker:])
        else:
            self._buffer.extend(chunk)

        frames: list[bytes] = []
        while len(self._buffer) >= 3:
            if self._buffer[0] != 0xA0:
                try:
                    marker = self._buffer.index(0xA0)
                except ValueError:
                    self._buffer.clear()
                    break
                del self._buffer[:marker]
                if len(self._buffer) < 3:
                    break

            length = int.from_bytes(self._buffer[1:3], "little")
            expected = length + 3
            if len(self._buffer) < expected:
                break

            frames.append(bytes(self._buffer[:expected]))
            del self._buffer[:expected]

        return frames


def parse_frame(frame: bytes) -> ScaleMessage | None:
    """Decode and validate a complete WHSCL1 notification frame."""
    if len(frame) < 7 or frame[0] != 0xA0:
        return None
    length = int.from_bytes(frame[1:3], "little")
    if len(frame) != length + 3 or length < 4:
        return None

    seq = frame[3]
    command = int.from_bytes(frame[4:6], "big")
    data = frame[6:-1]
    if checksum(seq, command, data) != frame[-1]:
        return None

    try:
        message_type = MessageType(command)
    except ValueError:
        return ScaleMessage(message_type=None, raw=frame, seq=seq, data=data)

    weight_raw: int | None = None
    stable = False

    if message_type is MessageType.LIVE_WEIGHT and len(data) >= 2:
        weight_raw = int.from_bytes(data[0:2], "big")
    elif message_type is MessageType.STABLE_WEIGHT and len(data) >= 3:
        weight_raw = int.from_bytes(data[1:3], "big")
        stable = True
    elif message_type is MessageType.MEASUREMENT and len(data) >= 34:
        weight_raw = int.from_bytes(data[2:4], "big")
        stable = True

    return ScaleMessage(
        message_type=message_type,
        raw=frame,
        seq=seq,
        data=data,
        weight_kg=weight_raw / 100 if weight_raw is not None else None,
        stable=stable,
    )


def decode_measurement_record(record: bytes) -> dict:
    """Decode one 35-byte A942 body-composition record payload."""
    assert len(record) >= 22, f"record too short: {len(record)}"
    be = lambda b: int.from_bytes(b, "big")  # noqa: E731
    result = {
        "instruction_type": record[0],
        "weight_kg": be(record[1:3]) / 100.0,
        "impedance": [be(record[3 + 2 * i : 5 + 2 * i]) for i in range(4)],
        "user_num": record[11],
        "gender": record[12],
        "age": record[13],
        "height_cm": record[14],
        "measure_time": be(record[15:19]),
    }
    if result["instruction_type"] == 1:
        assert len(record) >= 35
        result.update(
            {
                "body_fat_pct": None if be(record[22:24]) == 0xFFFF else be(record[22:24]) / 10.0,
                "muscle_kg": None if be(record[24:26]) == 0xFFFF else be(record[24:26]) / 10.0,
                "bone_kg": None if record[26] == 0xFF else record[26] / 10.0,
                "water_pct": None if be(record[27:29]) == 0xFFFF else be(record[27:29]) / 10.0,
                "protein_pct": None if be(record[29:31]) == 0xFFFF else be(record[29:31]) / 10.0,
            }
        )
    return result


DELTA = 0x9E3779B9


def _mx(s, y, z, p, e, k):
    return (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((s ^ y) + (k[(p & 3) ^ e] ^ z))


def _bytes_to_words(data: bytes, include_len: bool) -> list[int]:
    n = (len(data) + 3) // 4
    words = [0] * (n + 1 if include_len else n)
    for i, byte in enumerate(data):
        words[i >> 2] |= byte << ((i & 3) << 3)
    if include_len:
        words[n] = len(data)
    return words


def _words_to_bytes(words: list[int], strip_len: bool):
    length = len(words) << 2
    if strip_len:
        if words[-1] < length - 7 or words[-1] > length - 4:
            return None
        length = words[-1]
    return bytes((words[i >> 2] >> ((i & 3) << 3)) & 0xFF for i in range(length))


def _key_words(key: bytes) -> tuple[int, int, int, int]:
    padded = bytearray(16)
    if key:
        padded[: min(len(key), 16)] = key[:16]
    return struct.unpack("<4I", bytes(padded))


def _xxtea_encrypt_words(v, k):
    n = len(v) - 1
    rounds = 6 + 52 // (n + 1)
    s = 0
    z = v[n]
    for _ in range(rounds):
        s = (s + DELTA) & 0xFFFFFFFF
        e = (s >> 2) & 3
        for p in range(n):
            y = v[p + 1]
            v[p] = (v[p] + _mx(s, y, z, p, e, k)) & 0xFFFFFFFF
            z = v[p]
        y = v[0]
        v[n] = (v[n] + _mx(s, y, z, n, e, k)) & 0xFFFFFFFF
        z = v[n]
    return v


def _xxtea_decrypt_words(v, k):
    """Exact port of the app's XXTEA2.decrypt(int[], int[]).

    The Java loop carries z between rounds starting at v[0], decrypts p from
    len-1 down to 1 passing p (not p-1) as the key index, and finishes with
    v[0] using v[len-1] as the y-slot.
    """
    n = len(v) - 1
    if n < 1:
        return v
    rounds = (52 // len(v)) + 6
    s = (rounds * DELTA) & 0xFFFFFFFF
    z = v[0]
    while s != 0:
        e = (s >> 2) & 3
        z2 = z
        for p in range(n, 0, -1):
            z2 = (v[p] - _mx(s, z2, v[p - 1], p, e, k)) & 0xFFFFFFFF
            v[p] = z2
        z = (v[0] - _mx(s, z2, v[n], 0, e, k)) & 0xFFFFFFFF
        v[0] = z
        s = (s - DELTA) & 0xFFFFFFFF
    return v


def xxtea2_encrypt(data: bytes, key: bytes) -> bytes:
    """XXTEA2.encrypt: appends length word and keeps it (16B in -> 20B out)."""
    if not data:
        return data
    return _words_to_bytes(_xxtea_encrypt_words(_bytes_to_words(data, True), _key_words(key)), False)


def xxtea2_decrypt(data: bytes, key: bytes) -> bytes | None:
    """XXTEA2.decrypt: strips length word; returns None if the check fails."""
    if not data:
        return data
    return _words_to_bytes(_xxtea_decrypt_words(_bytes_to_words(data, False), _key_words(key)), True)