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
    CONF_WEATHER_ENTITY,
    CONF_VALUE_TEMPLATE,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_DEVICE_CLASS,
    CONF_STATE_CLASS,
    DEFAULT_SENSOR_PREFIX,
    DEFAULT_VALUE_TEMPLATE,
    DEFAULT_UNIT_OF_MEASUREMENT,
    DEFAULT_DEVICE_CLASS,
    DEFAULT_STATE_CLASS,
    DEFAULT_SQL_QUERY,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _get_percent_sensor_ids(hass) -> list[str]:
    """Return sensor entity_ids whose unit_of_measurement is '%' and that have a current value."""
    return [
        s.entity_id
        for s in hass.states.async_all("sensor")
        if s.attributes.get("unit_of_measurement") == "%"
        and s.state not in ("unknown", "unavailable", "")
    ]


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
        """Handle the initial step - Database Configuration."""
        errors = {}

        if user_input is not None:
            # Verwende Default DB URL wenn leer
            db_url = user_input.get(CONF_DB_URL, "").strip()
            if not db_url:
                db_url = "sqlite:////config/home-assistant_v2.db"
            user_input[CONF_DB_URL] = db_url
            
            try:
                await self.hass.async_add_executor_job(
                    self._validate_db_url, db_url
                )
            except ValueError as err:
                # SQLite not detected error
                _LOGGER.error("Database URL must be SQLite: %s", err)
                errors["base"] = "sqlite_required"
            except Exception as err:
                _LOGGER.error("Database connection failed: %s", err)
                errors["base"] = "invalid_db_url"
            else:
                # Speichere die DB-URL für den nächsten Schritt
                self.data_cache = user_input
                return await self.async_step_sensors()

        schema = vol.Schema(
            {
                vol.Required(CONF_SENSOR_PREFIX, default=DEFAULT_SENSOR_PREFIX): str,
                vol.Optional(CONF_DB_URL, default=""): str,
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
            sensor_pv = user_input.get(CONF_SENSOR_PV, "").strip()
            sensor_clouds = (user_input.get(CONF_SENSOR_CLOUDS) or "").strip()
            
            # Validiere Weather Entity
            if not weather_entity:
                errors["base"] = "weather_entity_required"
            elif not weather_entity.startswith("weather."):
                errors[CONF_WEATHER_ENTITY] = "must_be_weather_entity"
            
            # Validiere PV Sensor
            if not sensor_pv:
                errors["base"] = "sensor_pv_required"
            elif not sensor_pv.startswith("sensor."):
                errors[CONF_SENSOR_PV] = "must_be_sensor_entity"
            
            # Validiere Cloud Sensor wenn angegeben
            if sensor_clouds and not sensor_clouds.startswith("sensor."):
                errors[CONF_SENSOR_CLOUDS] = "must_be_sensor_entity"

            # Validiere PV Sensor Einheit (kWh oder Wh)
            if not errors.get(CONF_SENSOR_PV) and sensor_pv:
                pv_state = self.hass.states.get(sensor_pv)
                if pv_state:
                    pv_unit = pv_state.attributes.get("unit_of_measurement", "")
                    if pv_unit and pv_unit not in ("kWh", "Wh"):
                        errors[CONF_SENSOR_PV] = "sensor_pv_wrong_unit"

            if errors:
                return self.async_show_form(
                    step_id="sensors",
                    data_schema=self._get_sensors_schema(),
                    errors=errors,
                )
            
            # Merge mit den vorherigen Daten
            data = {**self.data_cache, **user_input}
            
            # Sensor-Namen aus Präfix ableiten
            prefix = data[CONF_SENSOR_PREFIX]
            data[CONF_SENSOR_FORECAST] = f"sensor.{prefix}_weather_forecast"

            # Wenn kein externer Cloud-Sensor gewählt: Auto-Sensor anlegen.
            # Der Auto-Sensor sammelt cloud_coverage von der Wetter-Entity und baut
            # LTS-Statistiken auf. Ab Tag 1 liefert die 3rd UNION der SQL-Query
            # Wetter-Entity-Daten als Fallback.
            if not data.get(CONF_SENSOR_CLOUDS):
                clouds_sensor = f"sensor.{prefix}_cloud_coverage"
                data[CONF_SENSOR_CLOUDS] = clouds_sensor
            else:
                clouds_sensor = data[CONF_SENSOR_CLOUDS]

            # Erstelle die SQL Query mit den Sensoren
            history_days = data.get(CONF_PV_HISTORY_DAYS, 30)
            sql_query = DEFAULT_SQL_QUERY.format(
                sensor_clouds=clouds_sensor,
                sensor_pv=data[CONF_SENSOR_PV],
                sensor_forecast=data[CONF_SENSOR_FORECAST],
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
        )
    
    def _get_sensors_schema(self) -> vol.Schema:
        """Get the sensors configuration schema with pre-filtered entity selectors."""
        percent_ids = _get_percent_sensor_ids(self.hass)
        energy_ids = _get_energy_sensor_ids(self.hass)
        cloud_selector = EntitySelector(
            EntitySelectorConfig(include_entities=percent_ids, multiple=False)
            if percent_ids
            else EntitySelectorConfig(domain="sensor", multiple=False)
        )
        pv_selector = EntitySelector(
            EntitySelectorConfig(include_entities=energy_ids, multiple=False)
            if energy_ids
            else EntitySelectorConfig(domain="sensor", device_class="energy", multiple=False)
        )
        return vol.Schema(
            {
                vol.Required(
                    CONF_WEATHER_ENTITY
                ): EntitySelector(
                    EntitySelectorConfig(
                        domain="weather",
                        multiple=False,
                    )
                ),
                vol.Optional(
                    CONF_SENSOR_CLOUDS
                ): cloud_selector,
                vol.Optional(
                    CONF_PV_HISTORY_DAYS,
                    default=30
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                vol.Required(
                    CONF_SENSOR_PV
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
            sensor_pv = (user_input.get(CONF_SENSOR_PV) or "").strip()
            sensor_clouds = (user_input.get(CONF_SENSOR_CLOUDS) or "").strip()

            if not weather_entity:
                errors["base"] = "weather_entity_required"
            elif not weather_entity.startswith("weather."):
                errors[CONF_WEATHER_ENTITY] = "must_be_weather_entity"
            if not sensor_pv:
                errors["base"] = "sensor_pv_required"
            elif not sensor_pv.startswith("sensor."):
                errors[CONF_SENSOR_PV] = "must_be_sensor_entity"
            if sensor_clouds and not sensor_clouds.startswith("sensor."):
                errors[CONF_SENSOR_CLOUDS] = "must_be_sensor_entity"

            # Validate PV sensor unit (kWh or Wh)
            if not errors.get(CONF_SENSOR_PV) and sensor_pv:
                pv_state = self.hass.states.get(sensor_pv)
                if pv_state:
                    pv_unit = pv_state.attributes.get("unit_of_measurement", "")
                    if pv_unit and pv_unit not in ("kWh", "Wh"):
                        errors[CONF_SENSOR_PV] = "sensor_pv_wrong_unit"

            if not errors:
                if not sensor_clouds:
                    sensor_clouds = auto_cloud
                history_days = user_input.get(CONF_PV_HISTORY_DAYS, data.get(CONF_PV_HISTORY_DAYS, 30))
                data.update({
                    CONF_WEATHER_ENTITY: weather_entity,
                    CONF_SENSOR_PV: sensor_pv,
                    CONF_SENSOR_CLOUDS: sensor_clouds,
                    CONF_PV_HISTORY_DAYS: history_days,
                })
                return self.async_update_reload_and_abort(entry, data=data)

        percent_ids = _get_percent_sensor_ids(self.hass)
        energy_ids = _get_energy_sensor_ids(self.hass)
        cloud_selector = EntitySelector(
            EntitySelectorConfig(include_entities=percent_ids, multiple=False)
            if percent_ids
            else EntitySelectorConfig(domain="sensor", multiple=False)
        )
        pv_selector = EntitySelector(
            EntitySelectorConfig(include_entities=energy_ids, multiple=False)
            if energy_ids
            else EntitySelectorConfig(domain="sensor", device_class="energy", multiple=False)
        )
        cloud_field_key = (
            vol.Optional(CONF_SENSOR_CLOUDS, default=display_cloud)
            if display_cloud is not None
            else vol.Optional(CONF_SENSOR_CLOUDS)
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({
                vol.Required(CONF_WEATHER_ENTITY, default=data.get(CONF_WEATHER_ENTITY, "")):
                    EntitySelector(EntitySelectorConfig(domain="weather", multiple=False)),
                cloud_field_key: cloud_selector,
                vol.Optional(CONF_PV_HISTORY_DAYS, default=data.get(CONF_PV_HISTORY_DAYS, 30)):
                    vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                vol.Required(CONF_SENSOR_PV, default=data.get(CONF_SENSOR_PV, "")):
                    pv_selector,
            }),
            errors=errors,
        )

    @staticmethod
    def _validate_db_url(db_url: str) -> bool:
        """Validate database URL format (SQLite only)."""
        if not db_url.startswith("sqlite://"):
            raise ValueError("Only SQLite is supported! URL must start with 'sqlite://'.")
        return True


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

            sensor_pv = (user_input.get(CONF_SENSOR_PV) or "").strip() or data.get(CONF_SENSOR_PV, "")

            # Validate PV sensor unit
            pv_state = self.hass.states.get(sensor_pv)
            if pv_state:
                pv_unit = pv_state.attributes.get("unit_of_measurement", "")
                if pv_unit and pv_unit not in ("kWh", "Wh"):
                    errors[CONF_SENSOR_PV] = "sensor_pv_wrong_unit"

            if not errors:
                sensor_forecast = data.get(CONF_SENSOR_FORECAST, f"sensor.{prefix}_weather_forecast")
                history_days = user_input.get(CONF_PV_HISTORY_DAYS, data.get(CONF_PV_HISTORY_DAYS, 30))

                # Always store new weather entity and regenerate SQL
                user_input[CONF_WEATHER_ENTITY] = weather_entity
                user_input[CONF_SENSOR_CLOUDS] = sensor_clouds
                user_input["sql_query"] = DEFAULT_SQL_QUERY.format(
                    sensor_clouds=sensor_clouds,
                    sensor_pv=sensor_pv,
                    sensor_forecast=sensor_forecast,
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
        cloud_selector = EntitySelector(
            EntitySelectorConfig(include_entities=percent_ids, multiple=False)
            if percent_ids
            else EntitySelectorConfig(domain="sensor", multiple=False)
        )
        pv_selector = EntitySelector(
            EntitySelectorConfig(include_entities=energy_ids, multiple=False)
            if energy_ids
            else EntitySelectorConfig(domain="sensor", device_class="energy", multiple=False)
        )
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
                    CONF_PV_HISTORY_DAYS,
                    default=_opt(CONF_PV_HISTORY_DAYS, 30),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                vol.Required(
                    CONF_SENSOR_PV,
                    default=_opt(CONF_SENSOR_PV, ""),
                ): pv_selector,
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
