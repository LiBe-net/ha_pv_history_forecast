{# --- DATENABRUF AUS SQL-SENSOR --- #}
{% set raw_json = state_attr('sensor.pv_remaining_statistics', 'json') %}

{% if raw_json and raw_json != '[]' and raw_json is not none %}
  {% set data = raw_json | from_json %}
  
  {# --- 1. DURCHSCHNITTLICHE BEWÖLKUNG MORGEN (bereits von SQL korrekt in UTC berechnet) --- #}
  {# Direktübernahme aus SQL-Daten vermeidet UTC/Lokalzeit-Fehler beim Forecast-Vergleich #}
  {% set f_avg_morgen = data[0].f_avg_morgen | float(default=50.0) %}

  {# ASTRONOMISCHE BASISDATEN MORGEN (ortsgenau via Breitengrad) #}
  {# latitude wird als Template-Variable vom Sensor übergeben (hass.config.latitude) #}
  {% set day_morgen = (now() + timedelta(days=1)).strftime('%j') | int %}
  {% set lat_rad = latitude * pi / 180 %}
  {% set decl_m = -0.4093 * cos(2 * pi * (day_morgen + 10) / 365) %}
  {% set dl_morgen = 24 / pi * acos([[(-tan(lat_rad) * tan(decl_m)), -1.0] | max, 1.0] | min) %}
  {% set sun_morgen = 0.65 + 0.35 * cos((day_morgen - 172) * 2 * pi / 365) %}

  {# --- 3. POOL MATCHING (HISTORISCHER VERGLEICH) --- #}
  {# Wir vergleichen den Forecast von morgen mit den Gesamterträgen der Vergangenheit #}
  {% set ns_pool = namespace(items=[], total_w=0) %}
  {% for item in data %}
    {% set yield_total = item.ertrag_tag_gesamt | float %}
    {% set clouds_hist = item.h_avg_gesamt | float %}
    
    {# Saisonale Korrektur: Skaliert den historischen Ertrag auf das Sonnen-Niveau von morgen #}
    {% set item_day = as_datetime(item.datum).strftime('%j') | int %}
    {% set decl_i = -0.4093 * cos(2 * pi * (item_day + 10) / 365) %}
    {% set dl_item = 24 / pi * acos([[(-tan(lat_rad) * tan(decl_i)), -1.0] | max, 1.0] | min) %}
    {% set sun_item = 0.65 + 0.35 * cos((item_day - 172) * 2 * pi / 365) %}
    {% set s_korr = (sun_morgen / sun_item) * (dl_morgen / dl_item) %}
    
    {# Gewichtung: Je näher die Bewölkung beieinander liegt, desto stärker das Gewicht #}
    {% set diff = (clouds_hist - f_avg_morgen) | abs %}
    {% set w = 1 / ([diff, 0.5] | max) %}
    
    {% set ns_pool.total_w = ns_pool.total_w + w %}
    {% set ns_pool.items = ns_pool.items + [{'y_korr': yield_total * s_korr, 'h_avg': clouds_hist, 'w': w}] %}
  {% endfor %}

  {# --- 4. ENTSCHEIDUNGSLOGIK --- #}
  {% set pool = ns_pool.items %}
  {% set brighter = pool | selectattr('h_avg', 'lt', f_avg_morgen) | list %}
  {% set darker = pool | selectattr('h_avg', 'gt', f_avg_morgen) | list %}
  {% set res = 0 %}

  {% if brighter | count > 0 and darker | count == 0 %}
    {# Fall A: Morgen wird dunkler als alle Tage im Pool -> Licht-Reduktion basierend auf dem schlechtesten Tag #}
    {% set worst_day = brighter | sort(attribute='y_korr') | first %}
    {% set res = worst_day.y_korr * ([120 - f_avg_morgen, 5.0] | max / [120 - worst_day.h_avg, 5.0] | max) %}
    
  {% elif darker | count > 0 and brighter | count == 0 %}
    {# Fall B: Morgen wird schöner als alle Tage im Pool -> Vorsichtige Max-Annahme #}
    {% set res = darker | map(attribute='y_korr') | max %}
    
  {% elif pool | count > 0 %}
    {# Fall C: Gemischter Pool -> Gewichteter Mittelwert aller Vergleichstage #}
    {% set ns_mix = namespace(ws=0) %}
    {% for item in pool %}
      {% set ns_mix.ws = ns_mix.ws + (item.y_korr * item.w) %}
    {% endfor %}
    {% set res = ns_mix.ws / ns_pool.total_w %}
  {% endif %}

  {# Ergebnis-Ausgabe: Wh in kWh konvertieren falls Wert sehr hoch ist (Logik-Check) #}
  {% set final_scale = 1000 if res > 200 else 1 %}
  {{ (res / final_scale) | round(2) }}

{% else %}
  {# Fallback wenn SQL-Daten fehlen #}
  0.0
{% endif %}