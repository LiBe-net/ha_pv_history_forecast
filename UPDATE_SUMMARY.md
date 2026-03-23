# Update: Advanced Setup Flow & SQL Query Integration

## 🎯 Zusammenfassung der Änderungen

Die Home Assistant SQL PV Forecast Integration wurde vollständig überarbeitet, um eine **production-ready Advanced SQL Query** mit **intelligenter Prognose-Logik** zu unterstützen.

---

## 🚀 Neue Features

### 1. **Vereinfachter Setup-Flow (2 Schritte)**

#### Vorher (4 Felder auf 1 Seite)
```
❌ Name
❌ DB URL  
❌ 3 Sensoren
❌ PV History Days
```

#### Nachher (Intuitiver 2-Schritt Flow)
```
✅ Schritt 1: Database Connection
   - Name
   - DB URL
   
✅ Schritt 2: Select 3 Sensors
   - Cloud Coverage Sensor
   - PV Panel Energy Sensor
   - Weather Forecast Sensor
```

### 2. **Automatische Advanced SQL Query**

Bei der Konfiguration wird automatisch eine **hochperformante SQL Query** erstellt:

```sql
WITH vars AS (
    SELECT 
        'weather.home' as sensor_clouds,           ← Aus UI gewählt
        'sensor.pv_panels_energy' as sensor_pv,    ← Aus UI gewählt
        'sensor.weather_forecast_hourly' as sensor_forecast,  ← Aus UI gewählt
        (strftime(...) - strftime(...)) || ' seconds' as offset
)
-- 300+ Zeilen Logik für:
-- ✓ Historische Datenanalyse (60 Tage)
-- ✓ Dynamische Sonnenauf-/Untergangszeiten
-- ✓ Bewölkungs-Prognosen
-- ✓ Saisonale Anpassung
-- ✓ Schneebedeckung-Erkennung (Winter)
```

### 3. **Intelligentes Template System**

Das Jinja2 Template verbindet die SQL-Daten mit Prognose-Logik:

```jinja2
{# Automatisch angewendet mit Funktionen für: #}
✓ Schnee-Erkennung (Wintermonate)
✓ Astronomische Berechnungen (Sonnenstand)
✓ Gewichtete historische Analyse
✓ 3-Faktor Fallunterscheidung (A/B/C)
✓ Finale Skalierung & Konvertierung
```

---

## 📝 Implementierte Änderungen

### const.py
- ✅ `DEFAULT_SQL_QUERY`: Komplette Advanced Query als Template
- ✅ `DEFAULT_VALUE_TEMPLATE`: Production-Grade Jinja2 Template (~150 Zeilen)
- ✅ Entfernt: `DEFAULT_PV_HISTORY_DAYS` (nicht mehr nötig)

### config_flow.py
- ✅ 2-Schritt Flow: `async_step_user()` → `async_step_sensors()`
- ✅ Automatische Query-Generierung: `DEFAULT_SQL_QUERY.format(...)`
- ✅ OptionsFlow: Query wird bei Sensor-Änderung neu erstellt
- ✅ Vereinfachte Validierung

### sensor.py
- ✅ `sql_query` Parameter hinzugefügt (`__init__`)
- ✅ `_rebuild_sql_query()` unterstützt nun Query-Templates
- ✅ `async_setup_entry()` übergibt Query an Sensor
- ✅ `_handle_options_update()` baut Query neu wenn Sensoren ändern

### strings.json / Translations
- ✅ `user`: Nur "Name" + "DB URL"
- ✅ `sensors`: Neu! 3-Sensoren Selection
- ✅ Klare deutsche/englische Beschreibungen
- ✅ Entfernt: "PV History Days" (nicht mehr sichtbar)

---

## 🔄 Workflow: Setup bis Prognose

```
1️⃣ SETUP (User)
   ├─ Gibt Name & DB URL ein
   ├─ Selectiert 3 Sensoren
   └─ Integration erstellt

2️⃣ KONFIGURATION (Integration)
   ├─ Lädt DEFAULT_SQL_QUERY Template
   ├─ Ersetzt {sensor_clouds}, {sensor_pv}, {sensor_forecast}
   ├─ Speichert resultierende SQL Query in config entry
   └─ Lädt Sensor mit der Query

3️⃣ POLLING (15 Min Intervall)
   ├─ Sensor führt SQL Query aus
   ├─ Erhält JSON-Array mit historischen Tagen
   ├─ Übergibt Wert an Jinja2 Template
   ├─ Template berechnet Prognose mit:
   │  ├─ Schnee-Erkennung
   │  ├─ Saisonale Anpassung
   │  ├─ Gewichtete Historians-Analyse
   │  └─ Intelligente Fallunterscheidung
   └─ Sensorwert wird aktualisiert

4️⃣ ÄNDERUNG (User ändert Sensor in Options)
   ├─ OptionsFlow wird aufgerufen
   ├─ _handle_options_update() detektiert Sensor-Änderung
   ├─ Query wird mit neuen Sensoren neu erstellt
   └─ Nächster Poll verw seit neuesten Query
```

---

## 📊 SQL Query: Was wird berechnet?

