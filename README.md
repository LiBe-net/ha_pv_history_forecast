# PV History Forecast

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

A Home Assistant HACS integration that forecasts the remaining and tomorrow's PV solar yield,
based on historical yield data from the Home Assistant database.

**Documentation:** [www.libe.net/pv-forecast](https://www.libe.net/pv-forecast)

## Installation

1. Open **HACS** → **⋮ → Add custom repository**
2. URL: LiBe-net/ha_pv_history_forecast — Category: **Integration**
3. Install **PV History Forecast** and restart Home Assistant
4. **Settings → Devices & Services** → Add **PV History Forecast**

## Lovelace Card

```yaml
type: markdown
content: >-
  Forecast remaining today: <b><big>{{ states.sensor.pv_hist_remaining_today.state | round(2)}} kWh</big></b> 
  remaining cloud cover: <b><big>{{ states.sensor.pv_hist_cloud_remaining_today.state }}%</big></b>
  
  {{ state_attr('sensor.pv_hist_remaining_today', 'lovelace_card_remaining_today') }}
```

```yaml
type: markdown
content: >-
  Forecast remaining for tomorrow: <b><big>{{ states.sensor.pv_hist_tomorrow.state | round(2)}} kWh</big></b>
  Cloud cover tomorrow: <b><big>{{ states.sensor.pv_hist_cloud_tomorrow.state }}%</big></b>
  
  {{ state_attr('sensor.pv_hist_remaining_today', 'lovelace_card_tomorrow') }}
```
## License

MIT — see [LICENSE](LICENSE)
