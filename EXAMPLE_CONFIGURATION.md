# Example Home Assistant Configuration

## Complete Setup Example

```yaml
# Complete example configuration.yaml

# Sensor Definitions (beispielhafte Cloud Coverage und PV Sensoren)
template:
  - triggers:
      - platform: time_pattern
        minutes: /15
    sensors:
      weather_cloud_coverage:
        friendly_name: "Cloud Coverage"
        unit_of_measurement: "%"
        value_template: "{{ state_attr('weather.forecast_home', 'cloud_coverage') | int(0) }}"

# PV Sensor (beispiel - muss an Ihre Integration angepasst werden)
sensor:
  - platform: integration # oder Ihr PV System
    source: sensor.pv_input
    name: pv_panels_energy
    unit_prefix: k
    unique_id: pv_panels_energy
    device_class: energy
    unit_of_measurement: kWh

# Weather Forecast Template (automatisch erstellt wenn nicht vorhanden)
template:
  - trigger:
      - platform: time_pattern
        minutes: /15
    action:
      - service: weather.get_forecasts
        data:
          type: hourly
        target:
          entity_id: weather.forecast_home
        response_variable: hourly
    sensor:
      - name: weather_forecast_hourly
        unique_id: weather_forecast_hourly
        state: "{{ now().isoformat() }}"
        attributes:
          forecast: "{{ hourly['weather.forecast_home'].forecast }}"

# PV History Forecast Integration (nach Installation verfügbar)
pv_history_forecast:
  # Wird über UI konfiguriert oder via YAML:
  # db_url: "sqlite:////config/home-assistant_v2.db"
  # sensor_clouds: "sensor.weather_cloud_coverage"
  # sensor_pv: "sensor.pv_panels_energy"
  # sensor_forecast: "sensor.weather_forecast_hourly"
  # pv_history_days: 30
```

## Separate Konfigurationsdateien

Für bessere Organisation können Sie die Konfigurationen in separate Dateien aufteilen:

### automations.yaml
```yaml
# automations.yaml
- id: '1234567890'
  alias: "Update Weather Forecast"
  trigger:
    - platform: time_pattern
      minutes: "/15"
  action:
    - service: weather.get_forecasts
      data:
        type: hourly
      target:
        entity_id: weather.forecast_home
      response_variable: hourly
```

### Template Konfiguration (templates.yaml)
```yaml
# templates.yaml
- trigger:
    - platform: time_pattern
      minutes: /15
  action:
    - service: weather.get_forecasts
      data:
        type: hourly
      target:
        entity_id: weather.forecast_home
      response_variable: hourly
  sensor:
    - name: weather_forecast_hourly
      unique_id: weather_forecast_hourly
      state: "{{ now().isoformat() }}"
      attributes:
        forecast: "{{ hourly['weather.forecast_home'].forecast }}"
```

## Minimal Konfiguration

Wenn Sie nur die Integration verwenden möchten, ist dies mit der Web-UI vollständig:

1. Navigieren Sie zu Einstellungen > Geräte & Dienste > Integrationen
2. Klicken Sie auf + INTEGRATION ERSTELLEN
3. Suchen Sie nach "SQL PV Forecast"
4. Folgen Sie dem Assistenten

## Template Beispiele

### Forecast basierter Wert
```yaml
value_template: "{{ ((1 - clouds / 100) * pv | float(0) * 1.1) | round(2) }}"
```

### Durchschnitt über Geschichte
```yaml
value_template: "{{ (pv | float(0) * 0.95) | round(2) }}"
```

### Mit Bedingung
```yaml
value_template: >
  {% if clouds | float(0) > 80 %}
    {{ pv | float(0) * 0.5 }}
  {% elif clouds | float(0) > 50 %}
    {{ pv | float(0) * 0.75 }}
  {% else %}
    {{ pv | float(0) }}
  {% endif %} | round(2)
```

## Datenbank-Konfiguration

### SQLite
- URL: `sqlite:////config/database.db`
- Lokal, keine externe Verbindung nötig
- Gut für kleine bis mittlere Datenmengen

### MySQL
- URL: `mysql+pymysql://user:password@host:3306/database`
- Benötigt MySQL-Server
- Gut für größere Installations

### PostgreSQL
- URL: `postgresql://user:password@host:5432/database`
- Benötigt PostgreSQL-Server
- Beste Performance für große Datenmengen

## Tipps & Tricks

1. **Performance**: Verwenden Sie SQLite für lokale Datenbanken
2. **Fehlerbehandlung**: Überprüfen Sie regelmäßig die Logs
3. **Backup**: Regelmäßige Datenbank-Backups erstellen
4. **Template-Test**: Verwenden Sie das Template Editor Tool zum Testen

## Troubleshooting

### Sensor zeigt "unavailable"
- Überprüfen Sie die Entity IDs
- Stellen Sie sicher, dass die Datenbank erreichbar ist
- Lesen Sie die HA-Logs nach Fehlern

### Datenbank-Fehler
- Überprüfen Sie die Verbindungs-URL
- Stellen Sie sicher, dass der Benutzer Schreib-Rechte hat
- Bei Docker: Überprüfen Sie Port-Mappings
