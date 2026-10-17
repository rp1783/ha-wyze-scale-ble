"""The Wyze Scale BLE integration."""

from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import ADDRESS, BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, callback

from .const import CONF_BIND_TOKEN, CONF_DEVICE_KEY, DOMAIN
from .manager import WyzeScaleManager

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Wyze Scale from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    manager = WyzeScaleManager(
        hass,
        address,
        entry.title,
        bind_token=entry.data.get(CONF_BIND_TOKEN),
        device_key=entry.data.get(CONF_DEVICE_KEY),
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager

    @callback
    def async_update_ble(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        _change: bluetooth.BluetoothChange,
    ) -> None:
        manager.advertisement_received(service_info)

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            async_update_ble,
            BluetoothCallbackMatcher({ADDRESS: address}),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )

    if service_info := bluetooth.async_last_service_info(hass, address, connectable=True):
        manager.advertisement_received(service_info)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Wyze Scale config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    manager: WyzeScaleManager = hass.data[DOMAIN].pop(entry.entry_id)
    await manager.async_stop()
    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN)
    return True
