"""Constants for the HA SQL PV Forecast integration."""
from __future__ import annotations

DOMAIN = "pv_history_forecast"

# Configuration keys
CONF_DB_URL = "db_url"
CONF_SENSOR_PREFIX = "sensor_prefix"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_SENSOR_CLOUDS = "sensor_clouds"
CONF_SENSOR_PV = "sensor_pv"
CONF_SENSOR_FORECAST = "sensor_forecast"
CONF_PV_HISTORY_DAYS = "pv_history_days"
CONF_LOVELACE_SENSOR = "lovelace_sensor"

# Advanced options
CONF_VALUE_TEMPLATE = "value_template"
CONF_UNIT_OF_MEASUREMENT = "unit_of_measurement"
CONF_DEVICE_CLASS = "device_class"
CONF_STATE_CLASS = "state_class"

# Defaults
DEFAULT_SENSOR_PREFIX = "pv_hist"
DEFAULT_VALUE_TEMPLATE = """{# PV FORECAST: Remaining yield today, weighted average of historically similar days #}
{% set raw = value %}
{% if raw and raw != '[]' and raw is not none %}
  {% set data = raw | from_json %}

  {# --- 0. NIGHT-CHECK (UTC-correct: pv_end and pv_start are UTC times from SQL) ---
     Evening window [pv_end .. local midnight in UTC) → yield 0.
     Morning window [UTC midnight .. pv_start) → yield 0.
     (e.g. 00:00 UTC = 01:00 CET: before sunrise → 0) #}
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
    {% set doy = now().strftime('%j') | int(default=1) %}
    {% set lat_rad = latitude * pi / 180 %}
    {% set decl = -0.4093 * cos(2 * pi * (doy + 10) / 365) %}
    {% set cos_ha = -tan(lat_rad) * tan(decl) %}
    {% set dl_today = 24 / pi * acos([[cos_ha, -1.0] | max, 1.0] | min) %}
    {% set sun_today = 0.65 + 0.35 * cos((doy - 172) * 2 * pi / 365) %}

    {# --- 4. BUILD DATA POOL --- #}
    {% set ns_pool = namespace(items=[], total_w=0) %}
    {% for item in data %}
      {% set yield_raw = item.yield_day_remaining | float(default=0) %}
      {% set clouds = item.h_avg_remaining | float(default=0) %}
      {% set dt_item = as_datetime(item.date) %}
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

    {# --- 5. FORECAST CALCULATION --- #}
    {% set pool = ns_pool.items %}
    {% set brighter = pool | selectattr('h_avg', 'le', f_avg) | list %}
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

    {# --- 6. FINAL SCALING --- #}
    {% set final_val = (res / (1000 if res > 200 else 1)) * snow_factor_today %}
    {{ final_val | round(2) }}

  {% endif %}
{% else %}
  0.0
{% endif %}"""

DEFAULT_VALUE_TEMPLATE_MIN = """{# PV-PROGNOSE MINIMUM: Pessimistischer Tagesrest aus Top-5 ähnlichen Tagen #}
{% set raw = value %}
{% if raw and raw != '[]' and raw is not none %}
  {% set data = raw | from_json %}
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
    {% set current_month = now().month %}
    {% set doy = now().strftime('%j') | int(default=1) %}
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
  0.0
{% endif %}"""

DEFAULT_VALUE_TEMPLATE_MAX = """{# PV-PROGNOSE MAXIMUM: Optimistischer Tagesrest aus Top-5 ähnlichen Tagen #}
{% set raw = value %}
{% if raw and raw != '[]' and raw is not none %}
  {% set data = raw | from_json %}
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
  0.0
{% endif %}"""

