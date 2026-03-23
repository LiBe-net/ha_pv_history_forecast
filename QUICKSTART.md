# Quick-Start Guide

## 5 Minuten bis zum funktionierenden Sensor

### 1. Integration installieren

```bash
# Option A: HACS (empfohlen)
# HACS → ⋮ → Benutzerdefiniertes Repository → LiBe-net/ha_pv_history_forecast (Integration)
# "PV History Forecast" suchen und installieren

# Option B: Manuell
git clone https://github.com/LiBe-net/ha_pv_history_forecast.git
cp -r ha_pv_history_forecast/custom_components/pv_history_forecast ~/.homeassistant/custom_components/
```

### 2. Neu starten
```
Einstellungen → System → Neustart
```

### 3. Integration hinzufügen

1. **Einstellungen → Geräte & Dienste → Integrationen**
2. **+ INTEGRATION ERSTELLEN**
3. **"PV History Forecast"** suchen und auswählen

### 4. Grundeinstellungen

Folgen Sie dem zweiteiligen Assistenten:

**Schritt 1 — Präfix & Datenbank:**
```
Sensor-Präfix: pv_hist  (Standard, bestimmt alle Sensornamen)
DB URL:        (leer lassen → nutzt home-assistant_v2.db)
```

**Schritt 2 — Sensoren:**
```
Wetter Entity:              weather.forecast_home  (Ihre HA Wetter Entity)
PV Panel Energie:           sensor.pv_panels_energy  (kWh oder Wh, device_class: energy)
Cloud Coverage Sensor:      (leer lassen → Auto-Sensor wird angelegt)
PV History Tage:            30  (Standard)
```

**Hinweis**: Nur SQLite wird unterstützt. Dropdowns zeigen nur passende Sensoren (PV: energy+kWh/Wh+Statistiken; Cloud: %-Sensoren mit Messwert). Wetter-Forecast-Sensor und Templates werden **automatisch** eingerichtet.

### 5. Fertig! 🎉

Sie sollten jetzt bis zu **7 neue Sensoren** haben:
```
sensor.pv_hist_remaining_today   ← verbleibender Ertrag heute
sensor.pv_hist_remaining_min     ← pessimistische Prognose
sensor.pv_hist_remaining_max     ← optimistische Prognose
sensor.pv_hist_tomorrow          ← Gesamtprognose morgen
sensor.pv_hist_weather_forecast  ← interner Wetter-Sensor
sensor.pv_hist_cloud_coverage    ← Auto-Bewölkungssensor (wenn kein externer gewählt)
sensor.pv_hist_lovelace          ← vorberechnete Lovelace Markdown-Card
```

**Lovelace Card einbinden:**
```yaml
type: markdown
content: "{{ state_attr('sensor.pv_hist_lovelace', 'lovelace_card') }}"
```

## Minimal Setup ohne existierende Sensoren

Falls Sie noch keine Sensoren haben, erstellen Sie diese zuerst:

### PV-Energie-Sensor (Beispiel `configuration.yaml`)

```yaml
sensor:
  - platform: integration
    source: sensor.pv_input_power   # Ihre Leistungs-Entity ersetzen
    name: pv_panels_energy
    unique_id: pv_panels_energy
    unit_prefix: k
    device_class: energy
    state_class: total_increasing
    unit_of_measurement: kWh
```

### Cloud Coverage Sensor (optional)

Wenn kein Cloud Coverage Sensor angegeben wird, legt die Integration automatisch `sensor.{prefix}_cloud_coverage` an. Dieser spiegelt die `cloud_coverage` der Wetter-Entity und baut über die Laufzeit LTS-Statistiken auf. Der SQL-Fallback sorgt dafür, dass auch ohne vorhandene Statistiken sofort Daten vorliegen.

### Integration hinzufügen

Danach:
1. Speichern und **Einstellungen → Automatisierungen & Szenen → Vorlagen → RELOAD TEMPLATES**
2. Integration hinzufügen wie oben beschrieben

## Behebung häufiger Probleme

### Sensor zeigt "unavailable"

1. Entity IDs überprüfen:
   ```
   Einstellungen → Geräte & Dienste → Entitäten
   ```

2. Integration neu laden:
   ```
   Einstellungen → Geräte & Dienste → SQL PV Forecast → ⋮ → NEULADEN
   ```

3. Logs überprüfen:
   ```
   Einstellungen → Systemverwaltung → Protokolle
   ```

### Datenbank-Fehler

```
Error: invalid_db_url
```

Überprüfen Sie die Verbindungs-URL (nur SQLite unterstützt):
- Standard HA-DB: `sqlite:////config/home-assistant_v2.db`
- Benutzerdefiniert: `sqlite:////config/ha.db`

### Sensoren nicht in der Liste

Falls Ihre Sensoren nicht in der Dropdown-Liste erscheinen:
1. Sie müssen existierende Entitäten in HA sein
2. Home Assistant neu starten und erneut versuchen
3. PV-Sensor benötigt `state_class: total_increasing`

## Nächste Schritte

- 📖 Siehe [README.md](README.md) für Dokumentation
- 📋 Siehe [INSTALLATION.md](INSTALLATION.md) für Details
- 🔧 Siehe [API.md](API.md) für erweiterte Nutzung
- 💬 [GitHub Issues](https://github.com/LiBe-net/ha_pv_history_forecast/issues) für Support

## Tipps & Tricks

### Automation für Weekly Update
```yaml
alias: "Update PV Forecast Sensor Weekly"
trigger:
  - platform: time
    at: "08:00:00"
action:
  - service: homeassistant.reload_custom_components
```

### Notifications
```yaml
alias: "Notify on High PV Forecast"
trigger:
  - platform: numeric_state
    entity_id: sensor.pv_hist_forecast
    above: 5.0
action:
  - service: notify.mobile_app
    data:
      message: "PV Forecast: {{ states('sensor.pv_hist_forecast') }} kWh"
```

### Dashboard Card
```yaml
type: entity
entity: sensor.pv_hist_forecast
name: "PV Forecast"
unit: kWh
state_color: true
```

## FAQ

**F: Kann ich mehrere Instanzen haben?**
A: Ja! Erstellen Sie einfach mehrere Integration-Instanzen mit verschiedenen Namen.

**F: Welche Datenbank wird empfohlen?**
A: Für kleine Installationen: SQLite. Für größere: PostgreSQL.

**F: Wie oft wird updated?**
A: Standardmäßig alle 15 Minuten (konfigurierbar).

**F: Kann ich das History importieren?**
A: Ja, über die Datenbank-API oder Skripte.

Viel Erfolg! 🚀
