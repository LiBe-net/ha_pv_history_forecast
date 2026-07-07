"""The HA SQL PV Forecast integration."""
from __future__ import annotations

import logging
import time
from typing import Final

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later

from .coordinator import WeatherCoordinator
from .const import CONF_WEATHER_ENTITY

_LOGGER: logging.Logger = logging.getLogger(__name__)

DOMAIN: Final = "pv_history_forecast"
SERVICE_TRIGGER_FULL_RETUNE: Final = "trigger_full_retune"
SERVICE_FORCE_RETUNE: Final = "force_retune"
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

SERVICE_SCHEMA_TRIGGER_FULL_RETUNE = vol.Schema(
    {
        vol.Optional("entity_id"): str,
        vol.Optional("entry_id"): str,
        vol.Optional("force_refresh", default=True): bool,
    }
)


async def _async_handle_trigger_full_retune(hass: HomeAssistant, call: ServiceCall) -> None:
    """Trigger a manual retune run from GUI/script/service call."""
    mode = "retune"
    force_refresh = bool(call.data.get("force_refresh", True))
    target_entity_id = call.data.get("entity_id")
    target_entry_id = call.data.get("entry_id")

    domain_data = hass.data.get(DOMAIN, {})
    entry_ids = [k for k in domain_data if not k.startswith("_")]
    candidates = []
    for entry_id in entry_ids:
        sensor = domain_data.get(entry_id, {}).get("main_sensor")
        if sensor is not None:
            candidates.append((entry_id, sensor))

    if not candidates:
        raise HomeAssistantError("No PV History Forecast main sensor is available yet")

    selected_sensor = None
    selected_entry_id = None

    if target_entry_id:
        selected_sensor = domain_data.get(target_entry_id, {}).get("main_sensor")
        selected_entry_id = target_entry_id if selected_sensor is not None else None
    elif target_entity_id:
        for entry_id, sensor in candidates:
            if getattr(sensor, "entity_id", None) == target_entity_id:
                selected_sensor = sensor
                selected_entry_id = entry_id
                break
    elif len(candidates) == 1:
        selected_entry_id, selected_sensor = candidates[0]

    if selected_sensor is None:
        if target_entity_id or target_entry_id:
            raise HomeAssistantError("Requested PV History Forecast sensor not found")
        raise HomeAssistantError(
            "Multiple PV History Forecast entries found. Please provide entity_id or entry_id."
        )

    if not getattr(selected_sensor, "_use_retune", False):
        raise HomeAssistantError("Retune is disabled for the selected sensor")

    ok = await selected_sensor.async_manual_retune(force_refresh=force_refresh)
    if not ok:
        raise HomeAssistantError(
            f"Manual retune could not run for {selected_sensor.entity_id}. "
            "Check whether SQL data is available and contains enough samples."
        )

    _LOGGER.info(
        "Manual retune triggered via service for entry_id=%s entity_id=%s mode=%s",
        selected_entry_id,
        getattr(selected_sensor, "entity_id", "unknown"),
        mode,
    )


def _async_register_services(hass: HomeAssistant) -> None:
    """Register domain services once per HA runtime."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("_runtime_start_ts", time.time())
    if hass.data[DOMAIN].get("_retune_service_registered"):
        return

    async def _handle_trigger_full_retune(call: ServiceCall) -> None:
        await _async_handle_trigger_full_retune(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_TRIGGER_FULL_RETUNE,
        _handle_trigger_full_retune,
        schema=SERVICE_SCHEMA_TRIGGER_FULL_RETUNE,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_RETUNE,
        _handle_trigger_full_retune,
        schema=SERVICE_SCHEMA_TRIGGER_FULL_RETUNE,
    )
    hass.data[DOMAIN]["_retune_service_registered"] = True


def _schedule_entry_kickstart_refresh(hass: HomeAssistant, entry_id: str) -> None:
    """Schedule early coordinator+sensor refreshes to clear startup fallback windows."""

    async def _kickstart(_now) -> None:
        entry_data = hass.data.get(DOMAIN, {}).get(entry_id, {})
        coordinator = entry_data.get("weather_coordinator")
        if coordinator is not None:
            try:
                await coordinator.async_request_refresh()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Kickstart coordinator refresh failed for %s: %s", entry_id, err)

        sensor = entry_data.get("main_sensor")
        if sensor is not None:
            try:
                sensor._last_update_time = None
                await sensor.async_update()
                sensor.async_write_ha_state()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Kickstart main sensor refresh failed for %s: %s", entry_id, err)

    for delay in (20.0, 60.0, 120.0):
        async_call_later(hass, delay, _kickstart)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up integration domain and services before config entries."""
    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}
    _async_register_services(hass)

    # Initialize Weather Coordinator (options override data so the user can change it via Edit)
    weather_entity = (entry.options or {}).get(CONF_WEATHER_ENTITY) or entry.data.get(CONF_WEATHER_ENTITY, "weather.forecast_home")
    weather_coordinator = WeatherCoordinator(
        hass=hass,
        weather_entity=weather_entity,
    )

    # Store coordinator in hass data
    hass.data[DOMAIN][entry.entry_id]["weather_coordinator"] = weather_coordinator

    # Kick off first update in background to avoid blocking startup setup path.
    hass.async_create_task(weather_coordinator.async_refresh())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _schedule_entry_kickstart_refresh(hass, entry.entry_id)

    # Reload the entry whenever the user saves options.
    # We delay by 0.5 s via async_call_later so that the options flow can fully
    # close and return a clean "success" response to the browser BEFORE the
    # reload unloads the integration context.  Without the delay HA tears down
    # the flow mid-response and the UI shows "Unknown error" even though the
    # data was saved correctly.
    async def _delayed_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
        async def _do_reload(_now) -> None:
            await hass.config_entries.async_reload(entry.entry_id)
        async_call_later(hass, 0.5, _do_reload)

    entry.async_on_unload(entry.add_update_listener(_delayed_reload))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    if unload_ok:
        remaining_entries = [k for k in hass.data[DOMAIN] if not k.startswith("_")]
        if not remaining_entries and hass.data[DOMAIN].get("_retune_service_registered"):
            hass.services.async_remove(DOMAIN, SERVICE_TRIGGER_FULL_RETUNE)
            hass.services.async_remove(DOMAIN, SERVICE_FORCE_RETUNE)
            hass.data[DOMAIN].pop("_retune_service_registered", None)

    return unload_ok

