{# PV-PROGNOSE LOGIK: Berechnet den Rest-Ertrag basierend auf historisch ähnlichen Tagen #}
{% set raw = value %}

{% if raw and raw != '[]' and raw is not none %}
  {% set data = raw | from_json %}
  

    {# --- 1. BASIS-DATEN --- #}
    {% set f_avg = data[0].f_avg_heute_rest | float(default=50.0) %}
    {% set current_month = now().month %}
    {% set schnee_faktor_heute = 1.0 %}

    {# --- 2. SAISONALE SCHNEE-ERKENNUNG (Nur Dez, Jan, Feb) --- #}
    {% if current_month in [12, 1, 2] %}
      {% set gestern_datum = (now() - timedelta(days=1)).strftime('%Y-%m-%d') %}
      {% set gestern_data = data | selectattr('datum', 'equalto', gestern_datum) | list | first %}

      {% if gestern_data is defined %}
        {% set y_rest_gestern = gestern_data.ertrag_tag_rest | float(default=0) %}
        {% set h_rest_gestern = gestern_data.h_avg_rest | float(default=0) %}
        {% set perf_gestern = y_rest_gestern / ([105 - h_rest_gestern, 5] | max) %}
        {% if perf_gestern < 0.02 %}
          {% set schnee_faktor_heute = 0.1 %}
        {% endif %}
      {% endif %}
    {% endif %}

    {# --- 3. ASTRONOMISCHE BASISDATEN (ortsgenau via Breitengrad aus HA-Standort) --- #}
    {# latitude wird als Template-Variable vom Sensor übergeben (hass.config.latitude) #}
    {% set day_of_year = now().strftime('%j') | int(default=1) %}
    {% set lat_rad = latitude * pi / 180 %}
    {% set decl = -0.4093 * cos(2 * pi * (day_of_year + 10) / 365) %}
    {% set cos_ha = -tan(lat_rad) * tan(decl) %}
    {% set dl_today = 24 / pi * acos([[cos_ha, -1.0] | max, 1.0] | min) %}
    {% set sun_today = 0.65 + 0.35 * cos((day_of_year - 172) * 2 * pi / 365) %}

    {# --- 4. DATEN-POOL AUFBEREITEN --- #}
    {% set ns_pool = namespace(items=[], total_w=0) %}
    {% for item in data %}
      {% set yield_raw = item.ertrag_tag_rest | float(default=0) %}
      {% set clouds = item.h_avg_rest | float(default=0) %}
      {% set dt_item = as_datetime(item.datum) %}
      
      {% if dt_item is not none %}
        {% set item_day = dt_item.strftime('%j') | int(default=1) %}
        {% set decl_i = -0.4093 * cos(2 * pi * (item_day + 10) / 365) %}
        {% set cos_ha_i = -tan(lat_rad) * tan(decl_i) %}
        {% set dl_item = 24 / pi * acos([[cos_ha_i, -1.0] | max, 1.0] | min) %}
        {% set sun_item = 0.65 + 0.35 * cos((item_day - 172) * 2 * pi / 365) %}
        {% set s_korr = (sun_today / sun_item) * (dl_today / dl_item) %}
        {% set diff = (clouds - f_avg) | abs %}
        {% set w = 1 / ([diff, 0.5] | max) %}

        {% if yield_raw > 0.05 or clouds > 95 or current_month in [12, 1, 2] %}
          {% set ns_pool.total_w = ns_pool.total_w + w %}
          {% set ns_pool.items = ns_pool.items + [{'h_avg': clouds, 'y_korr': yield_raw * s_korr, 'w': w}] %}
        {% endif %}
      {% endif %}
    {% endfor %}

    {# --- 5. PROGNOSE-BERECHNUNG --- #}
    {% set pool = ns_pool.items %}
    {% set brighter = pool | selectattr('h_avg', 'lt', f_avg) | list %}
    {% set darker = pool | selectattr('h_avg', 'gt', f_avg) | list %}
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
      {% set res = ns_mix.ws / (ns_pool.total_w if ns_pool.total_w > 0 else 1) %}
    {% endif %}

    {# --- 6. FINALE SKALIERUNG --- #}
    {% set final_val = (res / (1000 if res > 200 else 1)) * schnee_faktor_heute %}
    {{ final_val | round(2) }}

{% else %}
  0.0
{% endif %}