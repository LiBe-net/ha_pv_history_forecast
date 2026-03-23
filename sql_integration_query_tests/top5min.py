{% set raw_json = state_attr('sensor.pv_remaining_statistics', 'json') %}
{% if raw_json and raw_json != '[]' and raw_json is not none %}
  {% set data = raw_json | from_json %}
  {# Datenpunkt f_avg liegt im ersten Element #}
  {% set f_avg = data[0].f_avg_heute_rest | float(0) %}
  
  {# SONNEN-BASISDATEN HEUTE (ortsgenau via Breitengrad) #}
  {# latitude wird als Template-Variable vom Sensor übergeben (hass.config.latitude) #}
  {% set doy = now().strftime('%j') | int %}
  {% set lat_rad = latitude * pi / 180 %}
  {% set decl = -0.4093 * cos(2 * pi * (doy + 10) / 365) %}
  {% set dl_today = 24 / pi * acos([[(-tan(lat_rad) * tan(decl)), -1.0] | max, 1.0] | min) %}
  {% set sun_today = 0.65 + 0.35 * cos((doy - 172) * 2 * pi / 365) %}

  {% set ns_pool = namespace(items=[]) %}
  {% for item in data %}
    {% set i_doy = as_datetime(item.datum).strftime('%j') | int %}
    
    {# Saisonale Faktoren des Vergleichstags #}
    {% set decl_i = -0.4093 * cos(2 * pi * (i_doy + 10) / 365) %}
    {% set sun_then = 0.65 + 0.35 * cos((i_doy - 172) * 2 * pi / 365) %}
    {% set dl_then = 24 / pi * acos([[(-tan(lat_rad) * tan(decl_i)), -1.0] | max, 1.0] | min) %}
    
    {# KOMBINIERTE KORREKTUR #}
    {% set s_korr = (sun_today / sun_then) * (dl_today / dl_then) %}
    
    {% set yield_korr = item.ertrag_tag_rest | float(0) * s_korr %}
    {% set diff = (item.h_avg_gesamt | float(0) - f_avg) | abs %}
    {% set ns_pool.items = ns_pool.items + [{'diff': diff, 'h_avg': item.h_avg_gesamt | float(0), 'y_korr': yield_korr}] %}
  {% endfor %}

  {# Top 5 nach Ähnlichkeit #}
  {% set top_5 = (ns_pool.items | sort(attribute='diff'))[:5] %}
  {% set brighter = top_5 | selectattr('h_avg', 'lt', f_avg) | list %}
  {% set darker = top_5 | selectattr('h_avg', 'gt', f_avg) | list %}
  
  {% set res = 0 %}
  {% if top_5 | count > 0 %}
    {% if brighter | count > 0 and darker | count == 0 %}
      {# FALL A: Licht-Reduktion #}
      {% set worst = brighter | sort(attribute='y_korr') | first %}
      {% set f_heute = [120 - f_avg, 5.0] | max %}
      {% set f_damals = [120 - worst.h_avg, 5.0] | max %}
      {% set res = worst.y_korr * (f_heute / f_damals) %}
    {% elif darker | count > 0 and brighter | count == 0 %}
      {# FALL B: Minimum der dunklen Tage #}
      {% set res = darker | map(attribute='y_korr') | min %}
    {% else %}
      {# FALL C: Mix -> Minimum zur Sicherheit #}
      {% set res = top_5 | map(attribute='y_korr') | min %}
    {% endif %}
  {% endif %}

  {{ (res / (1000 if res > 200 else 1)) | round(2) }}
{% else %}
  0
{% endif %}