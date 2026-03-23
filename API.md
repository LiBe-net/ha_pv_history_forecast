# API Dokumentation

## Übersicht

Die SQL PV Forecast Integration erzeugt aus einer einzigen Konfiguration 5 Sensor-Entities und stellt deren Daten für Automationen und Dashboards bereit.

## Sensor Entities

Mit dem Standard-Präfix `sql_pv` entstehen folgende Entities:

### `sensor.sql_pv_remaining_today` — Hauptsensor

| Eigenschaft | Wert |
|-------------|------|
| `state` | Verbleibender Ertrag heute (kWh, Float) |
| `unit_of_measurement` | `kWh` |
| `device_class` | `energy` |
| `state_class` | `total_increasing` |
| `attributes.sql_raw_json` | JSON-Array der historischen Vergleichstage |
| `attributes.last_updated` | Zeitstempel der letzten SQL-Abfrage |

Das Attribut `sql_raw_json` enthält ein JSON-Array mit je einem Objekt pro Vergleichstag:

```json
[
  {
    "datum": "2025-06-15",
    "f_avg_heute_rest": 41.3,
    "f_avg_morgen": 28.5,
    "h_avg_gesamt": 43.0,
    "h_avg_rest": 39.2,
    "ertrag_tag_gesamt": 18.45,
    "ertrag_tag_rest": 6.12,
    "pv_start": "04:47",
    "pv_ende": "17:23"
  }
]
```

### `sensor.sql_pv_remaining_min` — Pessimistisch

Liest `sql_raw_json` vom Hauptsensor und wendet das MIN-Template an. Gibt die pessimistischste Prognose der top-5 ähnlichen Tage zurück.

### `sensor.sql_pv_remaining_max` — Optimistisch

Wie `_min`, aber mit dem MAX-Template — optimistischste Prognose.

### `sensor.sql_pv_tomorrow` — Morgen-Prognose

Gewichteter Mittelwert des Gesamtertrags für morgen, astronomisch auf den nächsten Tag skaliert.

### `sensor.sql_pv_weather_forecast` — Wetter-Helfer (intern)

| Eigenschaft | Wert |
|-------------|------|
| `state` | Anzahl der Forecast-Einträge (int) |
| `attributes.forecast` | JSON-Array mit stündlichen Forecast-Einträgen |
| `attributes.forecast_count` | Anzahl der Forecast-Einträge |
| `attributes.last_update` | Zeitstempel der letzten Aktualisierung |

Wird von der SQL-Query genutzt (`f_id` in `ids` CTE). Aktualisierung alle 15 Minuten automatisch.

### `sensor.sql_pv_cloud_coverage` — Auto-Bewölkungssensor

*Wird nur erstellt, wenn im Wizard kein externer Cloud Coverage Sensor gewählt wurde.*

| Eigenschaft | Wert |
|-------------|------|
| `state` | Bewölkung in % (float), gespiegelt von `weather.*` |
| `unit_of_measurement` | `%` |
| `state_class` | `measurement` |

Der Sensor sammelt Bewölkungsdaten von der konfigurierten Wetter-Entity und registriert sich als echter HA-Sensor, damit Home Assistant LTS-Statistiken aufbaut. **Ab >10 Tagen Laufzeit** stehen vollständige historische Vergleichsdaten zur Verfügung.

> **SQL-Fallback**: Für Tage, an denen der Auto-Sensor noch keine LTS-Statistik hat, greift der SQL-`cloud_history`-CTE automatisch direkt auf die States der Wetter-Entity zurück.

### `sensor.sql_pv_lovelace` — Lovelace Markdown-Card

| Eigenschaft | Wert |
|-------------|------|
| `state` | ISO-Zeitstempel des letzten Renders |
| `attributes.lovelace_card` | Fertig gerenderter Markdown-Inhalt |
| `attributes.source_sensor` | Konfigurierter Quellsensor (mit `sql_raw_json`) |
| `attributes.forecast_sensor` | Konfigurierter Forecast-Sensor |

**Verwendung in Lovelace:**

```yaml
type: markdown
content: "{{ state_attr('sensor.sql_pv_lovelace', 'lovelace_card') }}"
```

Die Karte enthält: Prognose-Wert, Methode, Schnee-Warnung, historische Vergleichstabellä und stündliche Bewölkungstabelle für den Rest-PV-Zeitraum.

## Template-Variablen (intern)

Die abgeleiteten Sensoren (`_min`, `_max`, `_tomorrow`) erhalten beim Template-Rendering:

| Variable | Typ | Inhalt |
|----------|-----|--------|
| `value` | str | Der `sql_raw_json`-String aus dem Hauptsensor |
| `latitude` | float | `hass.config.latitude` (Breitengrad des HA-Standorts) |

### Beispiel: Sensor aus Template auslesen (Automation)

```yaml
condition:
  - condition: numeric_state
    entity_id: sensor.sql_pv_remaining_today
    above: 2.0
```

### Beispiel: sql_raw_json in einer Template-Card

```jinja2
{% set data = state_attr('sensor.sql_pv_remaining_today', 'sql_raw_json') | from_json %}
{% if data and data | count > 0 %}
  Ähnlicher Tag: {{ data[0].datum }}
  Bewölkung hist.: {{ data[0].h_avg_gesamt }}%
  Ertrag gesamt: {{ data[0].ertrag_tag_gesamt }} kWh
{% endif %}
```

## Config Entry Struktur

```python
{
    "version": 1,
    "domain": "pv_history_forecast",
    "data": {
        "sensor_prefix": "sql_pv",
        "db_url": "sqlite:////config/home-assistant_v2.db",  # oder None für Standard
        "weather_entity": "weather.forecast_home",
        "sensor_pv": "sensor.pv_panels_energy",
        "sensor_clouds": "sensor.sql_pv_cloud_coverage",    # Auto-Sensor wenn leer gelassen
        "pv_history_days": 30,
        "lovelace_sensor": "sensor.sql_pv_remaining_today"  # Quellsensor für Lovelace-Card
    }
}
```

Abgeleitete Werte (automatisch gesetzt):
- `sensor_forecast` = `f"sensor.{prefix}_weather_forecast"`
- `unique_id` = `sensor_prefix`

## Automations-Beispiel

```yaml
automation:
  - alias: "PV Prognose Benachrichtigung"
    trigger:
      - platform: time
        at: "08:00:00"
    action:
      - service: notify.mobile_app
        data:
          message: >
            Heute noch {{ states('sensor.sql_pv_remaining_today') }} kWh
            (min: {{ states('sensor.sql_pv_remaining_min') }},
             max: {{ states('sensor.sql_pv_remaining_max') }})
            Morgen: {{ states('sensor.sql_pv_tomorrow') }} kWh
```

Vollständiges Beispiel: [automation_example.yaml](automation_example.yaml)

  {{ pv | float(0) * 0.7 }}
{% else %}
  {{ pv | float(0) }}
{% endif %}
```

#### Mit Filtern
```jinja2
{{ value_as_number | round(2) }}
{{ value_as_number | default(0) }}
{{ value_as_number | string | truncate(10) }}
```

## Debugging

### Logs aktivieren

```yaml
logger:
  logs:
    custom_components.pv_history_forecast: debug
```

### Log-Kategorien

- `custom_components.pv_history_forecast` — Allgemein
- `custom_components.pv_history_forecast.config_flow` — Konfiguration
- `custom_components.pv_history_forecast.sensor` — Sensor-Logik

