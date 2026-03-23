{# =================================================================
   PV-Tages-Restprognose – Lovelace Markdown Card (Variante B: Inline-Template)
   Quellsensor:    sensor.pv_hist_remaining_today  (Attribut: sql_raw_json)
   Forecast-Sensor: sensor.pv_hist_weather_forecast (Attribut: forecast)

   EMPFOHLEN: Statt dieses Inline-Templates lieber Variante A verwenden:
   {{ state_attr('sensor.pv_hist_remaining_today', 'lovelace_card') }}

   Variante B: Diesen Inhalt direkt als Lovelace-Markdown-Card nutzen.
   ================================================================= #}
{% set raw_json = state_attr('sensor.pv_hist_remaining_today', 'sql_raw_json') %}
{% if raw_json and raw_json != '[]' and raw_json is not none %}
  {% set data = raw_json | from_json %}

  {% if data | length > 0 %}
    {% set f_avg = data[0].f_avg_heute_rest | float(default=50.0) %}

    {# 1. SAISONALE SCHNEE-ERKENNUNG (Dez / Jan / Feb) #}
    {% set current_month = now().month %}
    {% set schnee_faktor_heute = 1.0 %}
    {% if current_month in [12, 1, 2] %}
      {% set gestern_datum = (now() - timedelta(days=1)).strftime('%Y-%m-%d') %}
      {% set gestern_data = data | selectattr('datum', 'equalto', gestern_datum) | list | first %}
      {% if gestern_data is defined %}
        {% set y_rest_gestern = gestern_data.ertrag_tag_rest | float(default=0) %}
        {% set h_rest_gestern = gestern_data.h_avg_rest | float(default=0) %}
        {% set perf_gestern = y_rest_gestern / ([105 - h_rest_gestern, 5] | max) %}
        {% if perf_gestern < 0.02 %}{% set schnee_faktor_heute = 0.1 %}{% endif %}
      {% endif %}
    {% endif %}

    {# 2. ASTRONOMISCHE BASISDATEN (Breitengrad aus zone.home) #}
    {% set latitude = state_attr('zone.home', 'latitude') | float(48.0) %}
    {% set doy = now().strftime('%j') | int(default=1) %}
    {% set lat_rad = latitude * pi / 180 %}
    {% set decl = -0.4093 * cos(2 * pi * (doy + 10) / 365) %}
    {% set cos_ha = -tan(lat_rad) * tan(decl) %}
    {% set dl_today = 24 / pi * acos([[cos_ha, -1.0] | max, 1.0] | min) %}
    {% set sun_today = 0.65 + 0.35 * cos((doy - 172) * 2 * pi / 365) %}

    {# 3. POOL AUFBAUEN #}
    {% set ns_pool = namespace(items=[], total_w=0) %}
    {% for item in data %}
      {% set yield_raw = item.ertrag_tag_rest | float(default=0) %}
      {% set clouds = item.h_avg_rest | float(default=0) %}
      {% set clouds_gesamt = item.h_avg_gesamt | float(default=0) %}
      {% set item_dt = as_datetime(item.datum) %}
      {% if item_dt is not none %}
        {% set item_day = item_dt.strftime('%j') | int(default=1) %}
        {% set decl_i = -0.4093 * cos(2 * pi * (item_day + 10) / 365) %}
        {% set cos_ha_i = -tan(lat_rad) * tan(decl_i) %}
        {% set dl_item = 24 / pi * acos([[cos_ha_i, -1.0] | max, 1.0] | min) %}
        {% set sun_item = 0.65 + 0.35 * cos((item_day - 172) * 2 * pi / 365) %}
        {% set s_korr = (sun_today / sun_item) * (dl_today / dl_item) %}
        {% set diff = (clouds - f_avg) | abs %}
        {% set w = 1 / ([diff, 0.5] | max) %}
        {% if yield_raw > 0.05 or clouds > 95 or current_month in [12, 1, 2] %}
          {% set ns_pool.total_w = ns_pool.total_w + w %}
          {% set ns_pool.items = ns_pool.items + [{'datum': item.datum, 'h_avg': clouds, 'h_avg_gesamt': clouds_gesamt, 'y_korr': yield_raw * s_korr, 's_fakt': s_korr, 'w': w, 'ertrag_tag_gesamt': item.ertrag_tag_gesamt, 'filtered': false}] %}
        {% else %}
          {% set ns_pool.items = ns_pool.items + [{'datum': item.datum, 'h_avg': clouds, 'h_avg_gesamt': clouds_gesamt, 'y_korr': yield_raw * s_korr, 's_fakt': s_korr, 'w': 0, 'ertrag_tag_gesamt': item.ertrag_tag_gesamt, 'filtered': true}] %}
        {% endif %}
      {% endif %}
    {% endfor %}

    {% set pool = ns_pool.items | selectattr('filtered', 'equalto', false) | list %}
    {% set brighter = pool | selectattr('h_avg', 'lt', f_avg) | list %}
    {% set darker = pool | selectattr('h_avg', 'gt', f_avg) | list %}
    {% set res = 0 %}
    {% set methode = "Keine Daten" %}

    {# 4. ENTSCHEIDUNGSLOGIK #}
    {% if brighter | count > 0 and darker | count == 0 %}
      {% set methode = "Licht-Reduktion" %}
      {% set worst_day = brighter | sort(attribute='y_korr') | first %}
      {% set res = worst_day.y_korr * ([120 - f_avg, 5.0] | max / [120 - worst_day.h_avg, 5.0] | max) %}
    {% elif darker | count > 0 and pool | selectattr('h_avg', 'le', f_avg) | list | count == 0 %}
      {% set methode = "Max-Annahme" %}
      {% set res = darker | map(attribute='y_korr') | max %}
    {% elif pool | count > 0 %}
      {% set methode = "Gewichteter Mittelwert" %}
      {% set ns_mix = namespace(ws=0) %}
      {% for item in pool %}
        {% set ns_mix.ws = ns_mix.ws + (item.y_korr * item.w) %}
      {% endfor %}
      {% set res = ns_mix.ws / (ns_pool.total_w if ns_pool.total_w > 0 else 1) %}
    {% endif %}

    {% set scale = 1000 if res > 200 else 1 %}
    {% set final_val = (res / scale) * schnee_faktor_heute %}

**Prognose:**
## {{ final_val | round(2) }} kWh
*Basis: **{{ f_avg }}%** Wolken | **{{ methode }}***
{% if schnee_faktor_heute < 1.0 %}⚠️ **Schnee-Verdacht! ({{ (schnee_faktor_heute * 100) | round(0) }}%)**{% endif %}

| Datum | Tag-Wolken | Tag-Ertrag | Rest-Wolken | Rest-Ertrag | Einfluss |
| :--- | :---: | :---: | :---: | :---: | :---: |
{%- for item in ns_pool.items | sort(attribute='w', reverse=True) %}
| {{ item.datum }} | {{ item.h_avg_gesamt }}% | {{ item.ertrag_tag_gesamt }} | **{{ item.h_avg }}%** | **{{ ((item.y_korr * schnee_faktor_heute) / scale) | round(2) }} <small><small>({{ item.s_fakt | round(2) }}x)</small></small>**{% if item.filtered %}❌{% endif %} | {{ (((item.w / ns_pool.total_w) * 100) if ns_pool.total_w > 0 else 0) | round(1) }}% |
{%- endfor %}

    {# 5. BEWÖLKUNG REST (stündliche Forecast-Tabelle) #}
    {% set forecast = state_attr('sensor.pv_hist_weather_forecast', 'forecast') %}
    {% if forecast %}
### ☁️ Bewölkung Rest
{# pv_start/pv_ende sind UTC-Zeiten aus SQL → Vergleich konsequent in UTC #}
{% set current_time = utcnow().strftime('%H:%M') %}
{% set pv_ende = data[0].pv_ende if data[0].pv_ende is defined else '17:00' %}
{% set pv_start = data[0].pv_start if data[0].pv_start is defined else '05:00' %}
{% set start_time = pv_start if pv_start > current_time else current_time %}
*Zeitfenster: {{ start_time }} bis {{ pv_ende }}*

| Uhrzeit | Wolken (%) |
| :--- | :---: |
{%- for hour in forecast %}
  {%- set hour_dt = as_datetime(hour.datetime) %}
  {%- if hour_dt is not none %}
    {%- set hour_time = hour_dt.strftime('%H:%M') %}
    {%- if hour_dt.date() == utcnow().date() and hour_time >= start_time and hour_time <= pv_ende %}
| {{ hour_time }} | {{ hour.cloud_coverage | float(default=0) }} % |
    {%- endif %}
  {%- endif %}
{%- endfor %}
    {% else %}
⚠️ Keine Forecast-Daten gefunden.
    {% endif %}

  {% else %}
**Keine Daten im SQL-Ergebnis vorhanden.**
  {% endif %}
{% else %}
**Warte auf SQL-Daten...**
{% endif %}