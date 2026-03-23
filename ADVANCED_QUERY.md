# Advanced SQL Query & Template - Dokumentation

## Überblick

Die SQL PV Forecast Integration enthält nun eine produktionsreife **Advanced SQL Query** mit **fortgeschrittenem Jinja2 Template**, die eine hochgenaue PV-Ertragsprognose basierend auf historischen, ähnlichen Tagen berechnet.

## SQL Query Struktur

### 1. **vars CTE** - Konfigurationsvariablen
```sql
WITH vars AS (
    SELECT 
        'weather.home' as sensor_clouds,
        'sensor.pv_panels_energy' as sensor_pv,
        'sensor.weather_forecast_hourly' as sensor_forecast,
        (strftime('%s', 'now', 'localtime') - strftime('%s', 'now')) || ' seconds' as offset
)
```

**Erklärung:**
- `sensor_clouds`: Weather Entity oder Cloud Coverage % Sensor
- `sensor_pv`: PV Panel Energie Sensor mit Total-Increasing State Class
- `sensor_forecast`: Wetter-Vorhersage Sensor (JSON-Format)
- `offset`: Zeitzonen-Offset für lokale Datumsberechnung (z.B. '+3600 seconds' für UTC+1)

---

### 2. **ids CTE** - Home Assistant Datenbank IDs
```sql
ids AS (
    SELECT 
        (SELECT id FROM statistics_meta WHERE statistic_id = (SELECT sensor_clouds FROM vars)) as w_id_stats,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_clouds FROM vars)) as w_id_states,
        -- ... weitere IDs für PV und Forecast Sensoren
)
```

**Zweck:** Holt die internen IDs der Sensoren aus der Home Assistant Datenbank für optimierte Queries.

---

### 3. **pv_activity CTE** - Sonnenauf- und Untergangszeiten
```sql
pv_activity AS (
    SELECT 
        COALESCE((
            SELECT strftime('%H:%M', last_updated_ts, 'unixepoch') 
            FROM states 
            WHERE metadata_id = (SELECT p_id_states FROM ids) 
              AND date(...) = date('now', ..., '-1 day') 
              AND state NOT IN ('unknown','0','0.0','unavailable') 
            ORDER BY last_updated_ts ASC LIMIT 1
        ), '05:30') as sun_start,
        COALESCE((
            -- Letzter Zeitpunkt, an dem der Sensor noch UNTER seinem Tagesmaximum lag
            -- = letzter aktiver Produktionszeitpunkt (kumulativer Sensor bleibt
            --   nach Sonnenuntergang auf dem Maximalwert bis Mitternacht)
            SELECT strftime('%H:%M', last_updated_ts, 'unixepoch') 
            FROM states 
            WHERE metadata_id = (SELECT p_id_states FROM ids) 
              AND date(...) = date('now', ..., '-1 day') 
              AND state NOT IN ('unknown', 'unavailable', '')
              AND CAST(state AS FLOAT) < (
                  SELECT MAX(CAST(state AS FLOAT)) FROM states
                  WHERE metadata_id = (SELECT p_id_states FROM ids)
                    AND date(...) = date('now', ..., '-1 day')
                    AND state NOT IN ('unknown', 'unavailable', '')
              )
            ORDER BY last_updated_ts DESC LIMIT 1
        ), '17:30') as sun_end
    FROM ids
)
```

**Zweck:**
- `sun_start`: Erster gültiger State gestern (> 0, nicht unavailable) → Sonnenaufgang
- `sun_end`: Letzter State **vor** dem kumulativen Tagesmaximum → Sonnenuntergang
- Fallbacks: `'05:30'` für Aufgang, `'17:30'` für Untergang wenn keine Daten vorhanden

**Wichtig**: `sun_start` und `sun_end` verwenden **unterschiedliche** Logiken. `sun_start` sucht den ersten positiven State (`ASC`); `sun_end` sucht den letzten State unter dem Tagesmax (`DESC`, `< MAX()`). Ein kumulativer Sensor würde sonst nach Sonnenuntergang auf dem Maximalwert verbleiben und zu einem falschen `sun_end` führen.

---

### 4. **forecast_val CTE** - Bewölkung heute (restlicher Tag)
```sql
forecast_val AS (
    SELECT COALESCE(
        (SELECT AVG(CAST(json_extract(f.value, '$.cloud_coverage') AS FLOAT)) 
         FROM states s 
         JOIN state_attributes a ON s.attributes_id = a.attributes_id, 
         json_each(a.shared_attrs, '$.forecast') f 
         WHERE ...
           AND substr(json_extract(f.value, '$.datetime'), 1, 10) = date('now', ...)
           AND substr(json_extract(f.value, '$.datetime'), 12, 5) 
               BETWEEN strftime('%H:%M', 'now') AND sun_end
        ), 50.0) as f_avg
)
```

