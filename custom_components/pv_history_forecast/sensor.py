"""Sensor platform for HA SQL PV Forecast."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import copy
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

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
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from sqlalchemy import create_engine, text

from . import const as _const
from .const import (
    CONF_DB_URL,
    CONF_SENSOR_CLOUDS,
    CONF_SENSOR_PREFIX,
    CONF_SENSOR_PV,
    CONF_SENSOR_FORECAST,
    CONF_SENSOR_UV,
    CONF_SENSOR_TEMP,
    CONF_SENSOR_PRECIP,
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

CONF_RETUNE = _const.CONF_RETUNE
DEFAULT_RETUNE = bool(_const.DEFAULT_RETUNE)


_LOGGER = logging.getLogger(__name__)

RetuneParamValue = float | int | str

# Best parameters from the current standard profile sensor.
RETUNE_BASE_PARAMS: dict[str, RetuneParamValue] = {
    "top_n": 10,
    "recency_amp": 0.02,
    "season_exponent": 0.5,
    "doy_weight": 0.1,
    "uv_weight": 2,
    "temp_weight": 2,
    "temp_coeff": -0.003,
    "precip_weight": 2,
    "score": 1.0,
}


def _select_initial_params(
    *,
    use_retune: bool,
    sensor_clouds: str | None,
    sensor_forecast: str | None,
    sensor_name: str | None = None,
) -> tuple[dict[str, RetuneParamValue], dict[str, RetuneParamValue]]:
    import copy
    retune_baseline = copy.deepcopy(RETUNE_BASE_PARAMS)
    return copy.deepcopy(retune_baseline), retune_baseline




_RETUNE_TUNABLE_KEYS: tuple[str, ...] = (
    "top_n",
    "recency_amp",
    "season_exponent",
    "doy_weight",
    "uv_weight",
    "temp_weight",
    "temp_coeff",
    "precip_weight",
)


def _merge_retune_param_defaults(
    params: Mapping[str, RetuneParamValue],
    baseline: Mapping[str, RetuneParamValue],
) -> dict[str, RetuneParamValue]:
    """Fill missing tunable keys after upgrades or restored entity state."""
    merged = dict(params)
    for key in _RETUNE_TUNABLE_KEYS:
        if key not in merged or merged[key] is None:
            if key in baseline:
                merged[key] = baseline[key]
    return merged


def _normalize_device_state_class(
    device_class: str | None,
    state_class: str | None,
) -> tuple[str | None, str | None]:
    """Return a HA-valid device/state class combination.

    HA rejects state_class=measurement for device_class=energy.
    """
    dev = device_class or None
    state = state_class or None
    dev_l = str(dev).lower() if dev is not None else ""
    state_l = str(state).lower() if state is not None else ""

    if dev_l == "energy" and state_l == "measurement":
        # Keep measurement semantics for forecast/remaining sensors and drop
        # the incompatible energy device class.
        return None, state

    return dev, state


def _resolve_retune_enabled(
    options: dict[str, Any] | None,
    data: dict[str, Any] | None,
    *,
    default_if_missing: bool,
) -> bool:
    """Resolve retune flag with explicit legacy fallback behavior."""
    opts = options or {}
    entry_data = data or {}
    if CONF_RETUNE in opts:
        return bool(opts.get(CONF_RETUNE))
    if CONF_RETUNE in entry_data:
        return bool(entry_data.get(CONF_RETUNE))
    return bool(default_if_missing)


def _utcnow_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")



def _apply_adaptive_ema_smoothing(
    new_val: float | str | None,
    old_val: float | str | None,
    pv_max: float,
    *,
    day_reset: bool = False,
    step_count: int = 0,
) -> tuple[float | str | None, int]:
    """Apply direction-aware EMA smoothing for 5-minute forecast updates.

    ``step_count`` stores the active trend direction:
      positive = target is falling, negative = target is rising, 0 = neutral.

    Day reset and missing old values are adopted directly. Normal updates ease
    in after a direction change, speed up while the trend persists, and ease out
    near the target. Falling values keep a higher minimum alpha because the
    remaining-day forecast usually decreases through the day.
    """
    if new_val is None:
        return old_val, step_count

    try:
        new_f = float(new_val)
    except (ValueError, TypeError):
        return new_val, 0

    if day_reset or old_val is None:
        return round(new_f, 3), 0

    try:
        old_f = float(old_val)
    except (ValueError, TypeError):
        return new_val, 0

    gap = new_f - old_f
    if abs(gap) <= 0.001:
        return round(new_f, 3), 0

    def _smoothstep(value: float) -> float:
        value = max(0.0, min(1.0, value))
        return value * value * (3.0 - 2.0 * value)

    is_down = gap < 0.0
    direction = 1 if is_down else -1
    previous_direction = 1 if step_count > 0 else -1 if step_count < 0 else 0

    if previous_direction == direction:
        new_step_count = max(-30, min(30, step_count + direction))
    else:
        new_step_count = direction

    trend = _smoothstep(abs(new_step_count) / 6.0)
    ref_val = max(abs(new_f), abs(old_f), pv_max * 0.05, 1.0)
    gap_strength = _smoothstep(abs(gap) / (ref_val * 0.30))

    if is_down:
        alpha_start, alpha_peak, alpha_near = 0.28, 0.72, 0.38
    else:
        alpha_start, alpha_peak, alpha_near = 0.015, 0.22, 0.04

    accelerated_alpha = alpha_start + (alpha_peak - alpha_start) * trend
    alpha = alpha_near + (accelerated_alpha - alpha_near) * gap_strength

    # A down-to-up reversal is deliberately damped for the first two cycles.
    if not is_down and previous_direction == 1:
        alpha *= 0.35 if abs(new_step_count) == 1 else 0.65

    delta = gap * alpha
    cap_base = max(pv_max, ref_val)
    if is_down:
        cap = cap_base * (0.18 + 0.22 * trend)
    else:
        cap = cap_base * (0.012 + 0.028 * trend)
    delta = max(min(delta, cap), -cap)

    return round(old_f + delta, 3), new_step_count


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors for the config entry."""
    data = config_entry.data
    options = config_entry.options or {}
    use_retune = _resolve_retune_enabled(options, data, default_if_missing=False)

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
    sensor_temp = options.get(CONF_SENSOR_TEMP, data.get(CONF_SENSOR_TEMP, ""))
    sensor_precip = options.get(CONF_SENSOR_PRECIP, data.get(CONF_SENSOR_PRECIP, ""))
    if not sensor_uv:
        # Fallback for existing installs that pre-date the UV sensor feature
        sensor_uv = f"sensor.{prefix}_uv"
    if not sensor_temp:
        sensor_temp = f"sensor.{prefix}_temperature"
    if not sensor_precip:
        sensor_precip = f"sensor.{prefix}_precipitation"
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
            sensor_temp=sensor_temp,
            sensor_precip=sensor_precip,
            history_days=history_days,
            weather_entity=weather_entity,
        )
    except KeyError:
        # Fallback to stored query if format fails (custom SQL)
        sql_query = data.get("sql_query")

    device_class = options.get(CONF_DEVICE_CLASS, DEFAULT_DEVICE_CLASS)
    state_class = options.get(CONF_STATE_CLASS, DEFAULT_STATE_CLASS)
    device_class, state_class = _normalize_device_state_class(device_class, state_class)

    # Main SQL sensor: runs the query, stores raw JSON + lovelace_card in attributes
    sql_sensor = SQLPVForecastSensor(
        hass=hass,
        config_entry=config_entry,
        name=f"{prefix}_remaining_today",
        db_url=data.get(CONF_DB_URL),
        sensor_clouds=sensor_clouds,
        sensor_pv=sensor_pv,
        sensor_forecast=sensor_forecast,
        sensor_temp=sensor_temp,
        sensor_precip=sensor_precip,
        pv_history_days=history_days,
        value_template=options.get(
            CONF_VALUE_TEMPLATE,
            DEFAULT_VALUE_TEMPLATE
        ),
        unit_of_measurement=options.get(CONF_UNIT_OF_MEASUREMENT, DEFAULT_UNIT_OF_MEASUREMENT),
        device_class=device_class,
        state_class=state_class,
        sql_query=sql_query,
        lovelace_today_str=lovelace_today_str,
        lovelace_tomorrow_str=lovelace_tomorrow_str,
        use_retune=use_retune,
    )
    hass.data[DOMAIN][config_entry.entry_id]["main_sensor"] = sql_sensor

    main_entity_id = f"sensor.{prefix}_remaining_today"

    # Derived sensors: read raw JSON from main sensor, apply different templates
    # min/max use throttle_minutes=5 so their EMA update rate matches the main
    # sensor and smoothing is not accelerated by more frequent polling calls.
    min_sensor = PVForecastTemplateSensor(
        hass=hass,
        config_entry=config_entry,
        main_entity_id=main_entity_id,
        name=f"{prefix}_remaining_today_min",
        value_template=DEFAULT_VALUE_TEMPLATE_MIN,
        throttle_minutes=5,
    )
    max_sensor = PVForecastTemplateSensor(
        hass=hass,
        config_entry=config_entry,
        main_entity_id=main_entity_id,
        name=f"{prefix}_remaining_today_max",
        value_template=DEFAULT_VALUE_TEMPLATE_MAX,
        throttle_minutes=5,
    )
    tomorrow_sensor = PVForecastTemplateSensor(
        hass=hass,
        config_entry=config_entry,
        main_entity_id=main_entity_id,
        name=f"{prefix}_tomorrow",
        value_template=DEFAULT_VALUE_TEMPLATE_TOMORROW,
        no_ema=True,
    )

    # Weather forecast helper sensor
    weather_sensor = WeatherForecastSensor(
        hass=hass,
        config_entry=config_entry,
        coordinator=coordinator,
        prefix=prefix,
    )

    entities = [sql_sensor, min_sensor, max_sensor, tomorrow_sensor]


    precipitation_sensor = PrecipitationSensor(
        hass=hass,
        config_entry=config_entry,
        name=f"{prefix}_precipitation",
        weather_entity=data.get(CONF_WEATHER_ENTITY, ""),
        forecast_sensor_entity_id=forecast_entity_id,
        coordinator=coordinator,
    )
    entities.append(precipitation_sensor)

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

    # Create dedicated temperature sensor when no external sensor is configured.
    # This auto-sensor mirrors weather entity data (forecast/current), same pattern as UV/cloud auto sensors.
    if sensor_temp == f"sensor.{prefix}_temperature":
        temp_entity = TemperatureSensor(
            hass=hass,
            config_entry=config_entry,
            name=f"{prefix}_temperature",
            weather_entity=data.get(CONF_WEATHER_ENTITY, ""),
            forecast_sensor_entity_id=forecast_sensor_entity_id,
            coordinator=coordinator,
        )
        entities.append(temp_entity)

    if coordinator:
        entities.append(weather_sensor)

    # Add entities without blocking setup; startup kickstart + listeners update
    # values shortly after boot without triggering slow-platform warnings.
    async_add_entities(entities, False)


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
    sensor._sensor_temp = options.get(CONF_SENSOR_TEMP, data.get(CONF_SENSOR_TEMP))
    sensor._sensor_precip = options.get(CONF_SENSOR_PRECIP, data.get(CONF_SENSOR_PRECIP))
    if not sensor._sensor_temp:
        prefix = data.get(CONF_SENSOR_PREFIX, DEFAULT_SENSOR_PREFIX)
        sensor._sensor_temp = f"sensor.{prefix}_temperature"
    if not sensor._sensor_precip:
        prefix = data.get(CONF_SENSOR_PREFIX, DEFAULT_SENSOR_PREFIX)
        sensor._sensor_precip = f"sensor.{prefix}_precipitation"
    sensor._pv_history_days = options.get(CONF_PV_HISTORY_DAYS, data.get(CONF_PV_HISTORY_DAYS, 30))
    sensor._unit_of_measurement = options.get(CONF_UNIT_OF_MEASUREMENT, DEFAULT_UNIT_OF_MEASUREMENT)
    sensor._device_class, sensor._state_class = _normalize_device_state_class(
        options.get(CONF_DEVICE_CLASS, DEFAULT_DEVICE_CLASS),
        options.get(CONF_STATE_CLASS, DEFAULT_STATE_CLASS),
    )
    sensor._attr_device_class = sensor._device_class
    sensor._attr_state_class = sensor._state_class
    sensor._use_retune = _resolve_retune_enabled(options, data, default_if_missing=False)
    sensor._retune_params, sensor._retune_base_params = _select_initial_params(
        use_retune=sensor._use_retune,
        sensor_clouds=sensor._sensor_clouds,
        sensor_forecast=sensor._sensor_forecast,
        sensor_name=sensor._attr_name,
    )
    sensor._retune_last_tune_day = None
    sensor._value_template_str = options.get(
        CONF_VALUE_TEMPLATE,
        DEFAULT_VALUE_TEMPLATE
    )

    # Rebuild SQL query
    sensor._rebuild_sql_query()
    sensor._write_state_throttled(force=True)


