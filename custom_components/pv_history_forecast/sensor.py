"""Sensor platform for HA SQL PV Forecast."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
    SensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.template import Template
from homeassistant.helpers.entity import generate_entity_id
from sqlalchemy import create_engine, text

from .const import (
    CONF_DB_URL,
    CONF_SENSOR_CLOUDS,
    CONF_SENSOR_PREFIX,
    CONF_SENSOR_PV,
    CONF_SENSOR_FORECAST,
    CONF_SENSOR_UV,
    CONF_WEATHER_ENTITY,
    CONF_PV_HISTORY_DAYS,
    CONF_VALUE_TEMPLATE,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_DEVICE_CLASS,
    CONF_STATE_CLASS,
    CONF_PV_MAX_RECORD,
    DEFAULT_SENSOR_PREFIX,
    DEFAULT_VALUE_TEMPLATE,
    DEFAULT_VALUE_TEMPLATE_MIN,
    DEFAULT_VALUE_TEMPLATE_MAX,
    DEFAULT_VALUE_TEMPLATE_TOMORROW,
    DEFAULT_VALUE_TEMPLATE_METHOD_TODAY,
    DEFAULT_VALUE_TEMPLATE_METHOD_TOMORROW,
    DEFAULT_LOVELACE_TEMPLATE_REMAINING_TODAY,
    DEFAULT_LOVELACE_TEMPLATE_TOMORROW,
    DEFAULT_SQL_QUERY,
    DEFAULT_UNIT_OF_MEASUREMENT,
    DEFAULT_DEVICE_CLASS,
    DEFAULT_STATE_CLASS,
    DEFAULT_PV_MAX_RECORD,
    DOMAIN,
)
from .coordinator import WeatherCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors for the config entry."""
    data = config_entry.data
    options = config_entry.options or {}

    prefix = data.get(CONF_SENSOR_PREFIX, DEFAULT_SENSOR_PREFIX)

    # Get weather coordinator
    coordinator: WeatherCoordinator = hass.data[DOMAIN][config_entry.entry_id].get("weather_coordinator")

    # Pre-build Lovelace templates (substitute forecast sensor once at setup)
    forecast_entity_id = data.get(CONF_SENSOR_FORECAST, f"sensor.{prefix}_weather_forecast")
    lovelace_today_str = DEFAULT_LOVELACE_TEMPLATE_REMAINING_TODAY
    lovelace_tomorrow_str = DEFAULT_LOVELACE_TEMPLATE_TOMORROW

    # Always regenerate the SQL query from current DEFAULT_SQL_QUERY + stored config.
    # This ensures any update to DEFAULT_SQL_QUERY (new CTEs, fallback UNIONs etc.)
    # takes effect immediately without the user needing to reconfigure.
    sensor_clouds = options.get(CONF_SENSOR_CLOUDS, data.get(CONF_SENSOR_CLOUDS, ""))
    sensor_pv = options.get(CONF_SENSOR_PV, data.get(CONF_SENSOR_PV, []))
    if isinstance(sensor_pv, str):
        sensor_pv = [sensor_pv] if sensor_pv else []
    sensor_forecast = options.get(CONF_SENSOR_FORECAST, data.get(CONF_SENSOR_FORECAST, forecast_entity_id))
    sensor_uv = options.get(CONF_SENSOR_UV, data.get(CONF_SENSOR_UV, ""))
    if not sensor_uv:
        # Fallback for existing installs that pre-date the UV sensor feature
        sensor_uv = f"sensor.{prefix}_uv"
    history_days = options.get(CONF_PV_HISTORY_DAYS, data.get(CONF_PV_HISTORY_DAYS, 30))
    weather_entity = options.get(CONF_WEATHER_ENTITY) or data.get(CONF_WEATHER_ENTITY, "")
    try:
        _pv_sql_list = ", ".join(f"'{s}'" for s in sensor_pv)
        _pv_first = sensor_pv[0] if sensor_pv else ""
        sql_query = DEFAULT_SQL_QUERY.format(
            sensor_clouds=sensor_clouds,
            sensor_pv_list=_pv_sql_list,
            sensor_pv_first=_pv_first,
            sensor_forecast=sensor_forecast,
            sensor_uv=sensor_uv,
            history_days=history_days,
            weather_entity=weather_entity,
        )
    except KeyError:
        # Fallback to stored query if format fails (custom SQL)
        sql_query = data.get("sql_query")

    # Main SQL sensor: runs the query, stores raw JSON + lovelace_card in attributes
    sql_sensor = SQLPVForecastSensor(
        hass=hass,
        config_entry=config_entry,
        name=f"{prefix}_remaining_today",
        db_url=data.get(CONF_DB_URL),
        sensor_clouds=sensor_clouds,
        sensor_pv=sensor_pv,
        sensor_forecast=sensor_forecast,
        pv_history_days=history_days,
        value_template=options.get(CONF_VALUE_TEMPLATE, DEFAULT_VALUE_TEMPLATE),
        unit_of_measurement=options.get(CONF_UNIT_OF_MEASUREMENT, DEFAULT_UNIT_OF_MEASUREMENT),
        device_class=options.get(CONF_DEVICE_CLASS, DEFAULT_DEVICE_CLASS),
        state_class=options.get(CONF_STATE_CLASS, DEFAULT_STATE_CLASS),
        sql_query=sql_query,
        lovelace_today_str=lovelace_today_str,
        lovelace_tomorrow_str=lovelace_tomorrow_str,
    )

    main_entity_id = f"sensor.{prefix}_remaining_today"

    # Derived sensors: read raw JSON from main sensor, apply different templates
    min_sensor = PVForecastTemplateSensor(
        hass=hass,
        config_entry=config_entry,
        main_entity_id=main_entity_id,
        name=f"{prefix}_remaining_today_min",
        value_template=DEFAULT_VALUE_TEMPLATE_MIN,
    )
    max_sensor = PVForecastTemplateSensor(
        hass=hass,
        config_entry=config_entry,
        main_entity_id=main_entity_id,
        name=f"{prefix}_remaining_today_max",
        value_template=DEFAULT_VALUE_TEMPLATE_MAX,
    )
    tomorrow_sensor = PVForecastTemplateSensor(
        hass=hass,
        config_entry=config_entry,
        main_entity_id=main_entity_id,
        name=f"{prefix}_tomorrow",
        value_template=DEFAULT_VALUE_TEMPLATE_TOMORROW,
    )

    # Weather forecast helper sensor
    weather_sensor = WeatherForecastSensor(
        hass=hass,
        config_entry=config_entry,
        coordinator=coordinator,
        prefix=prefix,
    )

    cloud_today_sensor = CloudForecastSensor(
        hass=hass,
        config_entry=config_entry,
        main_entity_id=main_entity_id,
        name=f"{prefix}_cloud_remaining_today",
        json_field="f_avg_today_remaining",
    )
    cloud_tomorrow_sensor = CloudForecastSensor(
        hass=hass,
        config_entry=config_entry,
        main_entity_id=main_entity_id,
        name=f"{prefix}_cloud_tomorrow",
        json_field="f_avg_tomorrow",
    )
    method_today_sensor = ForecastMethodSensor(
        hass=hass,
        config_entry=config_entry,
        main_entity_id=main_entity_id,
        name=f"{prefix}_method_remaining_today",
        value_template=DEFAULT_VALUE_TEMPLATE_METHOD_TODAY,
    )
    method_tomorrow_sensor = ForecastMethodSensor(
        hass=hass,
        config_entry=config_entry,
        main_entity_id=main_entity_id,
        name=f"{prefix}_method_tomorrow",
        value_template=DEFAULT_VALUE_TEMPLATE_METHOD_TOMORROW,
    )

    entities = [sql_sensor, min_sensor, max_sensor, tomorrow_sensor, cloud_today_sensor, cloud_tomorrow_sensor, method_today_sensor, method_tomorrow_sensor]

    # UV forecast sensors (read uv_avg_today_remaining / uv_avg_tomorrow from SQL JSON)
    uv_remaining_today_sensor = CloudForecastSensor(
        hass=hass,
        config_entry=config_entry,
        main_entity_id=main_entity_id,
        name=f"{prefix}_uv_remaining_today",
        json_field="uv_avg_today_remaining",
        unit_of_measurement="UV index",
        icon="mdi:sun-wireless",
    )
    uv_tomorrow_sensor = CloudForecastSensor(
        hass=hass,
        config_entry=config_entry,
        main_entity_id=main_entity_id,
        name=f"{prefix}_uv_tomorrow",
        json_field="uv_avg_tomorrow",
        unit_of_measurement="UV index",
        icon="mdi:sun-wireless",
    )
    entities.extend([uv_remaining_today_sensor, uv_tomorrow_sensor])

    # Create dedicated cloud coverage sensor when no external sensor is configured.
    # Mirrors cloud_coverage from the weather entity so HA accumulates LTS statistics.
    # The SQL 3rd UNION provides weather entity fallback from day 1 until LTS is built up.
    effective_cloud = options.get(CONF_SENSOR_CLOUDS, data.get(CONF_SENSOR_CLOUDS, ""))
    forecast_sensor_entity_id = f"sensor.{prefix}_weather_forecast"
    if effective_cloud == f"sensor.{prefix}_cloud_coverage":
        cloud_entity = CloudCoverageSensor(
            hass=hass,
            config_entry=config_entry,
            name=f"{prefix}_cloud_coverage",
            weather_entity=data.get(CONF_WEATHER_ENTITY, ""),
            forecast_sensor_entity_id=forecast_sensor_entity_id,
            coordinator=coordinator,
        )
        entities.append(cloud_entity)

    # Create dedicated UV index sensor when no external sensor is configured.
    # Mirrors uv_index from the weather entity so HA accumulates LTS statistics.
    # The SQL UV-sensor branch provides weather entity fallback until LTS is built up.
    # sensor_uv already has the auto-sensor fallback applied above.
    if sensor_uv == f"sensor.{prefix}_uv":
        uv_entity = UVIndexSensor(
            hass=hass,
            config_entry=config_entry,
            name=f"{prefix}_uv",
            weather_entity=data.get(CONF_WEATHER_ENTITY, ""),
            forecast_sensor_entity_id=forecast_sensor_entity_id,
            coordinator=coordinator,
        )
        entities.append(uv_entity)

    if coordinator:
        entities.append(weather_sensor)

    async_add_entities(entities, True)