**Berechnet:** Durchschnittliche Bewölkung ab JETZT bis Sonnenuntergang (heute).

---

### 5. **forecast_next_day CTE** - Bewölkung morgen (ganzer Tag)
```sql
forecast_next_day AS (
    SELECT COALESCE((...), 50.0) as f_avg_morgen
)
```

**Berechnet:** Durchschnittliche Bewölkung für morgen (Sonnenauf bis -untergang).

---

### 6. **cloud_history CTE** - 60-Tage Bewölkungs-Historie
```sql
cloud_history AS (
    SELECT start_ts as ts, CAST(...) as val 
    FROM statistics 
    WHERE metadata_id = (SELECT w_id_stats FROM ids) 
      AND start_ts > strftime('%s', 'now', '-60 days')
    UNION ALL
    SELECT s.last_updated_ts as ts, ... 
    FROM states s 
    WHERE ... AND s.last_updated_ts > strftime('%s', 'now', '-10 days')
)
```

**Kombiniert:**
- Langzeit-Statistiken (60 Tage)
- Kurzfristige States (10 Tage)
- Unterstützt beide: Weather Entities & % Sensoren

---

### 7. **matching_days CTE** - Historische Vergleichstage
```sql
matching_days AS (
    SELECT 
        date(ts, 'unixepoch') as day, 
        AVG(...between sun_start and sun_end...) as h_avg_total_val,
        AVG(...from now until sun_end...)         as h_avg_rest_val
    FROM cloud_history 
    WHERE date(...) < date('now', ...) 
    GROUP BY 1 
    HAVING h_avg_total_val IS NOT NULL AND h_avg_total_val > 0
    ORDER BY ABS(
        COALESCE(h_avg_rest_val, h_avg_total_val) - (SELECT f_avg FROM forecast_val)
    ) ASC
)
```

**Logik:**
- Gruppiert tägliche Bewölkung aus `cloud_history`
- Sortiert nach Ähnlichkeit zum heutigen Rest-Forecast (`h_avg_rest_val`) — fällt dieser weg (sehr früh morgens), wird der Tages-Gesamtdurchschnitt (`h_avg_total_val`) als Fallback genutzt (`COALESCE`)
- Der ähnlichste Tag steht an Position 1

---

### 8. **final_data CTE** - PV Ertragsdaten der Match-Tage
```sql
final_data AS (
    SELECT 
        md.*,
        (SELECT MAX(state) FROM statistics WHERE ... AND date(...) = md.day) as day_max,
        (SELECT MIN(state) FROM statistics WHERE ... AND date(...) = md.day AND state > 0) as day_min,
        -- Hourly Werte für aktuelle und vorherige Stunde
)
```

**Holt:** Ertragsdaten (Min, Max, Stunden-Werte) für die gefundenen historischen Tage.

---

### 9. **Finale SELECT** - JSON-Aggregation
```sql
SELECT json_group_array(
    json_object(
        'datum', day,
        'f_avg_heute_rest', ROUND(f_avg, 1),        
        'f_avg_morgen', ROUND(f_avg_morgen, 1),
        'h_avg_gesamt', ROUND(h_avg_total_val, 1),
        'h_avg_rest', ROUND(h_avg_rest_val, 1),
        'ertrag_tag_gesamt', ROUND(day_max - day_min, 2),
        'ertrag_tag_rest', ROUND(...),
        'pv_start', sun_start,
        'pv_ende', sun_end
    )
) as json 
FROM final_data 
WHERE day_max > 0
```

**Rückgabe:** JSON-Array mit allen historischen Tagen und ihren Daten.

---

## Jinja2 Template Dokumentation

### Template-Variablen

| Variable | Typ | Quelle |
|----------|-----|--------|
| `value` | str | `sql_raw_json`-Attribut des Hauptsensors |
| `latitude` | float | `hass.config.latitude` — HA-Standort-Konfiguration |

```jinja2
{% set raw = value %}
{% set data = raw | from_json %}
{% if raw and raw != '[]' and raw is not none %}
  {# Verarbeitung ... #}
{% else %}
  0.0
{% endif %}
```

### 1. Astronomische Basisdaten (ortsgenau)
```jinja2
{% set doy = now().strftime('%j') | int %}
{% set lat_rad = latitude * pi / 180 %}
{% set decl = -0.4093 * cos(2 * pi * (doy + 10) / 365) %}
{% set dl_today = 24 / pi * acos([[(-tan(lat_rad) * tan(decl)), -1.0] | max, 1.0] | min) %}
{% set sun_today = 0.65 + 0.35 * cos((doy - 172) * 2 * pi / 365) %}
```