class SQLPVForecastSensor(SensorEntity, RestoreEntity):

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
        sensor_temp: str,
        sensor_precip: str,
        pv_history_days: int,
        value_template: str,
        unit_of_measurement: str,
        device_class: str,
        state_class: str,
        sql_query: str | None = None,
        lovelace_today_str: str | None = None,
        lovelace_tomorrow_str: str | None = None,
        use_retune: bool = False,
    ) -> None:
        """Initialize the sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self._db_url = db_url
        self._sensor_clouds = sensor_clouds
        self._sensor_pv = sensor_pv
        self._sensor_forecast = sensor_forecast
        self._sensor_temp = sensor_temp
        self._sensor_precip = sensor_precip
        self._pv_history_days = pv_history_days
        self._value_template_str = value_template
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{config_entry.entry_id}"
        self._attr_native_unit_of_measurement = unit_of_measurement
        self._attr_device_class, self._attr_state_class = _normalize_device_state_class(
            device_class,
            state_class,
        )
        self._attr_native_value = None
        self._attr_available = True
        self._raw_data: list | None = None
        self._last_raw_result: str | None = None
        self._last_raw_date: str | None = None
        self._last_raw_generated_at: str | None = None
        self._lovelace_card_remaining_today: str | None = None
        self._lovelace_card_tomorrow: str | None = None
        self._lovelace_today_str = lovelace_today_str
        self._lovelace_tomorrow_str = lovelace_tomorrow_str
        self._use_retune = use_retune
        self._retune_params, self._retune_base_params = _select_initial_params(
            use_retune=use_retune,
            sensor_clouds=sensor_clouds,
            sensor_forecast=sensor_forecast,
            sensor_name=name,
        )
        self._retune_last_tune_day: str | None = None
        self._retune_task: asyncio.Task | None = None
        self._sql_query_template = sql_query
        self._sql_query = None
        self._engine = None
        self._startup_retry_unsubs: list[Any] = []
        self._last_state_write_time: datetime | None = None
        self._update_in_progress: bool = False
        self._startup_ts: float = time.time()
        self._runtime_start_ts: float = float(
            hass.data.get(DOMAIN, {}).get("_runtime_start_ts", self._startup_ts)
        )
        self._forecast_change_unsub = None
        self._forecast_refresh_task: asyncio.Task | None = None
        # Date tracking for EMA day-reset: stores the local date string of the
        # last update so that the first update after midnight bypasses smoothing.
        self._last_ema_date: str | None = None
        # Signed step count from the last EMA smoothing iteration.
        # Positive → moving down (toward lower values); negative → moving up.
        self._last_ema_step: int = 0
        # Time of the last EMA smoothing call (used to scale alpha if updates
        # happen more frequently than 5 minutes).
        self._last_ema_time: datetime | None = None

        # Use configured name as entity ID
        self.entity_id = generate_entity_id("sensor.{}", name, hass=hass)

        # Build SQL query text object (no DB connection yet)
        self._rebuild_sql_query()

    async def async_added_to_hass(self) -> None:
        """Schedule early startup retries so reboot fallback values are corrected quickly."""
        await super().async_added_to_hass()
        # Record when this entity was added to HA (used to detect stale pre-boot forecasts).
        self._startup_ts = time.time()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            if last_state.state not in ("unknown", "unavailable", "", None):
                try:
                    self._attr_native_value = float(last_state.state)
                except (TypeError, ValueError):
                    self._attr_native_value = last_state.state
            attrs = last_state.attributes or {}
            last_tune_day = attrs.get("retune_last_tune_day")
            last_params = attrs.get("retune_params")
            # Detect retune enable/disable transition:
            prev_retune_enabled = bool(attrs.get("retune", self._use_retune))
            retune_just_enabled = self._use_retune and not prev_retune_enabled
            if isinstance(last_tune_day, str) and last_tune_day and not retune_just_enabled:
                self._retune_last_tune_day = last_tune_day
            if isinstance(last_params, dict) and last_params and not retune_just_enabled:
                self._retune_params = _merge_retune_param_defaults(
                    last_params,
                    self._retune_base_params,
                )
            if retune_just_enabled:
                _LOGGER.debug(
                    "Retune was just enabled; starting from profile retune baseline "
                    "instead of restoring previous non-retune params."
                )

        async def _startup_retry(_now) -> None:
            try:
                await self._refresh_weather_coordinator()
                await self.async_update()
                self._write_state_throttled()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Startup retry update failed: %s", err)

        # Recorder writes for freshly restored forecast attributes can lag behind
        for delay in (30.0, 90.0, 180.0):
            unsub = async_call_later(self.hass, delay, _startup_retry)
            self._startup_retry_unsubs.append(unsub)
            self.async_on_remove(unsub)

        @callback
        def _on_forecast_state_change(_event) -> None:
            if self._forecast_refresh_task and not self._forecast_refresh_task.done():
                return

            async def _run_refresh() -> None:
                try:
                    await self.async_update()
                    self._write_state_throttled()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Forecast-change refresh failed: %s", err)
                finally:
                    self._forecast_refresh_task = None

            self._forecast_refresh_task = self.hass.async_create_task(_run_refresh())

        weather_entity = (
            (self.config_entry.options or {}).get(CONF_WEATHER_ENTITY)
            or self.config_entry.data.get(CONF_WEATHER_ENTITY, "")
        )
        tracked_entities = [self._sensor_forecast]
        if weather_entity:
            tracked_entities.append(weather_entity)

        self._forecast_change_unsub = async_track_state_change_event(
            self.hass,
            tracked_entities,
            _on_forecast_state_change,
        )
        self.async_on_remove(self._forecast_change_unsub)

        from homeassistant.helpers.event import async_track_time_change
        async def _nightly_retune_trigger(now_dt=None) -> None:
            if not getattr(self, "_use_retune", False):
                _LOGGER.info("PV Forecast: Automatischer Nachtlauf übersprungen, da Retuning deaktiviert ist.")
                return
            self._last_update_time = None
            try:
                await self.async_update()
            except Exception as update_err:
                _LOGGER.error("Nachtlauf abgebrochen: SQL-Vorab-Abfrage fehlgeschlagen: %s", update_err)
                return
            try:
                options = self.config_entry.options or {}
                data = self.config_entry.data
                current_pv_max = float(options.get(CONF_PV_MAX_RECORD, data.get(CONF_PV_MAX_RECORD, DEFAULT_PV_MAX_RECORD)))
            except (TypeError, ValueError):
                current_pv_max = 45.0
            prev_day_key = self._retune_last_tune_day
            try:
                self._retune_last_tune_day = None
                await self._maybe_tune_params(current_pv_max)
            finally:
                if self._retune_last_tune_day is None:
                    self._retune_last_tune_day = prev_day_key

            self._write_state_throttled(force=True)
        @callback
        def _nightly_callback_wrapper(now_dt) -> None:
            self.hass.async_create_background_task(
                _nightly_retune_trigger(now_dt),
                name="pv_forecast_nightly_retune"
            )

        # Der unbestechliche Wecker zündet jede Nacht um exakt 00:15:00 Uhr Ortszeit
        unsub_nightly = async_track_time_change(
            self.hass,
            _nightly_callback_wrapper,
            hour=0,
            minute=10,
            second=0
        )
        self._startup_retry_unsubs.append(unsub_nightly)
        self.async_on_remove(unsub_nightly)



    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending background retune task on unload."""
        if self._forecast_change_unsub is not None:
            try:
                self._forecast_change_unsub()
            except Exception:  # noqa: BLE001
                pass
            self._forecast_change_unsub = None
        if self._forecast_refresh_task and not self._forecast_refresh_task.done():
            self._forecast_refresh_task.cancel()
        for unsub in self._startup_retry_unsubs:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        self._startup_retry_unsubs.clear()
        if self._retune_task and not self._retune_task.done():
            self._retune_task.cancel()
        await super().async_will_remove_from_hass()

    def _schedule_retune(self, rows: list[dict[str, Any]], pv_max: float) -> None:
        """Run retune in background so startup/update path remains fast."""
        if self._retune_task and not self._retune_task.done():
            return

        async def _run_retune() -> None:
            try:
                await self._maybe_tune_params(pv_max)
                self._write_state_throttled()
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Hintergrund-Retune fehlgeschlagen: %s", err)
            finally:
                self._retune_task = None

        self._retune_task = self.hass.async_create_task(_run_retune())

    def _has_live_forecast_state(self) -> bool:
        """Return True when live forecast data is present for this entry."""
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {}).get("weather_coordinator")
        if coordinator is not None:
            forecast = (coordinator.data or {}).get("forecast", [])
            if isinstance(forecast, list) and len(forecast) > 0:
                return True

        state = self.hass.states.get(self._sensor_forecast)
        if state is None:
            return False
        forecast = state.attributes.get("forecast")
        return isinstance(forecast, list) and len(forecast) > 0

    async def _refresh_weather_coordinator(self) -> None:
        """Request an immediate weather refresh when a coordinator is available."""
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {}).get("weather_coordinator")
        if coordinator is None:
            return
        try:
            await coordinator.async_request_refresh()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Coordinator refresh before SQL update failed: %s", err)

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
                "SQL Query rebuilt with sensors: clouds=%s, pv=%s, forecast=%s, temp=%s, precip=%s",
                self._sensor_clouds, self._sensor_pv, self._sensor_forecast, self._sensor_temp, self._sensor_precip
            )
        except Exception as err:
            _LOGGER.error("Failed to rebuild SQL query: %s", err)
            self._available = False

    async def async_update(self) -> None:
        """Update the sensor only if last update was more than 5 minutes ago."""
        now = datetime.now()
        if self._update_in_progress:
            return
        if self._last_update_time is not None:
            elapsed = now - self._last_update_time
            if elapsed < timedelta(minutes=5):
                #_LOGGER.debug("SQLPVForecastSensor: Skipping update, only %s since last update", elapsed)
                return
        self._update_in_progress = True
        try:
            # Lazy-init DB engine in executor (blocking call)
            if self._engine is None:
                await self.hass.async_add_executor_job(self._init_database)

            result = await self.hass.async_add_executor_job(self._execute_query)

            if result is not None:
                options = self.config_entry.options or {}
                data = self.config_entry.data
                pv_max = float(options.get(CONF_PV_MAX_RECORD, data.get(CONF_PV_MAX_RECORD, DEFAULT_PV_MAX_RECORD)))
                today_str = now.strftime("%Y-%m-%d")
                generated_at = now.isoformat(timespec="microseconds")
                try:
                    self._raw_data = json.loads(result)
                    if isinstance(self._raw_data, dict):
                        self._raw_data["generated_for_date"] = today_str
                        self._raw_data["generated_at_local"] = generated_at
                        result = json.dumps(self._raw_data)
                    self._last_raw_result = result
                    self._last_raw_date = today_str
                    self._last_raw_generated_at = generated_at
                    _LOGGER.debug("SQL query returned %d rows, raw: %s", len(self._raw_data) if isinstance(self._raw_data, list) else 0, result[:200])
                except (ValueError, TypeError) as e:
                    _LOGGER.error("Failed to parse SQL result as JSON: %s — raw: %s", e, result[:200])
                    self._raw_data = None
                    self._last_raw_result = result
                    self._last_raw_date = today_str
                    self._last_raw_generated_at = generated_at
                # If the forecast in the SQL result is older than this HA boot, treat
                # cloud/uv as not-ready so sub-sensors keep their restored values.
                if isinstance(self._raw_data, list) and self._raw_data:
                    row0 = self._raw_data[0]
                    forecast_ts = row0.get("forecast_ts")
                    if forecast_ts is not None and float(forecast_ts) < self._runtime_start_ts:
                        if self._has_live_forecast_state():
                            _LOGGER.debug(
                                "Recorder forecast_ts %.0f is pre-runtime, but live forecast state exists; keeping readiness flags",
                                float(forecast_ts),
                            )
                        else:
                            _LOGGER.debug(
                                "Stale forecast detected (forecast_ts %.0f < runtime_start_ts %.0f) - "
                                "clearing cloud/uv readiness flags to prevent fallback values",
                                float(forecast_ts), self._runtime_start_ts,
                            )
                            for row in self._raw_data:
                                row["cloud_ready_today"] = 0
                                row["uv_ready_today"] = 0
                            result = json.dumps(self._raw_data)
                            self._last_raw_result = result
                if self._use_retune and isinstance(self._raw_data, list):
                    self._schedule_retune(self._raw_data, pv_max)
                template_params = self._retune_params
                new_val = self._apply_template(result)
                # Adaptive EMA smoothing applies only every 5 minutes to keep things simple
                # On first update after midnight bypass EMA entirely so the sensor
                # jumps immediately to the correct new-day forecast value.
                is_day_reset = self._last_ema_date is not None and self._last_ema_date != today_str
                # Also trigger day_reset if we're on a new day AND have a valid value to use
                if not is_day_reset and self._last_ema_date is None and self._attr_native_value is not None:
                    # First call of the day but we have a restored value: use it directly
                    is_day_reset = True

                # Apply EMA when value changes or after 5 minutes (sync with SQL update frequency)
                # Check if new_val is actually different from what we had before
                value_changed = False
                if new_val is not None and self._attr_native_value is not None:
                    try:
                        new_f = float(new_val)
                        old_f = float(self._attr_native_value)
                        # Consider it "changed" if difference is > 0.001 (very small threshold)
                        value_changed = abs(new_f - old_f) > 0.001
                    except (ValueError, TypeError):
                        value_changed = str(new_val) != str(self._attr_native_value)
                elif new_val is not None or self._attr_native_value is not None:
                    value_changed = True

                # Update EMA if: day reset, first time, value changed, or 5 min passed
                time_since_ema = (now - self._last_ema_time).total_seconds() if self._last_ema_time else float('inf')
                should_update_ema = (
                    is_day_reset or
                    self._last_ema_time is None or
                    value_changed or  # Update immediately when value changes
                    time_since_ema >= 300  # Failsafe: update every 5 minutes anyway
                )

                if should_update_ema:
                    self._last_ema_time = now
                    smoothed, applied_step = _apply_adaptive_ema_smoothing(
                        new_val, self._attr_native_value, pv_max,
                        day_reset=is_day_reset,
                        step_count=self._last_ema_step,
                    )
                    if new_val is not None:
                        self._attr_native_value = smoothed
                        self._last_ema_date = today_str
                        self._last_ema_step = applied_step
                    elif self._last_ema_date is None:
                        self._last_ema_date = today_str
                else:
                    # Between EMA updates, just track the date
                    if self._last_ema_date is None:
                        self._last_ema_date = today_str
                # Render Lovelace card with fresh SQL data passed as direct variable
                if self._lovelace_today_str:
                    try:
                        tmpl = Template(self._lovelace_today_str, self.hass)
                        self._lovelace_card_remaining_today = str(
                            tmpl.async_render(
                                {
                                    "raw_json": result,
                                    "pv_max_record": pv_max,
                                    "retune_params": template_params,
                                    # EMA-geglätteter Sensorwert (aktueller State nach Smoothing)
                                    "sensor_value": self._attr_native_value,
                                }
                            )
                        )
                    except Exception as lovelace_err:
                        _LOGGER.error("Failed to render lovelace_card_remaining_today: %s", lovelace_err)
                        self._lovelace_card_remaining_today = None
                if self._lovelace_tomorrow_str:
                    try:
                        tmpl = Template(self._lovelace_tomorrow_str, self.hass)
                        self._lovelace_card_tomorrow = str(
                            tmpl.async_render(
                                {
                                    "raw_json": result,
                                    "pv_max_record": pv_max,
                                    "retune_params": template_params,
                                }
                            )
                        )
                    except Exception as lovelace_err:
                        _LOGGER.error("Failed to render lovelace_card_tomorrow: %s", lovelace_err)
                        self._lovelace_card_tomorrow = None
                self._attr_available = self._attr_native_value is not None
            else:
                _LOGGER.warning("SQL query returned no rows")
                self._raw_data = None
                # Keep last value/state if we already had one.
                self._attr_available = self._attr_native_value is not None

            self._last_update_time = now

        except Exception as err:
            _LOGGER.error("Error updating sensor: %s", err)
            self._engine = None  # force reconnect next time
            # Preserve last known good value on transient runtime/DB issues.
            self._attr_available = self._attr_native_value is not None
        finally:
            self._update_in_progress = False

    def _write_state_throttled(self, *, force: bool = False) -> None:
        """Write state with a 5-minute guard and avoid sparse attrs rows."""
        now = datetime.now()
        if not force:
            if self._last_state_write_time is not None:
                elapsed = now - self._last_state_write_time
                if elapsed < timedelta(minutes=5):
                    return
            if self._last_raw_result is None:
                return
        self.async_write_ha_state()
        self._last_state_write_time = now

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
                "retune_params": self._retune_params,
            })
            rendered_text = str(rendered).strip()
            if rendered_text.lower() in ("", "none", "null", "unavailable", "unknown"):
                return None
            try:
                return float(rendered_text)
            except (ValueError, TypeError):
                return rendered_text
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
            "sensor_temp": self._sensor_temp,
            "sensor_precip": self._sensor_precip,
            "pv_history_days": self._pv_history_days,
        }
        if self._last_raw_result is not None:
            attrs["json"] = self._last_raw_result
        if self._last_raw_date is not None:
            attrs["json_generated_for_date"] = self._last_raw_date
        if self._last_raw_generated_at is not None:
            attrs["json_generated_at_local"] = self._last_raw_generated_at
        if self._lovelace_card_remaining_today is not None:
            attrs["lovelace_card_remaining_today"] = self._lovelace_card_remaining_today
        if self._lovelace_card_tomorrow is not None:
            attrs["lovelace_card_tomorrow"] = self._lovelace_card_tomorrow
        attrs["retune"] = bool(self._use_retune)
        # Keep a compact parameter set plus essential learning state.
        # These fields are restored on restart via async_added_to_hass, so chunked
        # retune can continue improving instead of restarting from scratch.
        public_retune_keys = (
            "top_n",
            "recency_amp",
            "season_exponent",
            "doy_weight",
            "uv_weight",
            "temp_weight",
            "temp_coeff",
            "precip_weight",
            "score",
            "last_retune_run_at",
            "last_retune_decision",
            "last_retune_samples",
            "last_retune_best_random_score",
            "last_retune_default_score",
            "retune_run_count",
            "retune_history",
            "retune_seed_bank",
            "last_retune_improve_method"
        )
        compact_retune_params: dict[str, Any] = {}
        for key in public_retune_keys:
            if key not in self._retune_params:
                continue
            value = self._retune_params[key]
            if key == "retune_history" and isinstance(value, list):
                sanitized_history: list[dict[str, Any]] = []
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    cleaned = dict(item)
                    cleaned.pop("objective", None)
                    sanitized_history.append(cleaned)
                value = sanitized_history
            compact_retune_params[key] = value
        attrs["retune_params"] = compact_retune_params
        attrs["retune_last_tune_day"] = self._retune_last_tune_day
        return attrs

    @staticmethod
    def _solar_factor(target_dt: datetime, item_dt: datetime, latitude: float) -> float:
        """Return season/daylength correction factor target vs historical item date."""
        target_doy = int(target_dt.strftime("%j"))
        item_doy = int(item_dt.strftime("%j"))
        lat_rad = latitude * math.pi / 180.0
        decl_t = -0.4093 * math.cos(2 * math.pi * (target_doy + 10) / 365)
        decl_i = -0.4093 * math.cos(2 * math.pi * (item_doy + 10) / 365)
        cos_ha_t = max(min(-math.tan(lat_rad) * math.tan(decl_t), 1.0), -1.0)
        cos_ha_i = max(min(-math.tan(lat_rad) * math.tan(decl_i), 1.0), -1.0)
        dl_t = 24 / math.pi * math.acos(cos_ha_t)
        dl_i = 24 / math.pi * math.acos(cos_ha_i)
        sun_t = 0.80 + 0.20 * math.cos((target_doy - 172) * 2 * math.pi / 365)
        sun_i = 0.80 + 0.20 * math.cos((item_doy - 172) * 2 * math.pi / 365)
        if sun_i <= 0 or dl_i <= 0:
            return 1.0
        return (sun_t / sun_i) * (dl_t / dl_i)


    async def _maybe_tune_params(
        self,
        pv_max: float,
    ) -> None:
        """Retest candidate parameter sets utilizing pre-parsed memory rows efficiently."""
        import copy

        # 1. ABSOLUTE ABSICHERUNG: Alle Kernvariablen als allererste Aktion deklarieren!
        best_params: dict[str, Any] | None = None
        day_key = datetime.now().strftime("%Y-%m-%d")
        retune_ran_at: str | None = None

        self._retune_params = _merge_retune_param_defaults(
            self._retune_params,
            copy.deepcopy(self._retune_base_params),
        )

        # 2. Gedächtnis-Laden sofort sichern
        history = self._retune_params.get("retune_history", [])
        if not isinstance(history, list):
            history = []

        prior_bank = self._retune_params.get("retune_seed_bank", [])
        if not isinstance(prior_bank, list):
            prior_bank = []

        # Tägliche Zirkulations-Sperre (Greift erst nach der Variablen-Sicherung fehlerfrei)
        if self._retune_last_tune_day == day_key and self._retune_params:
            return


        summary_list = []

        # 1. VERSUCH: Wir prüfen, ob im globalen _raw_data Array des Sensors die echten LTS-Tage liegen
        if hasattr(self, "_raw_data") and isinstance(self._raw_data, list) and len(self._raw_data) >= 10:
            summary_list = self._raw_data
            _LOGGER.info("Retune-Datenbasis: Nutze %d intakte LTS-Tage direkt aus dem Sensor-RAM.", len(summary_list))

        # 2. VERSUCH: Fallback auf das geparste JSON, falls das RAM-Array leer ist
        elif self._last_raw_result:
            try:
                full_data = json.loads(self._last_raw_result)
                raw_summary = full_data.get("daily_summary", [])
                summary_list = json.loads(raw_summary) if isinstance(raw_summary, str) else raw_summary
                _LOGGER.info("Retune-Datenbasis: Fallback auf %d Tage aus JSON-Cache.", len(summary_list) if summary_list else 0)
            except Exception as json_err:
                _LOGGER.error("Failed to parse fallback raw result: %s", json_err)
                return
        else:
            _LOGGER.warning("Retune skipped: No data available in memory or cache")
            return

        if not isinstance(summary_list, list):
            summary_list = []

        # 4. Daten-Pool für den Optimierer vorbereiten (Strikter Float-Zwang)
        samples: list[dict[str, Any]] = []
        for item in summary_list:
            if not isinstance(item, dict):
                continue

            raw_date = item.get("day", item.get("date", ""))
            try:
                dt = datetime.strptime(str(raw_date), "%Y-%m-%d")
            except Exception:
                continue

            if dt.date() >= datetime.now().date():
                continue

            pool_item_total = item.get("total", {})
            if not isinstance(pool_item_total, dict):
                continue

            try:
                pv_yield_val = float(pool_item_total.get("pv_yield", 0.0))
                cloud_val    = float(pool_item_total.get("cloud", 0.0))
                uv_val       = float(pool_item_total.get("uv", 0.0))
                temp_val     = float(pool_item_total.get("temp", 15.0))
                precip_val   = float(pool_item_total.get("precip", 0.0))
            except (TypeError, ValueError):
                continue

            if pv_yield_val <= 0.1:
                continue

            samples.append({
                "day": raw_date,
                "_dt": dt,
                "total": {
                    "pv_yield": pv_yield_val,
                    "cloud": cloud_val,
                    "uv": uv_val,
                    "temp": temp_val,
                    "precip": precip_val
                }
            })
        #_LOGGER.warning("Retune Pool: %d historical days successfully extracted for optimization", len(samples))
        _LOGGER.info("Retune Pool: %d historical days successfully extracted for optimization", len(samples))

        if len(samples) < 10:
            _LOGGER.debug("Retune skipped: Insufficient historical days found (%d)", len(samples))
            return

        # 5. Aufruf des mathematischen Optimierers im Executor-Thread
        latitude = float(self.hass.config.latitude)
        best_params = await self.hass.async_add_executor_job(
            self._compute_best_retune_params,
            samples,
            pv_max,
            latitude,
            self._retune_params
        )

        retune_ran_at = _utcnow_iso()

        # Vorzeitiger Abbruch greift nun felsenfest ohne jeglichen NameError!
        if best_params is None:
            self._retune_params["last_retune_run_at"] = retune_ran_at
            self._retune_params["last_retune_decision"] = "no_candidate"
            self._retune_params["last_retune_samples"] = len(samples)
            self._retune_params["retune_history"] = history
            self._retune_last_tune_day = day_key
            return

        # Metadaten für das Protokoll aus den Berechnungen ziehen
        chunk_total = int(best_params.get("chunk_total", 3))
        chunk_keys = str(best_params.get("chunk_keys", ""))
        probe_key = str(best_params.get("probe_key", ""))
        try:
            candidate_score = float(best_params.get("score_raw", best_params.get("score", float("inf"))))
        except (TypeError, ValueError):
            candidate_score = float("inf")
        candidate_has_score = "score" in best_params and math.isfinite(candidate_score)

        # 6. MATHEMATISCHE VARIABLEN SICHER VORAB DEKLARIEREN
        old_score = float(self._retune_params.get("score", 1.0))
        new_score = float(best_params.get("score", 0.0))
        ref_score_fresh = float(best_params.get("baseline_score", old_score))



        # Fehler des Werkstandards für den Notausstieg ermitteln
        try:
            default_score = ref_score_fresh
            if "last_retune_default_score" in best_params:
                default_score = float(best_params["last_retune_default_score"])
        except (TypeError, ValueError):
            default_score = float("inf")


        # 1. Bank-Auswahl auswerten
        refreshed_bank_entries = best_params.get("bank_seed_refresh")
        best_refreshed_bank = None
        best_refreshed_bank_score = float("inf")
        bank_cmp_epsilon = 1e-5

        if isinstance(refreshed_bank_entries, list):
            for item in refreshed_bank_entries:
                if not isinstance(item, dict):
                    continue
                try:
                    item_score = float(item.get("score", float("inf")))
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(item_score) or item_score <= 0.0:
                    continue

                if item_score < best_refreshed_bank_score:
                    best_refreshed_bank_score = item_score
                    best_refreshed_bank = item

        # Der beste neue Score kommt direkt aus der Spitze der aktuellen Optimierungsrunde
        last_retune_best_random_score = round(float(best_params.get("last_retune_best_random_score", 0.0)), 5)

        # Der Default-Score kommt EXKLUSIV aus dem separat berechneten Werks-Feld der Engine
        last_retune_default_score = round(float(best_params.get("last_retune_default_score", 1.0)), 5)
        default_score = last_retune_default_score

        # 3. Dreistufige Entscheidung: Wer liefert das absolute Fehler-Minimum?

        # PFAD A: Die Samenbank hat das historische Minimum
        if best_refreshed_bank is not None and (
            (not candidate_has_score) or (best_refreshed_bank_score <= (candidate_score - bank_cmp_epsilon))
        ) and (best_refreshed_bank_score <= (default_score - bank_cmp_epsilon)):

            # Wir bauen ein frisches Dictionary, das KEINE alten Keys der Engine mitschleppt!
            best_params = {
                "top_n": int(best_refreshed_bank.get("top_n", 10)),
                "recency_amp": float(best_refreshed_bank.get("recency_amp", 0.21)),
                "season_exponent": float(best_refreshed_bank.get("season_exponent", 1.00)),
                "doy_weight": float(best_refreshed_bank.get("doy_weight", 0.05)),
                "uv_weight": float(best_refreshed_bank.get("uv_weight", 1.0)),
                "temp_weight": float(best_refreshed_bank.get("temp_weight", 0.7)),
                "temp_coeff": float(best_refreshed_bank.get("temp_coeff", -0.003)),
                "precip_weight": float(best_refreshed_bank.get("precip_weight", 0.0015)),
                "score": round(float(best_refreshed_bank_score), 5),
                "baseline_score": round(float(ref_score_fresh), 5),
                "retune_selected_source": "bank_best",
                "chunk_total": chunk_total,
                "chunk_keys": chunk_keys,
                "probe_key": probe_key
            }

        # PFAD C: Der Optimizer hat regulär gewonnen
        else:
            # Wir behalten best_params, fügen aber NUR die Herkunft hinzu
            best_params["retune_selected_source"] = "optimizer"
        # 8. Physische Verifizierung und Live-Schaltung des Gewinns
        new_score = round(float(best_params.get("score", 0.0)), 5)

        self._retune_params["top_n"] = int(best_params["top_n"])
        self._retune_params["recency_amp"] = round(float(best_params["recency_amp"]), 5)
        self._retune_params["season_exponent"] = round(float(best_params["season_exponent"]), 5)
        self._retune_params["doy_weight"] = round(float(best_params["doy_weight"]), 5)
        self._retune_params["uv_weight"] = round(float(best_params["uv_weight"]), 5)
        self._retune_params["temp_weight"] = round(float(best_params["temp_weight"]), 5)
        self._retune_params["temp_coeff"] = round(float(best_params["temp_coeff"]), 6)
        self._retune_params["precip_weight"] = round(float(best_params["precip_weight"]), 5)
        self._retune_params["score"] = round(new_score, 5)


        # 7. Historien-Eintrag schreiben
        new_history_entry = {
            "run_at": retune_ran_at,
            "best_random_score": last_retune_best_random_score,
            "score": round(new_score, 5),
            "score_delta": round(old_score - new_score, 5),
            "improve_method": best_params.get("improve_method", "none"),
            "top_n": int(best_params.get("top_n", 15)),
            "recency_amp": round(float(best_params.get("recency_amp", 0.30)), 5),
            "season_exponent": round(float(best_params.get("season_exponent", 1.00)), 5),
            "doy_weight": round(float(best_params.get("doy_weight", 0.05)), 5),
            "uv_weight": round(float(best_params.get("uv_weight", 1.0)), 5),
            "temp_weight": round(float(best_params.get("temp_weight", 0.08)), 5),
            "temp_coeff": round(float(best_params.get("temp_coeff", -0.003)), 6),
            "precip_weight": round(float(best_params.get("precip_weight", 0.0015)), 5),
        }
        history.append(new_history_entry)
        self._retune_params["retune_history"] = history[-10:]

        # 10. Samenbank-Akkumulation: Reines Verschmelzen ohne künstliche Filter-Barrieren!
        refreshed_bank_entries = best_params.get("bank_seed_refresh", [])
        if not isinstance(refreshed_bank_entries, list):
            refreshed_bank_entries = []

        current_run_seed = {
            "score": round(float(best_params.get("score", 0.0)), 5),
            "top_n": int(best_params.get("top_n", 15)),
            "recency_amp": round(float(best_params.get("recency_amp", 0.30)), 5),
            "season_exponent": round(float(best_params.get("season_exponent", 1.00)), 5),
            "doy_weight": round(float(best_params.get("doy_weight", 0.05)), 5),
            "uv_weight": round(float(best_params.get("uv_weight", 1.0)), 5),
            "temp_weight": round(float(best_params.get("temp_weight", 0.08)), 5),
            "temp_coeff": round(float(best_params.get("temp_coeff", -0.003)), 6),
            "precip_weight": round(float(best_params.get("precip_weight", 0.0015)), 5),
        }

        aggregated_bank = [current_run_seed] + refreshed_bank_entries + prior_bank
        unique_bank = []
        seen_tuples = set()

        for seed in aggregated_bank:
            if not isinstance(seed, dict):
                continue

            tup = (int(seed.get("top_n", 15)), round(float(seed.get("recency_amp", 0.3)), 5))
            if tup not in seen_tuples:
                seen_tuples.add(tup)
                cleaned_seed = dict(seed)
                cleaned_seed["score"] = round(float(seed.get("score", 0.0)), 5)
                cleaned_seed.pop("score_raw", None)
                unique_bank.append(cleaned_seed)

        unique_bank.sort(key=lambda x: x.get("score", float("inf")))
        self._retune_params["retune_seed_bank"] = unique_bank[:8]

        # 11. Zustands-Attribute dauerhaft zurückschreibencursor
        self._retune_params["last_retune_run_at"] = retune_ran_at
        self._retune_params["last_retune_samples"] = len(samples)

        # Schreibt Deine Live-Kontrollmetriken sauber und tagesaktuell weg
        self._retune_params["last_retune_best_random_score"] = last_retune_best_random_score
        self._retune_params["last_retune_default_score"] = last_retune_default_score
        self._retune_params["last_retune_bank_best_score"] = round(float(best_refreshed_bank_score), 5) if best_refreshed_bank else float("inf")
        self._retune_params["last_retune_improve_method"] = best_params.get("improve_method", "none")

        self._retune_last_tune_day = day_key

    async def async_manual_retune(self, *, force_refresh: bool = True) -> bool:
        """Run retune once on-demand from GUI/service."""
        if not self._use_retune:
            return False

        if force_refresh:
            self._last_update_time = None
            await self.async_update()

        options = self.config_entry.options or {}
        data = self.config_entry.data
        pv_max = float(options.get(CONF_PV_MAX_RECORD, data.get(CONF_PV_MAX_RECORD, DEFAULT_PV_MAX_RECORD)))

        prev_day_key = self._retune_last_tune_day
        try:
            self._retune_last_tune_day = None
            await self._maybe_tune_params( pv_max)
        finally:
            if self._retune_last_tune_day is None:
                self._retune_last_tune_day = prev_day_key

        self._write_state_throttled(force=True)
        return bool(self._retune_params.get("last_retune_run_at"))

    @staticmethod
    def _compute_best_retune_params(
        samples: list[dict[str, Any]],
        pv_max: float,
        latitude: float,
        initial_params: dict[str, Any]
    ) -> dict[str, Any] | None:

        if not samples:
            return None

        try:
            run_count = int(initial_params.get("retune_run_count", 0) or 0)
        except (TypeError, ValueError):
            run_count = 0

        import random as system_random
        rng = system_random

        key_chunks = [
            ["top_n", "recency_amp","season_exponent","doy_weight"],
            ["uv_weight", "temp_weight", "precip_weight", "temp_coeff"],
        ]

        def _clamp(v: float, lo: float, hi: float) -> float:
            return min(max(v, lo), hi)

        # Bounds mathematisch sauber absteigend sortiert von Negativ bis Null
        bounds = {
            "top_n": (10.0, 15.0),
            "recency_amp": (0.00, 0.60),
            "season_exponent": (0.0, 2.650),
            "doy_weight": (0, 0.5),
            "uv_weight": (0.00, 2.50),
            "temp_weight": (0.00, 5.00),
            "temp_coeff": (-0.01, -0.0025),
            "precip_weight": (2.0000, 4.00),
        }

        current = {
            "top_n": int(_clamp(float(initial_params.get("top_n", 15)), bounds["top_n"][0], bounds["top_n"][1])),
            "recency_amp": _clamp(float(initial_params.get("recency_amp", 0.30)), bounds["recency_amp"][0], bounds["recency_amp"][1]),
            "season_exponent": _clamp(float(initial_params.get("season_exponent", 1.00)), bounds["season_exponent"][0], bounds["season_exponent"][1]),
            "doy_weight": _clamp(float(initial_params.get("doy_weight", 1.00)), bounds["doy_weight"][0], bounds["doy_weight"][1]),
            "uv_weight": _clamp(float(initial_params.get("uv_weight", 1.0)), bounds["uv_weight"][0], bounds["uv_weight"][1]),
            "temp_weight": _clamp(float(initial_params.get("temp_weight", 0.08)), bounds["temp_weight"][0], bounds["temp_weight"][1]),
            "temp_coeff": _clamp(float(initial_params.get("temp_coeff", -0.003)), bounds["temp_coeff"][0], bounds["temp_coeff"][1]),
            "precip_weight": _clamp(float(initial_params.get("precip_weight", 0.0015)), bounds["precip_weight"][0], bounds["precip_weight"][1]),
        }

        def _score_vectorized_all(params_list: list[dict[str, Any]]) -> list[float]:
            import numpy as np
            num_samples = len(samples)
            actuals = np.array([float(s["total"]["pv_yield"]) for s in samples])
            valid_target_mask = actuals > 1.0
            num_targets = np.sum(valid_target_mask)
            if num_targets == 0:
                return [float("inf")] * len(params_list)

            orig_target_indices = np.where(valid_target_mask)[0]
            cloud = np.array([float(s["total"]["cloud"]) for s in samples])
            uv = np.array([float(s["total"]["uv"]) for s in samples])
            temp = np.array([float(s["total"]["temp"]) for s in samples])
            precip = np.array([float(s["total"]["precip"]) for s in samples])
            pv_yield = np.array([float(s["total"]["pv_yield"]) for s in samples])

            doys = np.zeros(num_samples)
            cos_doys = np.zeros(num_samples)
            dls = np.zeros(num_samples)
            pi_val = 3.141592653589793
            for i, s in enumerate(samples):
                doy = int(s["_dt"].strftime("%j"))
                doys[i] = doy
                cos_doys[i] = math.cos((doy - 172) * 2 * pi_val / 365)
                dls[i] = 12.0 + 4.0 * (latitude / 50.0) * cos_doys[i]

            orig_t_grid, i_grid = np.meshgrid(orig_target_indices, np.arange(num_samples), indexing='ij')
            valid_pair_mask = (orig_t_grid != i_grid)

            t_dt = [samples[idx]["_dt"] for idx in orig_target_indices]
            p_dt = [samples[idx]["_dt"] for idx in range(num_samples)]
            days_ago_mat = np.zeros((num_targets, num_samples))
            for t_idx, t_val in enumerate(t_dt):
                for i_idx, p_val in enumerate(p_dt):
                    days_ago_mat[t_idx, i_idx] = max(0, (t_val - p_val).days)

            doy_diff_mat = np.abs(doys[orig_target_indices][:, np.newaxis] - doys[np.newaxis, :])
            doy_diff_mat = np.where(doy_diff_mat > 182.5, 365.0 - doy_diff_mat, doy_diff_mat)

            ratio_mat = dls[orig_target_indices][:, np.newaxis] / np.maximum(dls[np.newaxis, :], 1e-9)
            diff_c_mat = np.abs(cloud[np.newaxis, :] - cloud[orig_target_indices][:, np.newaxis])

            t_uv = uv[orig_target_indices][:, np.newaxis]
            t_cloud = cloud[orig_target_indices][:, np.newaxis]
            t_temp = temp[orig_target_indices][:, np.newaxis]
            t_precip = precip[orig_target_indices][:, np.newaxis]

            p_uv = uv[np.newaxis, :]
            p_temp = temp[np.newaxis, :]
            p_precip = precip[np.newaxis, :]
            p_yield = pv_yield[np.newaxis, :]

            uv_w = np.minimum(0.3 + 0.4 * (t_cloud / 100.0), 0.7)
            diff_c_weighted = diff_c_mat * (1.0 - uv_w)
            diff_uv_term = np.abs(p_uv - t_uv) * 6.0 * uv_w

            target_actuals = actuals[orig_target_indices]
            total_actual_yield = np.sum(target_actuals)

            res_scores = []
            for params in params_list:
                display_top_n = int(params.get("top_n", 15))
                uv_weight = float(params.get("uv_weight", 1.0))
                temp_weight = float(params.get("temp_weight", 0.08))
                precip_weight = float(params.get("precip_weight", 0.03))
                temp_coeff = float(params.get("temp_coeff", -0.003))
                recency_amp = float(params.get("recency_amp", 0.30))
                season_exponent = float(params.get("season_exponent", 1.0))
                doy_weight_val = float(params.get("doy_weight", 0.05))

                s_korr = np.minimum(ratio_mat ** season_exponent, 1.35)
                y_korr = p_yield * s_korr

                temp_factor = np.clip(1.0 + (t_temp - p_temp) * temp_coeff, 0.85, 1.15)
                y_korr = y_korr * temp_factor

                if pv_max > 0:
                    y_korr = np.minimum(y_korr, pv_max)

                diff_with_uv = diff_c_weighted + diff_uv_term * uv_weight
                diff = np.where(t_uv > 0, diff_with_uv, diff_c_mat)

                diff = diff + np.abs(t_temp - p_temp) * temp_weight
                diff = diff + np.abs(p_precip - t_precip) * precip_weight
                diff = diff + doy_diff_mat * doy_weight_val

                w_calc = (1.0 / np.maximum(diff * 0.5, 0.1)) * (1.0 + recency_amp * np.maximum(1.0 - days_ago_mat / 30.0, 0.0))
                w_calc = np.where(valid_pair_mask, w_calc, -1e9)

                sort_indices = np.argsort(w_calc, axis=1)[:, ::-1]
                top_n_indices = sort_indices[:, :display_top_n]

                row_indices = np.arange(num_targets)[:, np.newaxis]
                top_w = w_calc[row_indices, top_n_indices]
                top_y = y_korr[row_indices, top_n_indices]

                sum_w = np.sum(top_w, axis=1)
                pred = np.sum(top_y * top_w, axis=1) / np.maximum(sum_w, 1e-9)

                if pv_max > 0:
                    pred = np.minimum(pred, pv_max)
                pred = np.maximum(pred, 0.0)

                abs_err = np.abs(pred - target_actuals)
                score_val = np.sum(abs_err) / total_actual_yield if total_actual_yield > 0 else float("inf")
                res_scores.append(score_val)

            return res_scores


        # --- SUCHE UND OPTIMIERUNG ---
        best_params = dict(current)
        try:
            best_score = _score_vectorized_all([best_params])[0]
        except Exception as vectorized_err:
            _LOGGER.error("Matrix operations failed initially: %s. Falling back to RETUNE_BASE_PARAMS.", vectorized_err)
            return copy.deepcopy(RETUNE_BASE_PARAMS)

        improve_method = "kept"

        # 1. MUTATIONS-EXPLORATION: Zufällige Werte testen (Vectorized via NumPy):
        best_random_score = float(1.0)

        use_numpy = False
        try:
            import numpy as np
            use_numpy = True
        except ImportError:
            pass

        if use_numpy:
            n_samples = len(samples)
            # Dynamically scales mutations-exploration so that a low-power single-core
            # CPU (e.g. Raspberry Pi 3/4) takes approximately 60 seconds.
            # Empirical calibration:
            # - N=30 -> 98,304 seeds (~60s on low-power single-core)
            # - N=365 -> 664 seeds (~60s on low-power single-core)
            # - We enforce a floor of 256 seeds and a ceiling of 131,072 seeds.
            num_seeds = min(131072, max(256, int(88473600 / (n_samples ** 2))))
        else:
            num_seeds = 256
        seed_params_list = []
        for i in range(num_seeds):
            if i == 0:
                # Always include the exact current parameter set as the first candidate
                seed_params = dict(current)
            elif i % 2 == 0:
                # GLOBAL EXPLORATION: Generate parameters uniformly distributed across the bounds
                seed_params = {
                    "top_n": int(rng.randint(int(bounds["top_n"][0]), int(bounds["top_n"][1]))),
                    "recency_amp": rng.uniform(bounds["recency_amp"][0], bounds["recency_amp"][1]),
                    "season_exponent": rng.uniform(bounds["season_exponent"][0], bounds["season_exponent"][1]),
                    "doy_weight": rng.uniform(bounds["doy_weight"][0], bounds["doy_weight"][1]),
                    "uv_weight": rng.uniform(bounds["uv_weight"][0], bounds["uv_weight"][1]),
                    "temp_weight": rng.uniform(bounds["temp_weight"][0], bounds["temp_weight"][1]),
                    "temp_coeff": rng.uniform(bounds["temp_coeff"][0], bounds["temp_coeff"][1]),
                    "precip_weight": rng.uniform(bounds["precip_weight"][0], bounds["precip_weight"][1])
                }
            else:
                # LOCAL EXPLOITATION (Finetuning): Mutate parameters around current with smaller perturbations
                seed_params = {
                    "top_n": int(_clamp(current["top_n"] + rng.randint(-1, 1), bounds["top_n"][0], bounds["top_n"][1])),
                    "recency_amp": _clamp(current["recency_amp"] + rng.uniform(-0.10, 0.10), bounds["recency_amp"][0], bounds["recency_amp"][1]),
                    "season_exponent": _clamp(current["season_exponent"] + rng.uniform(-0.15, 0.15), bounds["season_exponent"][0], bounds["season_exponent"][1]),
                    "doy_weight": _clamp(current["doy_weight"] + rng.uniform(-0.02, 0.02), bounds["doy_weight"][0], bounds["doy_weight"][1]),
                    "uv_weight": _clamp(current["uv_weight"] + rng.uniform(-0.30, 0.30), bounds["uv_weight"][0], bounds["uv_weight"][1]),
                    "temp_weight": _clamp(current["temp_weight"] + rng.uniform(-0.30, 0.30), bounds["temp_weight"][0], bounds["temp_weight"][1]),
                    "temp_coeff": _clamp(current["temp_coeff"] + rng.uniform(-0.001, 0.001), bounds["temp_coeff"][0], bounds["temp_coeff"][1]),
                    "precip_weight": _clamp(current["precip_weight"] + rng.uniform(-0.30, 0.30), bounds["precip_weight"][0], bounds["precip_weight"][1])
                }
            seed_params_list.append(seed_params)

        best_seed_index = 0
        if use_numpy:
            try:
                seed_scores = _score_vectorized_all(seed_params_list)
                for idx, (seed_params, seed_score) in enumerate(zip(seed_params_list, seed_scores)):
                    if seed_score < best_random_score:
                        best_random_score = seed_score
                        best_seed_params = seed_params
                        best_seed_index = idx
            except Exception as vectorized_err:
                _LOGGER.error("Matrix operations failed for mutations-exploration: %s. Falling back to RETUNE_BASE_PARAMS.", vectorized_err)
                return copy.deepcopy(RETUNE_BASE_PARAMS)
        else:
            _LOGGER.error("NumPy is not available. Falling back to RETUNE_BASE_PARAMS.")
            return copy.deepcopy(RETUNE_BASE_PARAMS)

        if best_random_score < best_score:
            best_score = best_random_score
            best_params = best_seed_params
            if best_seed_index == 0:
                improve_method = "kept"
            elif best_seed_index % 2 == 0:
                improve_method = "global"
            else:
                improve_method = "local"

        #_LOGGER.warning(f"Bester Zufallsscore: {seed_score } | Parameters: {best_seed_params}")

        # 3. Bank-Refresh
        prior_bank = initial_params.get("retune_seed_bank", [])
        refreshed_bank_list = []
        best_bank_score = float("inf")

        if isinstance(prior_bank, list) and prior_bank:
            best_bank_score = float(1.0)
            bank_candidates = []
            for item in prior_bank:
                if not isinstance(item, dict):
                    continue
                test_seed = {
                    "top_n": int(item.get("top_n", 15)),
                    "recency_amp": float(item.get("recency_amp", 0.30)),
                    "season_exponent": float(item.get("season_exponent", 1.00)),
                    "doy_weight": float(item.get("doy_weight", 0.05)),
                    "uv_weight": float(item.get("uv_weight", 1.0)),
                    "temp_weight": float(item.get("temp_weight", 0.08)),
                    "temp_coeff": float(item.get("temp_coeff", item.get("temp_coeff", -0.003))),
                    "precip_weight": float(item.get("precip_weight", 0.0015)),
                }
                bank_candidates.append(test_seed)

            try:
                refreshed_scores = _score_vectorized_all(bank_candidates)
                for test_seed, refreshed_score in zip(bank_candidates, refreshed_scores):
                    if math.isfinite(refreshed_score) and refreshed_score > 0:
                        test_seed["score"] = round(float(refreshed_score), 5)
                        refreshed_bank_list.append(test_seed)
                        if refreshed_score < (best_bank_score - 1e-5):
                            best_bank_score = refreshed_score
                            best_bank_params = test_seed
            except Exception as vectorized_err:
                _LOGGER.error("Matrix operations failed for bank-refresh: %s. Falling back to RETUNE_BASE_PARAMS.", vectorized_err)
                return copy.deepcopy(RETUNE_BASE_PARAMS)

        if best_bank_score < (best_score - 1e-5):
            best_score = best_bank_score
            best_params = best_bank_params
            improve_method = improve_method + "/bank"



        #_LOGGER.warning(f"Bank-test: {best_bank_score } | Parameters: {best_bank_params}")

        import copy
        factory_defaults = copy.deepcopy(RETUNE_BASE_PARAMS)

        # Zwingt alle Integer-Werte des Tabellenkopfs in sichere Python-Floats um
        cleaned_defaults = {
            "top_n": int(factory_defaults.get("top_n", 10)),
            "recency_amp": float(factory_defaults.get("recency_amp", 0.21)),
            "season_exponent": float(factory_defaults.get("season_exponent", 1.00)),
            "doy_weight": float(factory_defaults.get("doy_weight", 0.05)),
            "uv_weight": float(factory_defaults.get("uv_weight", 1.0)),
            "temp_weight": float(factory_defaults.get("temp_weight", 0.7)),
            "temp_coeff": float(factory_defaults.get("temp_coeff", -0.003)),
            "precip_weight": float(factory_defaults.get("precip_weight", 0.0015)),
        }

        try:
            true_factory_default_score = round(_score_vectorized_all([cleaned_defaults])[0], 5)
        except Exception as vectorized_err:
            _LOGGER.error("Matrix operations failed for default score: %s. Falling back to RETUNE_BASE_PARAMS.", vectorized_err)
            return copy.deepcopy(RETUNE_BASE_PARAMS)

        #_LOGGER.warning(f"Default: {true_factory_default_score } | Parameters: {cleaned_defaults}")


        if true_factory_default_score < (best_score - 1e-5):
            best_score = true_factory_default_score
            best_params = cleaned_defaults
            improve_method = improve_method + "factory_params"

        # 5. Rückgabe-Dictionary für den HA-Zustandsmanager
        return {
            "top_n": int(best_params["top_n"]),
            "recency_amp": round(float(best_params["recency_amp"]), 4),
            "season_exponent": round(float(best_params["season_exponent"]), 4),
            "doy_weight": round(float(best_params["doy_weight"]), 5),
            "uv_weight": round(float(best_params["uv_weight"]), 4),
            "temp_weight": round(float(best_params["temp_weight"]), 4),
            "temp_coeff": round(float(best_params["temp_coeff"]), 6),
            "precip_weight": round(float(best_params["precip_weight"]), 5),
            "score": round(best_score, 5),
            "baseline_score": round(true_factory_default_score, 5),
            "last_retune_default_score": true_factory_default_score,
            "last_retune_best_bank_score": best_bank_score,
            "last_retune_best_random_score": best_random_score,
            "retune_run_count": run_count + 1,
            "samples_evaluated": len(samples),
            "chunk_total": len(key_chunks),
            "bank_seed_refresh": refreshed_bank_list,
            "improve_method": improve_method
        }