def _handle_options_update(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    sensor: SQLPVForecastSensor,
) -> None:
    """Handle option updates."""
    options = config_entry.options or {}
    data = config_entry.data

    sensor._sensor_clouds = options.get(CONF_SENSOR_CLOUDS, data.get(CONF_SENSOR_CLOUDS))
    sensor._sensor_pv = options.get(CONF_SENSOR_PV, data.get(CONF_SENSOR_PV))
    sensor._sensor_forecast = options.get(CONF_SENSOR_FORECAST, data.get(CONF_SENSOR_FORECAST))
    sensor._pv_history_days = options.get(CONF_PV_HISTORY_DAYS, data.get(CONF_PV_HISTORY_DAYS, 30))
    sensor._unit_of_measurement = options.get(CONF_UNIT_OF_MEASUREMENT, DEFAULT_UNIT_OF_MEASUREMENT)
    sensor._device_class = options.get(CONF_DEVICE_CLASS, DEFAULT_DEVICE_CLASS)
    sensor._state_class = options.get(CONF_STATE_CLASS, DEFAULT_STATE_CLASS)
    sensor._value_template_str = options.get(CONF_VALUE_TEMPLATE, DEFAULT_VALUE_TEMPLATE)

    # Rebuild SQL query
    sensor._rebuild_sql_query()
    sensor.async_write_ha_state()