**Berechnet:**
- `decl`: Sonnendeklination δ (astronomisch korrekt)
- `dl_today`: Tageslänge in Stunden — exakte Formel: `24/π · arccos(−tan(φ)·tan(δ))`
- `sun_today`: Relative Sonnenscheinstärke (0.3–1.0, Jahresgang)
- `latitude` kommt aus `hass.config.latitude` (HA-Standort-Konfiguration)

**Beispiele (Breite 48°N):**
- Sommer (Tag 172): `dl_today ≈ 15.8 h`
- Winter (Tag 355): `dl_today ≈ 8.4 h`
- Equator (0°): `dl_today = 12.0 h` konstant

---

### 2. Saisonale Skalierung historischer Erträge

Für jeden historischen Vergleichstag wird der Ertrag auf den heutigen Tag skaliert:

```jinja2
{% set item_day = as_datetime(item.datum).strftime('%j') | int %}
{% set decl_i = -0.4093 * cos(2 * pi * (item_day + 10) / 365) %}
{% set dl_item = 24 / pi * acos([[(-tan(lat_rad) * tan(decl_i)), -1.0] | max, 1.0] | min) %}
{% set sun_item = 0.65 + 0.35 * cos((item_day - 172) * 2 * pi / 365) %}
{% set s_korr = (sun_today / sun_item) * (dl_today / dl_item) %}
{% set yield_korr = item.ertrag_tag_rest | float * s_korr %}
```

**`s_korr`**: Kombinierter Skalierungsfaktor aus Tageslänge und Sonnenscheinstärke. Beispiel: Ein Juliertrag von 15 kWh, auf Dezember angewendet → `s_korr ≈ 0.35` → 5.25 kWh.

---

### 3. Top-5 Ähnlichkeitsauswahl & 3-Fall-Logik

```jinja2
{% set diff = (item.h_avg_rest | float - f_avg) | abs %}
{# ... Pool aufbauen ... #}
{% set top5 = (ns_pool.items | sort(attribute='diff'))[:5] %}
{% set brighter = top5 | selectattr('h_avg', 'lt', f_avg) | list %}
{% set darker   = top5 | selectattr('h_avg', 'gt', f_avg) | list %}

{% if brighter | count > 0 and darker | count == 0 %}
  {# FALL A: Alle heller als Forecast → Worst-Case #}
  {% set worst = brighter | sort(attribute='y_korr') | first %}
  {% set res = worst.y_korr * ([120 - f_avg, 5.0] | max / [120 - worst.h_avg, 5.0] | max) %}
{% elif darker | count > 0 and brighter | count == 0 %}
  {# FALL B: Alle dunkler als Forecast → Minimum nehmen #}
  {% set res = darker | map(attribute='y_korr') | min %}
{% else %}
  {# FALL C: Gemischt → Minimum der Top-5 #}
  {% set res = top5 | map(attribute='y_korr') | min %}
{% endif %}
```

**3-Fall-Logik (MIN-Template):**

| Szenario | Annahme | Ergebnis |
|----------|---------|---------|
| **A: Alle historical heller** (forecast trüber) | Es wird mindestens so dunkel wie der trübste Hist.-Tag | Worst-Case, proportional skaliert |
| **B: Alle historical dunkler** (forecast heller) | Es wird mindestens so hell wie der hellste Hist.-Tag | Minimum der dunklen Tage |
| **C: Gemischt** | Unklare Tendenz | Minimum aller Top-5 |

Das MAX-Template verwendet dasselbe Prinzip, gibt aber `max` statt `min` zurück.

---

### 4. UTC-Nacht-Prüfung

```jinja2
{% set now_min = utcnow().hour * 60 + utcnow().minute %}
{% set ende_min = (pv_ende.split(':')[0] | int) * 60 + (pv_ende.split(':')[1] | int) %}
{% set offset_min = (now().utcoffset().total_seconds() / 60) | int %}
{% set midnight_utc_min = (24 * 60 - offset_min) % (24 * 60) %}
{% if ende_min <= now_min < midnight_utc_min %}
  0.0  {# Nach Sonnenuntergang / vor Mitternacht UTC → kein Restbetrag #}
{% endif %}
```

Alle Zeitvergleiche erfolgen in UTC-Minuten seit Tagesbeginn — kein Fehler beim lokalen Tageswechsel.

---

## Beispiel-Szenario: 15. Juni (Sommertag, 48°N)

