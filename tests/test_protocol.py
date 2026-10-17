"""Tests for WHSCL1 protocol parsing and auth."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "wyze_scale_ble"
    / "protocol.py"
)
SPEC = importlib.util.spec_from_file_location("wyze_scale_protocol", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
PROTOCOL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROTOCOL
SPEC.loader.exec_module(PROTOCOL)

CMD = PROTOCOL.CMD
FrameAssembler = PROTOCOL.FrameAssembler
MessageType = PROTOCOL.MessageType
build_frame = PROTOCOL.build_frame
decode_measurement_record = PROTOCOL.decode_measurement_record
parse_frame = PROTOCOL.parse_frame
xxtea2_decrypt = PROTOCOL.xxtea2_decrypt
xxtea2_encrypt = PROTOCOL.xxtea2_encrypt


class ProtocolTests(unittest.TestCase):
    def test_captured_a9fa_round_trip(self) -> None:
        """The A9FA frame from the public capture must reproduce byte-for-byte."""
        frame = build_frame(CMD.AUTH_R1, b"C3opMuKaTRF8qqnG", seq=1)
        self.assertEqual(
            frame.hex(), "b0140001a9fa43336f704d754b615452463871716e4722"
        )

    def test_captured_a9fb_parse(self) -> None:
        raw = bytes.fromhex(
            "a0380000a9fb634b2335336b565c5433473c54225b5e130257cc9c755e8d"
            "28ea50e9d7ec46a319559e462caa8e408b1800000000000000000000f7"
        )
        message = parse_frame(raw)
        self.assertIsNotNone(message)
        self.assertEqual(message.message_type, MessageType.AUTH_R2)
        self.assertEqual(len(message.data), 52)
        self.assertEqual(
            bytes(message.data[36:]), bytes.fromhex("2caa8e408b18" + "00" * 10)
        )

    def test_auth_ok_frame(self) -> None:
        message = parse_frame(bytes.fromhex("a0040000a9fda6"))
        self.assertIsNotNone(message)
        self.assertEqual(message.message_type, MessageType.AUTH_OK)

    def test_checksum_rejects_corrupted_frame(self) -> None:
        raw = bytearray(bytes.fromhex("a0060000a9401ddae0"))
        raw[-1] ^= 0xFF
        self.assertIsNone(parse_frame(bytes(raw)))

    def test_xxtea2_round_trip(self) -> None:
        key = bytes.fromhex("00112233445566778899aabbccddeeff")
        data = b"C3opMuKaTRF8qqnG"
        encrypted = xxtea2_encrypt(data, key)
        self.assertEqual(len(encrypted), 20)
        self.assertEqual(xxtea2_decrypt(encrypted, key), data)

    def test_live_weight(self) -> None:
        message = parse_frame(bytes.fromhex("a0060000a9401ddae0"))
        self.assertIsNotNone(message)
        self.assertEqual(message.message_type, MessageType.LIVE_WEIGHT)
        self.assertAlmostEqual(message.weight_kg, 76.42)
        self.assertFalse(message.stable)

    def test_stable_weight(self) -> None:
        message = parse_frame(bytes.fromhex("a0070000a9410021707b"))
        self.assertIsNotNone(message)
        self.assertEqual(message.message_type, MessageType.STABLE_WEIGHT)
        self.assertAlmostEqual(message.weight_kg, 85.60)
        self.assertTrue(message.stable)

    def test_completed_measurement(self) -> None:
        frame = bytes.fromhex(
            "a0280000a942000125130282022d021201f800011fc16274a57e"
            "01000601270274290204009100da441f60"
        )
        message = parse_frame(frame)
        self.assertIsNotNone(message)
        self.assertEqual(message.message_type, MessageType.MEASUREMENT)
        self.assertAlmostEqual(message.weight_kg, 94.91)
        self.assertTrue(message.stable)

    def test_fragment_reassembly(self) -> None:
        chunks = (
            bytes.fromhex("a0280000a94200011dda0283022f021501fd0001"),
            bytes.fromhex("1fc16274a3a50100060097026128026e00af00f7"),
            bytes.fromhex("5565a6"),
        )
        assembler = FrameAssembler()
        self.assertEqual(assembler.feed(chunks[0]), [])
        self.assertEqual(assembler.feed(chunks[1]), [])
        frames = assembler.feed(chunks[2])
        self.assertEqual(len(frames), 1)
        message = parse_frame(frames[0])
        self.assertIsNotNone(message)
        self.assertAlmostEqual(message.weight_kg, 76.42)

    def test_rejects_malformed_frame(self) -> None:
        self.assertIsNone(parse_frame(bytes.fromhex("a0060000a9401dda")))


if __name__ == "__main__":
    unittest.main()