class SQLPVForecastSensor(SensorEntity):

    _last_update_time: datetime | None = None
    """SQL PV Forecast Sensor Entity."""

    _attr_icon = "mdi:database"

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        name: str,
        db_url: str,
        sensor_clouds: str,
        sensor_pv: list[str] | str,
        sensor_forecast: str,
        pv_history_days: int,
        value_template: str,
        unit_of_measurement: str,
        device_class: str,
        state_class: str,
        sql_query: str | None = None,
        lovelace_today_str: str | None = None,
        lovelace_tomorrow_str: str | None = None,
    ) -> None:
        """Initialize the sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self._db_url = db_url
        self._sensor_clouds = sensor_clouds
        self._sensor_pv = sensor_pv
        self._sensor_forecast = sensor_forecast
        self._pv_history_days = pv_history_days
        self._value_template_str = value_template
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{config_entry.entry_id}"
        self._attr_native_unit_of_measurement = unit_of_measurement
        self._attr_device_class = device_class or None
        self._attr_state_class = state_class or None
        self._attr_native_value = None
        self._attr_available = True
        self._raw_data: list | None = None
        self._last_raw_result: str | None = None
        self._lovelace_card_remaining_today: str | None = None
        self._lovelace_card_tomorrow: str | None = None
        self._lovelace_today_str = lovelace_today_str
        self._lovelace_tomorrow_str = lovelace_tomorrow_str
        self._sql_query_template = sql_query
        self._sql_query = None
        self._engine = None

        # Use configured name as entity ID
        self.entity_id = generate_entity_id("sensor.{}", name, hass=hass)

        # Build SQL query text object (no DB connection yet)
        self._rebuild_sql_query()

    def _init_database(self) -> None:
        """Initialize database connection (called in executor)."""
        self._engine = create_engine(self._db_url, echo=False)
        _LOGGER.debug("Database connection established to %s", self._db_url)

    def _rebuild_sql_query(self) -> None:
        """Rebuild the SQL query with current sensor configuration."""
        try:
            if self._sql_query_template:
                # Nutze die vordefinierte Query aus der Konfiguration
                self._sql_query = text(self._sql_query_template)
            else:
                # Fallback auf einfache Query wenn keine Template vorhanden
                _pv = self._sensor_pv
                _pv_first = (_pv[0] if isinstance(_pv, list) and _pv else _pv) or ""
                query_str = f"""
