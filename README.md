# PV History Forecast

Eine Home Assistant HACS-Erweiterung für eine SQL-basierte PV-Ertragsprognose. Die Integration analysiert historische Bewölkungsverläufe und gleicht diese mit der aktuellen Wettervorhersage ab, um eine genaue Schätzung des verbleibenden und morgigen Solarertrags zu liefern.

## Features

- 🔋 **6 Sensoren automatisch**: Aus einer einzigen Konfiguration entstehen alle benötigten Sensoren
- 💾 **SQLite Integration**: Direkte Analyse der Home Assistant Datenbank (`home-assistant_v2.db`)
- 🌍 **Ortsgerechte Astronomie**: Tageslängenkorrekturen basierend auf dem konfigurierten Breitengrad (`hass.config.latitude`)
- 🌥️ **Historischer Vergleich**: Gleicht aktuelle Bewölkung mit tageweise ähnlichen historischen Tagen ab
- ⏱️ **UTC-korrekt**: Alle Zeitvergleiche intern in UTC — keine Fehler beim lokalen Tageswechsel
- 🔄 **Automatisches Update**: Alle 5 Minuten ohne weitere Konfiguration
- 📈 **Glättung**: Adaptiver EMA-Filter dämpft stündliche LTS-Grenzsprünge — keine sichtbaren Spikes mehr
- 📊 **Lovelace Card**: Vorgefertigte Markdown-Karte als Sensor-Attribut, direkt im Dashboard verwendbar
- 🌤️ **Auto-Cloud-Sensor**: Sammelt Bewölkungsdaten ab Tag 1 von der Wetter-Entity und baut LTS-Statistiken auf
- 🔍 **Intelligente Sensor-Auswahl**: Dropdowns zeigen nur passende Sensoren — Bewölkung nach % gefiltert, PV nach Geräteklasse `energy` + Statistiken aktiv

## Erzeugte Sensoren

Nach der Konfiguration mit Präfix `pv_hist` (Standard) stehen folgende Sensoren bereit:

| Sensor | Beschreibung |
|--------|--------------|
| `sensor.pv_hist_remaining_today` | Verbleibender PV-Ertrag heute (kWh), Hauptsensor — enthält auch `lovelace_card` im Attribut |
| `sensor.pv_hist_remaining_min` | Pessimistische Tagesrest-Prognose (ähnliche Tage mit mehr Bewölkung) |
| `sensor.pv_hist_remaining_max` | Optimistische Tagesrest-Prognose (ähnliche Tage mit weniger Bewölkung) |
| `sensor.pv_hist_tomorrow` | Gewichtete Prognose für den Gesamtertrag morgen (kWh) |
| `sensor.pv_hist_weather_forecast` | Interner Hilfssensor: stündliche Wettervorhersage (JSON) |
| `sensor.pv_hist_cloud_coverage` | Auto-Bewölkungssensor (nur wenn kein externer Cloud-Sensor gewählt) |

Die Präfix-Bezeichnung (`pv_hist`) wird beim Setup frei gewählt — alle Sensornamen leiten sich davon ab.

## Installation

1. Öffnen Sie **HACS** in Home Assistant
2. Klicken Sie auf **⋮ → Benutzerdefiniertes Repository hinzufügen**
3. URL: `LiBe-net/ha_pv_history_forecast`, Kategorie: **Integration** → **Hinzufügen**
4. Suchen Sie nach **"PV History Forecast"** und installieren Sie die Integration
5. Starten Sie Home Assistant neu
6. Öffnen Sie **Einstellungen → Geräte & Dienste → Integrationen** und fügen Sie **"PV History Forecast"** hinzu

Detaillierte Schritte: [INSTALLATION.md](INSTALLATION.md)

## Konfiguration

### Schritt 1 — Sensor-Präfix & Datenbank

| Feld | Standard | Beschreibung |
|------|---------|-------------|
| **Sensor-Präfix** | `pv_hist` | Basis für alle Sensornamen, z.B. `pv_hist` → `sensor.pv_hist_remaining_today` |
| **Datenbankverbindungs-URL** | *(leer lassen)* | Leer = Home Assistant DB (`sqlite:////config/home-assistant_v2.db`) |

### Schritt 2 — Sensoren auswählen

| Feld | Pflicht | Beschreibung |
|------|---------|-------------|
| **Wetter Entity** | ✅ Ja | `weather.*` Entity, z.B. `weather.forecast_home` |
| **PV Panel Energie Sensor** | ✅ Ja | Dropdown zeigt nur Sensoren mit `device_class: energy`, Einheit `kWh`/`Wh` und aktiven Statistiken — Wh-Sensoren werden automatisch in kWh umgerechnet |
| **Cloud Coverage Sensor** | Optional | Dropdown zeigt nur Sensoren mit Einheit `%` und aktuellem Messwert. Leer lassen → Auto-Sensor `sensor.{prefix}_cloud_coverage` wird angelegt. |
| **PV History Tage** | Standard: 30 | Wie viele Tage der Vergangenheit für den Vergleich herangezogen werden |

