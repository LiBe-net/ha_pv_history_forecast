"""Config flow for HA SQL PV Forecast."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
)

from . import const as _const
from .const import (
    CONF_DB_URL,
    CONF_SENSOR_PRECIP,
    CONF_SENSOR_PREFIX,
    CONF_PV_HISTORY_DAYS,
    CONF_SENSOR_CLOUDS,
    CONF_SENSOR_FORECAST,
    CONF_SENSOR_PV,
    CONF_SENSOR_UV,
    CONF_SENSOR_TEMP,
    CONF_SENSOR_PRECIP,
    CONF_WEATHER_ENTITY,
    CONF_VALUE_TEMPLATE,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_DEVICE_CLASS,
    CONF_STATE_CLASS,
    CONF_PV_MAX_RECORD,
    DEFAULT_SENSOR_PREFIX,
    DEFAULT_VALUE_TEMPLATE,
    DEFAULT_UNIT_OF_MEASUREMENT,
    DEFAULT_DEVICE_CLASS,
    DEFAULT_STATE_CLASS,
    DEFAULT_PV_MAX_RECORD,
    DEFAULT_SQL_QUERY,
    DOMAIN,
)

CONF_RETUNE = _const.CONF_RETUNE
DEFAULT_RETUNE = bool(_const.DEFAULT_RETUNE)

_LOGGER = logging.getLogger(__name__)


def _auto_sensor_ids(prefix: str) -> dict[str, str]:
    """Return integration-managed cloud, UV, and temperature sensor entity IDs."""
    return {
        CONF_SENSOR_CLOUDS: f"sensor.{prefix}_cloud_coverage",
        CONF_SENSOR_UV: f"sensor.{prefix}_uv",
        CONF_SENSOR_TEMP: f"sensor.{prefix}_temperature",
        CONF_SENSOR_PRECIP: f"sensor.{prefix}_precipitation",
    }


def _apply_auto_sensors(target: dict[str, Any], prefix: str) -> None:
    """Always use integration auto-sensors for cloud, UV, and temperature."""
    target.update(_auto_sensor_ids(prefix))


def _build_sql_query(
    *,
    prefix: str,
    sensor_pv_list: list[str],
    sensor_forecast: str,
    history_days: int,
    weather_entity: str,
) -> str:
    """Format DEFAULT_SQL_QUERY with auto-managed auxiliary sensors."""
    auto = _auto_sensor_ids(prefix)
    sensor_pv_sql_list = ", ".join(f"'{s}'" for s in sensor_pv_list)
    sensor_pv_first = sensor_pv_list[0] if sensor_pv_list else ""
    return DEFAULT_SQL_QUERY.format(
        sensor_clouds=auto[CONF_SENSOR_CLOUDS],
        sensor_pv_list=sensor_pv_sql_list,
        sensor_pv_first=sensor_pv_first,
        sensor_forecast=sensor_forecast,
        sensor_uv=auto[CONF_SENSOR_UV],
        sensor_temp=auto[CONF_SENSOR_TEMP],
        sensor_precip=auto[CONF_SENSOR_PRECIP],
        history_days=history_days,
        weather_entity=weather_entity,
    )


def _resolve_retune_enabled(
    options: dict[str, Any] | None,
    data: dict[str, Any] | None,
    *,
    default_if_missing: bool,
) -> bool:
    """Resolve retune flag with explicit legacy fallback behavior.

    Existing entries created before the retune option existed should stay
    non-retuned unless the user explicitly enables it.
    """
    opts = options or {}
    entry_data = data or {}
    if CONF_RETUNE in opts:
        return bool(opts.get(CONF_RETUNE))
    if CONF_RETUNE in entry_data:
        return bool(entry_data.get(CONF_RETUNE))
    return bool(default_if_missing)


async def _check_weather_supports_forecasts(hass, weather_entity: str) -> bool:
    """Return True when weather_entity supports the weather.get_forecasts action."""
    try:
        response = await hass.services.async_call(
            "weather",
            "get_forecasts",
            {"entity_id": weather_entity, "type": "hourly"},
            blocking=True,
            return_response=True,
        )
        # Service call succeeded AND the entity is in the response
        return weather_entity in (response or {})
    except Exception:  # noqa: BLE001  (ServiceNotFound, ServiceNotSupported, …)
        return False


async def _check_weather_has_cloud_forecast(hass, weather_entity: str) -> bool:
    """Return True when the weather entity's hourly forecast contains cloud_coverage."""
    try:
        response = await hass.services.async_call(
            "weather",
            "get_forecasts",
            {"entity_id": weather_entity, "type": "hourly"},
            blocking=True,
            return_response=True,
        )
        forecasts = (response or {}).get(weather_entity, {}).get("forecast", [])
        return any(f.get("cloud_coverage") is not None for f in forecasts)
    except Exception:  # noqa: BLE001
        return True  # don't block setup on unexpected errors