WITH vars AS (
    SELECT 
        '{self._sensor_clouds}' as sensor_clouds,
        '{_pv_first}' as sensor_pv,
        '{self._sensor_forecast}' as sensor_forecast
)
SELECT json_object(
    'sensor_clouds', vars.sensor_clouds,
    'sensor_pv', vars.sensor_pv,
    'sensor_forecast', vars.sensor_forecast,
    'timestamp', datetime('now')
) as result_json
FROM vars
                """
                self._sql_query = text(query_str)

            _LOGGER.debug(
                "SQL Query rebuilt with sensors: clouds=%s, pv=%s, forecast=%s",
                self._sensor_clouds, self._sensor_pv, self._sensor_forecast
            )
        except Exception as err:
            _LOGGER.error("Failed to rebuild SQL query: %s", err)
            self._available = False

    async def async_update(self) -> None:
        """Update the sensor only if last update was more than 5 minutes ago."""
        now = datetime.now()
        if self._last_update_time is not None:
            elapsed = now - self._last_update_time
            if elapsed < timedelta(minutes=5):
                #_LOGGER.debug("SQLPVForecastSensor: Skipping update, only %s since last update", elapsed)
                return
        try:
            # Lazy-init DB engine in executor (blocking call)
            if self._engine is None:
                await self.hass.async_add_executor_job(self._init_database)

            result = await self.hass.async_add_executor_job(self._execute_query)

            if result is not None:
                self._last_raw_result = result
                try:
                    self._raw_data = json.loads(result)
                    _LOGGER.debug("SQL query returned %d rows, raw: %s", len(self._raw_data) if isinstance(self._raw_data, list) else 0, result[:200])
                except (ValueError, TypeError) as e:
                    _LOGGER.error("Failed to parse SQL result as JSON: %s — raw: %s", e, result[:200])
                    self._raw_data = None
                new_val = self._apply_template(result)
                # Adaptive EMA: smooth small jumps (hourly stat boundary), pass large changes through fast
                if new_val is not None and self._attr_native_value is not None:
                    try:
                        new_f = float(new_val)
                        old_f = float(self._attr_native_value)
                        if old_f > 0:
                            rel_change = abs(new_f - old_f) / old_f
                            # alpha ramps linearly from 0.3 (no change) to 1.0 (≥30% change)
                            alpha = min(1.0, 0.3 + rel_change * 2.33)
                            self._attr_native_value = round(alpha * new_f + (1.0 - alpha) * old_f, 3)
                        else:
                            self._attr_native_value = new_val
                    except (ValueError, TypeError):
                        self._attr_native_value = new_val
                else:
                    self._attr_native_value = new_val
                # Render Lovelace card with fresh SQL data passed as direct variable
                if self._lovelace_today_str:
                    try:
                        tmpl = Template(self._lovelace_today_str, self.hass)
                        self._lovelace_card_remaining_today = str(tmpl.async_render({"raw_json": result}))
                    except Exception as lovelace_err:
                        _LOGGER.error("Failed to render lovelace_card_remaining_today: %s", lovelace_err)
                        self._lovelace_card_remaining_today = None
                if self._lovelace_tomorrow_str:
                    try:
                        tmpl = Template(self._lovelace_tomorrow_str, self.hass)
                        self._lovelace_card_tomorrow = str(tmpl.async_render({"raw_json": result}))
                    except Exception as lovelace_err:
                        _LOGGER.error("Failed to render lovelace_card_tomorrow: %s", lovelace_err)
                        self._lovelace_card_tomorrow = None
                self._attr_available = True
            else:
                _LOGGER.warning("SQL query returned no rows")
                self._raw_data = None
                self._attr_available = False

            self._last_update_time = now

        except Exception as err:
            _LOGGER.error("Error updating sensor: %s", err)
            self._engine = None  # force reconnect next time
            self._attr_available = False

    def _execute_query(self) -> str | None:
        """Execute the SQL query and return the raw result as a string.

        Returns None when the query produces no rows OR when the single aggregate
        column is SQL NULL (e.g. json_group_array on an empty set without COALESCE).
        """
        with self._engine.connect() as conn:
            result = conn.execute(self._sql_query)
            row = result.fetchone()
            if row and row[0] is not None:
                return str(row[0])
        return None

    def _apply_template(self, raw_value: str) -> float | str | None:
        """Apply value template to the raw SQL result string."""
        try:
            template = Template(self._value_template_str, self.hass)
            options = self.config_entry.options or {}
            data = self.config_entry.data
            pv_max = float(options.get(CONF_PV_MAX_RECORD, data.get(CONF_PV_MAX_RECORD, DEFAULT_PV_MAX_RECORD)))
            rendered = template.async_render({
                "value": raw_value,
                "latitude": self.hass.config.latitude,
                "pv_max_record": pv_max,
            })
            try:
                return float(rendered)
            except (ValueError, TypeError):
                return str(rendered)
        except Exception as err:
            _LOGGER.error("Failed to apply template: %s", err)
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the raw SQL data as state attributes."""
        attrs: dict[str, Any] = {
            "sensor_pv": self._sensor_pv,
            "sensor_clouds": self._sensor_clouds,
            "sensor_forecast": self._sensor_forecast,
            "pv_history_days": self._pv_history_days,
        }
        if self._last_raw_result is not None:
            attrs["json"] = self._last_raw_result
        if self._lovelace_card_remaining_today is not None:
            attrs["lovelace_card_remaining_today"] = self._lovelace_card_remaining_today
        if self._lovelace_card_tomorrow is not None:
            attrs["lovelace_card_tomorrow"] = self._lovelace_card_tomorrow
        rows = self._raw_data if isinstance(self._raw_data, list) else []
        attrs["matching_days_count"] = len(rows)
        return attrs

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._attr_available

    @property
    def should_poll(self) -> bool:
        """Return True if entity has to be polled for state changes."""
        return True

    @property
    def update_interval(self) -> timedelta | None:
        """Return the polling interval."""
        return timedelta(minutes=5)


