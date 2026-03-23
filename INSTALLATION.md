# Installationsanleitung

## Home Assistant HACS Installation

### Voraussetzungen
- Home Assistant 2024.1.0 oder neuer
- HACS (Home Assistant Community Store) installiert
- Zugriff auf custom_components Verzeichnis
- **SQLite als Datenbank** (erforderlich - MySQL/PostgreSQL werden nicht unterstützt)

### Schritt 1: Integration über HACS hinzufügen

1. Öffnen Sie **HACS** in Home Assistant
2. Klicken Sie auf **⋮ → Benutzerdefiniertes Repository hinzufügen**
3. Repository-URL: `LiBe-net/ha_pv_history_forecast`, Kategorie: **Integration** → **Hinzufügen**
4. Suchen Sie nach **"PV History Forecast"** und klicken Sie **Installieren**
5. Starten Sie Home Assistant neu

### Schritt 2: Grundkonfiguration

Der Konfigurationsassistent führt Sie in zwei Schritten:

#### Schritt 2a — Sensor-Präfix & Datenbank

| Feld | Voreinstellung | Beschreibung |
|------|---------------|-------------|
| **Sensor-Präfix** | `pv_hist` | Basis aller Sensornamen. `pv_hist` → `sensor.pv_hist_remaining_today` etc. |
| **Datenbankverbindungs-URL** | *(leer lassen)* | Leer = HA-Standard: `sqlite:////config/home-assistant_v2.db` |

#### Schritt 2b — Sensoren auswählen

**⚠️ WICHTIG**: Die Felder zeigen nur **geeignete Sensoren aus Ihrer Home Assistant Installation** an.

**Dropdown-Listen**:

| Feld | Liste zeigt | Pflicht |
|------|------------|---------|
| **Wetter Entity** | Alle `weather.*` Entitäten | ✅ Ja |
| **PV Panel Energie Sensor** | Sensoren mit `device_class: energy`, Einheit `kWh`/`Wh` **und aktiven Statistiken** | ✅ Ja |
| Cloud Coverage Sensor | Sensoren mit Einheit `%` und aktuellem Messwert | Optional |
| **PV History Tage** | Zahl (Standard: 30) | Standard reicht |

> **Cloud Coverage Sensor leer lassen**: Die Integration erstellt automatisch `sensor.{prefix}_cloud_coverage`, der Bewölkungswerte von der Wetter-Entity sammelt. ⚠️ LTS-Statistiken (>10 Tage Verlauf) wachsen erst im Laufe der Betriebszeit — in der Zwischenzeit greift der SQL-Fallback auf die States der Wetter-Entity zurück.
>
> **Wh-Sensor**: Wird ein PV-Sensor mit Einheit `Wh` ausgewählt, rechnet die SQL-Query automatisch durch `1000` — die Prognose-Sensoren liefern stets kWh.
>
> **Wetter-Vorhersage-Sensor** und **Wert-Template** sind **nicht mehr konfigurierbar** — diese werden von der Integration automatisch verwaltet.

**Falls die Felder leer sind**:
1. **Wetter Entity nicht vorhanden?**
   - Konfigurieren Sie eine Wetter-Integration (z.B. OpenWeatherMap, Met.no)
   - **Einstellungen → Geräte & Dienste → + Integration hinzufügen**

2. **PV-Sensor nicht vorhanden?**
   - Überprüfen Sie **Einstellungen → Geräte & Dienste → Entitäten**
   - Der PV-Sensor benötigt `device_class: energy` und Einheit `kWh` oder `Wh` mit aktiven Statistiken

3. Nach dem Hinzufügen neuer Entitäten: Home Assistant **neu starten**, dann erneut konfigurieren

### Schritt 3: Automatische Sensor-Erstellung

Nach der Konfiguration erstellt die Integration automatisch bis zu **6 Sensoren**:

| Sensor | Beschreibung |
|--------|--------------|
| `sensor.{prefix}_remaining_today` | Verbleibender Ertrag heute (Hauptsensor mit SQL-Daten + Attribut `lovelace_card`) |
| `sensor.{prefix}_remaining_min` | Pessimistische Tagesrest-Prognose |
| `sensor.{prefix}_remaining_max` | Optimistische Tagesrest-Prognose |
| `sensor.{prefix}_tomorrow` | Gewichtete Prognose Gesamtertrag morgen |
| `sensor.{prefix}_weather_forecast` | Interner Wetter-Vorhersage-Sensor (JSON) |
| `sensor.{prefix}_cloud_coverage` | Auto-Bewölkungssensor *(nur wenn kein externer Cloud-Sensor gewählt)* |

