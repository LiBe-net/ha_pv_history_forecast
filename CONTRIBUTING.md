# Contributing to PV History Forecast

Danke, dass Sie an diesem Projekt beitragen möchten! Hier sind einige Richtlinien.

## Entwicklungsumgebung

```bash
# Repository klonen
git clone https://github.com/LiBe-net/ha_pv_history_forecast.git
cd ha_pv_history_forecast

# Dependencies installieren
pip install -r requirements.txt

# Code formatieren
black custom_components/

# Linting
flake8 custom_components/ --max-line-length=88
```

## Struktur des Projekts

```
ha_pv_history_forecast/
├── custom_components/pv_history_forecast/
│   ├── __init__.py                 # Integration Setup
│   ├── config_flow.py              # Konfigurationsflow
│   ├── const.py                    # Konstanten
│   ├── sensor.py                   # Sensor-Implementierung
│   ├── weather_helper.py           # Weather Template Helper
│   ├── manifest.json               # Integration Metadaten
│   ├── strings.json                # UI-Strings
│   ├── translations/               # Sprachübersetzungen
│   └── py.typed                    # Type hints Marker
├── README.md                       # Dokumentation
├── LICENSE                         # MIT License
└── EXAMPLE_CONFIGURATION.md        # Beispiele
```

## Code Standards

- **Stil**: Black Formatter (88 character line length)
- **Linting**: Flake8
- **Type Hints**: Verwenden Sie Type Hints überall wo möglich
- **Logging**: Benutzten Sie den integrierten Logger

## Testing

```bash
# Unit Tests ausführen
pytest tests/

# Coverage überprüfen
pytest --cov=custom_components tests/
```

## Erstelle einen Pull Request

1. Fork das Repository
2. Erstelle einen Feature Branch: `git checkout -b feature/meine-feature`
3. Commit änderungen: `git commit -am 'Add neue Feature'`
4. Push zum Branch: `git push origin feature/meine-feature`
5. Öffne einen Pull Request

## Meldung von Fehlern

Bitte verwenden Sie das GitHub Issue Template:

- **Titel**: Kurze Beschreibung des Fehlers
- **Beschreibung**: Detaillierte Beschreibung, Schritte zum Reproduzieren
- **Environment**: HA Version, Integration Version
- **Logs**: Relevante Error Logs

## Feature Requests

Feature Requests sind willkommen! Bitte erstellen Sie ein Issue mit:
- **Titel**: Kurze Feature-Beschreibung
- **Use Case**: Warum Sie dieses Feature benötigen
- **Mögliche Lösung**: Falls Ideen vorhanden

## Lizenz

Durch einen Beitrag erklären Sie sich einverstanden, dass Ihr Code unter der MIT License veröffentlicht wird.