class PVForecastTemplateSensor(SensorEntity):
    """Derived PV forecast sensor.

    Reads the raw SQL JSON cached by the main SQLPVForecastSensor and applies
    a dedicated Jinja2 template (min / max / tomorrow) without running a
    second SQL query.
    """

    _attr_icon = "mdi:solar-panel"
    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = "energy"
    _attr_state_class = "measurement"

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        main_entity_id: str,
        name: str,
        value_template: str,
    ) -> None:
        """Initialize the derived sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self._main_entity_id = main_entity_id
        self._value_template_str = value_template
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{config_entry.entry_id}_{name}"
        self._attr_native_value = None
        self._attr_available = False
        self.entity_id = generate_entity_id("sensor.{}", name, hass=hass)

    async def async_update(self) -> None:
        """Update by reading raw JSON from the main sensor's attributes."""
        main_state = self.hass.states.get(self._main_entity_id)
        if main_state is None or not main_state.attributes.get("json"):
            self._attr_available = False
            return
        raw = main_state.attributes["json"]
        self._attr_native_value = self._apply_template(raw)
        self._attr_available = self._attr_native_value is not None

    def _apply_template(self, raw_value: str) -> float | str | None:
        """Apply value template with latitude variable."""
        try:
            template = Template(self._value_template_str, self.hass)
            options = self.config_entry.options or {}
            data = self.config_entry.data
            pv_max = float(options.get(CONF_PV_MAX_RECORD, data.get(CONF_PV_MAX_RECORD, DEFAULT_PV_MAX_RECORD)))
            rendered = template.async_render({
                "value": raw_value,
                "latitude": self.hass.config.latitude,
                "pv_max_record": pv_max,
            })
            try:
                return float(rendered)
            except (ValueError, TypeError):
                return str(rendered)
        except Exception as err:
            _LOGGER.error("Failed to apply template for %s: %s", self._attr_name, err)
            return None

    @property
    def should_poll(self) -> bool:
        return True

    @property
    def update_interval(self) -> timedelta | None:
        return timedelta(minutes=15)


