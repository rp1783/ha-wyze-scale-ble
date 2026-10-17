# ha-wyze-scale-ble

Custom Home Assistant integration for the original **Wyze Scale (WHSCL1)** — connecting directly over Bluetooth, no cloud polling required.

Connects through Home Assistant's Bluetooth stack (including ESPHome Bluetooth proxies), authenticates with the scale's per-device key, and streams measurements in real time as you weigh.

## Features

- **Live weight** streams while you're on the scale (diagnostic sensor)
- **Weight, body fat, muscle mass, body water, protein, bone mass** from the completed measurement
- **Stored history sync** — weigh-ins recorded while HA was offline are pulled on next connect
- Units: **lb** (converted from the scale's native kg)
- Works entirely on your LAN; no Wyze account needed after initial setup

## Supported device

- Wyze Scale **WHSCL1** (original, BLE model) — advertises as `com.jiuan.BFSV21`
- Not compatible with Wyze Scale S / Scale X (different protocol)

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories**
2. Add `https://github.com/rp1783/ha-wyze-scale-ble` with category **Integration**
3. Find "Wyze Scale BLE" → **Download**
4. Restart Home Assistant

### Manual

Copy `custom_components/wyze_scale_ble/` into your `/config/custom_components/` directory and restart Home Assistant.

## Setup

1. Wake the scale (step on it briefly), then:
   **Settings → Devices & services → Add Integration → Wyze Scale BLE**
2. Select your discovered scale (named `WHSCL1`).

### Authentication (one-time)

The scale requires a per-device key that is normally provisioned by the Wyze app. To provision it directly from Home Assistant:

1. Create an API key at [developer.wyze.com](https://developer.wyze.com) (Personal App → API Key)
2. Create `/config/wyze_scale_ble.json`:
   ```json
   {
     "key_id": "your-key-id",
     "api_key": "your-api-key",
     "username": "your-wyze-email",
     "password": "your-wyze-password"
   }
   ```
3. Weigh once. The integration fetches a bind token, completes the key exchange, and stores the device key in `/config/wyze_scale_ble_device_key.txt`.
4. **Delete the JSON credentials file.** Future weigh-ins use the stored device key only — no cloud access.

> If you already have the Wyze app working, the scale may already be provisioned; the integration performs the same exchange.

## How it works

The WHSCL1 speaks a proprietary protocol over a custom GATT service (`com.jiuan.BFSV21`): length-prefixed frames with an additive checksum, a challenge–response handshake (A9FA/A9FB/A9FC/A9FD) using XXTEA with a per-device key provisioned by the Wyze cloud, then live weight (A940), final weight (A941), and body-composition records (A942) plus offline history (A931).

This integration was reverse-engineered from public Bluetooth HCI captures (openScale issue #853) and the Wyze Android app. See `custom_components/wyze_scale_ble/protocol.py` for the full protocol implementation.

## Notes

- The scale is Bluetooth-only and sleeps between uses; it advertises when stepped on, and the integration connects automatically via your best Bluetooth proxy. Keep a proxy reasonably close to the scale.
- Keep the Wyze app's Bluetooth off (or don't keep the app open) while weighing — only one central can hold the connection.
- First connection after a HA restart happens within a few seconds of stepping on the scale; live weight starts streaming once authenticated.

## License

MIT