> Wetter-Vorhersage-Sensor und Wert-Templates werden **automatisch** konfiguriert — Diese Felder sind nicht mehr manuell einzustellen.

## SQL Query Struktur

Die Integration führt eine komplexe CTE-Kette auf der Home Assistant SQLite-Datenbank aus:

```
vars → ids → pv_activity → forecast_val → forecast_next_day
     → cloud_history → matching_days → final_data → json_group_array(...)
```

Das Ergebnis ist ein JSON-Array historischer Vergleichstage, gespeichert im Attribut `sql_raw_json` des Hauptsensors `sensor.pv_hist_remaining_today`.

Technische Details: [ADVANCED_QUERY.md](ADVANCED_QUERY.md)

## Wetter-Vorhersage-Sensor (automatisch)

Die Integration erstellt und pflegt `sensor.pv_hist_weather_forecast` automatisch alle **5 Minuten** über den HA-Service `weather.get_forecasts`. Eine manuelle `configuration.yaml`-Konfiguration ist **nicht nötig**.

## Lovelace Dashboard Card

Der Sensor `sensor.{prefix}_lovelace` rendert alle 15 Minuten eine vollständige Markdown-Karte mit Prognose, historischen Vergleichstagen und stündlicher Bewölkungstabelle. Die fertige Karte liegt im Attribut `lovelace_card` und kann in einer Lovelace Markdown-Card wie folgt verwendet werden:

```yaml
type: markdown
content: "{{ state_attr('sensor.pv_hist_remaining_today', 'lovelace_card') }}"
```

## Sensor-Anforderungen

### PV Panel Sensor
- **Erforderlich**: `state_class: total_increasing` (kumulativer Tageswert)
- **Erforderlich**: Einheit `kWh` **oder `Wh`** — Wh wird automatisch in kWh umgerechnet
- **Erforderlich**: Statistiken aktiv (`device_class: energy` genügt in HA 2024+)
- **Beispiel**: `sensor.pv_panels_energy`

- **Typ**: Prozent (%) mit aktuellem Messwert oder Weather-Entity
- **Beispiel**: `sensor.weather_cloud_coverage` oder `weather.forecast_home`
- Wenn leer gelassen, wird die gewählte Wetter-Entity automatisch als Quelle genutzt

## Prognose-Logik

Die Sensoren `_remaining_min` und `_remaining_max` bestimmen einen optimistischen und
pessimistischen Tagesrest, indem die 5 historisch ähnlichsten Tage (nach Bewölkung) aus der
Vergangenheit herausgefiltert und saisonal auf den heutigen Tag skaliert werden.

Die Skalierung berücksichtigt den genauen Breitengrad (`hass.config.latitude`) mit einer
astronomisch korrekten Tageslängenformel:

```
dl = 24/π · arccos(−tan(φ) · tan(δ))   mit  δ = −0.4093 · cos(2π·(doy+10)/365)
```

## Fehlerbehebung

### Sensoren zeigen 0 kWh / sql_raw_json ist `[]`
1. Überprüfen Sie, ob der PV-Sensor gestern tatsächlich Daten produziert hat (mind. 1 kWh)
2. Stellen Sie sicher, dass der Cloud-Coverage-Sensor Werte zwischen 0–100 liefert
3. Vergewissern Sie sich, dass die Wetter-Entity `cloud_coverage` in den Forecast-Attributen enthält
4. Lesen Sie die Home Assistant Logs auf SQL-Fehler (`Einstellungen → System → Protokolle`)

### Sensor `unavailable`
1. Entity IDs prüfen: **Einstellungen → Geräte & Dienste → Entitäten**
2. Integration neu laden: **Integration → ⋮ → NEULADEN**

## Versions-History

| Version | Änderungen |
|---------|-----------|
| **0.1.0** | Initiale Veröffentlichung unter neuem Namen `pv_history_forecast`. Enthält alle Features: 6 Sensoren, adaptiver EMA-Filter, 5-Minuten-Intervall, intelligente Sensor-Dropdowns, Wh→kWh-Normierung, UTC-korrektes Tagesgruppieren (DST-sicher), Lovelace Card als Attribut des Hauptsensors, Auto-Cloud-Sensor. |

## Lizenz

MIT License — Siehe [LICENSE](LICENSE)

## Support

Fragen und Fehlerberichte bitte als Issue im [GitHub Repository](https://github.com/LiBe-net/ha_pv_history_forecast/issues) melden.