class ForecastMethodSensor(SensorEntity):
    """Sensor that exposes the calculation method name (Weighted average, Max assumption, etc.).

    Unlike PVForecastTemplateSensor it has no numeric device class or unit so that
    HA accepts a plain string as its state.
    """

    _attr_icon = "mdi:help-circle-outline"

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        main_entity_id: str,
        name: str,
        value_template: str,
    ) -> None:
        """Initialize the method sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self._main_entity_id = main_entity_id
        self._value_template_str = value_template
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{config_entry.entry_id}_{name}"
        self._attr_native_value = None
        self._attr_available = False
        self.entity_id = generate_entity_id("sensor.{}", name, hass=hass)

    async def async_update(self) -> None:
        """Update by reading raw JSON from the main sensor's attributes."""
        main_state = self.hass.states.get(self._main_entity_id)
        if main_state is None or not main_state.attributes.get("json"):
            self._attr_available = False
            return
        raw = main_state.attributes["json"]
        try:
            template = Template(self._value_template_str, self.hass)
            options = self.config_entry.options or {}
            data = self.config_entry.data
            pv_max = float(options.get(CONF_PV_MAX_RECORD, data.get(CONF_PV_MAX_RECORD, DEFAULT_PV_MAX_RECORD)))
            rendered = template.async_render({"value": raw, "latitude": self.hass.config.latitude, "pv_max_record": pv_max})
            value = str(rendered).strip()
            if value:
                self._attr_native_value = value
                self._attr_available = True
            else:
                self._attr_available = False
        except Exception as err:
            _LOGGER.error("Failed to render method template for %s: %s", self._attr_name, err)
            self._attr_available = False

    @property
    def should_poll(self) -> bool:
        return True

    @property
    def update_interval(self) -> timedelta | None:
        return timedelta(minutes=15)


class WeatherForecastSensor(CoordinatorEntity, SensorEntity):
    """Weather Forecast Sensor - displays the hourly forecast data."""

    _attr_icon = "mdi:weather-partly-cloudy"

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        coordinator: WeatherCoordinator,
        prefix: str,
    ) -> None:
        """Initialize the weather forecast sensor."""
        super().__init__(coordinator)
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{prefix} Weather Forecast"
        self._attr_unique_id = f"{DOMAIN}_{config_entry.entry_id}_weather_forecast"
        self.entity_id = f"sensor.{prefix}_weather_forecast"

    @property
    def native_value(self) -> str | None:
        """Return the number of forecast entries as the state."""
        if self.coordinator.data:
            forecast_list = self.coordinator.data.get("forecast", [])
            return len(forecast_list)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""
        if self.coordinator.data:
            forecast_list = self.coordinator.data.get("forecast", [])
            return {
                "forecast": forecast_list,
                "forecast_count": len(forecast_list),
                "last_update": self.coordinator.data.get("timestamp"),
            }
        return {}

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success


def _nearest_forecast_value(
    hass: HomeAssistant, forecast_sensor_entity_id: str, field: str
) -> float | None:
    """Return the value of *field* from the forecast entry closest to now (state machine)."""
    forecast_state = hass.states.get(forecast_sensor_entity_id)
    if forecast_state is None:
        return None
    return _nearest_forecast_field(forecast_state.attributes.get("forecast", []), field)