def _get_energy_sensor_ids(hass) -> list[str]:
    """Return sensor entity_ids with device_class=energy, unit kWh/Wh, and statistics enabled."""
    ent_reg = er.async_get(hass)
    result = []
    for state in hass.states.async_all("sensor"):
        if state.attributes.get("device_class") != "energy":
            continue
        unit = state.attributes.get("unit_of_measurement", "")
        if unit not in ("kWh", "Wh"):
            continue
        entry = ent_reg.async_get(state.entity_id)
        has_stats = (
            (entry.capabilities or {}).get("state_class") if entry else None
        ) or state.attributes.get("state_class")
        if has_stats:
            result.append(state.entity_id)
    return result


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HA SQL PV Forecast."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        return OptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step - Sensor Prefix Configuration."""
        errors = {}

        if user_input is not None:
            # Detect the HA recorder's database URL automatically
            try:
                from homeassistant.components.recorder import get_instance  # noqa: PLC0415
                db_url = get_instance(self.hass).db_url
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Could not access HA recorder: %s", err)
                errors["base"] = "invalid_db_url"
            else:
                if not db_url.startswith("sqlite://"):
                    _LOGGER.error(
                        "Home Assistant is not using a SQLite database: %s — only SQLite is supported",
                        db_url,
                    )
                    errors["base"] = "sqlite_required"
                else:
                    user_input[CONF_DB_URL] = db_url
                    self.data_cache = user_input
                    return await self.async_step_sensors()

        schema = vol.Schema(
            {
                vol.Required(CONF_SENSOR_PREFIX, default=DEFAULT_SENSOR_PREFIX): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_sensors(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the sensors configuration step."""
        errors = {}

        if user_input is not None:
            weather_entity = user_input.get(CONF_WEATHER_ENTITY, "").strip()
            sensor_pv_raw = user_input.get(CONF_SENSOR_PV) or []
            sensor_pv_list = [sensor_pv_raw] if isinstance(sensor_pv_raw, str) and sensor_pv_raw else (sensor_pv_raw if isinstance(sensor_pv_raw, list) else [])

            if not weather_entity:
                errors["base"] = "weather_entity_required"
            elif not weather_entity.startswith("weather."):
                errors[CONF_WEATHER_ENTITY] = "must_be_weather_entity"

            if not sensor_pv_list:
                errors["base"] = "sensor_pv_required"
            else:
                for _pv in sensor_pv_list:
                    if not _pv.startswith("sensor."):
                        errors[CONF_SENSOR_PV] = "must_be_sensor_entity"
                        break

            if not errors.get(CONF_SENSOR_PV) and sensor_pv_list:
                for _pv in sensor_pv_list:
                    pv_state = self.hass.states.get(_pv)
                    if pv_state:
                        pv_unit = pv_state.attributes.get("unit_of_measurement", "")
                        if pv_unit and pv_unit not in ("kWh", "Wh"):
                            errors[CONF_SENSOR_PV] = "sensor_pv_wrong_unit"
                            break

            if errors:
                return self.async_show_form(
                    step_id="sensors",
                    data_schema=self._get_sensors_schema(defaults=user_input),
                    errors=errors,
                )

            supports_forecasts = await _check_weather_supports_forecasts(self.hass, weather_entity)
            if not supports_forecasts:
                return self.async_show_form(
                    step_id="sensors",
                    data_schema=self._get_sensors_schema(defaults=user_input),
                    errors={"base": "no_forecast_support"},
                )

            has_cloud = await _check_weather_has_cloud_forecast(self.hass, weather_entity)
            if not has_cloud:
                return self.async_show_form(
                    step_id="sensors",
                    data_schema=self._get_sensors_schema(defaults=user_input),
                    errors={"base": "no_cloud_forecast"},
                )

            data = {**self.data_cache, **user_input}
            prefix = data[CONF_SENSOR_PREFIX]
            data[CONF_SENSOR_FORECAST] = f"sensor.{prefix}_weather_forecast"
            _apply_auto_sensors(data, prefix)

            history_days = data.get(CONF_PV_HISTORY_DAYS, 30)
            _pv_entries = data[CONF_SENSOR_PV]
            if isinstance(_pv_entries, str):
                _pv_entries = [_pv_entries] if _pv_entries else []
            data[CONF_SENSOR_PV] = _pv_entries
            data["sql_query"] = _build_sql_query(
                prefix=prefix,
                sensor_pv_list=_pv_entries,
                sensor_forecast=data[CONF_SENSOR_FORECAST],
                history_days=history_days,
                weather_entity=weather_entity,
            )
            data[CONF_VALUE_TEMPLATE] = DEFAULT_VALUE_TEMPLATE
            data[CONF_UNIT_OF_MEASUREMENT] = DEFAULT_UNIT_OF_MEASUREMENT
            data[CONF_DEVICE_CLASS] = DEFAULT_DEVICE_CLASS
            data[CONF_STATE_CLASS] = (
                None
                if str(DEFAULT_DEVICE_CLASS).lower() == "energy"
                and str(DEFAULT_STATE_CLASS).lower() == "measurement"
                else DEFAULT_STATE_CLASS
            )
            data[CONF_RETUNE] = bool(data.get(CONF_RETUNE, DEFAULT_RETUNE))

            await self.async_set_unique_id(prefix)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=prefix, data=data
            )

        return self.async_show_form(
            step_id="sensors",
            data_schema=self._get_sensors_schema(),
            errors=errors,
        )

    def _get_sensors_schema(self, defaults: dict[str, Any] | None = None) -> vol.Schema:
        """Get the sensors configuration schema with pre-filtered entity selectors."""
        d = defaults or {}
        energy_ids = _get_energy_sensor_ids(self.hass)
        pv_selector = EntitySelector(
            EntitySelectorConfig(include_entities=energy_ids, multiple=True)
            if energy_ids
            else EntitySelectorConfig(domain="sensor", device_class="energy", multiple=True)
        )
        _pv_default = d.get(CONF_SENSOR_PV, [])
        if isinstance(_pv_default, str):
            _pv_default = [_pv_default] if _pv_default else []
        return vol.Schema(
            {
                vol.Required(
                    CONF_WEATHER_ENTITY,
                    default=d.get(CONF_WEATHER_ENTITY, ""),
                ): EntitySelector(
                    EntitySelectorConfig(
                        domain="weather",
                        multiple=False,
                    )
                ),
                vol.Required(
                    CONF_SENSOR_PV,
                    default=_pv_default,
                ): pv_selector,
                vol.Optional(
                    CONF_PV_HISTORY_DAYS,
                    default=d.get(CONF_PV_HISTORY_DAYS, 30),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                vol.Optional(
                    CONF_RETUNE,
                    default=d.get(CONF_RETUNE, DEFAULT_RETUNE),
                ): bool,
            }
        )

    def _get_reconfigure_schema(
        self,
        *,
        data: dict[str, Any],
        defaults: dict[str, Any] | None = None,
    ) -> vol.Schema:
        """Schema for reconfigure step (weather + PV only)."""
        d = defaults or {}
        energy_ids = _get_energy_sensor_ids(self.hass)
        pv_selector = EntitySelector(
            EntitySelectorConfig(include_entities=energy_ids, multiple=True)
            if energy_ids
            else EntitySelectorConfig(domain="sensor", device_class="energy", multiple=True)
        )
        _pv_default = d.get(CONF_SENSOR_PV, data.get(CONF_SENSOR_PV, []))
        if isinstance(_pv_default, str):
            _pv_default = [_pv_default] if _pv_default else []
        return vol.Schema(
            {
                vol.Required(
                    CONF_WEATHER_ENTITY,
                    default=d.get(CONF_WEATHER_ENTITY, data.get(CONF_WEATHER_ENTITY, "")),
                ): EntitySelector(EntitySelectorConfig(domain="weather", multiple=False)),
                vol.Required(CONF_SENSOR_PV, default=_pv_default): pv_selector,
                vol.Optional(
                    CONF_PV_HISTORY_DAYS,
                    default=d.get(CONF_PV_HISTORY_DAYS, data.get(CONF_PV_HISTORY_DAYS, 30)),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                vol.Optional(
                    CONF_RETUNE,
                    default=d.get(
                        CONF_RETUNE,
                        _resolve_retune_enabled(None, data, default_if_missing=False),
                    ),
                ): bool,
            }
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Allow the user to reconfigure an existing entry without removing it."""
        entry = self._get_reconfigure_entry()
        data = dict(entry.data)
        errors: dict[str, str] = {}
        prefix = data.get(CONF_SENSOR_PREFIX, DEFAULT_SENSOR_PREFIX)

        if user_input is not None:
            weather_entity = (user_input.get(CONF_WEATHER_ENTITY) or "").strip()
            sensor_pv_raw = user_input.get(CONF_SENSOR_PV) or []
            sensor_pv_list = [sensor_pv_raw] if isinstance(sensor_pv_raw, str) and sensor_pv_raw else (sensor_pv_raw if isinstance(sensor_pv_raw, list) else [])

            if not weather_entity:
                errors["base"] = "weather_entity_required"
            elif not weather_entity.startswith("weather."):
                errors[CONF_WEATHER_ENTITY] = "must_be_weather_entity"
            if not sensor_pv_list:
                errors["base"] = "sensor_pv_required"
            else:
                for _pv in sensor_pv_list:
                    if not _pv.startswith("sensor."):
                        errors[CONF_SENSOR_PV] = "must_be_sensor_entity"
                        break

            if not errors.get(CONF_SENSOR_PV) and sensor_pv_list:
                for _pv in sensor_pv_list:
                    pv_state = self.hass.states.get(_pv)
                    if pv_state:
                        pv_unit = pv_state.attributes.get("unit_of_measurement", "")
                        if pv_unit and pv_unit not in ("kWh", "Wh"):
                            errors[CONF_SENSOR_PV] = "sensor_pv_wrong_unit"
                            break

            if errors:
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=self._get_reconfigure_schema(data=data, defaults=user_input),
                    errors=errors,
                )

            history_days = user_input.get(CONF_PV_HISTORY_DAYS, data.get(CONF_PV_HISTORY_DAYS, 30))
            supports_forecasts = await _check_weather_supports_forecasts(self.hass, weather_entity)
            if not supports_forecasts:
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=self._get_reconfigure_schema(data=data, defaults=user_input),
                    errors={"base": "no_forecast_support"},
                )

            has_cloud = await _check_weather_has_cloud_forecast(self.hass, weather_entity)
            if not has_cloud:
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=self._get_reconfigure_schema(data=data, defaults=user_input),
                    errors={"base": "no_cloud_forecast"},
                )

            data.update({
                CONF_WEATHER_ENTITY: weather_entity,
                CONF_SENSOR_PV: sensor_pv_list,
                CONF_PV_HISTORY_DAYS: history_days,
                CONF_RETUNE: bool(
                    user_input.get(
                        CONF_RETUNE,
                        _resolve_retune_enabled(None, data, default_if_missing=False),
                    )
                ),
            })
            _apply_auto_sensors(data, prefix)
            data.pop("retune_mode", None)
            data["sql_query"] = _build_sql_query(
                prefix=prefix,
                sensor_pv_list=sensor_pv_list,
                sensor_forecast=data.get(CONF_SENSOR_FORECAST, f"sensor.{prefix}_weather_forecast"),
                history_days=history_days,
                weather_entity=weather_entity,
            )
            return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._get_reconfigure_schema(data=data),
            errors=errors,
        )



