{% set raw_json = state_attr('sensor.pv_remaining_states', 'json') %}
{% if raw_json and raw_json != '[]' and raw_json is not none %}
  {% set data = raw_json | from_json %}
  {# f_avg is in the first element (best-matching day) #}
  {% set f_avg = data[0].f_avg_today_remaining | float(0) %}

  {# SOLAR BASE DATA TODAY (location-specific via latitude) #}
  {% set latitude = latitude if latitude is defined else state_attr('zone.home', 'latitude') | float(48.0) %}
  {% set doy = now().strftime('%j') | int %}
  {% set lat_rad = latitude * pi / 180 %}
  {% set decl = -0.4093 * cos(2 * pi * (doy + 10) / 365) %}
  {% set dl_today = 24 / pi * acos([[(-tan(lat_rad) * tan(decl)), -1.0] | max, 1.0] | min) %}
  {% set sun_today = 0.65 + 0.35 * cos((doy - 172) * 2 * pi / 365) %}

  {% set ns_pool = namespace(items=[]) %}
  {% for item in data %}
    {% set i_doy = as_datetime(item.date).strftime('%j') | int %}
    
    {# Seasonal factors for the comparison day #}
    {% set decl_i = -0.4093 * cos(2 * pi * (i_doy + 10) / 365) %}
    {% set sun_then = 0.65 + 0.35 * cos((i_doy - 172) * 2 * pi / 365) %}
    {% set dl_then = 24 / pi * acos([[(-tan(lat_rad) * tan(decl_i)), -1.0] | max, 1.0] | min) %}

    {# Combined seasonal correction factor #}
    {% set s_korr = (sun_today / sun_then) * (dl_today / dl_then) %}
    
    {% set yield_korr = item.yield_day_remaining | float(0) * s_korr %}
    {% set diff = (item.h_avg_total | float(0) - f_avg) | abs %}
    {% set ns_pool.items = ns_pool.items + [{'diff': diff, 'h_avg': item.h_avg_total | float(0), 'y_korr': yield_korr}] %}
  {% endfor %}

  {# Top 5 by similarity (closest cloud coverage) #}
  {% set top_5 = (ns_pool.items | sort(attribute='diff'))[:5] %}
  {% set brighter = top_5 | selectattr('h_avg', 'lt', f_avg) | list %}
  {% set darker = top_5 | selectattr('h_avg', 'gt', f_avg) | list %}
  
  {% set res = 0 %}
  {% if top_5 | count > 0 %}
    {% if brighter | count > 0 and darker | count == 0 %}
      {# Case A: Light reduction #}
      {% set worst = brighter | sort(attribute='y_korr') | first %}
      {% set f_today = [120 - f_avg, 5.0] | max %}
      {% set f_then = [120 - worst.h_avg, 5.0] | max %}
      {% set res = worst.y_korr * (f_today / f_then) %}
    {% elif darker | count > 0 and brighter | count == 0 %}
      {# Case B: Minimum of dark days #}
      {% set res = darker | map(attribute='y_korr') | min %}
    {% else %}
      {# Case C: Mixed -> minimum for safety #}
      {% set res = top_5 | map(attribute='y_korr') | min %}
    {% endif %}
  {% endif %}

  {{ (res / (1000 if res > 200 else 1)) | round(2) }}
{% else %}
  0
{% endif %}