def _nearest_forecast_field(forecast_list: list, field: str) -> float | None:
    """Return the value of *field* from the forecast entry closest to now.

    Works directly on a forecast list so it can be called before the
    WeatherForecastSensor has written its state to hass.states.
    Skips entries whose value for *field* is explicitly null.
    Returns None when no valid entry is found.
    """
    if not forecast_list:
        return None
    now_ts = datetime.now(tz=timezone.utc).timestamp()
    best_value: float | None = None
    best_delta = float("inf")
    for entry in forecast_list:
        raw = entry.get(field)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (ValueError, TypeError):
            continue
        dt_str = entry.get("datetime", "")
        try:
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = abs(dt.timestamp() - now_ts)
        except (ValueError, TypeError):
            delta = float("inf")
        if delta < best_delta:
            best_delta = delta
            best_value = value
    return best_value


class CloudCoverageSensor(SensorEntity):
    """Cloud Coverage Sensor that mirrors the weather entity's cloud_coverage attribute.

    Created automatically when no external cloud coverage sensor is configured.
    Registers as a proper HA sensor so Home Assistant tracks its long-term
    statistics (LTS).  After >10 days of runtime the SQL forecast query will
    use these accumulated statistics for richer historical matching.
    """

    _attr_icon = "mdi:weather-cloudy"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        name: str,
        weather_entity: str,
        forecast_sensor_entity_id: str | None = None,
        coordinator: WeatherCoordinator | None = None,
    ) -> None:
        """Initialize the sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{config_entry.entry_id}_cloud_coverage"
        self._attr_native_value = None
        self._attr_available = False
        self._weather_entity = weather_entity
        self._forecast_sensor_entity_id = forecast_sensor_entity_id
        self._coordinator = coordinator
        self.entity_id = generate_entity_id("sensor.{}", name, hass=hass)

    async def async_update(self) -> None:
        """Read cloud_coverage, preferring coordinator forecast data.

        Priority order:
        1. Nearest entry in coordinator.data['forecast'] (independent of the
           weather entity state — avoids OWM startup-unavailable race condition).
        2. Direct cloud_coverage attribute on the weather entity current state.
        3. Nearest entry via state machine WeatherForecastSensor (no-coordinator
           fallback).
        """
        # 1. Coordinator forecast (most reliable — already fetched before entity setup)
        if self._coordinator and self._coordinator.data:
            cloud_coverage = _nearest_forecast_field(
                self._coordinator.data.get("forecast", []), "cloud_coverage"
            )
            if cloud_coverage is not None:
                try:
                    self._attr_native_value = float(cloud_coverage)
                    self._attr_available = True
                    return
                except (ValueError, TypeError):
                    pass

        # 2. Direct weather entity state attribute
        state = self.hass.states.get(self._weather_entity)
        if state is not None and state.state not in ("unknown", "unavailable", ""):
            cloud_coverage = state.attributes.get("cloud_coverage")
            if cloud_coverage is not None:
                try:
                    self._attr_native_value = float(cloud_coverage)
                    self._attr_available = True
                    return
                except (ValueError, TypeError):
                    pass

        # 3. State-machine WeatherForecastSensor (fallback when coordinator is None)
        if self._forecast_sensor_entity_id:
            cloud_coverage = _nearest_forecast_value(
                self.hass, self._forecast_sensor_entity_id, "cloud_coverage"
            )
            if cloud_coverage is not None:
                try:
                    self._attr_native_value = float(cloud_coverage)
                    self._attr_available = True
                    return
                except (ValueError, TypeError):
                    pass

        self._attr_available = False

    @property
    def should_poll(self) -> bool:
        return True


class UVIndexSensor(SensorEntity):
    """UV Index Sensor that mirrors the weather entity's uv_index attribute.

    Created automatically when no external UV index sensor is configured.
    Registers as a proper HA sensor so Home Assistant tracks its long-term
    statistics (LTS).  After >10 days of runtime the SQL forecast query will
    use these accumulated statistics for richer historical matching.
    """

    _attr_icon = "mdi:sun-wireless"
    _attr_native_unit_of_measurement = "UV index"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        name: str,
        weather_entity: str,
        forecast_sensor_entity_id: str | None = None,
        coordinator: WeatherCoordinator | None = None,
    ) -> None:
        """Initialize the sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{config_entry.entry_id}_uv_index"
        self._attr_native_value = None
        self._attr_available = False
        self._weather_entity = weather_entity
        self._forecast_sensor_entity_id = forecast_sensor_entity_id
        self._coordinator = coordinator
        self.entity_id = generate_entity_id("sensor.{}", name, hass=hass)

    async def async_update(self) -> None:
        """Read uv_index, preferring coordinator forecast data.

        Priority order:
        1. Nearest entry in coordinator.data['forecast'] (independent of the
           weather entity state — avoids OWM startup-unavailable race condition).
        2. Direct uv_index attribute on the weather entity current state.
        3. Nearest entry via state machine WeatherForecastSensor (no-coordinator
           fallback).
        When no UV data is found at all (e.g. OWM free tier: all null), the
        sensor reports 0.0 so HA accumulates LTS statistics and Jinja templates
        disable UV weighting automatically (guard: {% if f_uv_avg > 0 %}).
        """
        uv_index: float | None = None

        # 1. Coordinator forecast
        if self._coordinator and self._coordinator.data:
            uv_index = _nearest_forecast_field(
                self._coordinator.data.get("forecast", []), "uv_index"
            )

        # 2. Direct weather entity state attribute
        if uv_index is None:
            state = self.hass.states.get(self._weather_entity)
            if state is not None and state.state not in ("unknown", "unavailable", ""):
                raw = state.attributes.get("uv_index")
                if raw is not None:
                    try:
                        uv_index = float(raw)
                    except (ValueError, TypeError):
                        pass

        # 3. State-machine WeatherForecastSensor
        if uv_index is None and self._forecast_sensor_entity_id:
            uv_index = _nearest_forecast_value(
                self.hass, self._forecast_sensor_entity_id, "uv_index"
            )

        # If no UV data from any source (OWM free tier: all null) → report 0.0
        # so the sensor stays available and LTS statistics keep accumulating.
        # The coordinator itself must be reachable; if we have no coordinator AND
        # the weather entity is completely unknown, stay unavailable.
        if uv_index is None:
            if self._coordinator and self._coordinator.data is not None:
                uv_index = 0.0
            else:
                state = self.hass.states.get(self._weather_entity)
                if state is not None and state.state not in ("unknown", "unavailable", ""):
                    uv_index = 0.0

        if uv_index is not None:
            self._attr_native_value = uv_index
            self._attr_available = True
        else:
            self._attr_available = False

    @property
    def should_poll(self) -> bool:
        return True


