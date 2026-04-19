"""Config flow for HA SQL PV Forecast."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import (
    CONF_DB_URL,
    CONF_SENSOR_PREFIX,
    CONF_PV_HISTORY_DAYS,
    CONF_SENSOR_CLOUDS,
    CONF_SENSOR_FORECAST,
    CONF_SENSOR_PV,
    CONF_SENSOR_UV,
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

_LOGGER = logging.getLogger(__name__)

MIN_HISTORY_DAYS = 10


def _count_entity_history_days(db_url: str, entity_id: str) -> int:
    """Count distinct days with non-empty states for entity_id in the last 30 days.

    Runs in an executor (blocking SQLAlchemy call).
    Returns 0 on any error so the caller can decide whether to warn.
    """
    try:
        from sqlalchemy import create_engine, text as sa_text  # noqa: PLC0415
        engine = create_engine(db_url, echo=False)
        with engine.connect() as conn:
            row = conn.execute(
                sa_text(
                    "SELECT COUNT(DISTINCT date(s.last_updated_ts, 'unixepoch')) "
                    "FROM states s "
                    "JOIN states_meta m ON s.metadata_id = m.metadata_id "
                    "WHERE m.entity_id = :eid "
                    "AND s.last_updated_ts > strftime('%s', 'now', '-30 days') "
                    "AND s.state NOT IN ('unknown', 'unavailable', '')"
                ),
                {"eid": entity_id},
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# Warning helpers
# ---------------------------------------------------------------------------

_WARNINGS: dict[str, dict[str, str]] = {
    "low_history": {
        "de": (
            "⚠️ **{entity}** hat nur **{days} Tag(e)** Cloud-Verlauf "
            "(empfohlen: ≥{min_days}). "
            "Prognosen sind bis zum Aufbau eines ausreichenden Verlaufs ungenau. "
            "Zum Bestätigen erneut absenden."
        ),
        "en": (
            "⚠️ **{entity}** has only **{days} day(s)** of cloud history "
            "(recommended: ≥{min_days}). "
            "Forecasts will be inaccurate until sufficient cloud history is built up. "
            "Submit again to confirm."
        ),
    },
}


def _format_warning(hass, key: str, **kwargs: object) -> str:
    """Return a language-aware warning message for *key*."""
    lang = getattr(hass.config, "language", "en")[:2].lower()
    tmpl = _WARNINGS.get(key, {}).get(lang) or _WARNINGS.get(key, {}).get("en", "")
    return tmpl.format(**kwargs)


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


def _get_percent_sensor_ids(hass) -> list[str]:
    """Return sensor entity_ids whose unit_of_measurement is '%' and that have a current value."""
    return [
        s.entity_id
        for s in hass.states.async_all("sensor")
        if s.attributes.get("unit_of_measurement") == "%"
        and s.state not in ("unknown", "unavailable", "")
    ]


def _get_uv_sensor_ids(hass) -> list[str]:
    """Return sensor entity_ids that look like UV index sensors."""
    uv_units = {"UV index", "UVI", "uv"}
    result = []
    for s in hass.states.async_all("sensor"):
        unit = (s.attributes.get("unit_of_measurement") or "").lower()
        name = s.entity_id.lower()
        if unit in {u.lower() for u in uv_units} or "uv" in name:
            if s.state not in ("unknown", "unavailable", ""):
                result.append(s.entity_id)
    return result


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
            # Validiere die eingegebenen Entity IDs
            weather_entity = user_input.get(CONF_WEATHER_ENTITY, "").strip()
            sensor_pv_raw = user_input.get(CONF_SENSOR_PV) or []
            sensor_pv_list = [sensor_pv_raw] if isinstance(sensor_pv_raw, str) and sensor_pv_raw else (sensor_pv_raw if isinstance(sensor_pv_raw, list) else [])
            sensor_clouds = (user_input.get(CONF_SENSOR_CLOUDS) or "").strip()
            sensor_uv = (user_input.get(CONF_SENSOR_UV) or "").strip()

            # Validiere Weather Entity
            if not weather_entity:
                errors["base"] = "weather_entity_required"
            elif not weather_entity.startswith("weather."):
                errors[CONF_WEATHER_ENTITY] = "must_be_weather_entity"

            # Validiere PV Sensoren (mind. 1, alle müssen sensor.* sein)
            if not sensor_pv_list:
                errors["base"] = "sensor_pv_required"
            else:
                for _pv in sensor_pv_list:
                    if not _pv.startswith("sensor."):
                        errors[CONF_SENSOR_PV] = "must_be_sensor_entity"
                        break

            # Validiere Cloud Sensor wenn angegeben
            if sensor_clouds and not sensor_clouds.startswith("sensor."):
                errors[CONF_SENSOR_CLOUDS] = "must_be_sensor_entity"

            # Validiere UV Sensor wenn angegeben
            if sensor_uv and not sensor_uv.startswith("sensor."):
                errors[CONF_SENSOR_UV] = "must_be_sensor_entity"

            # Validiere PV Sensor Einheiten (kWh oder Wh für jeden Sensor)
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
                    description_placeholders={"weather_history_warning": ""},
                )

            # ---- get_forecasts support check: blocking ----
            supports_forecasts = await _check_weather_supports_forecasts(self.hass, weather_entity)
            if not supports_forecasts:
                return self.async_show_form(
                    step_id="sensors",
                    data_schema=self._get_sensors_schema(defaults=user_input),
                    errors={"base": "no_forecast_support"},
                    description_placeholders={"weather_history_warning": ""},
                )

            # ---- Cloud-coverage check: blocking — cannot function without it ----
            has_cloud = (
                await _check_weather_has_cloud_forecast(self.hass, weather_entity)
                if not sensor_clouds
                else True
            )
            if not has_cloud:
                return self.async_show_form(
                    step_id="sensors",
                    data_schema=self._get_sensors_schema(defaults=user_input),
                    errors={"base": "no_cloud_forecast"},
                    description_placeholders={"weather_history_warning": ""},
                )

            # ---- History warning (non-blocking) ----
            db_url = self.data_cache.get(CONF_DB_URL) or "sqlite:////config/home-assistant_v2.db"
            check_entity = sensor_clouds if sensor_clouds else weather_entity
            days = await self.hass.async_add_executor_job(
                _count_entity_history_days, db_url, check_entity
            )

            warn_parts: list[str] = []
            if days < MIN_HISTORY_DAYS:
                warn_parts.append(
                    _format_warning(
                        self.hass, "low_history",
                        entity=check_entity, days=days, min_days=MIN_HISTORY_DAYS,
                    )
                )

            if warn_parts and not getattr(self, "_history_warning_confirmed", False):
                self._history_warning_confirmed = True
                return self.async_show_form(
                    step_id="sensors",
                    data_schema=self._get_sensors_schema(defaults=user_input),
                    errors={},
                    description_placeholders={"weather_history_warning": "\n\n".join(warn_parts)},
                )
            self._history_warning_confirmed = False
            # -----------------------------------------------------------

            # Merge mit den vorherigen Daten
            data = {**self.data_cache, **user_input}
            
            # Sensor-Namen aus Präfix ableiten
            prefix = data[CONF_SENSOR_PREFIX]
            data[CONF_SENSOR_FORECAST] = f"sensor.{prefix}_weather_forecast"

            # Wenn kein externer Cloud-Sensor gewählt: Auto-Sensor anlegen.
            if not data.get(CONF_SENSOR_CLOUDS):
                clouds_sensor = f"sensor.{prefix}_cloud_coverage"
                data[CONF_SENSOR_CLOUDS] = clouds_sensor
            else:
                clouds_sensor = data[CONF_SENSOR_CLOUDS]

            # Wenn kein UV-Sensor gewählt: Auto-Sensor anlegen.
            if not data.get(CONF_SENSOR_UV):
                uv_sensor = f"sensor.{prefix}_uv"
                data[CONF_SENSOR_UV] = uv_sensor
            else:
                uv_sensor = data[CONF_SENSOR_UV]

            # Erstelle die SQL Query mit den Sensoren
            history_days = data.get(CONF_PV_HISTORY_DAYS, 30)
            _pv_entries = data[CONF_SENSOR_PV]
            if isinstance(_pv_entries, str):
                _pv_entries = [_pv_entries] if _pv_entries else []
            sensor_pv_sql_list = ", ".join(f"'{s}'" for s in _pv_entries)
            sensor_pv_first = _pv_entries[0] if _pv_entries else ""
            sql_query = DEFAULT_SQL_QUERY.format(
                sensor_clouds=clouds_sensor,
                sensor_pv_list=sensor_pv_sql_list,
                sensor_pv_first=sensor_pv_first,
                sensor_forecast=data[CONF_SENSOR_FORECAST],
                sensor_uv=uv_sensor,
                history_days=history_days,
                weather_entity=data[CONF_WEATHER_ENTITY],
            )
            
            data["sql_query"] = sql_query
            data[CONF_VALUE_TEMPLATE] = DEFAULT_VALUE_TEMPLATE
            data[CONF_UNIT_OF_MEASUREMENT] = DEFAULT_UNIT_OF_MEASUREMENT
            data[CONF_DEVICE_CLASS] = DEFAULT_DEVICE_CLASS
            data[CONF_STATE_CLASS] = DEFAULT_STATE_CLASS
            
            await self.async_set_unique_id(prefix)
            self._abort_if_unique_id_configured()
            
            return self.async_create_entry(
                title=prefix, data=data
            )

        return self.async_show_form(
            step_id="sensors",
            data_schema=self._get_sensors_schema(),
            errors=errors,
            description_placeholders={"weather_history_warning": ""},
        )
    
    def _get_sensors_schema(self, defaults: dict[str, Any] | None = None) -> vol.Schema:
        """Get the sensors configuration schema with pre-filtered entity selectors.

        Pass *defaults* (e.g. the current ``user_input``) to keep field values when
        re-displaying the form after a warning or validation error.
        """
        d = defaults or {}
        percent_ids = _get_percent_sensor_ids(self.hass)
        energy_ids = _get_energy_sensor_ids(self.hass)
        uv_ids = _get_uv_sensor_ids(self.hass)
        cloud_selector = EntitySelector(
            EntitySelectorConfig(include_entities=percent_ids, multiple=False)
            if percent_ids
            else EntitySelectorConfig(domain="sensor", multiple=False)
        )
        pv_selector = EntitySelector(
            EntitySelectorConfig(include_entities=energy_ids, multiple=True)
            if energy_ids
            else EntitySelectorConfig(domain="sensor", device_class="energy", multiple=True)
        )
        uv_selector = EntitySelector(
            EntitySelectorConfig(include_entities=uv_ids, multiple=False)
            if uv_ids
            else EntitySelectorConfig(domain="sensor", multiple=False)
        )
        cloud_key = (
            vol.Optional(CONF_SENSOR_CLOUDS, default=d[CONF_SENSOR_CLOUDS])
            if d.get(CONF_SENSOR_CLOUDS)
            else vol.Optional(CONF_SENSOR_CLOUDS)
        )
        uv_key = (
            vol.Optional(CONF_SENSOR_UV, default=d[CONF_SENSOR_UV])
            if d.get(CONF_SENSOR_UV)
            else vol.Optional(CONF_SENSOR_UV)
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
                cloud_key: cloud_selector,
                uv_key: uv_selector,
                vol.Optional(
                    CONF_PV_HISTORY_DAYS,
                    default=d.get(CONF_PV_HISTORY_DAYS, 30),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                vol.Required(
                    CONF_SENSOR_PV,
                    default=_pv_default,
                ): pv_selector,
                # CONF_SENSOR_FORECAST wird automatisch gesetzt
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
        auto_cloud = f"sensor.{prefix}_cloud_coverage"
        current_cloud = data.get(CONF_SENSOR_CLOUDS, "")
        # show empty in the field when the auto-sensor is active
        display_cloud = current_cloud if current_cloud and current_cloud != auto_cloud else None

        if user_input is not None:
            weather_entity = (user_input.get(CONF_WEATHER_ENTITY) or "").strip()
            sensor_pv_raw = user_input.get(CONF_SENSOR_PV) or []
            sensor_pv_list = [sensor_pv_raw] if isinstance(sensor_pv_raw, str) and sensor_pv_raw else (sensor_pv_raw if isinstance(sensor_pv_raw, list) else [])
            sensor_clouds = (user_input.get(CONF_SENSOR_CLOUDS) or "").strip()
            sensor_uv = (user_input.get(CONF_SENSOR_UV) or "").strip()

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
            if sensor_clouds and not sensor_clouds.startswith("sensor."):
                errors[CONF_SENSOR_CLOUDS] = "must_be_sensor_entity"
            if sensor_uv and not sensor_uv.startswith("sensor."):
                errors[CONF_SENSOR_UV] = "must_be_sensor_entity"

            # Validate PV sensor units (kWh or Wh for each sensor)
            if not errors.get(CONF_SENSOR_PV) and sensor_pv_list:
                for _pv in sensor_pv_list:
                    pv_state = self.hass.states.get(_pv)
                    if pv_state:
                        pv_unit = pv_state.attributes.get("unit_of_measurement", "")
                        if pv_unit and pv_unit not in ("kWh", "Wh"):
                            errors[CONF_SENSOR_PV] = "sensor_pv_wrong_unit"
                            break

            if not errors:
                if not sensor_clouds:
                    sensor_clouds = auto_cloud
                if not sensor_uv:
                    sensor_uv = f"sensor.{prefix}_uv"
                history_days = user_input.get(CONF_PV_HISTORY_DAYS, data.get(CONF_PV_HISTORY_DAYS, 30))

                # ---- get_forecasts support check: blocking ----
                supports_forecasts = await _check_weather_supports_forecasts(self.hass, weather_entity)
                if not supports_forecasts:
                    cloud_val = sensor_clouds if sensor_clouds != auto_cloud else None
                    uv_val = sensor_uv if sensor_uv != f"sensor.{prefix}_uv" else None
                    cloud_err_key = (
                        vol.Optional(CONF_SENSOR_CLOUDS, default=cloud_val)
                        if cloud_val is not None
                        else vol.Optional(CONF_SENSOR_CLOUDS)
                    )
                    uv_err_key = (
                        vol.Optional(CONF_SENSOR_UV, default=uv_val)
                        if uv_val is not None
                        else vol.Optional(CONF_SENSOR_UV)
                    )
                    return self.async_show_form(
                        step_id="reconfigure",
                        data_schema=vol.Schema({
                            vol.Required(CONF_WEATHER_ENTITY, default=weather_entity):
                                EntitySelector(EntitySelectorConfig(domain="weather", multiple=False)),
                            cloud_err_key: EntitySelector(EntitySelectorConfig(domain="sensor", multiple=False)),
                            uv_err_key: EntitySelector(EntitySelectorConfig(domain="sensor", multiple=False)),
                            vol.Optional(CONF_PV_HISTORY_DAYS, default=history_days):
                                vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                            vol.Required(CONF_SENSOR_PV, default=sensor_pv_list):
                                EntitySelector(EntitySelectorConfig(domain="sensor", multiple=True)),
                        }),
                        errors={"base": "no_forecast_support"},
                        description_placeholders={"weather_history_warning": ""},
                    )

                # ---- Cloud-coverage check: blocking ----
                has_cloud = (
                    await _check_weather_has_cloud_forecast(self.hass, weather_entity)
                    if sensor_clouds == auto_cloud
                    else True
                )
                if not has_cloud:
                    cloud_val = sensor_clouds if sensor_clouds != auto_cloud else None
                    uv_val = sensor_uv if sensor_uv != f"sensor.{prefix}_uv" else None
                    cloud_err_key = (
                        vol.Optional(CONF_SENSOR_CLOUDS, default=cloud_val)
                        if cloud_val is not None
                        else vol.Optional(CONF_SENSOR_CLOUDS)
                    )
                    uv_err_key = (
                        vol.Optional(CONF_SENSOR_UV, default=uv_val)
                        if uv_val is not None
                        else vol.Optional(CONF_SENSOR_UV)
                    )
                    return self.async_show_form(
                        step_id="reconfigure",
                        data_schema=vol.Schema({
                            vol.Required(CONF_WEATHER_ENTITY, default=weather_entity):
                                EntitySelector(EntitySelectorConfig(domain="weather", multiple=False)),
                            cloud_err_key: EntitySelector(EntitySelectorConfig(domain="sensor", multiple=False)),
                            uv_err_key: EntitySelector(EntitySelectorConfig(domain="sensor", multiple=False)),
                            vol.Optional(CONF_PV_HISTORY_DAYS, default=history_days):
                                vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                            vol.Required(CONF_SENSOR_PV, default=sensor_pv_list):
                                EntitySelector(EntitySelectorConfig(domain="sensor", multiple=True)),
                        }),
                        errors={"base": "no_cloud_forecast"},
                        description_placeholders={"weather_history_warning": ""},
                    )

                # ---- History warning (non-blocking) ----
                db_url = data.get(CONF_DB_URL) or "sqlite:////config/home-assistant_v2.db"
                check_entity = sensor_clouds if sensor_clouds != auto_cloud else weather_entity
                days = await self.hass.async_add_executor_job(
                    _count_entity_history_days, db_url, check_entity
                )

                warn_parts: list[str] = []
                if days < MIN_HISTORY_DAYS:
                    warn_parts.append(
                        _format_warning(
                            self.hass, "low_history",
                            entity=check_entity, days=days, min_days=MIN_HISTORY_DAYS,
                        )
                    )

                if warn_parts and not getattr(self, "_reconf_history_warning_confirmed", False):
                    self._reconf_history_warning_confirmed = True
                    # Build inline schema preserving the user's selections
                    cloud_val = sensor_clouds if sensor_clouds != auto_cloud else None
                    uv_val = sensor_uv if sensor_uv != f"sensor.{prefix}_uv" else None
                    cloud_warn_key = (
                        vol.Optional(CONF_SENSOR_CLOUDS, default=cloud_val)
                        if cloud_val is not None
                        else vol.Optional(CONF_SENSOR_CLOUDS)
                    )
                    uv_warn_key = (
                        vol.Optional(CONF_SENSOR_UV, default=uv_val)
                        if uv_val is not None
                        else vol.Optional(CONF_SENSOR_UV)
                    )
                    return self.async_show_form(
                        step_id="reconfigure",
                        data_schema=vol.Schema({
                            vol.Required(CONF_WEATHER_ENTITY, default=weather_entity):
                                EntitySelector(EntitySelectorConfig(domain="weather", multiple=False)),
                            cloud_warn_key: EntitySelector(EntitySelectorConfig(domain="sensor", multiple=False)),
                            uv_warn_key: EntitySelector(EntitySelectorConfig(domain="sensor", multiple=False)),
                            vol.Optional(CONF_PV_HISTORY_DAYS, default=history_days):
                                vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                            vol.Required(CONF_SENSOR_PV, default=sensor_pv_list):
                                EntitySelector(EntitySelectorConfig(domain="sensor", multiple=True)),
                        }),
                        errors={},
                        description_placeholders={"weather_history_warning": "\n\n".join(warn_parts)},
                    )
                self._reconf_history_warning_confirmed = False
                # -----------------------------------------------------------

                data.update({
                    CONF_WEATHER_ENTITY: weather_entity,
                    CONF_SENSOR_PV: sensor_pv_list,
                    CONF_SENSOR_CLOUDS: sensor_clouds,
                    CONF_SENSOR_UV: sensor_uv,
                    CONF_PV_HISTORY_DAYS: history_days,
                })
                return self.async_update_reload_and_abort(entry, data=data)

        percent_ids = _get_percent_sensor_ids(self.hass)
        energy_ids = _get_energy_sensor_ids(self.hass)
        uv_ids = _get_uv_sensor_ids(self.hass)
        cloud_selector = EntitySelector(
            EntitySelectorConfig(include_entities=percent_ids, multiple=False)
            if percent_ids
            else EntitySelectorConfig(domain="sensor", multiple=False)
        )
        pv_selector = EntitySelector(
            EntitySelectorConfig(include_entities=energy_ids, multiple=True)
            if energy_ids
            else EntitySelectorConfig(domain="sensor", device_class="energy", multiple=True)
        )
        uv_selector = EntitySelector(
            EntitySelectorConfig(include_entities=uv_ids, multiple=False)
            if uv_ids
            else EntitySelectorConfig(domain="sensor", multiple=False)
        )
        auto_uv = f"sensor.{prefix}_uv"
        current_uv = data.get(CONF_SENSOR_UV, "")
        display_uv = current_uv if current_uv and current_uv != auto_uv else None
        cloud_field_key = (
            vol.Optional(CONF_SENSOR_CLOUDS, default=display_cloud)
            if display_cloud is not None
            else vol.Optional(CONF_SENSOR_CLOUDS)
        )
        uv_field_key = (
            vol.Optional(CONF_SENSOR_UV, default=display_uv)
            if display_uv is not None
            else vol.Optional(CONF_SENSOR_UV)
        )
        _pv_default = data.get(CONF_SENSOR_PV, [])
        if isinstance(_pv_default, str):
            _pv_default = [_pv_default] if _pv_default else []
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({
                vol.Required(CONF_WEATHER_ENTITY, default=data.get(CONF_WEATHER_ENTITY, "")):
                    EntitySelector(EntitySelectorConfig(domain="weather", multiple=False)),
                cloud_field_key: cloud_selector,
                uv_field_key: uv_selector,
                vol.Optional(CONF_PV_HISTORY_DAYS, default=data.get(CONF_PV_HISTORY_DAYS, 30)):
                    vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                vol.Required(CONF_SENSOR_PV, default=_pv_default):
                    pv_selector,
            }),
            errors=errors,
            description_placeholders={"weather_history_warning": ""},
        )



class OptionsFlow(config_entries.OptionsFlow):
    """Handle options flow."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            prefix = self.config_entry.data.get(CONF_SENSOR_PREFIX, DEFAULT_SENSOR_PREFIX)
            data = self.config_entry.data

            # Resolve weather entity (new value wins over stored data)
            weather_entity = (user_input.get(CONF_WEATHER_ENTITY) or "").strip() or data.get(CONF_WEATHER_ENTITY, "")

            # Resolve cloud sensor — empty means keep/create the auto-sensor
            sensor_clouds = (user_input.get(CONF_SENSOR_CLOUDS) or "").strip()
            if not sensor_clouds:
                sensor_clouds = f"sensor.{prefix}_cloud_coverage"

            # Resolve UV sensor — empty means keep/create the auto-sensor
            sensor_uv = (user_input.get(CONF_SENSOR_UV) or "").strip()
            if not sensor_uv:
                sensor_uv = f"sensor.{prefix}_uv"

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

                # ---- get_forecasts support check: blocking ----
                auto_cloud = f"sensor.{prefix}_cloud_coverage"
                supports_forecasts = await _check_weather_supports_forecasts(self.hass, weather_entity)
                if not supports_forecasts:
                    pids = _get_percent_sensor_ids(self.hass)
                    eids = _get_energy_sensor_ids(self.hass)
                    uids = _get_uv_sensor_ids(self.hass)
                    return self.async_show_form(
                        step_id="init",
                        data_schema=vol.Schema({
                            vol.Required(CONF_WEATHER_ENTITY, default=weather_entity):
                                EntitySelector(EntitySelectorConfig(domain="weather", multiple=False)),
                            vol.Optional(CONF_SENSOR_CLOUDS):
                                EntitySelector(EntitySelectorConfig(include_entities=pids, multiple=False) if pids else EntitySelectorConfig(domain="sensor", multiple=False)),
                            vol.Optional(CONF_SENSOR_UV):
                                EntitySelector(EntitySelectorConfig(include_entities=uids, multiple=False) if uids else EntitySelectorConfig(domain="sensor", multiple=False)),
                            vol.Optional(CONF_PV_HISTORY_DAYS, default=history_days):
                                vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                            vol.Required(CONF_SENSOR_PV, default=sensor_pv_list):
                                EntitySelector(EntitySelectorConfig(include_entities=eids, multiple=True) if eids else EntitySelectorConfig(domain="sensor", multiple=True)),
                        }),
                        errors={"base": "no_forecast_support"},
                        description_placeholders={"weather_history_warning": ""},
                    )

                # ---- Cloud-coverage check: blocking ----
                has_cloud = (
                    await _check_weather_has_cloud_forecast(self.hass, weather_entity)
                    if sensor_clouds == auto_cloud
                    else True
                )
                if not has_cloud:
                    pids = _get_percent_sensor_ids(self.hass)
                    eids = _get_energy_sensor_ids(self.hass)
                    uids = _get_uv_sensor_ids(self.hass)
                    cloud_err_val = sensor_clouds if sensor_clouds != auto_cloud else None
                    uv_err_val = sensor_uv if sensor_uv != f"sensor.{prefix}_uv" else None
                    cloud_err_key = (
                        vol.Optional(CONF_SENSOR_CLOUDS, default=cloud_err_val)
                        if cloud_err_val is not None
                        else vol.Optional(CONF_SENSOR_CLOUDS)
                    )
                    uv_err_key = (
                        vol.Optional(CONF_SENSOR_UV, default=uv_err_val)
                        if uv_err_val is not None
                        else vol.Optional(CONF_SENSOR_UV)
                    )
                    err_schema = vol.Schema({
                        vol.Required(CONF_WEATHER_ENTITY, default=weather_entity):
                            EntitySelector(EntitySelectorConfig(domain="weather", multiple=False)),
                        cloud_err_key:
                            EntitySelector(EntitySelectorConfig(include_entities=pids, multiple=False) if pids else EntitySelectorConfig(domain="sensor", multiple=False)),
                        uv_err_key:
                            EntitySelector(EntitySelectorConfig(include_entities=uids, multiple=False) if uids else EntitySelectorConfig(domain="sensor", multiple=False)),
                        vol.Optional(CONF_PV_HISTORY_DAYS, default=history_days):
                            vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                        vol.Required(CONF_SENSOR_PV, default=sensor_pv_list):
                            EntitySelector(EntitySelectorConfig(include_entities=eids, multiple=True) if eids else EntitySelectorConfig(domain="sensor", multiple=True)),
                    })
                    return self.async_show_form(
                        step_id="init",
                        data_schema=err_schema,
                        errors={"base": "no_cloud_forecast"},
                        description_placeholders={"weather_history_warning": ""},
                    )

                # ---- History warning (non-blocking) ----
                db_url = data.get(CONF_DB_URL) or "sqlite:////config/home-assistant_v2.db"
                check_entity = sensor_clouds if sensor_clouds != auto_cloud else weather_entity
                days = await self.hass.async_add_executor_job(
                    _count_entity_history_days, db_url, check_entity
                )

                warn_parts: list[str] = []
                if days < MIN_HISTORY_DAYS:
                    warn_parts.append(
                        _format_warning(
                            self.hass, "low_history",
                            entity=check_entity, days=days, min_days=MIN_HISTORY_DAYS,
                        )
                    )

                if warn_parts and not getattr(self, "_opts_history_warning_confirmed", False):
                    self._opts_history_warning_confirmed = True
                    pids = _get_percent_sensor_ids(self.hass)
                    eids = _get_energy_sensor_ids(self.hass)
                    uids = _get_uv_sensor_ids(self.hass)
                    cloud_warn_val = sensor_clouds if sensor_clouds != auto_cloud else None
                    uv_warn_val = sensor_uv if sensor_uv != f"sensor.{prefix}_uv" else None
                    cloud_warn_key = (
                        vol.Optional(CONF_SENSOR_CLOUDS, default=cloud_warn_val)
                        if cloud_warn_val is not None
                        else vol.Optional(CONF_SENSOR_CLOUDS)
                    )
                    uv_warn_key = (
                        vol.Optional(CONF_SENSOR_UV, default=uv_warn_val)
                        if uv_warn_val is not None
                        else vol.Optional(CONF_SENSOR_UV)
                    )
                    warn_schema = vol.Schema({
                        vol.Required(CONF_WEATHER_ENTITY, default=weather_entity):
                            EntitySelector(EntitySelectorConfig(domain="weather", multiple=False)),
                        cloud_warn_key:
                            EntitySelector(EntitySelectorConfig(include_entities=pids, multiple=False) if pids else EntitySelectorConfig(domain="sensor", multiple=False)),
                        uv_warn_key:
                            EntitySelector(EntitySelectorConfig(include_entities=uids, multiple=False) if uids else EntitySelectorConfig(domain="sensor", multiple=False)),
                        vol.Optional(CONF_PV_HISTORY_DAYS, default=history_days):
                            vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                        vol.Required(CONF_SENSOR_PV, default=sensor_pv_list):
                            EntitySelector(EntitySelectorConfig(include_entities=eids, multiple=True) if eids else EntitySelectorConfig(domain="sensor", multiple=True)),
                    })
                    return self.async_show_form(
                        step_id="init",
                        data_schema=warn_schema,
                        errors={},
                        description_placeholders={"weather_history_warning": "\n\n".join(warn_parts)},
                    )
                self._opts_history_warning_confirmed = False
                # -----------------------------------------------------------

                # Always store new weather entity and regenerate SQL
                user_input[CONF_WEATHER_ENTITY] = weather_entity
                user_input[CONF_SENSOR_CLOUDS] = sensor_clouds
                user_input[CONF_SENSOR_UV] = sensor_uv
                user_input[CONF_SENSOR_PV] = sensor_pv_list
                _pv_sql_list = ", ".join(f"'{s}'" for s in sensor_pv_list)
                _pv_first = sensor_pv_list[0] if sensor_pv_list else ""
                user_input["sql_query"] = DEFAULT_SQL_QUERY.format(
                    sensor_clouds=sensor_clouds,
                    sensor_pv_list=_pv_sql_list,
                    sensor_pv_first=_pv_first,
                    sensor_forecast=sensor_forecast,
                    sensor_uv=sensor_uv,
                    history_days=history_days,
                    weather_entity=weather_entity,
                )

                return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options or {}
        data = self.config_entry.data

        def _opt(key, fallback=None):
            return options.get(key, data.get(key, fallback))

        percent_ids = _get_percent_sensor_ids(self.hass)
        energy_ids = _get_energy_sensor_ids(self.hass)
        uv_ids = _get_uv_sensor_ids(self.hass)
        cloud_selector = EntitySelector(
            EntitySelectorConfig(include_entities=percent_ids, multiple=False)
            if percent_ids
            else EntitySelectorConfig(domain="sensor", multiple=False)
        )
        pv_selector = EntitySelector(
            EntitySelectorConfig(include_entities=energy_ids, multiple=True)
            if energy_ids
            else EntitySelectorConfig(domain="sensor", device_class="energy", multiple=True)
        )
        uv_selector = EntitySelector(
            EntitySelectorConfig(include_entities=uv_ids, multiple=False)
            if uv_ids
            else EntitySelectorConfig(domain="sensor", multiple=False)
        )
        _pv_default = _opt(CONF_SENSOR_PV, [])
        if isinstance(_pv_default, str):
            _pv_default = [_pv_default] if _pv_default else []
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_WEATHER_ENTITY,
                    default=_opt(CONF_WEATHER_ENTITY, ""),
                ): EntitySelector(EntitySelectorConfig(domain="weather", multiple=False)),
                vol.Optional(
                    CONF_SENSOR_CLOUDS,
                    default=_opt(CONF_SENSOR_CLOUDS),
                ): cloud_selector,
                vol.Optional(
                    CONF_SENSOR_UV,
                    default=_opt(CONF_SENSOR_UV),
                ): uv_selector,
                vol.Optional(
                    CONF_PV_HISTORY_DAYS,
                    default=_opt(CONF_PV_HISTORY_DAYS, 30),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                vol.Required(
                    CONF_SENSOR_PV,
                    default=_pv_default,
                ): pv_selector,
                vol.Optional(
                    CONF_PV_MAX_RECORD,
                    default=_opt(CONF_PV_MAX_RECORD, DEFAULT_PV_MAX_RECORD),
                ): vol.Coerce(float),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors, description_placeholders={"weather_history_warning": ""})