class PVForecastTemplateSensor(SensorEntity, RestoreEntity):
    """Derived PV forecast sensor.

    Reads the raw SQL JSON cached by the main SQLPVForecastSensor and applies
    a dedicated Jinja2 template (min / max / tomorrow) without running a
    second SQL query.
    """

    _attr_icon = "mdi:solar-panel"
    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = "energy"
    _attr_state_class = None

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        main_entity_id: str,
        name: str,
        value_template: str,
        throttle_minutes: int = 0,
        no_ema: bool = False,
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
        # Date tracking for EMA day-reset (only used when no_ema is False).
        self._last_ema_date: str | None = None
        # Signed step count from the last EMA smoothing iteration.
        # Positive → moving down (toward lower values); negative → moving up.
        self._last_ema_step: int = 0
        # Time of the last EMA smoothing call (used to scale alpha if updates
        # happen more frequently than 5 minutes).
        self._last_ema_time: datetime | None = None
        # Optional update throttle (0 = no throttle).
        self._throttle_minutes: int = throttle_minutes
        self._last_update_time: datetime | None = None
        self._last_processed_main_json_stamp: str | None = None
        # When True, EMA smoothing is completely skipped – sensor uses raw template value.
        self._no_ema: bool = no_ema
        self.entity_id = generate_entity_id("sensor.{}", name, hass=hass)

    async def async_added_to_hass(self) -> None:
        """Restore last known value so reboot gaps don't flip to unavailable."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        if last_state.state in ("unknown", "unavailable", "", None):
            return
        try:
            self._attr_native_value = float(last_state.state)
        except (TypeError, ValueError):
            self._attr_native_value = last_state.state
        self._attr_available = True

    async def async_update(self) -> None:
        """Update by reading raw JSON from the main sensor's attributes."""
        now = datetime.now()
        main_state = self.hass.states.get(self._main_entity_id)
        if main_state is None or not main_state.attributes.get("json"):
            # Keep last method while main sensor is transiently unavailable.
            self._attr_available = self._attr_native_value is not None
            return
        main_json_stamp = str(
            main_state.attributes.get("json_generated_at_local")
            or main_state.attributes.get("json_generated_for_date")
            or ""
        )
        is_new_main_json = main_json_stamp != self._last_processed_main_json_stamp
        # Keep the 5-minute cadence, but let derived sensors sync immediately
        # when the main sensor has produced a fresh SQL/JSON cycle.
        if (
            self._throttle_minutes > 0
            and self._last_update_time is not None
            and not is_new_main_json
            and (now - self._last_update_time) < timedelta(minutes=self._throttle_minutes)
        ):
            return
        now_dt = datetime.now()
        today_str = now_dt.strftime("%Y-%m-%d")
        raw_date = main_state.attributes.get("json_generated_for_date")
        raw_is_current_day = raw_date == today_str
        is_day_reset = self._last_ema_date is not None and self._last_ema_date != today_str
        if not is_day_reset and self._last_ema_date is None and self._attr_native_value is not None:
            is_day_reset = True
        if is_day_reset and not raw_is_current_day:
            self._attr_available = self._attr_native_value is not None
            return
        raw = main_state.attributes["json"]
        new_val = self._apply_template(raw)

        if self._no_ema:
            # No EMA smoothing – use raw template value directly.
            if new_val is not None:
                self._attr_native_value = new_val
            self._attr_available = self._attr_native_value is not None
            self._last_update_time = now
            self._last_processed_main_json_stamp = main_json_stamp
            return

        # Apply the same adaptive EMA smoothing used by the main remaining sensor
        # so that remaining_today_min and remaining_today_max never jump abruptly.
        options = self.config_entry.options or {}
        data = self.config_entry.data
        pv_max = float(options.get(CONF_PV_MAX_RECORD, data.get(CONF_PV_MAX_RECORD, DEFAULT_PV_MAX_RECORD)))

        today_str = now_dt.strftime("%Y-%m-%d")
        raw_date = main_state.attributes.get("json_generated_for_date")
        raw_is_current_day = raw_date == today_str

        # Determine day reset condition
        is_day_reset = self._last_ema_date is not None and self._last_ema_date != today_str
        if not is_day_reset and self._last_ema_date is None and self._attr_native_value is not None:
            is_day_reset = True
        if is_day_reset and not raw_is_current_day:
            self._attr_available = self._attr_native_value is not None
            return

        # OBERSTES GEBOT FÜR DEN TAGESWECHSEL:
        # Egal wann das erste Update nach Mitternacht kommt (0:00, 0:05 oder 0:10):
        # Der neue Wert wird SOFORT ungeglättet übernommen und der EMA-Schritt zurückgesetzt.
        if is_day_reset and new_val is not None:
            self._attr_native_value = new_val
            self._last_ema_date = today_str
            self._last_ema_time = now_dt
            self._last_ema_step = 0  # Wichtig für _apply_adaptive_ema_smoothing beim nächsten Mal
            self._attr_available = True
            self._last_update_time = now
            self._last_processed_main_json_stamp = main_json_stamp
            return

        # Apply EMA when value changes or after 5 minutes (sync with SQL update frequency)
        value_changed = False
        if new_val is not None and self._attr_native_value is not None:
            try:
                new_f = float(new_val)
                old_f = float(self._attr_native_value)
                value_changed = abs(new_f - old_f) > 0.001
            except (ValueError, TypeError):
                value_changed = str(new_val) != str(self._attr_native_value)
        elif new_val is not None or self._attr_native_value is not None:
            value_changed = True

        # Update EMA if: first time, value changed, or 5 min passed
        # (is_day_reset wurde oben bereits abgefangen und per return beendet)
        time_since_ema = (now_dt - self._last_ema_time).total_seconds() if self._last_ema_time else float('inf')
        should_update_ema = (
            self._last_ema_time is None or
            value_changed or
            time_since_ema >= 300
        )

        if should_update_ema:
            self._last_ema_time = now_dt
            smoothed, applied_step = _apply_adaptive_ema_smoothing(
                new_val, self._attr_native_value, pv_max,
                day_reset=False, # Immer False, da True oben abgefangen wurde
                step_count=self._last_ema_step,
            )
            if new_val is not None:
                self._attr_native_value = smoothed
                self._last_ema_date = today_str
                self._last_ema_step = applied_step
            elif self._last_ema_date is None:
                self._last_ema_date = today_str
        else:
            if self._last_ema_date is None:
                self._last_ema_date = today_str

        self._attr_available = self._attr_native_value is not None
        self._last_update_time = now
        self._last_processed_main_json_stamp = main_json_stamp

    def _apply_template(self, raw_value: str) -> float | str | None:
        """Apply value template with latitude variable."""
        try:
            template = Template(self._value_template_str, self.hass)
            options = self.config_entry.options or {}
            data = self.config_entry.data
            pv_max = float(options.get(CONF_PV_MAX_RECORD, data.get(CONF_PV_MAX_RECORD, DEFAULT_PV_MAX_RECORD)))
            main_state = self.hass.states.get(self._main_entity_id)
            retune_params = main_state.attributes.get("retune_params", {}) if main_state is not None else {}
            rendered = template.async_render({
                "value": raw_value,
                "latitude": self.hass.config.latitude,
                "pv_max_record": pv_max,
                "retune_params": retune_params,
            })
            rendered_text = str(rendered).strip()
            if rendered_text.lower() in ("", "none", "null", "unavailable", "unknown"):
                return None
            try:
                return float(rendered_text)
            except (ValueError, TypeError):
                return rendered_text
        except Exception as err:
            _LOGGER.error("Failed to apply template for %s: %s", self._attr_name, err)
            return None

    @property
    def should_poll(self) -> bool:
        return True

    @property
    def update_interval(self) -> timedelta | None:
        return timedelta(minutes=15)


