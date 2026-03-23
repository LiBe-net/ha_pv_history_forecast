# Projekt-Übersicht

## Verzeichnisstruktur

```
ha_pv_history_forecast/
│
├── README.md                           # Hauptdokumentation
├── INSTALLATION.md                     # Installationsanleitung
├── QUICKSTART.md                       # 5 Minuten Schnelleinstieg
├── EXAMPLE_CONFIGURATION.md            # Konfigurationsbeispiele
├── CONTRIBUTING.md                     # Beitragsrichtlinien
├── API.md                              # API Referenz
├── LICENSE                             # MIT Lizenz
├── .gitignore                          # Git Ignore Datei
├── requirements.txt                    # Python Dependencies
├── hacs.json                           # HACS Metadaten
│
└── custom_components/pv_history_forecast/
    ├── __init__.py                     # Integration Einstiegspunkt
    ├── config_flow.py                  # Konfigurationsflow
    ├── const.py                        # Konstanten
    ├── sensor.py                       # Sensorlogik & SQL Abfragen
    ├── weather_helper.py               # Weather Template Helper
    ├── info.md                         # Entity Registry Info
    ├── py.typed                        # Type Hints Marker
    ├── manifest.json                   # Integration Manifest
    ├── strings.json                    # UI-Strings (Englisch)
    │
    └── translations/
        ├── de.json                     # Deutsche Übersetzung
        └── en.json                     # Englische Übersetzung
```

## Dateienbeschreibungen

### Kernkomponenten

| Datei | Funktion |
|-------|----------|
| `__init__.py` | Setup/Teardown der Integration |
| `config_flow.py` | Benutzer-Eingabeformular und Validierung |
| `sensor.py` | Sensoren-Entity und SQL Query Logik |
| `const.py` | Konstanten und Standard-Werte |
| `manifest.json` | Integration Metadaten |

### Dokumentation

| Datei | Inhalt |
|-------|--------|
| `README.md` | Vollständige Dokumentation |
| `QUICKSTART.md` | 5-Minuten Einstieg |
| `INSTALLATION.md` | Detaillierte Installation |
| `API.md` | API-Referenz |
| `EXAMPLE_CONFIGURATION.md` | Konfigurationsbeispiele |

### Konfiguration

| Datei | Zweck |
|-------|--------|
| `manifest.json` | HA Integration Definition |
| `hacs.json` | HACS Community Store Info |
| `strings.json` | User-facing Strings |
| `translations/*.json` | Sprachdateien |

## Funktionsweise

### 1. Installation
```
User installiert via HACS oder manuell
         ↓
Home Assistant erkennt `custom_components/pv_history_forecast`
         ↓
`manifest.json` wird gelesen
         ↓
Integration wird registriert
```

### 2. Konfiguration (2 Schritte)
```
User startet Konfigurationsfluss
         ↓
Schritt 1: sensor_prefix + optionale db_url
         ↓
Schritt 2: weather_entity, sensor_pv, sensor_clouds (optional), lovelace_sensor (optional), pv_history_days
         ↓
Validierung (DB-URL + Entity IDs)
         ↓
Config Entry in Home Assistant gespeichert
```

### 3. Sensor-Setup
```
`async_setup_entry()` in `__init__.py` wird aufgerufen
         ↓
`async_setup_entry()` in `sensor.py` erstellt bis zu **7 Entitäten**:
  • SQLPVForecastSensor          ({prefix}_remaining_today)
  • PVForecastTemplateSensor ×3  (_remaining_min, _remaining_max, _tomorrow)
  • LovelaceCardSensor           ({prefix}_lovelace)
  • WeatherForecastSensor        ({prefix}_weather_forecast)  [wenn Coordinator verfügbar]
  • CloudCoverageSensor          ({prefix}_cloud_coverage)    [nur wenn kein externer Cloud-Sensor]
         ↓
SQL-Query wird generiert (Sensoren aus Config substituiert)
         ↓
Polling startet (15-Min-Intervall)
```

### 4. Sensor Update
```
15 Min Intervall
         ↓
SQLPVForecastSensor.async_update():
  SQL Query ausführen → Ergebnis in sql_raw_json speichern
         ↓
PVForecastTemplateSensor.async_update():
  sql_raw_json aus Hauptsensor lesen
  Template mit {value, latitude} rendern
  Sensorwert setzen
         ↓
LovelaceCardSensor.async_update():
  DEFAULT_LOVELACE_TEMPLATE rendern (Jinja2)
  Ergebnis in extra_state_attributes["lovelace_card"] speichern
         ↓
WeatherForecastSensor: weather.get_forecasts Service aufrufen
         ↓
CloudCoverageSensor.async_update():
  cloud_coverage-Attribut der Wetter-Entity auslesen
  Sensorwert setzen (LTS-Statistiken werden von HA akkumuliert)
```

## Wichtige Klassen