class OptionsFlow(config_entries.OptionsFlow):
    """Handle options flow."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        options = self.config_entry.options or {}

        if user_input is not None:
            prefix = self.config_entry.data.get(CONF_SENSOR_PREFIX, DEFAULT_SENSOR_PREFIX)
            data = self.config_entry.data

            weather_entity = (user_input.get(CONF_WEATHER_ENTITY) or "").strip() or data.get(CONF_WEATHER_ENTITY, "")

            sensor_pv_raw = user_input.get(CONF_SENSOR_PV) or data.get(CONF_SENSOR_PV) or []
            sensor_pv_list = [sensor_pv_raw] if isinstance(sensor_pv_raw, str) and sensor_pv_raw else (sensor_pv_raw if isinstance(sensor_pv_raw, list) else [])
            if not sensor_pv_list:
                # Fall back to stored data
                _stored = data.get(CONF_SENSOR_PV, [])
                sensor_pv_list = [_stored] if isinstance(_stored, str) and _stored else (_stored if isinstance(_stored, list) else [])

            # Validate PV sensor units
            if not sensor_pv_list:
                errors[CONF_SENSOR_PV] = "sensor_pv_required"
            else:
                for _pv in sensor_pv_list:
                    pv_state = self.hass.states.get(_pv)
                    if pv_state:
                        pv_unit = pv_state.attributes.get("unit_of_measurement", "")
                        if pv_unit and pv_unit not in ("kWh", "Wh"):
                            errors[CONF_SENSOR_PV] = "sensor_pv_wrong_unit"
                            break

            if not errors:
                sensor_forecast = data.get(CONF_SENSOR_FORECAST, f"sensor.{prefix}_weather_forecast")
                history_days = user_input.get(CONF_PV_HISTORY_DAYS, data.get(CONF_PV_HISTORY_DAYS, 30))

                supports_forecasts = await _check_weather_supports_forecasts(self.hass, weather_entity)
                if not supports_forecasts:
                    return self.async_show_form(
                        step_id="init",
                        data_schema=self._get_options_schema(data, options, defaults=user_input),
                        errors={"base": "no_forecast_support"},
                    )

                has_cloud = await _check_weather_has_cloud_forecast(self.hass, weather_entity)
                if not has_cloud:
                    return self.async_show_form(
                        step_id="init",
                        data_schema=self._get_options_schema(data, options, defaults=user_input),
                        errors={"base": "no_cloud_forecast"},
                    )

                user_input[CONF_WEATHER_ENTITY] = weather_entity
                _apply_auto_sensors(user_input, prefix)
                user_input[CONF_SENSOR_PV] = sensor_pv_list
                user_input[CONF_RETUNE] = bool(
                    user_input.get(
                        CONF_RETUNE,
                        _resolve_retune_enabled(options, data, default_if_missing=False),
                    )
                )
                user_input.pop("retune_mode", None)
                user_input["sql_query"] = _build_sql_query(
                    prefix=prefix,
                    sensor_pv_list=sensor_pv_list,
                    sensor_forecast=sensor_forecast,
                    history_days=history_days,
                    weather_entity=weather_entity,
                )

                return self.async_create_entry(title="", data=user_input)

        data = self.config_entry.data

        return self.async_show_form(
            step_id="init",
            data_schema=self._get_options_schema(data, options),
            errors=errors,
        )

    def _get_options_schema(
        self,
        data: dict[str, Any],
        options: dict[str, Any],
        *,
        defaults: dict[str, Any] | None = None,
    ) -> vol.Schema:
        """Schema for options step (weather + PV only)."""
        d = defaults or {}

        def _opt(key: str, fallback: Any = None) -> Any:
            val = options.get(key, None)
            if val is None:
                return data.get(key, fallback)
            return val

        energy_ids = _get_energy_sensor_ids(self.hass)
        pv_selector = EntitySelector(
            EntitySelectorConfig(include_entities=energy_ids, multiple=True)
            if energy_ids
            else EntitySelectorConfig(domain="sensor", device_class="energy", multiple=True)
        )
        _pv_default = d.get(CONF_SENSOR_PV, _opt(CONF_SENSOR_PV, []))
        if isinstance(_pv_default, str):
            _pv_default = [_pv_default] if _pv_default else []
        return vol.Schema(
            {
                vol.Required(
                    CONF_WEATHER_ENTITY,
                    default=d.get(CONF_WEATHER_ENTITY, _opt(CONF_WEATHER_ENTITY, "")),
                ): EntitySelector(EntitySelectorConfig(domain="weather", multiple=False)),
                vol.Required(CONF_SENSOR_PV, default=_pv_default): pv_selector,
                vol.Optional(
                    CONF_PV_HISTORY_DAYS,
                    default=d.get(CONF_PV_HISTORY_DAYS, _opt(CONF_PV_HISTORY_DAYS, 30)),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                vol.Optional(
                    CONF_PV_MAX_RECORD,
                    default=d.get(CONF_PV_MAX_RECORD, _opt(CONF_PV_MAX_RECORD, DEFAULT_PV_MAX_RECORD)),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_RETUNE,
                    default=d.get(
                        CONF_RETUNE,
                        _resolve_retune_enabled(options, data, default_if_missing=False),
                    ),
                ): bool,
            }
        )
