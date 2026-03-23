# Feature Übersicht - SQL PV Forecast Integration

## Implementierte Features (v1.2.7)

### ✅ Kern Features

| Feature | Status | Details |
|---------|--------|---------|
| Home Assistant Integration | ✅ Fertig | Domain: `pv_history_forecast` |
| HACS Support | ✅ Fertig | hacs.json konfiguriert |
| Web-UI Konfiguration | ✅ Fertig | 2-Schritt Config Flow mit Präfix-Setup |
| SQL Datenbank | ✅ Fertig | SQLite (lokal, Home Assistant DB) |
| 5 Sensor-Entities | ✅ Fertig | Vollständig aus einer Konfiguration |

### ✅ Sensor-Architektur

| Sensor | Status | Details |
|--------|--------|---------|
| `{prefix}_remaining_today` | ✅ Fertig | Hauptsensor mit SQL, `sql_raw_json`-Attribut |
| `{prefix}_remaining_min` | ✅ Fertig | Pessimistisch, liest `sql_raw_json` vom Hauptsensor |
| `{prefix}_remaining_max` | ✅ Fertig | Optimistisch, liest `sql_raw_json` vom Hauptsensor |
| `{prefix}_tomorrow` | ✅ Fertig | Gesamtprognose morgen, gewichteter Mittelwert |
| `{prefix}_weather_forecast` | ✅ Fertig | Interner Wetter-Helfer, alle **5 Min** aktualisiert |
| `{prefix}_cloud_coverage` | ✅ Fertig | Auto-Sensor (nur wenn kein externer Cloud-Sensor) — spiegelt `cloud_coverage` der Wetter-Entity, baut LTS-Statistiken auf |
| `{prefix}_lovelace` | ✅ Fertig | Vorberechnete Lovelace Markdown-Card im Attribut `lovelace_card` |

### ✅ SQL Query Logik

| Feature | Status | Details |
|---------|--------|---------|
| CTE-Ketten-Query | ✅ Fertig | `vars → ids → pv_activity → ... → json_group_array` |
| Dynamische Sensor-IDs | ✅ Fertig | Über `statistics_meta` und `states_meta` |
| `pv_activity` (Sonnenzeiten) | ✅ Fertig | `sun_start`: erstes aktives PV-State gestern; `sun_end`: letzter Anstieg vor Tagesmax |
| UTC-korrekte Zeitvergleiche | ✅ Fertig | `ertrag_tag_rest`: Integer-Minuten-BETWEEN statt String-Vergleich |
| Kumulative Sensor-Erkennung | ✅ Fertig | `sun_end` = letzter State **unter** Tagesmaximum |
| Historische Vergleichstage | ✅ Fertig | Sortiert nach Ähnlichkeit der Bewölkung (COALESCE rest/total) |
| JSON-Array Ausgabe | ✅ Fertig | `json_group_array(json_object(...))` |
| Cloud-History Fallback | ✅ Fertig | Für Tage ohne Auto-Sensor-Statistik: direkte Abfrage der Wetter-Entity-States (`UNION ALL`) |
| Wh → kWh Normierung | ✅ Fertig | `pv_divisor` in SQL: `statistics_meta.unit = 'Wh'` → `/1000` automatisch |

### ✅ Template-Logik

| Feature | Status | Details |
|---------|--------|---------|
| Breitengrad-Astronomie | ✅ Fertig | `dl = 24/π · arccos(−tan(φ)·tan(δ))` via `hass.config.latitude` |
| Saisonale Skalierung | ✅ Fertig | Historische Erträge auf heutiges Datum skaliert |
| Top-5 Ähnlichkeitsauswahl | ✅ Fertig | Sortierung nach `|cloudy_hist - forecast|` |
| 3-Fall-Logik (Min/Max) | ✅ Fertig | Heller / Dunkler / Gemischt → Worst/Best/Weighted |
| Gewichteter Mittelwert (Tomorrow) | ✅ Fertig | `w = 1 / max(diff, 0.5)` |
| UTC-Nacht-Check | ✅ Fertig | `ende_min ≤ now_min < midnight_utc_min` |

### ✅ Konfiguration

