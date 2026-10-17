"""Config flow for Wyze Scale BLE."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback

from .const import DOMAIN, SERVICE_UUID


class WyzeScaleBleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle configuration for an original Wyze Scale."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        from .options_flow import WyzeScaleBleOptionsFlow

        return WyzeScaleBleOptionsFlow()

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle discovery by Home Assistant Bluetooth."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or "Wyze Scale"
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered scale."""
        if user_input is not None and self._discovery_info is not None:
            return self.async_create_entry(
                title=self._discovery_info.name or "Wyze Scale",
                data={CONF_ADDRESS: self._discovery_info.address},
            )
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": (
                    self._discovery_info.name
                    if self._discovery_info and self._discovery_info.name
                    else "Wyze Scale"
                )
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow manual selection from currently discovered scales."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            discovery = self._discovered[address]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=discovery.name or "Wyze Scale",
                data={CONF_ADDRESS: address},
            )

        configured = self._async_current_ids(include_ignore=False)
        for discovery in async_discovered_service_info(self.hass):
            if discovery.address in configured:
                continue
            service_uuids = {uuid.lower() for uuid in discovery.service_uuids}
            if SERVICE_UUID not in service_uuids and discovery.name != "WHSCL1":
                continue
            self._discovered[discovery.address] = discovery

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            address: f"{info.name or 'Wyze Scale'} ({address})"
                            for address, info in self._discovered.items()
                        }
                    )
                }
            ),
        )