Mit Standard-Präfix `pv_hist` heißt der Hauptsensor also `sensor.pv_hist_remaining_today`.

Der Wetter-Forecast-Abruf startet automatisch alle 15 Minuten — **keine manuelle `configuration.yaml`-Konfiguration nötig**.

### Schritt 4: Datenbank-URL (SQLite)

**Nur SQLite wird unterstützt!** Die Integration liest direkt die Home Assistant Datenbank.

Lassen Sie das Feld **leer**, um die Standard-HA-Datenbank zu nutzen:
```
sqlite:////config/home-assistant_v2.db
```

Nur wenn Ihre Datenbank an einem anderen Pfad liegt, tragen Sie eine benutzerdefinierte URL ein, z.B.:
```
sqlite:////config/ha.db
```

## Manuelle Installation (ohne HACS)

### Schritt 1: Dateien kopieren

```bash
# Repository klonen
git clone https://github.com/LiBe-net/ha_pv_history_forecast.git

# Dateien in custom_components kopieren
cp -r ha_pv_history_forecast/custom_components/pv_history_forecast /path/to/homeassistant/custom_components/
```

### Schritt 2: Home Assistant neu starten

Starten Sie Home Assistant neu, damit die neue Integration erkannt wird.

### Schritt 3: Konfiguration über UI

Folgen Sie dann den Schritten der GUI-Konfiguration (Schritt 1-4 oben).

## Konfiguration von Sensoren

### Lovelace Dashboard Card

Der Sensor `sensor.{prefix}_lovelace` rendert automatisch eine vollständige Markdown-Karte. Diese kann direkt in einer **Lovelace Markdown-Card** verwendet werden:

```yaml
type: markdown
content: "{{ state_attr('sensor.pv_hist_remaining_today', 'lovelace_card') }}"
```

Die Karte enthält Prognose, historische Vergleichstage und stündliche Bewölkungstabelle.

### PV Sensor Konfiguration

Der PV Sensor sollte folgende Eigenschaften haben:

```yaml
sensor:
  - platform: integration
    source: sensor.pv_input  # Ersetzen Sie mit Ihrer Power Entity
    name: pv_panels_energy
    unit_prefix: k
    unique_id: pv_panels_energy
    device_class: energy
    unit_of_measurement: kWh
```

> **Tipp**: Die genaue Konfiguration hängt von Ihrem PV-System ab (z.B. SolarEdge, Fronius, etc.)

## Überprüfung der Installation

Nach erfolgreicher Installation können Sie überprüfen:

1. **Integrations-Seite**: "SQL PV Forecast Sensor" sollte in der Liste erscheinen
2. **Neu hinzugefügter Sensor**: Ein neuer Sensor `sensor.pv_hist_forecast` sollte in der Entity-Liste vorhanden sein
3. **Logs überprüfen**: Gagen Sie nach Errors in den Home Assistant Logs

## Fehlerbehandlung

### Integration nicht sichtbar
- Stellen Sie sicher, dass HACS installiert ist
- Überprüfen Sie, dass Home Assistant neu gestartet wurde
- Versuchen Sie die Seite neu zu laden (Ctrl+F5)

### Datenbank-Verbindungsfehler
- Überprüfen Sie die URL auf Tippfehler
- Stellen Sie sicher, dass die Datenbank erreichbar ist
- Überprüfen Sie Benutzerrechte (bei MySQL/PostgreSQL)

### Sensoren nicht verfügbar
- Überprüfen Sie, dass die Entity IDs korrekt sind
- Stellen Sie sicher, dass die Sensoren selbst verfügbar sind
- Überprüfen Sie die Integration-Logs

### Fehler: "Entity is neither a valid entity ID nor a valid UUID"

**Ursache**: Dies sollte nicht mehr vorkommen. Die aktuelle Version verwendet ausschließlich Dropdown-Listen.

**Falls dieser Fehler weiterhin auftritt**, starten Sie Home Assistant neu und konfigurieren Sie die Integration erneut. Stellen Sie sicher, dass HACS die aktuellste Version installiert hat (v1.1.1+).

## Nächste Schritte

Siehe [README.md](README.md) für:
- Detaillierte Feature-Dokumentation
- Template Beispiele
- SQL Query Struktur
- Erweiterte Konfiguration

## Support

- GitHub Issues: https://github.com/LiBe-net/ha_pv_history_forecast/issues
- Home Assistant Forum: https://community.home-assistant.io/