| Feature | Status | Details |
|---------|--------|---------|
| Präfix-basiertes Setup | ✅ Fertig | Standard: `pv_hist`, alle Sensornamen abgeleitet |
| GUI-Konfiguration | ✅ Fertig | Vollständiger 2-Schritt Config Flow |
| Optionen-Flow | ✅ Fertig | Nachträgliche Änderungen möglich, automatisches Neuladen |
| Validierung | ✅ Fertig | DB URL, Entity ID, PV-Einheit (kWh/Wh Pflicht) |
| Mehrsprachigkeit | ✅ Fertig | DE + EN Übersetzungen |
| Auto-Cloud-Sensor | ✅ Fertig | Wizard-Option: leer lassen → `{prefix}_cloud_coverage` wird angelegt |
| Sensor-Filterung | ✅ Fertig | Cloud-Dropdown: nur `%`-Sensoren mit Messwert; PV-Dropdown: nur `energy`-Klasse + kWh/Wh + Statistiken |

### ✅ Fehlerbehandlung

| Feature | Status | Details |
|---------|--------|---------|
| DB Connection Error | ✅ Fertig | `available = False` |
| Leeres SQL-Ergebnis | ✅ Fertig | Template gibt `0.0` zurück wenn `sql_raw_json = '[]'` |
| Template Error | ✅ Fertig | Error Log + None Return |
| Validation Error | ✅ Fertig | Config Flow Fehler |

### ✅ Dokumentation

| Datei | Inhalt |
|-------|--------|
| `README.md` | Vollständige Dokumentation |
| `INSTALLATION.md` | Schritt-für-Schritt Anleitung |
| `QUICKSTART.md` | 5-Minuten Setup |
| `API.md` | API Referenz |
| `ADVANCED_QUERY.md` | SQL & Template Technische Dokumentation |
| `PROJECT_STRUCTURE.md` | Technischer Aufbau |
| `EXAMPLE_CONFIGURATION.md` | Konfigurationsbeispiele |
| `CONTRIBUTING.md` | Beitragsrichtlinien |

## Bekannte Einschränkungen

- Nur SQLite wird unterstützt (MySQL/PostgreSQL nicht)
- Mindestens 1 Tag PV-Produktionsdaten erforderlich für `sun_start`/`sun_end`
- `sql_raw_json` ist `[]` wenn kein historischer Vergleichstag mit gültiger Bewölkung gefunden wird
- Wetter-Entity muss `cloud_coverage` im stündlichen Forecast enthalten
- Auto-Cloud-Sensor (`{prefix}_cloud_coverage`): LTS-Statistiken (>10 Tage) wachsen erst mit der Laufzeit; in der Zwischenzeit greift der SQL-Fallback auf die States der Wetter-Entity zurück

## Performance Features

| Feature | Status | Details |
|---------|--------|---------|
| Async/Await Pattern | ✅ Fertig | Non-blocking Operations |
| Executor Jobs | ✅ Fertig | DB Ops in Thread Pool |
| 5 Min Polling | ✅ Fertig | Optimiert für PV Daten (vorher 15 Min) |
| Adaptiver EMA-Filter | ✅ Fertig | Glättet stündliche LTS-Grenzsprünge; alpha 0.3–1.0 je nach relativer Änderung |
| Abgeleitete Sensoren | ✅ Fertig | Lesen `sql_raw_json` direkt aus Hauptsensor, kein zweiter SQL-Call |

## Kompatibilität

| Kriterium | Status | Details |
|-----------|--------|---------|
| Home Assistant | ✅ | 2024.1.0+ |
| Python | ✅ | 3.11+ |
| HACS | ✅ | Vollständig |
| SQLAlchemy | ✅ | 1.4.0+ |
| Datenbank | ✅ | Nur SQLite |


## Zusammenfassung

**Implementiert: 45+ Features**
- **Kern**: 100% ✅
- **Dokumentation**: 100% ✅
- **Fehlerbehandlung**: 100% ✅
- **Performance**: 100% ✅

**Frühjahre Erweiterungen**
- Service Handler (geplant)
- Webhook Integration (geplant)
- ML Forecasting (geplant)

**Status: PRODUKTIONSREIF** 🚀

Die Integration ist vollständig funktional und kann produktiv eingesetzt werden.
