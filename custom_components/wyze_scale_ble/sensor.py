"""Sensors for Wyze Scale BLE."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfMass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MODEL
from .manager import WyzeScaleManager

KG_TO_LB = 2.2046226218487757


@dataclass(frozen=True, kw_only=True)
class WyzeScaleSensorDescription(SensorEntityDescription):
    """Describe a Wyze Scale sensor."""

    value_fn: Callable[[WyzeScaleManager], Any]


SENSORS = (
    WyzeScaleSensorDescription(
        key="weight",
        translation_key="weight",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.POUNDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda manager: (
            manager.weight_kg * KG_TO_LB if manager.weight_kg is not None else None
        ),
    ),
    WyzeScaleSensorDescription(
        key="live_weight",
        translation_key="live_weight",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.POUNDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda manager: (
            manager.live_weight_kg * KG_TO_LB
            if manager.live_weight_kg is not None
            else None
        ),
    ),
    WyzeScaleSensorDescription(
        key="body_fat",
        translation_key="body_fat",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda manager: (manager.last_measurement or {}).get("body_fat_pct"),
    ),
    WyzeScaleSensorDescription(
        key="muscle_mass",
        translation_key="muscle_mass",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.POUNDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda manager: (
            (manager.last_measurement or {}).get("muscle_kg", 0) * KG_TO_LB
            if (manager.last_measurement or {}).get("muscle_kg") is not None
            else None
        ),
    ),
    WyzeScaleSensorDescription(
        key="body_water",
        translation_key="body_water",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda manager: (manager.last_measurement or {}).get("water_pct"),
    ),
    WyzeScaleSensorDescription(
        key="protein",
        translation_key="protein",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda manager: (manager.last_measurement or {}).get("protein_pct"),
    ),
    WyzeScaleSensorDescription(
        key="bone_mass",
        translation_key="bone_mass",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.POUNDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda manager: (
            (manager.last_measurement or {}).get("bone_kg", 0) * KG_TO_LB
            if (manager.last_measurement or {}).get("bone_kg") is not None
            else None
        ),
    ),
    WyzeScaleSensorDescription(
        key="raw_packet",
        translation_key="raw_packet",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda manager: manager.last_packet,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wyze Scale sensors."""
    manager: WyzeScaleManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        WyzeScaleSensor(manager, entry, description) for description in SENSORS
    )


class WyzeScaleSensor(SensorEntity):
    """Representation of a Wyze Scale value."""

    _attr_has_entity_name = True

    def __init__(
        self,
        manager: WyzeScaleManager,
        entry: ConfigEntry,
        description: WyzeScaleSensorDescription,
    ) -> None:
        self.manager = manager
        self.entity_description = description
        self._attr_unique_id = f"{manager.address}_{description.key}"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, manager.address)},
            identifiers={(DOMAIN, manager.address)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=entry.title,
        )

    @property
    def native_value(self) -> Any:
        """Return the current sensor value."""
        return self.entity_description.value_fn(self.manager)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the last measurement and auth state for diagnostics."""
        if self.entity_description.key != "raw_packet":
            return {}
        return {
            "authenticated": self.manager.authenticated,
            "measurement": self.manager.last_measurement,
        }

    async def async_added_to_hass(self) -> None:
        """Register for decoded packet updates."""
        self.async_on_remove(self.manager.register_listener(self._async_update))

    @callback
    def _async_update(self) -> None:
        self.async_write_ha_state()