| Phase | Was | Ergebnis |
|-------|-----|----------|
| **vars** | Konfigurationsvariablen & Zeitzonen-Offset | Base-Daten |
| **ids** | Home Assistant Datenbank-IDs für Sensoren | Optimierte Queries |
| **pv_activity** | Sonnenauf-/Untergangszeiten von gestern | Tagesaktivität-Fenster |
| **forecast_val** | Bewölkung heute (ab jetzt) | Heute's Expectation |
| **forecast_next_day** | Bewölkung morgen | Tomorrow's Expectation |
| **cloud_history** | 60-Tage Bewölkungs-Historie | Historische Basis |
| **matching_days** | Ähnliche historische Tage | Vergleichstage gefunden |
| **final_data** | PV Erträge der Match-Tage | Ertrag-Daten |
| **Final SELECT** | JSON-Aggregation | **JSON-Array zurück** |

---

## 🧮 Jinja2 Template: Was wird calculated?

| Schritt | Feature | Berechnung |
|---------|---------|-----------|
| 1️⃣ | Schnee-Erkennung | Winter + Performance < 2% → Faktor 0.1 |
| 2️⃣ | Astronomie | Sinus-Funktionen für Sonnenscheindauer |
| 3️⃣ | Pool-Aufbereitung | Saisonale Skalierung + Gewichtung |
| 4️⃣ | Prognose 3-Faktoren | A: Heller, B: Dunkler, C: Gemischt |
| 5️⃣ | Finale Skalierung | Wh→kWh + Schneebedeckung-Anwendung |

---

## 🔧 Advanced Options (bleiben gleich)

Nach Setup können Sie noch anpassen:

```
Sensoren (werden neu gelesen):
├─ Cloud Coverage Sensor
├─ PV Panel Energy Sensor
└─ Weather Forecast Sensor

Template & Eigenschaften:
├─ Value Template (Jinja2)
├─ Maßeinheit (kWh)
├─ Geräteklasse (energy)
└─ Zustandklasse (total_increasing)
```

---

## 📚 Dokumentation

| Datei | Inhalt |
|-------|--------|
| **ADVANCED_QUERY.md** | 📖 Detaillierte Query & Template Erklärung |
| **QUICKSTART.md** | ⚡ 5 Minuten Setup |
| **INSTALLATION.md** | 🔧 Installationsanleitung |
| **README.md** | 📋 Allgemeine Dokumentation |
| **API.md** | 🔌 API Referenz |

---

## ✅ Checkliste: Was funktioniert?

- ✅ 2-Schritt Setup-Flow
- ✅ 3-Sensoren Selection
- ✅ Automatische Query-Generierung
- ✅ Automatisches Template-Anwenden
- ✅ Query-Neuerstellen bei Sensor-Änderung
- ✅ Mehrsprachig (DE/EN)
- ✅ Fehlerbehandlung
- ✅ Logging & Debugging
- ✅ Async/Executor Pattern
- ✅ Options-Flow für Nachträgliche Änderungen

---

## 🎯 Verwendungsbeispiel

### Szenario: Sie haben
```
Sensoren:
- weather.home          (Weather Entity mit cloud_coverage)
- sensor.pv_energy_day  (PV Panel Energy, Total Increasing)
- sensor.weather_fc_h   (JSON Forecast Sensor)

Datenbank:
- SQLite unter /config/home-assistant_v2.db
```

### Setup-Prozess
```
1. Einstellungen → Integrationen → + CREATE
2. Suche: "SQL PV Forecast"
3. Name: "My PV Forecast"
4. DB URL: "sqlite:////config/home-assistant_v2.db"
5. Nächster Schritt
6. Cloud: weather.home
7. PV: sensor.pv_energy_day
8. Forecast: sensor.weather_fc_h
9. SUBMIT
10. ✅ Fertig! Sensor: sensor.my_pv_forecast erstellt
```

### Automatische Resultat
```
Erstellte SQL Query mit GENAU IHREN Sensoren:
WITH vars AS (
    SELECT 
        'weather.home' as sensor_clouds,
        'sensor.pv_energy_day' as sensor_pv,
        'sensor.weather_fc_h' as sensor_forecast,
        ...
)

Template wird auf den Sensor angewendet:
✓ 15-Min Polling
✓ SQL Query wird ausgeführt
✓ Historische Analyse mit Ihren Daten
✓ Schnee-Erkennung (Winter)
✓ Saisonale Anpassung
✓ Gewichtete Prognose berechnet
✓ Sensorwert: z.B. "12.34" kWh
```

---

## 🚀 Production Ready

Dieses Setup ist nun **production-ready** mit:

- 🏆 Production-Grade SQL Query (300+ Zeilen, optimiert)
- 🏆 Intelligentes Template (150+ Zeilen Logik)
- 🏆 Benutzerfreundlicher Setup-Flow
- 🏆 Automatische Query-Generierung
- 🏆 Robuste Fehlerbehandlung
- 🏆 Vollständige Dokumentation

---

## 📞 Support

Siehe [ADVANCED_QUERY.md](ADVANCED_QUERY.md) für:
- Detaillierte Query-Erklärung
- Template-Logik Step-by-Step
- Beispiel-Szenarien
- Troubleshooting

Oder [README.md](README.md) für allgemeine Fragen.
