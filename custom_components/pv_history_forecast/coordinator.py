"""Data coordinator for HA SQL PV Forecast integration."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=15)


class WeatherCoordinator(DataUpdateCoordinator):
    """Coordinator to manage fetching weather forecast data."""

    def __init__(
        self,
        hass: HomeAssistant,
        weather_entity: str,
        forecast_sensor_name: str = "weather_forecast_hourly",
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"Weather Forecast - {weather_entity}",
            update_interval=SCAN_INTERVAL,
        )
        self.weather_entity = weather_entity
        self.forecast_sensor_name = forecast_sensor_name
        self.forecast_entity = f"sensor.{forecast_sensor_name}"

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch weather forecast data."""
        try:
            # Call the weather.get_forecasts service
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {
                    "type": "hourly",
                },
                target={"entity_id": self.weather_entity},
                blocking=True,
                return_response=True,
            )

            if not response:
                _LOGGER.warning("No forecast response from %s", self.weather_entity)
                return {"forecast": []}

            # Extract forecast data for the weather entity
            # Response format: {entity_id: {"forecast": [...]}}
            entity_forecast = response.get(self.weather_entity, {})
            forecast_list = entity_forecast.get("forecast", [])

            _LOGGER.debug(
                "Successfully fetched %d forecast entries from %s",
                len(forecast_list),
                self.weather_entity,
            )

            return {
                "forecast": forecast_list,
                "timestamp": datetime.now().isoformat(),
            }

        except Exception as err:
            _LOGGER.error("Error fetching weather forecast: %s", err)
            raise UpdateFailed(f"Error fetching weather forecast: {err}") from err

    async def _ensure_forecast_sensor(self) -> bool:
        """
        Ensure the forecast sensor entity exists (or inform user to create it).
        
        This checks if sensor.weather_forecast_hourly exists and creates
        an input_text helper if it doesn't.
        """
        # First check if the sensor already exists
        if self.hass.states.get(self.forecast_entity) is not None:
            _LOGGER.debug("Forecast entity %s already exists", self.forecast_entity)
            return True

        # Try to create an input_text helper as fallback
        try:
            await self.hass.services.async_call(
                "input_text",
                "create",
                {
                    "object_id": self.forecast_sensor_name,
                    "name": "Weather Forecast Hourly",
                    "icon": "mdi:weather-partly-cloudy",
                    "max": 5000,
                },
            )
            _LOGGER.info(
                "Created input_text helper %s for weather forecast storage",
                self.forecast_entity,
            )
            return True
        except Exception as err:
            _LOGGER.warning(
                "Could not create input_text helper %s: %s. "
                "Please create a template sensor manually.",
                self.forecast_entity,
                err,
            )
            return False

    async def update_forecast_sensor(self) -> None:
        """Update the forecast sensor with latest data."""
        if not self.last_update_success:
            return

        forecast_data = self.data.get("forecast", [])
        forecast_json = json.dumps(forecast_data)

        # Try to update the sensor entity
        if self.hass.states.get(self.forecast_entity) is not None:
            try:
                # Determine service based on entity type
                if self.forecast_entity.startswith("sensor.input_text_"):
                    await self.hass.services.async_call(
                        "input_text",
                        "set_value",
                        {"value": forecast_json},
                        target={"entity_id": self.forecast_entity},
                    )
                else:
                    # For template sensors - they update automatically
                    _LOGGER.debug("Template sensor will update automatically")
                    pass

                _LOGGER.debug(
                    "Updated %s with %d forecast entries",
                    self.forecast_entity,
                    len(forecast_data),
                )
            except Exception as err:
                _LOGGER.error("Error updating forecast sensor %s: %s", self.forecast_entity, err)