DEFAULT_VALUE_TEMPLATE_TOMORROW = """{# PV FORECAST TOMORROW: Total yield tomorrow, weighted average of similar days #}
{% set raw = value %}
{% if raw and raw != '[]' and raw is not none %}
  {% set data = raw | from_json %}
  {# f_avg_tomorrow from SQL: calculated correctly in UTC via pv_start/pv_end #}
  {% set f_avg_tomorrow = data[0].f_avg_tomorrow | float(default=50.0) %}

  {# ASTRONOMICAL BASE DATA FOR TOMORROW (location-specific via latitude) #}
  {% set doy_tomorrow = (now() + timedelta(days=1)).strftime('%j') | int(default=1) %}
  {% set lat_rad = latitude * pi / 180 %}
  {% set decl_tomorrow = -0.4093 * cos(2 * pi * (doy_tomorrow + 10) / 365) %}
  {% set dl_tomorrow = 24 / pi * acos([[(-tan(lat_rad) * tan(decl_tomorrow)), -1.0] | max, 1.0] | min) %}
  {% set sun_tomorrow = 0.65 + 0.35 * cos((doy_tomorrow - 172) * 2 * pi / 365) %}

  {% set ns_pool = namespace(items=[], total_w=0) %}
  {% for item in data %}
    {% set yield_total = item.yield_day_total | float(default=0) %}
    {% set clouds_hist = item.h_avg_total | float(default=0) %}
    {% set dt_item = as_datetime(item.date) %}
    {% if dt_item is not none %}
      {% set item_day = dt_item.strftime('%j') | int(default=1) %}
      {% set decl_i = -0.4093 * cos(2 * pi * (item_day + 10) / 365) %}
      {% set dl_item = 24 / pi * acos([[(-tan(lat_rad) * tan(decl_i)), -1.0] | max, 1.0] | min) %}
      {% set sun_item = 0.65 + 0.35 * cos((item_day - 172) * 2 * pi / 365) %}
      {% set s_korr = (sun_tomorrow / sun_item) * (dl_tomorrow / dl_item) %}
      {% set diff = (clouds_hist - f_avg_tomorrow) | abs %}
      {% set w = 1 / ([diff, 0.5] | max) %}
      {% set ns_pool.total_w = ns_pool.total_w + w %}
      {% set ns_pool.items = ns_pool.items + [{'y_korr': yield_total * s_korr, 'h_avg': clouds_hist, 'w': w}] %}
    {% endif %}
  {% endfor %}

  {% set pool = ns_pool.items %}
  {% set brighter = pool | selectattr('h_avg', 'le', f_avg_tomorrow) | list %}
  {% set darker = pool | selectattr('h_avg', 'gt', f_avg_tomorrow) | list %}
  {% set res = 0 %}
  {% if brighter | count > 0 and darker | count == 0 %}
    {% set worst_day = brighter | sort(attribute='y_korr') | first %}
    {% set res = worst_day.y_korr * ([120 - f_avg_tomorrow, 5.0] | max / [120 - worst_day.h_avg, 5.0] | max) %}
  {% elif darker | count > 0 and pool | selectattr('h_avg', 'le', f_avg_tomorrow) | list | count == 0 %}
    {% set res = darker | map(attribute='y_korr') | max %}
  {% elif pool | count > 0 %}
    {% set ns_mix = namespace(ws=0) %}
    {% for item in pool %}
      {% set ns_mix.ws = ns_mix.ws + (item.y_korr * item.w) %}
    {% endfor %}
    {% set res = ns_mix.ws / (ns_pool.total_w if ns_pool.total_w > 0 else 1) %}
  {% endif %}
  {{ (res / (1000 if res > 200 else 1)) | round(2) }}
{% else %}
  0.0
{% endif %}"""

DEFAULT_UNIT_OF_MEASUREMENT = "kWh"
DEFAULT_DEVICE_CLASS = "energy"
DEFAULT_STATE_CLASS = "total_increasing"
DEFAULT_PV_HISTORY_DAYS = 30