### `SQLPVForecastSensor`
Hauptsensor mit SQL-Ausführung:
- Liest HA SQLite Datenbank direkt
- Speichert JSON-Rohdaten in `extra_state_attributes["sql_raw_json"]`
- Wendet `DEFAULT_VALUE_TEMPLATE` an

**Methoden:**
- `_init_database()` — DB-Verbindung initialisieren
- `_build_sql_query()` — Query mit Sensor-Substitution erstellen
- `async_update()` — SQL ausführen, Template anwenden
- `_apply_template()` — Jinja2-Template rendern mit `{value, latitude}`

### `PVForecastTemplateSensor`
Abgeleiteter Sensor (kein SQL-Call):
- Liest `sql_raw_json` vom Hauptsensor (`hass.states.get(main_entity_id)`)
- Wendet spezifisches Template an (MIN / MAX / TOMORROW)
- `latitude` wird aus `hass.config.latitude` übergeben

### `WeatherForecastSensor`
Wetter-Hilfssensor:
- Ruft `weather.get_forecasts` Service auf
- Speichert stündliche Vorhersage in `attributes.forecast`
- Wird von SQL Query via `f_id` referenziert und von der Lovelace-Card via `state_attr(..., 'forecast')`

### `CloudCoverageSensor`
Auto-Bewölkungssensor (nur ohne externen Cloud-Sensor):
- Spiegelt `cloud_coverage` der Wetter-Entity als echter HA-Sensor
- HA baut automatisch LTS-Statistiken auf
- SQL-`cloud_history`-CTE greift für Tage ohne Statistiken auf Wetter-Entity-States zurück

### `LovelaceCardSensor`
Vorgefertigte Lovelace Markdown-Card:
- Rendert `DEFAULT_LOVELACE_TEMPLATE` mit `async_render({})`
- Ergebnis in `extra_state_attributes["lovelace_card"]`
- Quellsensor (`__SOURCE_SENSOR__`) und Forecast-Sensor (`__FORECAST_SENSOR__`) werden bei Init via `.replace()` eingesetzt

### `ConfigFlow`
2-Schritt-Konfigurationsformular:
- Schritt 1: `sensor_prefix` + optionale `db_url`
- Schritt 2: Sensoren (Dropdowns aus HA-Entitylisten)
- `unique_id = sensor_prefix`

### `OptionsFlow`
Optionen-Bearbeitung nach Setup

## Dependencies

```
sqlalchemy>=1.4.0     # Database ORM
pymysql>=1.0.0        # MySQL Driver  
psycopg2-binary>=2.9.0 # PostgreSQL Driver
```

Für SQLite ist kein zusätzlicher Driver nötig (Teil von Python).

## Datenfluss

```
Sensor Entity (Home Assistant)
    ↓
SQLPVForecastSensor
    ↓
async_update() → _execute_query()
    ↓
Database (SQLite/MySQL/PostgreSQL)
    ↓ (Sensordaten + Query Result)
_apply_template() 
    ↓
Value Template Rendering
    ↓
Sensorwert wird gespeichert
    ↓
Home Assistant State Update
```

## Konfigurationsflow

```
User erstellt Integration
    ↓
ConfigFlow.async_step_user() 
    ↓ (Benutzer gibt Daten ein)
_validate_db_url() & Validierung
    ↓ (Validierung erfolgreich)
ConfigFlow.async_step_user_advanced()
    ↓ (Erweiterte Optionen)
async_create_entry()
    ↓
Config Entry gespeichert
    ↓
async_setup_entry() aufgerufen
```

## Fehlerbehandlung

### Datenbank-Fehler
- Ungültige URL → Validierungsfehler
- Verbindung fehlgeschlagen → `available = False`
- Query-Fehler → Log-Eintrag, Wert zu null

### Sensor-Fehler
- Entity nicht verfügbar → 0.0 oder Skip
- Template-Fehler → None zurück

### Options Update
- Sensor geändert → Query wird neu erstellt
- Config geändert → Sensor wird reloaded

## Wichtige Patterns

### Async Pattern
```python
async def async_update(self):
    result = await self.hass.async_add_executor_job(
        self._execute_query  # Blocking operation
    )
    self._attr_native_value = result
```

### Template Rendering
```python
template = Template(self._value_template_str, self.hass)
rendered = template.async_render(variables)
```

### Entity Updates
```python
self._attr_native_value = value
self.async_write_ha_state()
```

## Testing

Zusätzliche test Files könnten hinzugefügt werden:
```
tests/
├── test_config_flow.py
├── test_sensor.py
└── test_query_builder.py
```

## Erweiterungsmöglichkeiten

1. **Service-Handler** - für manuelle Updates
2. **Webhook-Support** - externe Daten Integration
3. **Cache-Layer** - für bessere Performance
4. **Mehrere Sensoren** - pro Konfiguration
5. **Advanced SQL Editor** - GUI für Custom Queries
6. **Forecasting-Algorithmen** - ML-basiert
7. **Export-Funktionen** - CSV, JSON Export

## Lizenz

MIT - Frei verwendbar für kommerzielle und private Projekte
