"""Wyze cloud client for scale device-key provisioning.

Implements the exact flow from the decompiled Wyze app (vg/b.java + kh/a.java):
- auth-prod.api.wyze.com/api/user/login  (triple-MD5 password, user API key)
- wyze-scale-service.wyzecam.com/plugin/scale/get_token (WpkExService GET signing)
- wyze-scale-service.wyzecam.com/plugin/scale/check_device_token
  (encrypt_bind_token = BASE64(R1e), device_token = ASCII(R2))

The signing follows WpkNetServiceClient: signature2 = HMAC-MD5(
  md5(access_token + app_secret), sorted-querystring-or-json-body), and headers
  access_token/requestid/appid/appinfo/phoneid.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

_LOGGER = logging.getLogger(__name__)

AUTH_URL = "https://auth-prod.api.wyze.com/api/user/login"
SCALE_SERVICE_URL = "https://wyze-scale-service.wyzecam.com"
APP_ID = "9319141212m2ik"
APP_SECRET = "wyze_app_secret_key_132"
APP_VERSION = "2.19.14"
GET_TOKEN_ID = 101
CHECK_TOKEN_ID = 201


def _triple_md5(password: str) -> str:
    once = hashlib.md5(password.encode()).hexdigest()
    twice = hashlib.md5(once.encode()).hexdigest()
    return hashlib.md5(twice.encode()).hexdigest()


def _request_id(nonce: int) -> str:
    return hashlib.md5(hashlib.md5(str(nonce).encode()).hexdigest().encode()).hexdigest()


def _dynamic_signature(access_token: str, body: str) -> str:
    key = hashlib.md5(f"{access_token}{APP_SECRET}".encode()).hexdigest()
    return hmac.new(key.encode(), body.encode(), hashlib.md5).hexdigest()


def _ex_headers(access_token: str, phone_id: str) -> dict[str, str]:
    return {
        "access_token": access_token,
        "User-Agent": f"wyze_android_{APP_VERSION}",
        "appinfo": f"wyze_android_{APP_VERSION}",
        "appid": APP_ID,
        "phoneid": phone_id,
    }


class WyzeCloudClient:
    """Minimal client for the Wyze scale token endpoints."""

    def __init__(self, key_id: str, api_key: str, username: str, password: str) -> None:
        self.key_id = key_id
        self.api_key = api_key
        self.username = username
        self.password = password
        self._access_token: str | None = None
        self._phone_id = str(uuid.uuid4())

    def _login(self) -> str:
        payload = json.dumps({"email": self.username, "password": _triple_md5(self.password)}).encode()
        req = urllib.request.Request(
            AUTH_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "keyid": self.key_id,
                "apikey": self.api_key,
                "User-Agent": "wyze-sdk-2.3.8",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            body = json.load(response)
        if not body.get("access_token"):
            raise RuntimeError(f"Wyze login failed: {body.get('errorCode')}")
        self._access_token = body["access_token"]
        return self._access_token

    def _token(self) -> str:
        if self._access_token is None:
            self._login()
        return self._access_token

    def _get(self, path: str, params: dict[str, str]) -> dict:
        access_token = self._token()
        nonce = round(time.time() * 1000)
        params = dict(params)
        params["nonce"] = str(nonce)
        body = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        headers = _ex_headers(access_token, self._phone_id)
        headers["requestid"] = _request_id(nonce)
        headers["signature2"] = _dynamic_signature(access_token, body)
        url = f"{SCALE_SERVICE_URL}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as err:
            if err.code == 401 and self._access_token:
                self._access_token = None
                self._login()
                return self._get(path, params)
            raise

    def _post(self, path: str, payload: dict) -> dict:
        access_token = self._token()
        nonce = round(time.time() * 1000)
        payload = dict(payload)
        payload["nonce"] = str(nonce)
        body = json.dumps(payload, separators=(",", ":"))
        headers = _ex_headers(access_token, self._phone_id)
        headers["requestid"] = _request_id(nonce)
        headers["signature2"] = _dynamic_signature(access_token, body)
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{SCALE_SERVICE_URL}{path}", data=body.encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as err:
            if err.code == 401 and self._access_token:
                self._access_token = None
                self._login()
                return self._post(path, payload)
            raise

    def get_bind_token(self, device_id: str) -> str:
        """Fetch a fresh bind token for the scale."""
        resp = self._get("/plugin/scale/get_token", {"device_id": device_id})
        if resp.get("code") != 1:
            raise RuntimeError(f"get_token failed: {resp.get('code')} {resp.get('message')}")
        return resp["data"]["bind_token"]

    def check_device_token(
        self, device_id: str, r1_encrypted: bytes, r2: bytes
    ) -> tuple[bytes, bytes]:
        """Exchange the live A9FB values for the device key.

        r1_encrypted: the 20-byte R1e from A9FB (encrypt_bind_token = base64).
        r2: the 16-byte scale nonce (device_token = raw ASCII string).
        Returns (encrypt_device_token bytes, device_key bytes).

        Both values arrive base64-encoded (the app decodes them with nh.a.a
        before storing/using; the key is stored as base64 in prefs too).
        """
        import base64

        resp = self._post(
            "/plugin/scale/check_device_token",
            {
                "device_id": device_id,
                "encrypt_bind_token": base64.b64encode(r1_encrypted).decode(),
                "device_token": r2.decode("latin-1"),
            },
        )
        if resp.get("code") != 1:
            raise RuntimeError(
                f"check_device_token failed: {resp.get('code')} {resp.get('message')}"
            )
        data = resp["data"]
        return (
            base64.b64decode(data["encrypt_device_token"]),
            base64.b64decode(data["device_key"]),
        )