class ForecastMethodSensor(SensorEntity, RestoreEntity):
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

    async def async_added_to_hass(self) -> None:
        """Restore last known method text across reboot."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        if last_state.state in ("unknown", "unavailable", "", None):
            return
        self._attr_native_value = str(last_state.state)
        self._attr_available = True

    async def async_update(self) -> None:
        """Update by reading raw JSON from the main sensor's attributes."""
        main_state = self.hass.states.get(self._main_entity_id)
        if main_state is None or not main_state.attributes.get("json"):
            self._attr_available = self._attr_native_value is not None
            return
        raw = main_state.attributes["json"]
        try:
            template = Template(self._value_template_str, self.hass)
            options = self.config_entry.options or {}
            data = self.config_entry.data
            pv_max = float(options.get(CONF_PV_MAX_RECORD, data.get(CONF_PV_MAX_RECORD, DEFAULT_PV_MAX_RECORD)))
            rendered = template.async_render(
                {
                    "value": raw,
                    "latitude": self.hass.config.latitude,
                    "pv_max_record": pv_max,
                    "retune_params": main_state.attributes.get("retune_params", {}),
                }
            )
            value = str(rendered).strip()
            if value:
                self._attr_native_value = value
                self._attr_available = True
            else:
                self._attr_available = self._attr_native_value is not None
        except Exception as err:
            _LOGGER.error("Failed to render method template for %s: %s", self._attr_name, err)
            self._attr_available = self._attr_native_value is not None

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