class CloudForecastSensor(SensorEntity):
    """Exposes a single numeric field from the main sensor's SQL JSON result.

    Used to surface the cloud-coverage values that the forecast calculation
    actually uses (f_avg_today_remaining, f_avg_tomorrow) as proper HA sensors,
    so their history is visible in the UI and available for automations.
    """

    _attr_icon = "mdi:weather-cloudy"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        main_entity_id: str,
        name: str,
        json_field: str,
        unit_of_measurement: str | None = None,
        icon: str | None = None,
    ) -> None:
        """Initialize the sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self._main_entity_id = main_entity_id
        self._json_field = json_field
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{config_entry.entry_id}_{name}"
        self._attr_native_value = None
        self._attr_available = False
        self.entity_id = generate_entity_id("sensor.{}", name, hass=hass)
        if unit_of_measurement is not None:
            self._attr_native_unit_of_measurement = unit_of_measurement
        if icon is not None:
            self._attr_icon = icon

    async def async_update(self) -> None:
        """Read the target field from the main sensor's raw JSON attribute."""
        main_state = self.hass.states.get(self._main_entity_id)
        if main_state is None:
            self._attr_available = False
            return
        raw = main_state.attributes.get("json")
        if not raw:
            self._attr_available = False
            return
        try:
            data = json.loads(raw)
            if isinstance(data, list) and data:
                value = data[0].get(self._json_field)
                if value is not None:
                    self._attr_native_value = float(value)
                    self._attr_available = True
                    return
        except (ValueError, TypeError, KeyError):
            pass
        self._attr_available = False

    @property
    def should_poll(self) -> bool:
        return True

    @property
    def update_interval(self) -> timedelta | None:
        return timedelta(minutes=5)

