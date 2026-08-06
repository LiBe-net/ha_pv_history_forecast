"""Helpers for reading forecast values across provider-specific field names."""
from __future__ import annotations

from typing import Any, Mapping


_FORECAST_FIELD_ALIASES = {
    "cloud_coverage": ("cloud_coverage", "cloud_cover", "cloud_cover_percentage", "clouds", "cloud"),
    "uv_index": ("uv_index", "uv", "uv_index_value"),
    "temperature": ("temperature", "temp", "temperature_value"),
    "precipitation": ("precipitation", "precipitation_probability", "precipitation_rate", "rain"),
}


def _coerce_float(value: Any) -> float | None:
    """Return a float for numeric values or simple numeric strings."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_forecast_field_value(payload: Mapping[str, Any] | None, field: str) -> float | None:
    """Resolve a forecast field from provider-specific aliases.

    The first alias may be a placeholder like ``0.0`` while a later alias holds
    the actual provider value. In that case we keep looking for a non-zero or
    more specific alias instead of stopping too early.
    """
    if not payload:
        return None

    aliases = _FORECAST_FIELD_ALIASES.get(field, (field,))
    best_value: float | None = None
    for alias in aliases:
        raw_value = payload.get(alias)
        if raw_value is None:
            continue

        if field == "precipitation":
            value = _coerce_float(raw_value)
            if value is None:
                continue
            if alias in {"precipitation_probability", "precipitation_prob", "probability"}:
                value = max(0.0, min(100.0, value))
                if value > 0.0:
                    return value
                if best_value is None:
                    best_value = value
                continue
            if value <= 0.05:
                if best_value is None:
                    best_value = 0.0
                continue
            if value >= 1.05:
                return 100.0
            return (value - 0.05) * 100.0

        value = _coerce_float(raw_value)
        if value is None:
            continue
        if value != 0.0:
            return value
        if best_value is None:
            best_value = value

    return best_value