class TemperatureSensor(SensorEntity):
    """Temperature Sensor mirroring weather forecast/current temperature.

    Created automatically when no external outdoor temperature sensor is configured.
    Registers as a proper HA temperature sensor so long-term statistics can be
    used by the SQL matcher and retune logic.
    Source stays weather-based (coordinator forecast / weather entity attribute),
    keeping behavior aligned with UV/cloud auto sensors.
    """

    _attr_icon = "mdi:thermometer"
    _attr_native_unit_of_measurement = "°C"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
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
        self._attr_unique_id = f"{DOMAIN}_{config_entry.entry_id}_temperature"
        self._attr_native_value = None
        self._attr_available = False
        self._weather_entity = weather_entity
        self._forecast_sensor_entity_id = forecast_sensor_entity_id
        self._coordinator = coordinator
        self.entity_id = generate_entity_id("sensor.{}", name, hass=hass)

    async def async_update(self) -> None:
        """Read temperature with forecast-first fallback strategy."""
        temp: float | None = None

        # 1. Coordinator forecast
        if self._coordinator and self._coordinator.data:
            temp = _nearest_forecast_field(self._coordinator.data.get("forecast", []), "temperature")

        # 2. Direct weather entity state attribute
        if temp is None:
            state = self.hass.states.get(self._weather_entity)
            if state is not None and state.state not in ("unknown", "unavailable", ""):
                raw = state.attributes.get("temperature")
                if raw is not None:
                    try:
                        temp = float(raw)
                    except (ValueError, TypeError):
                        pass

        # 3. State-machine WeatherForecastSensor
        if temp is None and self._forecast_sensor_entity_id:
            temp = _nearest_forecast_value(self.hass, self._forecast_sensor_entity_id, "temperature")

        if temp is not None:
            self._attr_native_value = temp
            self._attr_available = True
        else:
            self._attr_available = False

    @property
    def should_poll(self) -> bool:
        return True


