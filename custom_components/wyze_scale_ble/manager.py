"""Bluetooth client for the original Wyze Scale."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
from pathlib import Path
import secrets
import struct

from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BLEAK_RETRY_EXCEPTIONS,
    BleakClientWithServiceCache,
    establish_connection,
)

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback

from .const import CONNECT_TIMEOUT, NOTIFY_CHAR_UUID, WRITE_CHAR_UUID
from .cloud import WyzeCloudClient
from .protocol import (
    CMD,
    FrameAssembler,
    MessageType,
    ScaleMessage,
    build_frame,
    decode_measurement_record,
    parse_frame,
    xxtea2_decrypt,
    xxtea2_encrypt,
)

_LOGGER = logging.getLogger(__name__)

MAX_WRITE_CHUNK = 20

WYZE_DEVICE_ID_PREFIX = "JA.SC."


def _load_cloud_credentials(hass: HomeAssistant) -> tuple[str, str, str, str] | None:
    """Load Wyze cloud credentials from /config/wyze_scale_ble.json."""
    try:
        path = Path(hass.config.config_dir) / "wyze_scale_ble.json"
        data = json.loads(path.read_text())
        if all(data.get(k) for k in ("key_id", "api_key", "username", "password")):
            return (data["key_id"], data["api_key"], data["username"], data["password"])
    except (OSError, ValueError, KeyError):
        pass
    return None


class WyzeScaleManager:
    """Own the BLE connection, the auth handshake, and decoded scale values."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str,
        bind_token: str | None = None,
        device_key: str | None = None,
    ) -> None:
        self.hass = hass
        self.address = address
        self.name = name
        self.bind_token = bind_token
        self.device_key = bytes.fromhex(device_key) if device_key else None
        creds = _load_cloud_credentials(hass)
        self.cloud: WyzeCloudClient | None = WyzeCloudClient(*creds) if creds else None
        self._load_saved_device_key()

        self.live_weight_kg: float | None = None
        self.weight_kg: float | None = None
        self.last_packet: str | None = None
        self.last_message: ScaleMessage | None = None
        self.authenticated = False
        self.auth_r1: bytes | None = None
        self.auth_r2: bytes | None = None
        self.auth_r1e: bytes | None = None
        self.last_measurement: dict | None = None
        self._user_list_ready = False
        self._user_record: bytes | None = None
        self._online_user_sent = False
        # Defaults for the scale-side user profile; refined from A920 responses.
        self.user_weight_100g = 8000  # 80.00 kg
        self.user_flags = 0x01

        self._assembler = FrameAssembler()
        self._client: BleakClientWithServiceCache | None = None
        self._connect_lock = asyncio.Lock()
        self._connect_task: asyncio.Task[None] | None = None
        self._seq = 1
        self._listeners: set[Callable[[], None]] = set()

    @callback
    def register_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register an entity update callback."""
        self._listeners.add(listener)

        @callback
        def remove_listener() -> None:
            self._listeners.discard(listener)

        return remove_listener

    def _next_seq(self) -> int:
        """Return the next phone sequence number (odd, incrementing by 2)."""
        seq = self._seq
        self._seq += 2
        return seq

    async def _write_frame(self, cmd: int, data: bytes = b"") -> None:
        """Send a request frame to the scale, chunked to the ATT MTU."""
        client = self._client
        if client is None or not client.is_connected:
            raise RuntimeError("not connected")
        frame = build_frame(cmd, data, seq=self._next_seq())
        _LOGGER.debug("WHSCL1 write frame: %s", frame.hex())
        for offset in range(0, len(frame), MAX_WRITE_CHUNK):
            await client.write_gatt_char(
                WRITE_CHAR_UUID, frame[offset : offset + MAX_WRITE_CHUNK], response=False
            )

    @callback
    def advertisement_received(self, service_info: bluetooth.BluetoothServiceInfoBleak) -> None:
        """Connect when the sleeping scale wakes and advertises."""
        if self._client is not None and self._client.is_connected:
            return
        if self._connect_task is not None and not self._connect_task.done():
            return
        self._connect_task = self.hass.async_create_task(
            self._async_connect(service_info.device),
            f"Connect to {self.name}",
        )

    async def _async_connect(self, ble_device: BLEDevice) -> None:
        async with self._connect_lock:
            if self._client is not None and self._client.is_connected:
                return

            _LOGGER.info("Connecting to Wyze Scale %s", self.address)
            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self.name,
                    self._disconnected,
                    timeout=CONNECT_TIMEOUT,
                    use_services_cache=True,
                )
                self._client = client
                await client.start_notify(NOTIFY_CHAR_UUID, self._notification_received)
            except BLEAK_RETRY_EXCEPTIONS as err:
                _LOGGER.debug("Unable to connect to %s: %s", self.address, err)
                self._client = None
                return
            except Exception:
                _LOGGER.exception("Unexpected error connecting to %s", self.address)
                self._client = None
                return

            self._assembler = FrameAssembler()
            self._seq = 1
            self.authenticated = False

            # Kick off the auth handshake; measurements only flow after A9FD.
            self.hass.async_create_task(
                self._async_start_handshake(),
                f"Wyze Scale handshake {self.address}",
            )

    async def _async_start_handshake(self) -> None:
        """Send A9FA: random nonce (local-key auth) or cloud bind token.

        Matches the app: when a device key is stored locally, use the fast
        path with a random 16-char R1. Otherwise fall back to the cloud
        bind-token path for first-time provisioning.
        """
        try:
            import secrets

            r1 = None
            if self.device_key is not None:
                charset = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                r1 = "".join(secrets.choice(charset) for _ in range(16)).encode()
                _LOGGER.info("Using local device key for fast auth")
            elif self.cloud is not None:
                try:
                    device_id = WYZE_DEVICE_ID_PREFIX + self.address.replace(":", "").upper()
                    self.bind_token = await self.hass.async_add_executor_job(
                        self.cloud.get_bind_token, device_id
                    )
                    r1 = self.bind_token.encode()
                    _LOGGER.info("Fetched fresh bind token from Wyze cloud")
                except Exception:
                    _LOGGER.exception("Cloud bind token fetch failed")
            if r1 is None and self.bind_token:
                r1 = self.bind_token.encode()
            if r1 is None or len(r1) != 16:
                _LOGGER.error("No valid 16-char R1 available; cannot authenticate")
                return
            self.auth_r1 = r1
            _LOGGER.info("Sending A9FA (auth R1) to %s", self.address)
            await self._write_frame(CMD.AUTH_R1, r1)
        except Exception:
            _LOGGER.exception("Failed to send A9FA to %s", self.address)

    async def _async_finish_handshake(self, r2: bytes) -> None:
        """Respond to A9FB with A9FC once we know the device key."""
        if self.device_key is None:
            if self.cloud is not None and self.auth_r1e is not None:
                await self._async_provision_device_key(r2)
                return
            _LOGGER.warning(
                "Scale sent A9FB (R2=%s R1e=%s) but no device_key is configured; "
                "capture these values to obtain the key",
                r2.hex(),
                self.auth_r1e.hex() if self.auth_r1e else None,
            )
            return
        try:
            r1e = self.auth_r1e
            if r1e is not None:
                check = xxtea2_decrypt(r1e, self.device_key)
                if check != (self.auth_r1 or b""):
                    _LOGGER.warning(
                        "Scale R1e does not decrypt to our R1; device_key may be stale"
                    )
            payload = xxtea2_encrypt(r2, self.device_key)
            _LOGGER.info("Sending A9FC (auth R2 proof)")
            await self._write_frame(CMD.AUTH_R1E, payload)
        except Exception:
            _LOGGER.exception("Failed to complete auth handshake")

    async def _async_provision_device_key(self, r2: bytes) -> None:
        """Exchange the live A9FB challenge for the per-device key via the cloud."""
        r1e = self.auth_r1e
        if r1e is None or self.cloud is None:
            return
        try:
            device_id = WYZE_DEVICE_ID_PREFIX + self.address.replace(":", "").upper()
            _LOGGER.info("Provisioning device key via Wyze cloud (check_device_token)")
            a9fc_payload, device_key = await self.hass.async_add_executor_job(
                self.cloud.check_device_token, device_id, r1e, r2
            )
            self.device_key = device_key
            self._save_device_key(device_key)
            _LOGGER.info("Device key provisioned; sending A9FC")
            # The cloud returns the exact A9FC payload; send it directly.
            await self._write_frame(CMD.AUTH_R1E, a9fc_payload)
        except Exception:
            _LOGGER.exception("Device key provisioning failed")

    def _save_device_key(self, device_key: bytes) -> None:
        """Persist the device key to /config/wyze_scale_ble_device_key.txt."""
        import base64

        try:
            path = Path(self.hass.config.config_dir) / "wyze_scale_ble_device_key.txt"
            path.write_text(base64.b64encode(device_key).decode())
        except OSError:
            _LOGGER.exception("Could not persist device key")

    def _load_saved_device_key(self) -> None:
        """Load a previously provisioned device key if present."""
        import base64

        try:
            path = Path(self.hass.config.config_dir) / "wyze_scale_ble_device_key.txt"
            if path.exists():
                self.device_key = base64.b64decode(path.read_text().strip())
                _LOGGER.info("Loaded saved device key")
        except (OSError, ValueError):
            pass

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Handle the scale going back to sleep."""
        _LOGGER.debug("Wyze Scale %s disconnected", self.address)
        if self._client is client:
            self._client = None
            self.authenticated = False

    def _notification_received(self, _sender: object, data: bytearray) -> None:
        """Handle one BLE notification fragment."""
        chunk = bytes(data)
        _LOGGER.debug("WHSCL1 notification fragment: %s", chunk.hex())
        for frame in self._assembler.feed(chunk):
            self._handle_frame(frame)

    @callback
    def _handle_frame(self, frame: bytes) -> None:
        message = parse_frame(frame)
        if message is None:
            _LOGGER.debug("Rejected malformed WHSCL1 frame: %s", frame.hex())
            return

        self.last_packet = frame.hex()
        self.last_message = message

        if message.message_type is MessageType.AUTH_R2 and len(message.data) == 52:
            self.auth_r2 = message.data[0:16]
            self.auth_r1e = message.data[16:36]
            self.hass.async_create_task(
                self._async_finish_handshake(self.auth_r2),
                f"Wyze Scale A9FC {self.address}",
            )
        elif message.message_type is MessageType.AUTH_OK:
            self.authenticated = True
            _LOGGER.info("Wyze Scale auth succeeded (A9FD)")
            # Sync time like the app does, so stored records make sense.
            self.hass.async_create_task(
                self._async_post_auth(), f"Wyze Scale post-auth {self.address}"
            )
        elif message.message_type is MessageType.AUTH_FAIL:
            _LOGGER.error("Wyze Scale auth failed (A9FE); key is wrong")
        elif message.message_type is MessageType.GET_USER_INFO:
            # A920 response: user count + 28-byte records.
            self._parse_user_list(message.data)
        elif message.message_type is MessageType.ONLINE_USER_QUERY:
            # The scale pings A924 when it has unassigned data; the app
            # answers by re-running A931 with the user record.
            if self._user_record is not None:
                self.hass.async_create_task(
                    self._async_respond_a924(),
                    f"Wyze Scale A924 {self.address}",
                )
        elif message.message_type is MessageType.OFFLINE_DATA:
            # A931: stored measurement history (offline weigh-ins).
            self._parse_offline_records(message.data)
            self.hass.async_create_task(
                self._async_ack(CMD.GET_OFFLINE_DATA),
                f"Wyze Scale ack A931 {self.address}",
            )
        elif message.message_type is MessageType.LIVE_WEIGHT:
            self.live_weight_kg = message.weight_kg
        elif message.message_type is MessageType.STABLE_WEIGHT:
            self.live_weight_kg = message.weight_kg
            self.weight_kg = message.weight_kg
        elif message.message_type is MessageType.MEASUREMENT:
            try:
                # A942 payload: 00 | record(35B) where record = type weight ...
                record = message.data[1:36]
                self.last_measurement = decode_measurement_record(record)
                self.weight_kg = self.last_measurement["weight_kg"]
                self.live_weight_kg = self.last_measurement["weight_kg"]
                _LOGGER.info("WHSCL1 measurement: %s", self.last_measurement)
                self.hass.async_create_task(
                    self._async_ack_measurement(), f"Wyze Scale ack {self.address}"
                )
            except Exception:
                _LOGGER.exception("Failed to decode A942 record: %s", message.data.hex())

        if message.weight_kg is not None:
            _LOGGER.info(
                "WHSCL1 %s weight: %.2f kg (stable=%s)",
                message.message_type.name if message.message_type else "unknown",
                message.weight_kg,
                message.stable,
            )
        else:
            _LOGGER.info("WHSCL1 packet: %s", frame.hex())

        for listener in tuple(self._listeners):
            listener()

    async def _async_post_auth(self) -> None:
        """Send the app-like init sequence after successful auth.

        Capture flow: A910 (timestamp) then the user sync sequence
        (A920/A921/A923/A931) which lets the scale assign measurements.
        """
        import time as _time

        try:
            await self._write_frame(
                CMD.GET_DEVICE_INFO, struct.pack(">I", int(_time.time()))
            )
        except Exception:
            _LOGGER.exception("Post-auth device info failed")
        try:
            await self._async_send_online_user()
        except Exception:
            _LOGGER.exception("Post-auth user sync failed")

    async def _async_send_online_user(self) -> None:
        """Run the app's post-auth user sequence so the scale syncs data.

        Exact capture flow: A920 (empty) -> scale replies 01 + 28-byte user
        record -> phone echoes A921 (record), A923 (01 + record), and A931
        (record[0:16] + 0000). The scale then streams live weights and
        pushes stored measurements.
        """
        try:
            await self._write_frame(CMD.GET_USER_INFO)
            for _ in range(20):
                await asyncio.sleep(0.25)
                if self._user_list_ready:
                    break
            if self._user_record is None:
                _LOGGER.warning("No scale user record available; cannot sync")
                return
            record = self._user_record
            await self._write_frame(CMD.ONLINE_USER, record)
            await self._write_frame(CMD.CREATE_USER, b"\x01" + record)
            await self._write_frame(CMD.GET_OFFLINE_DATA, record[0:16] + b"\x00\x00")
            _LOGGER.info("User sync sequence sent (A921/A923/A931)")
        except Exception:
            _LOGGER.exception("Failed to run user sync sequence")

    async def _async_ack_measurement(self) -> None:
        """Acknowledge A942 so the scale does not replay the record."""
        try:
            await self._write_frame(CMD.BODY_FAT_RESULT, b"")
        except Exception:
            _LOGGER.exception("Failed to ack measurement")

    async def _async_ack(self, cmd: int) -> None:
        """Acknowledge a scale push so it clears the record."""
        try:
            await self._write_frame(cmd, b"")
        except Exception:
            _LOGGER.exception("Failed to ack cmd %04x", cmd)

    async def _async_respond_a924(self) -> None:
        """Answer the scale's A924 data-available ping with A931."""
        try:
            record = self._user_record
            if record is None:
                return
            await self._write_frame(CMD.GET_OFFLINE_DATA, record[0:16] + b"\x00\x00")
            _LOGGER.debug("Answered A924 with A931 offline-data request")
        except Exception:
            _LOGGER.exception("Failed to answer A924")

    def _parse_user_list(self, data: bytes) -> None:
        """Parse the A920 user-list response.

        Captured shape: user_count byte followed by 28-byte records:
        id[20] weight_be16 sex age height athlete onlyweight impedance_be16.
        The phone echoes the whole record back in A921.
        """
        if not data:
            return
        count = data[0]
        records = []
        body = data[1:]
        for offset in range(0, len(body) - 27, 28):
            record = body[offset : offset + 28]
            records.append(record)
        _LOGGER.info(
            "Scale user list: count=%d records=%s",
            count,
            [r.hex() for r in records],
        )
        if records:
            self._user_record = records[0]
            first = records[0]
            self.user_weight_100g = int.from_bytes(first[20:22], "big")
        self._user_list_ready = True

    def _parse_offline_records(self, data: bytes) -> None:
        """Parse A931/A942 offline history records into measurements.

        A931 payload: 4-byte header (0000 + count) then 35-byte records:
        type[1] weight_be16 impedance[8] user sex age height time_be32
        right_time body_building data_shape fat_be16 muscle_be16 bone
        water_be16 protein_be16 (encrypted impedance u32).
        """
        offset = 4 if len(data) >= 4 and data[0:2] == b"\x00\x00" else 2
        count = 0
        while offset + 35 <= len(data):
            record = data[offset : offset + 35]
            if record[0] == 1:
                try:
                    measurement = decode_measurement_record(record)
                    self.last_measurement = measurement
                    self.weight_kg = measurement["weight_kg"]
                    self.live_weight_kg = measurement["weight_kg"]
                    count += 1
                    _LOGGER.info("WHSCL1 offline measurement: %s", measurement)
                except Exception:
                    _LOGGER.exception(
                        "Failed to decode offline record: %s", record.hex()
                    )
            offset += 35
        if count:
            _LOGGER.info("Parsed %d offline measurement(s)", count)
            for listener in tuple(self._listeners):
                listener()

    async def async_stop(self) -> None:
        """Stop pending work and disconnect."""
        if self._connect_task is not None and not self._connect_task.done():
            self._connect_task.cancel()
        client = self._client
        self._client = None
        if client is not None and client.is_connected:
            await client.disconnect()