# Advanced SQL Query Template
DEFAULT_SQL_QUERY = """WITH vars AS (
    SELECT 
        '{sensor_clouds}' as sensor_clouds,
        '{sensor_pv}' as sensor_pv,
        '{sensor_forecast}' as sensor_forecast,
        '{weather_entity}' as weather_entity,
        (strftime('%s', 'now', 'localtime') - strftime('%s', 'now')) || ' seconds' as offset,
        COALESCE(
            CASE WHEN (SELECT unit_of_measurement FROM statistics_meta
                       WHERE statistic_id = '{sensor_pv}' LIMIT 1) = 'Wh'
                 THEN 1000.0 ELSE 1.0 END,
            1.0
        ) as pv_divisor
),

ids AS (
    SELECT 
        (SELECT id FROM statistics_meta WHERE statistic_id = (SELECT sensor_clouds FROM vars)) as w_id_stats,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_clouds FROM vars)) as w_id_states,
        (SELECT id FROM statistics_meta WHERE statistic_id = (SELECT sensor_pv FROM vars) LIMIT 1) as p_id,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_pv FROM vars) LIMIT 1) as p_id_states,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_forecast FROM vars) LIMIT 1) as f_id,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT weather_entity FROM vars)) as w_entity_id
),

pv_activity AS (
    SELECT 
        COALESCE((
            SELECT strftime('%H:%M', last_updated_ts, 'unixepoch') 
            FROM states 
            WHERE metadata_id = (SELECT p_id_states FROM ids) 
              AND date(last_updated_ts, 'unixepoch', (SELECT offset FROM vars)) = date('now', (SELECT offset FROM vars), '-1 day') 
              AND state NOT IN ('unknown', '0', '0.0', 'unavailable')
            ORDER BY last_updated_ts ASC LIMIT 1
        ), '05:30') as sun_start,
        COALESCE((
            SELECT strftime('%H:%M', last_updated_ts, 'unixepoch') 
            FROM states 
            WHERE metadata_id = (SELECT p_id_states FROM ids) 
              AND date(last_updated_ts, 'unixepoch', (SELECT offset FROM vars)) = date('now', (SELECT offset FROM vars), '-1 day') 
              AND state NOT IN ('unknown', 'unavailable', '')
              AND CAST(state AS FLOAT) < (
                  SELECT MAX(CAST(state AS FLOAT))
                  FROM states
                  WHERE metadata_id = (SELECT p_id_states FROM ids)
                    AND date(last_updated_ts, 'unixepoch', (SELECT offset FROM vars)) = date('now', (SELECT offset FROM vars), '-1 day')
                    AND state NOT IN ('unknown', 'unavailable', '')
              )
            ORDER BY last_updated_ts DESC LIMIT 1
        ), '17:30') as sun_end
    FROM ids
),

forecast_val AS (
    SELECT COALESCE(
        (SELECT AVG(CAST(json_extract(f.value, '$.cloud_coverage') AS FLOAT)) 
         FROM states s 
         JOIN state_attributes a ON s.attributes_id = a.attributes_id, 
         json_each(a.shared_attrs, '$.forecast') f 
         WHERE s.metadata_id = (SELECT f_id FROM ids) 
           AND s.last_updated_ts = (SELECT MAX(last_updated_ts) FROM states WHERE metadata_id = (SELECT f_id FROM ids)) 
           AND substr(json_extract(f.value, '$.datetime'), 1, 10) = date('now', (SELECT offset FROM vars))
           AND substr(json_extract(f.value, '$.datetime'), 12, 5) 
               BETWEEN CASE 
                         WHEN strftime('%H:%M', 'now') > (SELECT sun_start FROM pv_activity) THEN strftime('%H:%M', 'now') 
                         ELSE (SELECT sun_start FROM pv_activity) 
                       END
               AND (SELECT sun_end FROM pv_activity)
        ), 50.0) as f_avg
),

forecast_next_day AS (
    SELECT COALESCE((
        SELECT AVG(CAST(json_extract(f.value, '$.cloud_coverage') AS FLOAT)) 
        FROM states s 
        JOIN state_attributes a ON s.attributes_id = a.attributes_id, 
        json_each(a.shared_attrs, '$.forecast') f 
        WHERE s.metadata_id = (SELECT f_id FROM ids) 
          AND s.last_updated_ts = (SELECT MAX(last_updated_ts) FROM states WHERE metadata_id = (SELECT f_id FROM ids)) 
          AND substr(json_extract(f.value, '$.datetime'), 1, 10) = date('now', (SELECT offset FROM vars), '+1 day') 
          AND substr(json_extract(f.value, '$.datetime'), 12, 5) BETWEEN (SELECT sun_start FROM pv_activity) AND (SELECT sun_end FROM pv_activity)
    ), 50.0) as f_avg_tomorrow
),

cloud_history AS (
    SELECT start_ts as ts, CAST(COALESCE(mean, state) AS FLOAT) as val 
    FROM statistics 
    WHERE metadata_id = (SELECT w_id_stats FROM ids) 
      AND start_ts > strftime('%s', 'now', '-{history_days} days')
    UNION ALL
    SELECT s.last_updated_ts as ts, 
      CASE WHEN (SELECT sensor_clouds FROM vars) LIKE 'weather.%' 
           THEN CAST(json_extract(a.shared_attrs, '$.cloud_coverage') AS FLOAT) 
           ELSE CAST(s.state AS FLOAT) 
      END as val 
    FROM states s 
    LEFT JOIN state_attributes a ON s.attributes_id = a.attributes_id 
    WHERE s.metadata_id = (SELECT w_id_states FROM ids) 
      AND ((SELECT sensor_clouds FROM vars) LIKE 'weather.%' OR NOT EXISTS (SELECT 1 FROM statistics WHERE metadata_id = (SELECT w_id_stats FROM ids)))
      AND s.last_updated_ts > strftime('%s', 'now', '-10 days') 
      AND s.state NOT IN ('unknown', 'unavailable', '')
    UNION ALL
    SELECT s.last_updated_ts as ts,
        CAST(json_extract(a.shared_attrs, '$.cloud_coverage') AS FLOAT) as val
    FROM states s
    LEFT JOIN state_attributes a ON s.attributes_id = a.attributes_id
    WHERE s.metadata_id = (SELECT w_entity_id FROM ids)
      AND (SELECT w_entity_id FROM ids) IS NOT NULL
      AND (SELECT sensor_clouds FROM vars) NOT LIKE 'weather.%'
      AND s.last_updated_ts > strftime('%s', 'now', '-10 days')
      AND json_extract(a.shared_attrs, '$.cloud_coverage') IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM statistics
          WHERE metadata_id = (SELECT w_id_stats FROM ids)
            AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) = date(s.last_updated_ts, 'unixepoch', (SELECT offset FROM vars))
      )
),

matching_days AS (
    SELECT 
        date(ts, 'unixepoch', (SELECT offset FROM vars)) as day, 
        AVG(CASE WHEN strftime('%H:%M', ts, 'unixepoch') BETWEEN (SELECT sun_start FROM pv_activity) AND (SELECT sun_end FROM pv_activity) THEN val END) as h_avg_total_val,
        AVG(CASE WHEN strftime('%H:%M', ts, 'unixepoch') >= strftime('%H:00', 'now') AND strftime('%H:%M', ts, 'unixepoch') <= (SELECT sun_end FROM pv_activity) THEN val END) as h_avg_rest_val
    FROM cloud_history 
    WHERE date(ts, 'unixepoch', (SELECT offset FROM vars)) < date('now', (SELECT offset FROM vars)) 
    GROUP BY 1 
    HAVING h_avg_total_val IS NOT NULL
    ORDER BY ABS(
    COALESCE(h_avg_rest_val, h_avg_total_val)
    - (SELECT f_avg FROM forecast_val)
) ASC
),

final_data AS (
    SELECT 
        md.*,
        (SELECT MAX(state) FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) = md.day) as day_max,
        (SELECT MIN(state) FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) = md.day AND state > 0) as day_min,
        COALESCE((SELECT state FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) = md.day AND strftime('%H', start_ts, 'unixepoch') = strftime('%H', 'now') LIMIT 1), (SELECT MIN(state) FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) = md.day AND state > 0)) as h_hour_curr,
        COALESCE((SELECT state FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) = md.day AND strftime('%H', start_ts, 'unixepoch') = strftime('%H', 'now', '-1 hour') LIMIT 1), (SELECT MIN(state) FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) = md.day AND state > 0)) as h_hour_prev
    FROM matching_days md
)

SELECT COALESCE(json_group_array(
    json_object(
        'date', day,
        'f_avg_today_remaining', (SELECT ROUND(f_avg, 1) FROM forecast_val),        
        'f_avg_tomorrow', (SELECT ROUND(f_avg_tomorrow, 1) FROM forecast_next_day),
        'h_avg_total', ROUND(h_avg_total_val, 1),
        'h_avg_remaining', ROUND(h_avg_rest_val, 1),
        'yield_day_total', ROUND((day_max - day_min) / (SELECT pv_divisor FROM vars), 2),
        'yield_day_remaining', ROUND(CASE 
            WHEN strftime('%H:%M', 'now') > (SELECT sun_end FROM pv_activity)
                THEN 0.0
            WHEN strftime('%H:%M', 'now') < (SELECT sun_start FROM pv_activity)
                THEN (day_max - day_min) / (SELECT pv_divisor FROM vars)
            ELSE MAX(0, 
                ((h_hour_curr - h_hour_prev) * (1.0 - (CAST(strftime('%M', 'now') AS FLOAT) / 60.0)) * 
                  CASE 
                    WHEN strftime('%H', 'now') = strftime('%H', (SELECT sun_start FROM pv_activity)) THEN 0.85 
                    WHEN strftime('%H', 'now') = strftime('%H', (SELECT sun_end FROM pv_activity)) THEN 0.70 
                    ELSE 1.0 
                  END) 
                + (day_max - h_hour_curr)
            ) / (SELECT pv_divisor FROM vars)
        END, 2),
        'pv_start', (SELECT sun_start FROM pv_activity),
        'pv_end', (SELECT sun_end FROM pv_activity)
    )
), '[]') as json 
FROM final_data 
WHERE day_max > 0"""