class PrecipitationSensor(SensorEntity):
    """Precipitation Sensor that mirrors weather forecast/current precipitation."""

    _attr_icon = "mdi:weather-rainy"
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
        self._attr_unique_id = f"{DOMAIN}_{config_entry.entry_id}_precipitation"
        self._attr_native_value = None
        self._attr_available = False
        self._weather_entity = weather_entity
        self._forecast_sensor_entity_id = forecast_sensor_entity_id
        self._coordinator = coordinator
        self.entity_id = generate_entity_id("sensor.{}", name, hass=hass)

    async def async_update(self) -> None:
        """Read precipitation with forecast-first fallback strategy."""
        precip: float | None = None

        # 1. Coordinator forecast
        if self._coordinator and self._coordinator.data:
            precip = _nearest_forecast_field(self._coordinator.data.get("forecast", []), "precipitation")

        # 2. Direct weather entity state attribute
        if precip is None:
            state = self.hass.states.get(self._weather_entity)
            if state is not None and state.state not in ("unknown", "unavailable", ""):
                raw = state.attributes.get("precipitation")
                if raw is not None:
                    try:
                        precip = float(raw)
                    except (ValueError, TypeError):
                        pass

        # 3. State-machine WeatherForecastSensor
        if precip is None and self._forecast_sensor_entity_id:
            precip = _nearest_forecast_value(self.hass, self._forecast_sensor_entity_id, "precipitation")

        if precip is not None:
            # 1. Unter 0.05 mm/h -> 0%
            if precip <= 0.05:
                pct_val = 0.0
            # 2. Über 1.05 mm/h -> 100% (Gedeckelt)
            elif precip >= 1.05:
                pct_val = 100.0
            # 3. Lineare Rampe dazwischen skaliert auf 0% - 100%
            else:
                pct_val = (precip - 0.05) * 100.0

            # Als gerundeten Integer speichern (z.B. 45 statt 45.23)
            self._attr_native_value = int(round(pct_val))
            self._attr_available = True
        else:
            self._attr_native_value = None
            self._attr_available = False

    @property
    def should_poll(self) -> bool:
        return True


