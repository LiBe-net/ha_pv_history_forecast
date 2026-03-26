{% set raw_json = state_attr('sensor.pv_remaining_states', 'json') %}
{% if raw_json and raw_json != '[]' and raw_json is not none %}
  {% set data = raw_json | from_json %}
  {% set now_min = utcnow().hour * 60 + utcnow().minute %}
  {% set pv_end = data[0].pv_end | default('17:00') %}
  {% set pv_start = data[0].pv_start | default('05:30') %}
  {% set end_min = (pv_end.split(':')[0] | int) * 60 + (pv_end.split(':')[1] | int) %}
  {% set start_min = (pv_start.split(':')[0] | int) * 60 + (pv_start.split(':')[1] | int) %}
  {% set offset_min = (now().utcoffset().total_seconds() / 60) | int %}
  {% set midnight_utc_min = (24 * 60 - offset_min) % (24 * 60) %}
  {% if (end_min <= now_min < midnight_utc_min) or now_min < start_min %}
    0.0
  {% else %}
    {% set f_avg = data[0].f_avg_today_remaining | float(default=50.0) %}
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
        {% set ns_pool.items = ns_pool.items + [{'diff': diff, 'y_korr': yield_korr}] %}
      {% endif %}
    {% endfor %}
    {% set top5 = (ns_pool.items | sort(attribute='diff'))[:5] %}
    {% set max_yield = top5 | map(attribute='y_korr') | max if top5 | count > 0 else 0 %}
    {{ (max_yield / (1000 if max_yield > 200 else 1)) | round(2) }}
  {% endif %}
{% else %}
  0
{% endif %}