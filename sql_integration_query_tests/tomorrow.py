{# --- DATA FETCH FROM SQL SENSOR --- #}
{% set raw_json = state_attr('sensor.pv_remaining_states', 'json') %}

{% if raw_json and raw_json != '[]' and raw_json is not none %}
  {% set data = raw_json | from_json %}
  
  {# --- 1. AVERAGE CLOUD COVERAGE TOMORROW (already correctly computed in UTC by SQL) --- #}
  {# Direct read from SQL data avoids UTC/local-time errors in the forecast comparison #}
  {% set f_avg_tomorrow = data[0].f_avg_tomorrow | float(default=50.0) %}

  {# ASTRONOMICAL BASE DATA FOR TOMORROW (location-specific via latitude) #}
  {% set latitude = latitude if latitude is defined else state_attr('zone.home', 'latitude') | float(48.0) %}
  {% set day_tomorrow = (now() + timedelta(days=1)).strftime('%j') | int %}
  {% set lat_rad = latitude * pi / 180 %}
  {% set decl_tomorrow = -0.4093 * cos(2 * pi * (day_tomorrow + 10) / 365) %}
  {% set dl_tomorrow = 24 / pi * acos([[(-tan(lat_rad) * tan(decl_tomorrow)), -1.0] | max, 1.0] | min) %}
  {% set sun_tomorrow = 0.65 + 0.35 * cos((day_tomorrow - 172) * 2 * pi / 365) %}

  {# --- 3. POOL MATCHING (HISTORICAL COMPARISON) --- #}
  {# Compare tomorrow's forecast with total yields from history #}
  {% set ns_pool = namespace(items=[], total_w=0) %}
  {% for item in data %}
    {% set yield_total = item.yield_day_total | float(default=0) %}
    {% set clouds_hist = item.h_avg_total | float(default=0) %}
    {% set dt_item = as_datetime(item.date) %}
    {% if dt_item is not none %}
      {# Seasonal correction: scale historical yield to tomorrow's solar level #}
      {% set item_day = dt_item.strftime('%j') | int(default=1) %}
      {% set decl_i = -0.4093 * cos(2 * pi * (item_day + 10) / 365) %}
      {% set dl_item = 24 / pi * acos([[(-tan(lat_rad) * tan(decl_i)), -1.0] | max, 1.0] | min) %}
      {% set sun_item = 0.65 + 0.35 * cos((item_day - 172) * 2 * pi / 365) %}
      {% set s_korr = (sun_tomorrow / sun_item) * (dl_tomorrow / dl_item) %}
      {# Weighting: the closer the cloud coverage, the stronger the weight #}
      {% set diff = (clouds_hist - f_avg_tomorrow) | abs %}
      {% set w = 1 / ([diff, 0.5] | max) %}
      {% set ns_pool.total_w = ns_pool.total_w + w %}
      {% set ns_pool.items = ns_pool.items + [{'y_korr': yield_total * s_korr, 'h_avg': clouds_hist, 'w': w}] %}
    {% endif %}
  {% endfor %}

  {# --- 4. FORECAST CALCULATION --- #}
  {% set pool = ns_pool.items %}
  {% set brighter = pool | selectattr('h_avg', 'le', f_avg_tomorrow) | list %}
  {% set darker = pool | selectattr('h_avg', 'ge', f_avg_tomorrow) | list %}
  {% set res = 0 %}

  {% if brighter | count > 0 and darker | count == 0 %}
    {# Case A: tomorrow brighter than all pool days → light reduction based on worst day #}
    {% set worst_day = brighter | sort(attribute='y_korr') | first %}
    {% set res = worst_day.y_korr * ([120 - f_avg_tomorrow, 5.0] | max / [120 - worst_day.h_avg, 5.0] | max) %}
    
  {% elif darker | count > 0 and pool | selectattr('h_avg', 'le', f_avg_tomorrow) | list | count == 0 %}
    {# Case B: tomorrow darker than all pool days → cautious max assumption #}
    {% set res = darker | map(attribute='y_korr') | max %}
    
  {% elif pool | count > 0 %}
    {# Case C: mixed pool → weighted average of all comparison days #}
    {% set ns_mix = namespace(ws=0) %}
    {% for item in pool %}
      {% set ns_mix.ws = ns_mix.ws + (item.y_korr * item.w) %}
    {% endfor %}
    {% set res = ns_mix.ws / (ns_pool.total_w if ns_pool.total_w > 0 else 1) %}
  {% endif %}

  {# Ergebnis-Ausgabe: Wh in kWh konvertieren falls Wert sehr hoch ist (Logik-Check) #}
  {% set final_scale = 1000 if res > 200 else 1 %}
  {{ (res / final_scale) | round(2) }}

{% else %}
  {# Fallback SQL Data missing #}
  0.0
{% endif %}