class CloudForecastSensor(SensorEntity, RestoreEntity):
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

    async def async_added_to_hass(self) -> None:
        """Restore last numeric value to bridge forecast readiness gaps on reboot."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        if last_state.state in ("unknown", "unavailable", "", None):
            return
        try:
            self._attr_native_value = float(last_state.state)
        except (TypeError, ValueError):
            self._attr_native_value = last_state.state
        self._attr_available = True

    async def async_update(self) -> None:
        """Read the target field from the main sensor's raw JSON attribute."""
        main_state = self.hass.states.get(self._main_entity_id)
        if main_state is None:
            self._attr_available = self._attr_native_value is not None
            return
        raw = main_state.attributes.get("json")
        if not raw:
            self._attr_available = self._attr_native_value is not None
            return
        try:
            data = json.loads(raw)
            if isinstance(data, list) and data:
                row0 = data[0]
                # For remaining-today forecast fields, hide hard SQL fallback values
                # during daytime startup gaps; after sunset keep explicit 0.0.
                readiness_key = {
                    "f_avg_today_remaining": "cloud_ready_today",
                    "uv_avg_today_remaining": "uv_ready_today",
                }.get(self._json_field)
                if readiness_key is not None:
                    is_after_sunset = int(row0.get("is_after_sunset_local", 0)) == 1
                    is_ready = int(row0.get(readiness_key, 0)) == 1
                    if not is_ready:
                        if is_after_sunset:
                            self._attr_native_value = 0.0
                            self._attr_available = True
                        else:
                            # Keep last valid daytime value until forecast window is ready.
                            self._attr_available = self._attr_native_value is not None
                        return

                value = row0.get(self._json_field)
                if value is not None:
                    self._attr_native_value = float(value)
                    self._attr_available = True
                    return
        except (ValueError, TypeError, KeyError):
            pass
        self._attr_available = self._attr_native_value is not None

    @property
    def should_poll(self) -> bool:
        return True

    @property
    def update_interval(self) -> timedelta | None:
        return timedelta(minutes=5)