```
Eingang:
- Heute Bewölkung Forecast: 35% (Rest des Tages)
- pv_start = '04:47', pv_ende = '17:23' (UTC, aus SQL)
- `latitude` = 48.2

Astronomie:
- doy = 166, lat_rad = 0.841
- decl = -0.013 rad
- dl_today = 15.6 h
- sun_today = 0.99

Historische Vergleichstage (Auswahl):
  1. 12. Jun: 33% Bewölkung, 18.2 kWh Rest           → diff = 2,  s_korr ≈ 1.01  → 18.4 kWh
  2. 20. Jun: 41% Bewölkung, 16.5 kWh Rest           → diff = 6,  s_korr ≈ 0.99  → 16.3 kWh
  3. 08. Sep: 30% Bewölkung, 11.1 kWh Rest (Herbst)  → diff = 5,  s_korr ≈ 1.28  → 14.2 kWh

Top-5 sortiert nach diff = [Tag12Jun, Tag8Sep, Tag20Jun, ...]
brighter = [Tag8Sep (30% < 35%)]
darker   = [Tag20Jun (41% > 35%)]

→ FALL C (gemischt) → MIN = 14.2 kWh

Ergebnis: 14.2 kWh verbleibend (pessimistisches MIN)
```

---

## Integration in Home Assistant

### Setup
1. Öffnen Sie: **Einstellungen → Geräte & Dienste → Integrationen**
2. Klicken Sie: **+ INTEGRATION ERSTELLEN**
3. Suchen Sie: **"SQL PV Forecast Sensor"**
4. Folgen Sie dem Assistenten:
   - **Schritt 1:** Sensor-Präfix (Standard: `pv_hist`) + optionale Datenbankverbindungs-URL
   - **Schritt 2:** Wetter-Entity, PV Energie Sensor, optional Cloud Coverage Sensor

### Automatische Query-Generierung
Die SQL-Query wird vollständig automatisch mit den gewählten Sensoren erzeugt:
```sql
WITH vars AS (
    SELECT 
        'weather.forecast_home' as sensor_clouds,      -- Aus UI gewählt
        'sensor.pv_panels_energy' as sensor_pv,        -- Aus UI gewählt
        'sensor.pv_hist_weather_forecast' as sensor_forecast,  -- Automatisch
        (strftime('%s', 'now', 'localtime') - strftime('%s', 'now')) || ' seconds' as offset
)
```

### Automatisches Template
Die Prognose-Templates für alle 4 Wert-Sensoren werden automatisch angewendet, mit:
- ✅ Ortsgerechte astronomische Tageslängenkorrektur (Breitengrad)
- ✅ Saisonale Skalierung historischer Erträge auf das aktuelle Datum
- ✅ Top-5 Ähnlichkeitsauswahl nach Bewölkungsdelta
- ✅ 3-Fall-Logik: pessimistisch / optimistisch / gemischt
- ✅ UTC-korrekte Nacht-Prüfung (kein Fehler beim lokalen Tageswechsel)

---

## Performance-Optimierungen

### Query Optimierungen
- **Statistiken**: Nutzt Home Assistant's `statistics` Tabelle (aggregierte Stunden)
- **States**: Ergänzt mit rohen States der letzten 10 Tage
 - **IDs**: Nutzt vorgecachte Metadaten-IDs statt Entity-String-Suchen
- **Indexes**: Empfohlen: `CREATE INDEX idx_ts ON cloud_history(ts)`

### Template Optimierungen
- **Kosinus-Funktionen**: Pre-berechnete Sinus-Werte für Geschwindigkeit
- **Gewichtungs-Limitierung**: `[diff, 0.5] | max` verhindert Division durch sehr kleine Zahlen
- **Pool-Filterung**: Filtert irrelevante Tage vor Verarbeitung

---

## Troubleshooting

### Query liefert keine Daten
```
Überprüfen Sie:
1. Sensor-IDs in der Datenbank vorhanden?
2. Home Assistant Statistics konfiguriert?
3. Datenbank hat Zugang zu states_meta & statistics_meta?
```

### Template liefert immer 0
```
Überprüfen Sie:
1. SQL Query gibt gültiges JSON zurück?
2. Historische Daten vorhanden (>5 Tage)?
3. day_max > 0 in der SQL Query?
```

### Prognose ist zu hoch/niedrig
```
Template Debug aktivieren:
- Überprüfen Sie saisonale Korrektur
- Überprüfen Sie Gewichtungs-Logik
- Verifizieren Sie Schnee-Faktor (Winter)
```

---

## Weitere Ressourcen

- [README.md](README.md) - Hauptdokumentation
- [INSTALLATION.md](INSTALLATION.md) - Installation
- [API.md](API.md) - API Referenz
- [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md) - Projektstruktur
