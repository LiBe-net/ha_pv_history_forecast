"""The HA SQL PV Forecast integration."""
from __future__ import annotations

import logging
from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import WeatherCoordinator
from .const import CONF_WEATHER_ENTITY

_LOGGER: logging.Logger = logging.getLogger(__name__)

DOMAIN: Final = "pv_history_forecast"
PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}

    # Initialize Weather Coordinator (options override data so the user can change it via Edit)
    weather_entity = (entry.options or {}).get(CONF_WEATHER_ENTITY) or entry.data.get(CONF_WEATHER_ENTITY, "weather.forecast_home")
    weather_coordinator = WeatherCoordinator(
        hass=hass,
        weather_entity=weather_entity,
    )
    
    # Store coordinator in hass data
    hass.data[DOMAIN][entry.entry_id]["weather_coordinator"] = weather_coordinator
    
    # Perform first update
    await weather_coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the entry whenever the user saves options.
    # We delay by 0.5 s via async_call_later so that the options flow can fully
    # close and return a clean "success" response to the browser BEFORE the
    # reload unloads the integration context.  Without the delay HA tears down
    # the flow mid-response and the UI shows "Unknown error" even though the
    # data was saved correctly.
    async def _delayed_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
        def _do_reload(_now: object) -> None:
            hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
        hass.call_later(0.5, _do_reload)

    entry.async_on_unload(entry.add_update_listener(_delayed_reload))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok

