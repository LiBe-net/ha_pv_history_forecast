{% set raw_json = state_attr('sensor.pv_remaining_states', 'json') %}
{% if raw_json and raw_json != '[]' and raw_json is not none %}
  {% set data = raw_json | from_json %}
  {# Night check: 0.0 after local sunset until midnight; midnight→sunrise shows full-day forecast #}
  {% set offset_min = (now().utcoffset().total_seconds() / 60) | int %}
  {% set pv_end_utc = data[0].pv_end | default('17:30') %}
  {% set end_min_local = ((pv_end_utc.split(':')[0] | int) * 60 + (pv_end_utc.split(':')[1] | int) + offset_min) % 1440 %}
  {% if (now().hour * 60 + now().minute) > end_min_local %}
    0.0
  {% else %}
    {% set f_avg = data[0].f_avg_today_remaining | float(default=50.0) %}
    {% set current_month = now().month %}
    {% set doy = now().strftime('%j') | int(default=1) %}
    {% set latitude = latitude if latitude is defined else state_attr('zone.home', 'latitude') | float(48.0) %}
    {% set lat_rad = latitude * pi / 180 %}
    {% set decl = -0.4093 * cos(2 * pi * (doy + 10) / 365) %}
    {% set dl_today = 24 / pi * acos([[(-tan(lat_rad) * tan(decl)), -1.0] | max, 1.0] | min) %}
    {% set sun_today = 0.65 + 0.35 * cos((doy - 172) * 2 * pi / 365) %}
    {% set ns_pool = namespace(items=[]) %}
    {% for item in data %}
      {% set dt_item = as_datetime(item.date) %}
      {% if dt_item is not none %}
        {% set item_day = dt_item.strftime('%j') | int(default=1) %}
        {% set decl_i = -0.4093 * cos(2 * pi * (item_day + 10) / 365) %}
        {% set dl_item = 24 / pi * acos([[(-tan(lat_rad) * tan(decl_i)), -1.0] | max, 1.0] | min) %}
        {% set sun_item = 0.65 + 0.35 * cos((item_day - 172) * 2 * pi / 365) %}
        {% set s_korr = (sun_today / sun_item) * (dl_today / dl_item) %}
        {% set yield_korr = item.yield_day_remaining | float(default=0) * s_korr %}
        {% set diff = (item.h_avg_remaining | float(default=0) - f_avg) | abs %}
        {% set ns_pool.items = ns_pool.items + [{'diff': diff, 'h_avg': item.h_avg_remaining | float(0), 'y_korr': yield_korr}] %}
      {% endif %}
    {% endfor %}
    {% set top5 = (ns_pool.items | sort(attribute='diff'))[:5] %}
    {% set brighter = top5 | selectattr('h_avg', 'le', f_avg) | list %}
    {% set darker = top5 | selectattr('h_avg', 'gt', f_avg) | list %}
    {% set res = 0 %}
    {% if top5 | count > 0 %}
      {% if brighter | count > 0 and darker | count == 0 %}
        {% set worst = brighter | sort(attribute='y_korr') | first %}
        {% set res = worst.y_korr * ([120 - f_avg, 5.0] | max / [120 - worst.h_avg, 5.0] | max) %}
      {% elif darker | count > 0 and brighter | count == 0 %}
        {% set res = darker | map(attribute='y_korr') | min %}
      {% else %}
        {% set res = top5 | map(attribute='y_korr') | min %}
      {% endif %}
    {% endif %}
    {{ (res / (1000 if res > 200 else 1)) | round(2) }}
  {% endif %}
{% else %}
  0
{% endif %}