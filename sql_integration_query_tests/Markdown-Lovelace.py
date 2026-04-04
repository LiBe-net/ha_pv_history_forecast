{# =================================================================
   PV remaining yield today – Lovelace Markdown Card (Option B: Inline template)
   Source sensor:   sensor.pv_hist_remaining_today  (attribute: json)
   Forecast sensor: sensor.pv_hist_weather_forecast (attribute: forecast)

   RECOMMENDED: Use Option A instead of this inline template:
   {{ state_attr('sensor.pv_hist_remaining_today', 'lovelace_card_card_remaining_today') }}

   Option B: Use this content directly as a Lovelace Markdown card.
   ================================================================= #}
{% set raw_json = state_attr('sensor.pv_remaining_states', 'json') %}
{% if raw_json and raw_json != '[]' and raw_json is not none %}
  {% set data = raw_json | from_json %}

  {% if data | length > 0 %}
    {% set f_avg = data[0].f_avg_today_remaining | float(default=50.0) %}
    {% set f_uv_avg = data[0].uv_avg_today_remaining | float(default=0.0) %}

    {# 0. NIGHT-CHECK: 0.0 only after local sunset until midnight. Midnight→sunrise: full-day forecast. #}
    {% set offset_min = (now().utcoffset().total_seconds() / 60) | int %}
    {% set pv_end_utc = data[0].pv_end | default('17:30') %}
    {% set end_min_local = ((pv_end_utc.split(':')[0] | int) * 60 + (pv_end_utc.split(':')[1] | int) + offset_min) % 1440 %}
    {% set is_night = (now().hour * 60 + now().minute) > end_min_local %}

    {# 1. SEASONAL SNOW DETECTION (Dec / Jan / Feb) #}
    {% set current_month = now().month %}
    {% set snow_factor_today = 1.0 %}
    {% if current_month in [12, 1, 2] %}
      {% set yesterday_date = (now() - timedelta(days=1)).strftime('%Y-%m-%d') %}
      {% set yesterday_data = data | selectattr('date', 'equalto', yesterday_date) | list | first %}
      {% if yesterday_data is defined %}
        {% set yesterday_yield = yesterday_data.yield_day_remaining | float(default=0) %}
        {% set yesterday_h_avg = yesterday_data.h_avg_remaining | float(default=0) %}
        {% set yesterday_perf = yesterday_yield / ([105 - yesterday_h_avg, 5] | max) %}
        {% if yesterday_perf < 0.02 %}{% set snow_factor_today = 0.1 %}{% endif %}
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
      {% set yield_raw = item.yield_day_remaining | float(default=0) %}
      {% set clouds = item.h_avg_remaining | float(default=0) %}
      {% set clouds_total = item.h_avg_total | float(default=0) %}
      {% set uv = item.uv_avg_remaining | float(default=0) %}
      {% set item_dt = as_datetime(item.date) %}
      {% if item_dt is not none %}
        {% set item_day = item_dt.strftime('%j') | int(default=1) %}
        {% set decl_i = -0.4093 * cos(2 * pi * (item_day + 10) / 365) %}
        {% set cos_ha_i = -tan(lat_rad) * tan(decl_i) %}
        {% set dl_item = 24 / pi * acos([[cos_ha_i, -1.0] | max, 1.0] | min) %}
        {% set sun_item = 0.65 + 0.35 * cos((item_day - 172) * 2 * pi / 365) %}
        {% set s_korr = (sun_today / sun_item) * (dl_today / dl_item) %}
        {% set diff_c = (clouds - f_avg) | abs %}
        {% if f_uv_avg > 0 %}
          {% set diff = diff_c * 0.7 + (uv - f_uv_avg) | abs * 8.0 * 0.3 %}
        {% else %}
          {% set diff = diff_c %}
        {% endif %}
        {% set w = 1 / ([diff, 0.5] | max) %}
        {% if yield_raw > 0.05 or clouds > 95 or current_month in [12, 1, 2] %}
          {% set ns_pool.total_w = ns_pool.total_w + w %}
          {% set ns_pool.items = ns_pool.items + [{'date': item.date, 'h_avg': clouds, 'h_avg_total': clouds_total, 'uv_avg': uv, 'y_korr': yield_raw * s_korr, 's_fakt': s_korr, 'w': w, 'yield_day_total': item.yield_day_total, 'filtered': false}] %}
        {% else %}
          {% set ns_pool.items = ns_pool.items + [{'date': item.date, 'h_avg': clouds, 'h_avg_total': clouds_total, 'uv_avg': uv, 'y_korr': yield_raw * s_korr, 's_fakt': s_korr, 'w': 0, 'yield_day_total': item.yield_day_total, 'filtered': true}] %}
        {% endif %}
      {% endif %}
    {% endfor %}

    {% set pool = ns_pool.items | selectattr('filtered', 'equalto', false) | list %}
    {% set brighter = pool | selectattr('h_avg', 'le', f_avg) | list %}
    {% set darker = pool | selectattr('h_avg', 'ge', f_avg) | list %}
    {% set res = 0 %}
    {% set method = "No data" %}

    {# 4. Decision logic #}
    {% if brighter | count > 0 and darker | count == 0 %}
      {% set method = "Light reduction" %}
      {% set worst_day = brighter | sort(attribute='y_korr') | first %}
      {% set res = worst_day.y_korr * ([120 - f_avg, 5.0] | max / [120 - worst_day.h_avg, 5.0] | max) %}
    {% elif darker | count > 0 and pool | selectattr('h_avg', 'le', f_avg) | list | count == 0 %}
      {% set method = "Max assumption" %}
      {% set res = darker | map(attribute='y_korr') | max %}
    {% elif pool | count > 0 %}
      {% set method = "Weighted average" %}
      {% set ns_mix = namespace(ws=0) %}
      {% for item in pool %}
        {% set ns_mix.ws = ns_mix.ws + (item.y_korr * item.w) %}
      {% endfor %}
      {% set res = ns_mix.ws / (ns_pool.total_w if ns_pool.total_w > 0 else 1) %}
    {% endif %}

    {% set scale = 1000 if res > 200 else 1 %}
    {% set final_val = (res / scale) * snow_factor_today %}

**Forecast:**
## {{ (0.0 if is_night else final_val) | round(2) }} kWh
*Basis: **{{ f_avg }}%** clouds, **{{ f_uv_avg }}** uv | **{{ method }}***
{% if snow_factor_today < 1.0 %}⚠️ **Snow suspected! ({{ (snow_factor_today * 100) | round(0) }}%)**{% endif %}

| Date | Day clouds | Day yield | Rem. clouds | Rem. uv | Rem. yield | Weight |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
{%- for item in ns_pool.items | sort(attribute='w', reverse=True) %}
| {{ item.date }} | {{ item.h_avg_total }}% | {{ item.yield_day_total }} | **{{ item.h_avg }}%** | {{ item.uv_avg | round(1) }} | **{{ ((item.y_korr * snow_factor_today) / scale) | round(2) }} <small><small>({{ item.s_fakt | round(2) }}x)</small></small>**{% if item.filtered %}❌{% endif %} | {{ (((item.w / ns_pool.total_w) * 100) if ns_pool.total_w > 0 else 0) | round(1) }}% |
{%- endfor %}

  {% else %}
**No data in SQL result.**
  {% endif %}
{% else %}
**Waiting for SQL data...**
{% endif %}