DEFAULT_LOVELACE_TEMPLATE = """{# =================================================================
   PV remaining yield today – Lovelace Markdown Card
   Forecast sensor: __FORECAST_SENSOR__ (attribute: forecast)
   raw_json is passed directly as a template variable.
   ================================================================= #}
{% if raw_json and raw_json != '[]' and raw_json is not none %}
  {% set data = raw_json | from_json %}

  {% if data | length > 0 %}
    {% set f_avg = data[0].f_avg_today_remaining | float(default=50.0) %}

    {# 0. NIGHT-CHECK (same logic as sensor templates) #}
    {% set now_min = utcnow().hour * 60 + utcnow().minute %}
    {% set pv_end = data[0].pv_end | default('17:30') %}
    {% set pv_start = data[0].pv_start | default('05:30') %}
    {% set end_min = (pv_end.split(':')[0] | int) * 60 + (pv_end.split(':')[1] | int) %}
    {% set start_min = (pv_start.split(':')[0] | int) * 60 + (pv_start.split(':')[1] | int) %}
    {% set offset_min = (now().utcoffset().total_seconds() / 60) | int %}
    {% set midnight_utc_min = (24 * 60 - offset_min) % (24 * 60) %}
    {% set is_night = (end_min <= now_min < midnight_utc_min) or now_min < start_min %}

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

    {# 2. ASTRONOMICAL BASE DATA (latitude from zone.home) #}
    {% set latitude = state_attr('zone.home', 'latitude') | float(48.0) %}
    {% set doy = now().strftime('%j') | int(default=1) %}
    {% set lat_rad = latitude * pi / 180 %}
    {% set decl = -0.4093 * cos(2 * pi * (doy + 10) / 365) %}
    {% set cos_ha = -tan(lat_rad) * tan(decl) %}
    {% set dl_today = 24 / pi * acos([[cos_ha, -1.0] | max, 1.0] | min) %}
    {% set sun_today = 0.65 + 0.35 * cos((doy - 172) * 2 * pi / 365) %}

    {# 3. BUILD DATA POOL #}
    {% set ns_pool = namespace(items=[], total_w=0) %}
    {% for item in data %}
      {% set yield_raw = item.yield_day_remaining | float(default=0) %}
      {% set clouds = item.h_avg_remaining | float(default=0) %}
      {% set clouds_total = item.h_avg_total | float(default=0) %}
      {% set item_dt = as_datetime(item.date) %}
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
          {% set ns_pool.items = ns_pool.items + [{'date': item.date, 'h_avg': clouds, 'h_avg_total': clouds_total, 'y_korr': yield_raw * s_korr, 's_fakt': s_korr, 'w': w, 'yield_day_total': item.yield_day_total, 'filtered': false}] %}
        {% else %}
          {% set ns_pool.items = ns_pool.items + [{'date': item.date, 'h_avg': clouds, 'h_avg_total': clouds_total, 'y_korr': yield_raw * s_korr, 's_fakt': s_korr, 'w': 0, 'yield_day_total': item.yield_day_total, 'filtered': true}] %}
        {% endif %}
      {% endif %}
    {% endfor %}

    {% set pool = ns_pool.items | selectattr('filtered', 'equalto', false) | list %}
    {% set brighter = pool | selectattr('h_avg', 'le', f_avg) | list %}
    {% set darker = pool | selectattr('h_avg', 'gt', f_avg) | list %}
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
*Basis: **{{ f_avg }}%** clouds | **{{ method }}***
{% if snow_factor_today < 1.0 %}⚠️ **Snow suspected! ({{ (snow_factor_today * 100) | round(0) }}%)**{% endif %}

| Date | Day clouds | Day yield | Rem. clouds | Rem. yield | Weight |
| :--- | :---: | :---: | :---: | :---: | :---: |
{%- for item in ns_pool.items | sort(attribute='w', reverse=True) %}
| {{ item.date }} | {{ item.h_avg_total }}% | {{ item.yield_day_total }} | **{{ item.h_avg }}%** | **{{ ((item.y_korr * snow_factor_today) / scale) | round(2) }} <small><small>({{ item.s_fakt | round(2) }}x)</small></small>**{% if item.filtered %}❌{% endif %} | {{ (((item.w / ns_pool.total_w) * 100) if ns_pool.total_w > 0 else 0) | round(1) }}% |
{%- endfor %}

  {% else %}
**No data in SQL result.**
  {% endif %}
{% else %}
**Waiting for SQL data...**
{% endif %}"""
