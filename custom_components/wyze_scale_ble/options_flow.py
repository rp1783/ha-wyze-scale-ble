"""Options flow for Wyze Scale BLE: bind token and device key."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlowResult,
    OptionsFlow,
)

from .const import CONF_BIND_TOKEN, CONF_DEVICE_KEY


class WyzeScaleBleOptionsFlow(OptionsFlow):
    """Manage the Wyze Scale auth options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            bind_token = user_input.get(CONF_BIND_TOKEN, "").strip()
            device_key = user_input.get(CONF_DEVICE_KEY, "").strip()
            if bind_token and len(bind_token) != 16:
                errors[CONF_BIND_TOKEN] = "invalid_bind_token"
            if device_key and len(device_key) not in (16, 32):
                errors[CONF_DEVICE_KEY] = "invalid_device_key"
            if not errors:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_BIND_TOKEN: bind_token,
                        CONF_DEVICE_KEY: device_key,
                    },
                )

        current = self.config_entry.options
        schema = {
            vol.Optional(
                CONF_BIND_TOKEN,
                description={"suggested_value": current.get(CONF_BIND_TOKEN, "")},
            ): str,
            vol.Optional(
                CONF_DEVICE_KEY,
                description={"suggested_value": current.get(CONF_DEVICE_KEY, "")},
            ): str,
        }
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            errors=errors,
        )