"""Button platform for HA SQL PV Forecast integration."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import generate_entity_id
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_SENSOR_PREFIX, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up retune trigger buttons for one config entry."""
    prefix = config_entry.data.get(CONF_SENSOR_PREFIX, "pv_hist")

    async_add_entities(
        [
            PVForecastRetuneButton(
                hass=hass,
                config_entry=config_entry,
                name=f"{prefix}_retune",
                icon="mdi:tune-variant",
            ),
        ],
        False,
    )


class PVForecastRetuneButton(ButtonEntity):
    """Button to trigger manual retune for the entry's main sensor."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        name: str,
        icon: str,
    ) -> None:
        """Initialize retune button."""
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{DOMAIN}_{config_entry.entry_id}_{name}"
        self.entity_id = generate_entity_id("button.{}", name, hass=hass)

    async def async_press(self) -> None:
        """Trigger manual retune on button press."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
        sensor = entry_data.get("main_sensor")
        if sensor is None:
            raise HomeAssistantError("Main PV History Forecast sensor is not available yet")

        if not getattr(sensor, "_use_retune", False):
            raise HomeAssistantError("Retune is disabled for this entry")

        ok = await sensor.async_manual_retune(force_refresh=True)
        if not ok:
            raise HomeAssistantError("Manual retune failed. Check SQL data availability.")
