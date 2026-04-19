{# PV FORECAST LOGIC: Calculates remaining yield based on historically similar days #}
{# Standalone test: reads from sensor attribute when 'value' is not passed by the sensor. #}
{% set raw = value if value is defined else state_attr('sensor.pv_hist_remaining_today', 'json') %}

{% if raw and raw != '[]' and raw is not none %}
  {% set data = raw | from_json %}

  {# --- 0. NIGHT-CHECK: 0.0 only after local sunset until local midnight.               #}
  {# Between local midnight and sunrise, SQL provides full-day forecast; show it.        #}
  {# pv_end is UTC HH:MM from SQL → convert to local minutes for correct comparison.    #}
  {% set offset_min = (now().utcoffset().total_seconds() / 60) | int %}
  {% set pv_end_utc = data[0].pv_end | default('17:30') %}
  {% set end_min_local = ((pv_end_utc.split(':')[0] | int) * 60 + (pv_end_utc.split(':')[1] | int) + offset_min) % 1440 %}

  {% if (now().hour * 60 + now().minute) > end_min_local %}
    0.0
  {% else %}

    {# --- 1. BASE DATA --- #}
    {% set f_avg = data[0].f_avg_today_remaining | float(default=50.0) %}
    {% set current_month = now().month %}
    {% set snow_factor_today = 1.0 %}

    {# --- 2. SEASONAL SNOW DETECTION (Dec, Jan, Feb only) --- #}
    {% if current_month in [12, 1, 2] %}
      {% set yesterday_date = (now() - timedelta(days=1)).strftime('%Y-%m-%d') %}
      {% set yesterday_data = data | selectattr('date', 'equalto', yesterday_date) | list | first %}

      {% if yesterday_data is defined %}
        {% set yesterday_yield = yesterday_data.yield_day_remaining | float(default=0) %}
        {% set yesterday_h_avg = yesterday_data.h_avg_remaining | float(default=0) %}
        {% set yesterday_perf = yesterday_yield / ([105 - yesterday_h_avg, 5] | max) %}
        {% if yesterday_perf < 0.02 %}
          {% set snow_factor_today = 0.1 %}
        {% endif %}
      {% endif %}
    {% endif %}

    {# --- 3. ASTRONOMICAL BASE DATA (location-specific via latitude from HA config) --- #}
    {% set day_of_year = now().strftime('%j') | int(default=1) %}
    {% set latitude = latitude if latitude is defined else state_attr('zone.home', 'latitude') | float(48.0) %}
    {% set lat_rad = latitude * pi / 180 %}
    {% set decl = -0.4093 * cos(2 * pi * (day_of_year + 10) / 365) %}
    {% set cos_ha = -tan(lat_rad) * tan(decl) %}
    {% set dl_today = 24 / pi * acos([[cos_ha, -1.0] | max, 1.0] | min) %}
    {% set sun_today = 0.80 + 0.20 * cos((day_of_year - 172) * 2 * pi / 365) %}

    {# --- 4. BUILD DATA POOL --- #}
    {% set f_uv_avg = data[0].uv_avg_today_remaining | float(default=0.0) %}
    {% set ns_pool = namespace(items=[], total_w=0) %}
    {% for item in data %}
      {% set yield_raw = item.yield_day_remaining | float(default=0) %}
      {% set clouds = item.h_avg_remaining | float(default=0) %}
      {% set uv_hist = item.uv_avg_remaining | float(default=0) %}
      {% set dt_item = as_datetime(item.date) %}
      
      {% if dt_item is not none %}
        {% set item_day = dt_item.strftime('%j') | int(default=1) %}
        {% set decl_i = -0.4093 * cos(2 * pi * (item_day + 10) / 365) %}
        {% set cos_ha_i = -tan(lat_rad) * tan(decl_i) %}
        {% set dl_item = 24 / pi * acos([[cos_ha_i, -1.0] | max, 1.0] | min) %}
        {% set sun_item = 0.80 + 0.20 * cos((item_day - 172) * 2 * pi / 365) %}
        {% set s_korr = (sun_today / sun_item) * (dl_today / dl_item) %}
        {% set diff_c = (clouds - f_avg) | abs %}
        {% if f_uv_avg > 0 %}
          {% set diff = diff_c * 0.7 + (uv_hist - f_uv_avg) | abs * 8.0 * 0.3 %}
        {% else %}
          {% set diff = diff_c %}
        {% endif %}
        {% set days_ago = ((now().timestamp() - dt_item.timestamp()) / 86400) | int(0) %}
        {% set w = (1 / ([diff, 0.5] | max)) * (1.0 + 0.3 * ([1.0 - days_ago / 30.0, 0.0] | max)) %}

        {% if yield_raw > 0.05 or clouds > 95 or current_month in [12, 1, 2] %}
          {% set ns_pool.total_w = ns_pool.total_w + w %}
          {% set ns_pool.items = ns_pool.items + [{'h_avg': clouds, 'y_korr': yield_raw * s_korr, 'w': w}] %}
        {% endif %}
      {% endif %}
    {% endfor %}

    {# --- 5. FORECAST CALCULATION --- #}
    {% set top15 = (ns_pool.items | sort(attribute='w', reverse=True))[:15] %}
    {% set ns_top = namespace(total_w=0) %}
    {% for item in top15 %}{% set ns_top.total_w = ns_top.total_w + item.w %}{% endfor %}
    {% set pool = top15 %}
    {% set brighter = pool | selectattr('h_avg', 'le', f_avg) | list %}
    {% set darker = pool | selectattr('h_avg', 'ge', f_avg) | list %}
    {% set res = 0 %}

    {% if brighter | count > 0 and darker | count == 0 %}
      {% set worst_day = brighter | sort(attribute='y_korr') | first %}
      {% set res = worst_day.y_korr * ([120 - f_avg, 5.0] | max / [120 - worst_day.h_avg, 5.0] | max) %}
    {% elif darker | count > 0 and pool | selectattr('h_avg', 'le', f_avg) | list | count == 0 %}
      {% set res = darker | map(attribute='y_korr') | max %}
    {% elif pool | count > 0 %}
      {% set ns_mix = namespace(ws=0) %}
      {% for item in pool %}
        {% set ns_mix.ws = ns_mix.ws + (item.y_korr * item.w) %}
      {% endfor %}
      {% set res = ns_mix.ws / (ns_top.total_w if ns_top.total_w > 0 else 1) %}
    {% endif %}

    {# --- 6. FINAL SCALING --- #}
    {% set final_val = (res / (1000 if res > 200 else 1)) * snow_factor_today %}
    {{ final_val | round(2) }}

  {% endif %}
{% else %}
  0.0
{% endif %}