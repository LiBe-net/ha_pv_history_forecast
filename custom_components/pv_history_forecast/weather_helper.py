"""Weather forecast helper module."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.template import Template
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def ensure_weather_forecast_template(
    hass: HomeAssistant,
    weather_entity: str,
    forecast_sensor_name: str = "weather_forecast_hourly",
) -> bool:
    """
    Ensure weather forecast template exists.
    
    If the forecast sensor doesn't exist, this function will suggest
    adding it to configuration.yaml.
    """
    forecast_entity = f"sensor.{forecast_sensor_name}"
    
    # Check if forecast entity already exists
    if hass.states.get(forecast_entity) is not None:
        _LOGGER.debug("Forecast entity %s already exists", forecast_entity)
        return True
    
    _LOGGER.warning(
        "Forecast entity %s does not exist. "
        "Please add the following to your configuration.yaml:\n"
        "\ntemplate:\n"
        "  - trigger:\n"
        "      - platform: time_pattern\n"
        "        minutes: /15\n"
        "    action:\n"
        "      - service: weather.get_forecasts\n"
        "        data:\n"
        "          type: hourly\n"
        "        target:\n"
        "          entity_id: %s\n"
        "        response_variable: hourly\n"
        "    sensor:\n"
        "      - name: %s\n"
        "        unique_id: %s\n"
        "        state: \"{{ now().isoformat() }}\"\n"
        "        attributes:\n"
        "          forecast: \"{{ hourly['%s'].forecast }}\"\n",
        forecast_entity,
        weather_entity,
        forecast_sensor_name,
        forecast_sensor_name,
        weather_entity,
    )
    
    return False


def get_weather_forecast_template_config() -> dict[str, Any]:
    """Get the weather forecast template configuration."""
    return {
        "trigger": [
            {
                "platform": "time_pattern",
                "minutes": "/15",
            }
        ],
        "action": [
            {
                "service": "weather.get_forecasts",
                "data": {
                    "type": "hourly",
                },
                "target": {
                    "entity_id": "weather.forecast_home",  # Customize this
                },
                "response_variable": "hourly",
            }
        ],
        "sensor": [
            {
                "name": "weather_forecast_hourly",
                "unique_id": "weather_forecast_hourly",
                "state": "{{ now().isoformat() }}",
                "attributes": {
                    "forecast": "{{ hourly['weather.forecast_home'].forecast }}",  # Customize this
                },
            }
        ],
    }
