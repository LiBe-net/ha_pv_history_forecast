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
CONF_SENSOR_UV = "sensor_uv"
CONF_LOVELACE_SENSOR = "lovelace_sensor"
CONF_PV_MAX_RECORD = "pv_max_record"

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

  {# --- 0. NIGHT-CHECK: 0.0 only after local sunset until local midnight.               #}
  {# Between local midnight and sunrise, SQL provides full-day forecast; show it.        #}
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
    {% set doy = now().strftime('%j') | int(default=1) %}
    {% set lat_rad = latitude * pi / 180 %}
    {% set decl = -0.4093 * cos(2 * pi * (doy + 10) / 365) %}
    {% set cos_ha = -tan(lat_rad) * tan(decl) %}
    {% set dl_today = 24 / pi * acos([[cos_ha, -1.0] | max, 1.0] | min) %}
    {% set sun_today = 0.80 + 0.20 * cos((doy - 172) * 2 * pi / 365) %}

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
        {% set y_korr = [yield_raw * s_korr, pv_max_record] | min if pv_max_record > 0 else yield_raw * s_korr %}
        {% set diff_c = (clouds - f_avg) | abs %}
        {% if f_uv_avg > 0 %}
          {% set uv_w = [0.3 + 0.4 * (f_avg / 100.0), 0.7] | min %}
          {% set diff = diff_c * (1.0 - uv_w) + (uv_hist - f_uv_avg) | abs * 8.0 * uv_w %}
        {% else %}
          {% set diff = diff_c %}
        {% endif %}
        {% set days_ago = ((now().timestamp() - dt_item.timestamp()) / 86400) | int(0) %}
        {% set w = (1 / ([diff, 0.5] | max)) * (1.0 + 0.3 * ([1.0 - days_ago / 30.0, 0.0] | max)) %}
        {% if yield_raw > 0.05 or clouds > 95 or current_month in [12, 1, 2] %}
          {% set ns_pool.total_w = ns_pool.total_w + w %}
          {% set ns_pool.items = ns_pool.items + [{'date': item.date, 'h_avg': clouds, 'y_korr': y_korr, 'w': w, 'days_ago': days_ago}] %}
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

    {# --- 6. LOO CROSS-VALIDATION: down-weight outlier pool days --- #}
    {% set ns_cv = namespace(items=[]) %}
    {% for item_i in pool %}
      {% set ns_loo = namespace(w=0, wy=0) %}
      {% for item_j in pool %}
        {% if item_j.date != item_i.date %}
          {% set ns_loo.w  = ns_loo.w  + item_j.w %}
          {% set ns_loo.wy = ns_loo.wy + item_j.w * item_j.y_korr %}
        {% endif %}
      {% endfor %}
      {% if ns_loo.w > 0 and ns_loo.wy > 0 %}
        {% set acc = ((item_i.y_korr / (ns_loo.wy / ns_loo.w)) * 100) | round(0) | int %}
      {% else %}
        {% set acc = 100 %}
      {% endif %}
      {% set ns_cv.items = ns_cv.items + [{'date': item_i.date, 'acc': acc}] %}
    {% endfor %}
    {% if pool | count > 1 %}
      {% set ns_corr = namespace(w=0, wy=0) %}
      {% for item_i in pool %}
        {% set cv = ns_cv.items | selectattr('date', 'equalto', item_i.date) | list %}
        {% if cv | length > 0 %}
          {% set acc_factor = 1.0 / (1.0 + ((cv[0].acc - 100) | abs) / 100.0) %}
          {% set ns_corr.w  = ns_corr.w  + item_i.w * acc_factor %}
          {% set ns_corr.wy = ns_corr.wy + item_i.y_korr * item_i.w * acc_factor %}
        {% else %}
          {% set ns_corr.w  = ns_corr.w  + item_i.w %}
          {% set ns_corr.wy = ns_corr.wy + item_i.y_korr * item_i.w %}
        {% endif %}
      {% endfor %}
      {% if ns_corr.w > 0 and ns_corr.wy > 0 %}{% set res = ns_corr.wy / ns_corr.w %}{% endif %}
    {% endif %}

    {# --- 7. TREND DAMPING: recent ≤14d avg >15% above older → dampen 50% of excess --- #}
    {% set ns_rec = namespace(w=0, wy=0) %}
    {% set ns_old = namespace(w=0, wy=0) %}
    {% for item_i in pool %}
      {% if item_i.days_ago <= 14 %}
        {% set ns_rec.w  = ns_rec.w  + item_i.w %}
        {% set ns_rec.wy = ns_rec.wy + item_i.y_korr * item_i.w %}
      {% else %}
        {% set ns_old.w  = ns_old.w  + item_i.w %}
        {% set ns_old.wy = ns_old.wy + item_i.y_korr * item_i.w %}
      {% endif %}
    {% endfor %}
    {% if ns_rec.w > 0 and ns_old.w > 0 %}
      {% set avg_rec = ns_rec.wy / ns_rec.w %}
      {% set avg_old = ns_old.wy / ns_old.w %}
      {% if avg_old > 0 and (avg_rec / avg_old) > 1.15 %}
        {% set res = res / (1.0 + 0.5 * ((avg_rec / avg_old) - 1.0)) %}
      {% endif %}
    {% endif %}

    {# --- 8. BACK-TEST: derive carry-through from consecutive moderate-shortfall pairs --- #}
    {% set ns_all = namespace(sum_y=0.0, count_y=0) %}
    {% for item in pool %}
      {% if item.y_korr > 0 %}
        {% set ns_all.sum_y   = ns_all.sum_y   + item.y_korr %}
        {% set ns_all.count_y = ns_all.count_y + 1 %}
      {% endif %}
    {% endfor %}
    {% set mean_y = ns_all.sum_y / ([ns_all.count_y, 1] | max) %}
    {% set ns_bt = namespace(total=0, useful=0, trigger_sum=0.0, carry_sum=0.0) %}
    {% for item_i in pool %}
      {% if item_i.y_korr >= 0.40 * mean_y and item_i.y_korr < 0.85 * mean_y %}
        {% set next_date  = (as_datetime(item_i.date) + timedelta(days=1)).strftime('%Y-%m-%d') %}
        {% set next_items = pool | selectattr('date', 'equalto', next_date) | list %}
        {% if next_items | length > 0 %}
          {% set item_j            = next_items[0] %}
          {% set ns_bt.total       = ns_bt.total + 1 %}
          {% set ns_bt.trigger_sum = ns_bt.trigger_sum + (1.0 - item_i.y_korr / mean_y) %}
          {% set ns_bt.carry_sum   = ns_bt.carry_sum   + ([1.0 - item_j.y_korr / mean_y, 0.0] | max) %}
          {% if item_j.y_korr < mean_y %}{% set ns_bt.useful = ns_bt.useful + 1 %}{% endif %}
        {% endif %}
      {% endif %}
    {% endfor %}
    {% set effective_carry = (ns_bt.carry_sum / ns_bt.trigger_sum) * (ns_bt.useful / ns_bt.total) if (ns_bt.total > 0 and ns_bt.trigger_sum > 0) else 0.3 %}

    {# --- 9. CLOUD-GATED YESTERDAY PENALTY: fires only when both days ≥60% cloudy --- #}
    {% set yesterday_date_yp = (now() - timedelta(days=1)).strftime('%Y-%m-%d') %}
    {% set yest_cv   = ns_cv.items | selectattr('date', 'equalto', yesterday_date_yp) | list %}
    {% set yest_item = pool | selectattr('date', 'equalto', yesterday_date_yp) | list %}
    {% set yest_clouds = yest_item[0].h_avg if yest_item | length > 0 else 0 %}
    {% if yest_cv | length > 0 %}
      {% set yest_acc = yest_cv[0].acc %}
      {% if yest_acc >= 40 and yest_acc < 85 and f_avg >= 60 and yest_clouds >= 60 %}
        {% set res = res * ([1.0 - effective_carry * (1.0 - yest_acc / 100.0), 0.5] | max) %}
      {% endif %}
    {% endif %}

    {# --- 10. FINAL SCALING --- #}
    {% set final_val = res * snow_factor_today %}
    {{ final_val | round(2) }}

  {% endif %}
{% else %}
  0.0
{% endif %}"""

DEFAULT_VALUE_TEMPLATE_MIN = """{# PV-PROGNOSE MINIMUM: Pessimistischer Tagesrest aus Top-5 ähnlichen Tagen #}
{% set raw = value %}
{% if raw and raw != '[]' and raw is not none %}
  {% set data = raw | from_json %}
  {# Night check: 0.0 after local sunset until midnight; midnight→sunrise uses full-day SQL data #}
  {% set offset_min = (now().utcoffset().total_seconds() / 60) | int %}
  {% set pv_end_utc = data[0].pv_end | default('17:30') %}
  {% set end_min_local = ((pv_end_utc.split(':')[0] | int) * 60 + (pv_end_utc.split(':')[1] | int) + offset_min) % 1440 %}
  {% if (now().hour * 60 + now().minute) > end_min_local %}
    0.0
  {% else %}
    {% set f_avg = data[0].f_avg_today_remaining | float(default=50.0) %}
    {% set current_month = now().month %}
    {% set doy = now().strftime('%j') | int(default=1) %}
    {% set lat_rad = latitude * pi / 180 %}
    {% set decl = -0.4093 * cos(2 * pi * (doy + 10) / 365) %}
    {% set dl_today = 24 / pi * acos([[(-tan(lat_rad) * tan(decl)), -1.0] | max, 1.0] | min) %}
    {% set f_uv_avg = data[0].uv_avg_today_remaining | float(default=0.0) %}
    {% set sun_today = 0.80 + 0.20 * cos((doy - 172) * 2 * pi / 365) %}
    {% set ns_pool = namespace(items=[]) %}
    {% for item in data %}
      {% set dt_item = as_datetime(item.date) %}
      {% if dt_item is not none %}
        {% set item_day = dt_item.strftime('%j') | int(default=1) %}
        {% set decl_i = -0.4093 * cos(2 * pi * (item_day + 10) / 365) %}
        {% set dl_item = 24 / pi * acos([[(-tan(lat_rad) * tan(decl_i)), -1.0] | max, 1.0] | min) %}
        {% set sun_item = 0.80 + 0.20 * cos((item_day - 172) * 2 * pi / 365) %}
        {% set s_korr = (sun_today / sun_item) * (dl_today / dl_item) %}
        {% set uv_hist = item.uv_avg_remaining | float(default=0) %}
        {% set yield_korr = item.yield_day_remaining | float(default=0) * s_korr %}
        {% if pv_max_record > 0 %}{% set yield_korr = [yield_korr, pv_max_record] | min %}{% endif %}
        {% set diff_c = (item.h_avg_remaining | float(default=0) - f_avg) | abs %}
        {% if f_uv_avg > 0 %}
          {% set uv_w = [0.3 + 0.4 * (f_avg / 100.0), 0.7] | min %}
          {% set diff = diff_c * (1.0 - uv_w) + (uv_hist - f_uv_avg) | abs * 8.0 * uv_w %}
        {% else %}
          {% set diff = diff_c %}
        {% endif %}
        {% set ns_pool.items = ns_pool.items + [{'diff': diff, 'h_avg': item.h_avg_remaining | float(0), 'y_korr': yield_korr}] %}
      {% endif %}
    {% endfor %}
    {% set top15 = (ns_pool.items | sort(attribute='diff'))[:15]   %}
    {% set brighter = top15 | selectattr('h_avg', 'le', f_avg) | list %}
    {% set darker = top15 | selectattr('h_avg', 'gt', f_avg) | list %}
    {% set res = 0 %}
    {% if top15 | count > 0 %}
      {% if brighter | count > 0 and darker | count == 0 %}
        {% set worst = brighter | sort(attribute='y_korr') | first %}
        {% set res = worst.y_korr * ([120 - f_avg, 5.0] | max / [120 - worst.h_avg, 5.0] | max) %}
      {% elif darker | count > 0 and brighter | count == 0 %}
        {% set res = darker | map(attribute='y_korr') | min %}
      {% else %}
        {% set res = top15 | map(attribute='y_korr') | min %}
      {% endif %}
    {% endif %}
    {{ res | round(2) }}
  {% endif %}
{% else %}
  0.0
{% endif %}"""

DEFAULT_VALUE_TEMPLATE_MAX = """{# PV-PROGNOSE MAXIMUM: Optimistischer Tagesrest aus Top-5 ähnlichen Tagen #}
{% set raw = value %}
{% if raw and raw != '[]' and raw is not none %}
  {% set data = raw | from_json %}
  {# Night check: 0.0 after local sunset until midnight; midnight→sunrise uses full-day SQL data #}
  {% set offset_min = (now().utcoffset().total_seconds() / 60) | int %}
  {% set pv_end_utc = data[0].pv_end | default('17:30') %}
  {% set end_min_local = ((pv_end_utc.split(':')[0] | int) * 60 + (pv_end_utc.split(':')[1] | int) + offset_min) % 1440 %}
  {% if (now().hour * 60 + now().minute) > end_min_local %}
    0.0
  {% else %}
    {% set f_avg = data[0].f_avg_today_remaining | float(default=50.0) %}
    {% set doy = now().strftime('%j') | int(default=1) %}
    {% set lat_rad = latitude * pi / 180 %}
    {% set decl = -0.4093 * cos(2 * pi * (doy + 10) / 365) %}
    {% set dl_today = 24 / pi * acos([[(-tan(lat_rad) * tan(decl)), -1.0] | max, 1.0] | min) %}
    {% set f_uv_avg = data[0].uv_avg_today_remaining | float(default=0.0) %}
    {% set sun_today = 0.80 + 0.20 * cos((doy - 172) * 2 * pi / 365) %}
    {% set ns_pool = namespace(items=[]) %}
    {% for item in data %}
      {% set dt_item = as_datetime(item.date) %}
      {% if dt_item is not none %}
        {% set item_day = dt_item.strftime('%j') | int(default=1) %}
        {% set decl_i = -0.4093 * cos(2 * pi * (item_day + 10) / 365) %}
        {% set dl_item = 24 / pi * acos([[(-tan(lat_rad) * tan(decl_i)), -1.0] | max, 1.0] | min) %}
        {% set sun_item = 0.80 + 0.20 * cos((item_day - 172) * 2 * pi / 365) %}
        {% set s_korr = (sun_today / sun_item) * (dl_today / dl_item) %}
        {% set uv_hist = item.uv_avg_remaining | float(default=0) %}
        {% set yield_korr = item.yield_day_remaining | float(default=0) * s_korr %}
        {% if pv_max_record > 0 %}{% set yield_korr = [yield_korr, pv_max_record] | min %}{% endif %}
        {% set diff_c = (item.h_avg_remaining | float(default=0) - f_avg) | abs %}
        {% if f_uv_avg > 0 %}
          {% set uv_w = [0.3 + 0.4 * (f_avg / 100.0), 0.7] | min %}
          {% set diff = diff_c * (1.0 - uv_w) + (uv_hist - f_uv_avg) | abs * 8.0 * uv_w %}
        {% else %}
          {% set diff = diff_c %}
        {% endif %}
        {% set ns_pool.items = ns_pool.items + [{'diff': diff, 'y_korr': yield_korr}] %}
      {% endif %}
    {% endfor %}
    {% set top15 = (ns_pool.items | sort(attribute='diff'))[:15] %}
    {% set max_yield = top15 | map(attribute='y_korr') | max if top15 | count > 0 else 0 %}
    {{ max_yield | round(2) }}
  {% endif %}
{% else %}
  0.0
{% endif %}"""

DEFAULT_VALUE_TEMPLATE_TOMORROW = """{# PV FORECAST TOMORROW: weighted average · +LOO · ↓td · ↓yp(cloud-gated) #}
{% set raw = value %}
{% if raw and raw != '[]' and raw is not none %}
  {% set data = raw | from_json %}
  {% set f_avg_tomorrow = data[0].f_avg_tomorrow | float(default=50.0) %}
  {% set f_uv_avg_tomorrow = data[0].uv_avg_tomorrow | float(default=0.0) %}

  {# ASTRONOMICAL BASE DATA FOR TOMORROW (location-specific via latitude) #}
  {% set doy_tomorrow = (now() + timedelta(days=1)).strftime('%j') | int(default=1) %}
  {% set lat_rad = latitude * pi / 180 %}
  {% set decl_tomorrow = -0.4093 * cos(2 * pi * (doy_tomorrow + 10) / 365) %}
  {% set dl_tomorrow = 24 / pi * acos([[(-tan(lat_rad) * tan(decl_tomorrow)), -1.0] | max, 1.0] | min) %}
  {% set sun_tomorrow = 0.80 + 0.20 * cos((doy_tomorrow - 172) * 2 * pi / 365) %}

  {% set ns_pool = namespace(items=[], total_w=0) %}
  {% for item in data %}
    {% set yield_total = item.yield_day_total | float(default=0) %}
    {% set clouds_hist = item.h_avg_total | float(default=0) %}
    {% set uv_hist = item.uv_avg_total | float(default=0) %}
    {% set dt_item = as_datetime(item.date) %}
    {% if dt_item is not none %}
      {% set item_day = dt_item.strftime('%j') | int(default=1) %}
      {% set decl_i = -0.4093 * cos(2 * pi * (item_day + 10) / 365) %}
      {% set dl_item = 24 / pi * acos([[(-tan(lat_rad) * tan(decl_i)), -1.0] | max, 1.0] | min) %}
      {% set sun_item = 0.80 + 0.20 * cos((item_day - 172) * 2 * pi / 365) %}
      {% set s_korr = (sun_tomorrow / sun_item) * (dl_tomorrow / dl_item) %}
      {% set y_korr = [yield_total * s_korr, pv_max_record] | min if pv_max_record > 0 else yield_total * s_korr %}
      {% set diff_c = (clouds_hist - f_avg_tomorrow) | abs %}
      {% if f_uv_avg_tomorrow > 0 %}
        {% set uv_w = [0.3 + 0.4 * (f_avg_tomorrow / 100.0), 0.7] | min %}
        {% set diff = diff_c * (1.0 - uv_w) + (uv_hist - f_uv_avg_tomorrow) | abs * 8.0 * uv_w %}
      {% else %}
        {% set diff = diff_c %}
      {% endif %}
      {% set days_ago = ((now().timestamp() - dt_item.timestamp()) / 86400) | int(0) %}
      {% set w = (1 / ([diff, 0.5] | max)) * (1.0 + 0.3 * ([1.0 - days_ago / 30.0, 0.0] | max)) %}
      {% if yield_total > 0.05 %}
        {% set ns_pool.total_w = ns_pool.total_w + w %}
        {% set ns_pool.items = ns_pool.items + [{'date': item.date, 'h_avg': clouds_hist, 'w': w, 'y_korr': y_korr, 'days_ago': days_ago, 'filtered': false}] %}
      {% else %}
        {% set ns_pool.items = ns_pool.items + [{'date': item.date, 'h_avg': clouds_hist, 'w': 0, 'y_korr': y_korr, 'days_ago': days_ago, 'filtered': true}] %}
      {% endif %}
    {% endif %}
  {% endfor %}

  {% set top15 = (ns_pool.items | sort(attribute='w', reverse=True))[:15] %}
  {% set ns_top = namespace(total_w=0) %}
  {% for item in top15 %}{% if not item.filtered %}{% set ns_top.total_w = ns_top.total_w + item.w %}{% endif %}{% endfor %}
  {% set pool = top15 | selectattr('filtered', 'equalto', false) | list %}
  {% set brighter = pool | selectattr('h_avg', 'le', f_avg_tomorrow) | list %}
  {% set darker = pool | selectattr('h_avg', 'ge', f_avg_tomorrow) | list %}
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
    {% set res = ns_mix.ws / (ns_top.total_w if ns_top.total_w > 0 else 1) %}
  {% endif %}

  {# LOO CROSS-VALIDATION: down-weight outlier pool days #}
  {% set ns_cv = namespace(items=[]) %}
  {% for item_i in pool %}
    {% set ns_loo = namespace(w=0, wy=0) %}
    {% for item_j in pool %}
      {% if item_j.date != item_i.date %}
        {% set ns_loo.w  = ns_loo.w  + item_j.w %}
        {% set ns_loo.wy = ns_loo.wy + item_j.w * item_j.y_korr %}
      {% endif %}
    {% endfor %}
    {% if ns_loo.w > 0 and ns_loo.wy > 0 %}
      {% set acc = ((item_i.y_korr / (ns_loo.wy / ns_loo.w)) * 100) | round(0) | int %}
    {% else %}
      {% set acc = 100 %}
    {% endif %}
    {% set ns_cv.items = ns_cv.items + [{'date': item_i.date, 'acc': acc}] %}
  {% endfor %}
  {% if pool | count > 1 %}
    {% set ns_corr = namespace(w=0, wy=0) %}
    {% for item_i in pool %}
      {% set cv = ns_cv.items | selectattr('date', 'equalto', item_i.date) | list %}
      {% if cv | length > 0 %}
        {% set acc_factor = 1.0 / (1.0 + ((cv[0].acc - 100) | abs) / 100.0) %}
        {% set ns_corr.w  = ns_corr.w  + item_i.w * acc_factor %}
        {% set ns_corr.wy = ns_corr.wy + item_i.y_korr * item_i.w * acc_factor %}
      {% else %}
        {% set ns_corr.w  = ns_corr.w  + item_i.w %}
        {% set ns_corr.wy = ns_corr.wy + item_i.y_korr * item_i.w %}
      {% endif %}
    {% endfor %}
    {% if ns_corr.w > 0 and ns_corr.wy > 0 %}{% set res = ns_corr.wy / ns_corr.w %}{% endif %}
  {% endif %}

  {# TREND DAMPING: recent ≤14d avg > older avg by >15% → dampen 50% of excess #}
  {% set ns_rec = namespace(w=0, wy=0) %}
  {% set ns_old = namespace(w=0, wy=0) %}
  {% for item_i in pool %}
    {% if item_i.days_ago <= 14 %}
      {% set ns_rec.w  = ns_rec.w  + item_i.w %}
      {% set ns_rec.wy = ns_rec.wy + item_i.y_korr * item_i.w %}
    {% else %}
      {% set ns_old.w  = ns_old.w  + item_i.w %}
      {% set ns_old.wy = ns_old.wy + item_i.y_korr * item_i.w %}
    {% endif %}
  {% endfor %}
  {% if ns_rec.w > 0 and ns_old.w > 0 %}
    {% set avg_rec = ns_rec.wy / ns_rec.w %}
    {% set avg_old = ns_old.wy / ns_old.w %}
    {% if avg_old > 0 and (avg_rec / avg_old) > 1.15 %}
      {% set res = res / (1.0 + 0.5 * ((avg_rec / avg_old) - 1.0)) %}
    {% endif %}
  {% endif %}

  {# BACK-TEST: derive carry-through from consecutive moderate-shortfall pairs #}
  {% set ns_all = namespace(sum_y=0.0, count_y=0) %}
  {% for item in pool %}
    {% if item.y_korr > 0 %}
      {% set ns_all.sum_y   = ns_all.sum_y   + item.y_korr %}
      {% set ns_all.count_y = ns_all.count_y + 1 %}
    {% endif %}
  {% endfor %}
  {% set mean_y = ns_all.sum_y / ([ns_all.count_y, 1] | max) %}
  {% set ns_bt = namespace(total=0, useful=0, trigger_sum=0.0, carry_sum=0.0) %}
  {% for item_i in pool %}
    {% if item_i.y_korr >= 0.40 * mean_y and item_i.y_korr < 0.85 * mean_y %}
      {% set next_date  = (as_datetime(item_i.date) + timedelta(days=1)).strftime('%Y-%m-%d') %}
      {% set next_items = pool | selectattr('date', 'equalto', next_date) | list %}
      {% if next_items | length > 0 %}
        {% set item_j            = next_items[0] %}
        {% set ns_bt.total       = ns_bt.total + 1 %}
        {% set ns_bt.trigger_sum = ns_bt.trigger_sum + (1.0 - item_i.y_korr / mean_y) %}
        {% set ns_bt.carry_sum   = ns_bt.carry_sum   + ([1.0 - item_j.y_korr / mean_y, 0.0] | max) %}
        {% if item_j.y_korr < mean_y %}{% set ns_bt.useful = ns_bt.useful + 1 %}{% endif %}
      {% endif %}
    {% endif %}
  {% endfor %}
  {% set effective_carry = (ns_bt.carry_sum / ns_bt.trigger_sum) * (ns_bt.useful / ns_bt.total) if (ns_bt.total > 0 and ns_bt.trigger_sum > 0) else 0.3 %}

  {# CLOUD-GATED YESTERDAY PENALTY: fires only when both yesterday and tomorrow ≥60% cloudy #}
  {% set yesterday_date = (now() - timedelta(days=1)).strftime('%Y-%m-%d') %}
  {% set yest_cv   = ns_cv.items | selectattr('date', 'equalto', yesterday_date) | list %}
  {% set yest_item = ns_pool.items | selectattr('date', 'equalto', yesterday_date) | selectattr('filtered', 'equalto', false) | list %}
  {% set yest_clouds = yest_item[0].h_avg if yest_item | length > 0 else 0 %}
  {% if yest_cv | length > 0 %}
    {% set yest_acc = yest_cv[0].acc %}
    {% if yest_acc >= 40 and yest_acc < 85 and f_avg_tomorrow >= 60 and yest_clouds >= 60 %}
      {% set res = res * ([1.0 - effective_carry * (1.0 - yest_acc / 100.0), 0.5] | max) %}
    {% endif %}
  {% endif %}

  {{ res | round(2) }}
{% else %}
  0.0
{% endif %}"""

DEFAULT_VALUE_TEMPLATE_METHOD_TODAY = """{#- Return the decision method used for remaining-today forecast -#}
{% set raw = value %}
{% if raw and raw != '[]' and raw is not none %}
  {% set data = raw | from_json %}
  {% if data | length > 0 %}
    {% set f_avg = data[0].f_avg_today_remaining | float(default=50.0) %}
    {% set current_month = now().month %}
    {% set ns_pool = namespace(items=[]) %}
    {% for item in data %}
      {% if as_datetime(item.date) is not none %}
        {% set yield_raw = item.yield_day_remaining | float(default=0) %}
        {% set clouds = item.h_avg_remaining | float(default=0) %}
        {% if yield_raw > 0.05 or clouds > 95 or current_month in [12, 1, 2] %}
          {% set ns_pool.items = ns_pool.items + [{'h_avg': clouds}] %}
        {% endif %}
      {% endif %}
    {% endfor %}
    {% set pool = ns_pool.items %}
    {% set brighter = pool | selectattr('h_avg', 'le', f_avg) | list %}
    {% set darker = pool | selectattr('h_avg', 'ge', f_avg) | list %}
    {% if brighter | count > 0 and darker | count == 0 %}Light reduction
    {% elif darker | count > 0 and pool | selectattr('h_avg', 'le', f_avg) | list | count == 0 %}Max assumption
    {% elif pool | count > 0 %}Weighted average
    {% elif data | selectattr('date', 'equalto', 'forecast_only') | list | count > 0 %}No history yet
    {% else %}No data
    {% endif %}
  {% else %}No data
  {% endif %}
{% else %}No data
{% endif %}"""

DEFAULT_VALUE_TEMPLATE_METHOD_TOMORROW = """{#- Return the decision method used for tomorrow forecast -#}
{% set raw = value %}
{% if raw and raw != '[]' and raw is not none %}
  {% set data = raw | from_json %}
  {% if data | length > 0 %}
    {% set f_avg_tomorrow = data[0].f_avg_tomorrow | float(default=50.0) %}
    {% set ns_pool = namespace(items=[]) %}
    {% for item in data %}
      {% if as_datetime(item.date) is not none %}
        {% set clouds_hist = item.h_avg_total | float(default=0) %}
        {% set ns_pool.items = ns_pool.items + [{'h_avg': clouds_hist}] %}
      {% endif %}
    {% endfor %}
    {% set pool = ns_pool.items %}
    {% set brighter = pool | selectattr('h_avg', 'le', f_avg_tomorrow) | list %}
    {% set darker = pool | selectattr('h_avg', 'ge', f_avg_tomorrow) | list %}
    {% if brighter | count > 0 and darker | count == 0 %}Light reduction
    {% elif darker | count > 0 and pool | selectattr('h_avg', 'le', f_avg_tomorrow) | list | count == 0 %}Max assumption
    {% elif pool | count > 0 %}Weighted average
    {% elif data | selectattr('date', 'equalto', 'forecast_only') | list | count > 0 %}No history yet
    {% else %}No data
    {% endif %}
  {% else %}No data
  {% endif %}
{% else %}No data
{% endif %}"""

DEFAULT_UNIT_OF_MEASUREMENT = "kWh"
DEFAULT_DEVICE_CLASS = "energy"
DEFAULT_STATE_CLASS = "total_increasing"
DEFAULT_PV_HISTORY_DAYS = 30
DEFAULT_PV_MAX_RECORD = 0.0

# Advanced SQL Query Template
DEFAULT_SQL_QUERY = """WITH vars AS (
    SELECT 
        '{sensor_clouds}' as sensor_clouds,
        '{sensor_pv_first}' as sensor_pv,
        '{sensor_forecast}' as sensor_forecast,
        '{sensor_uv}' as sensor_uv,
        '{weather_entity}' as weather_entity,
        (strftime('%s', 'now', 'localtime') - strftime('%s', 'now')) || ' seconds' as offset
),

ids AS (
    SELECT 
        (SELECT id FROM statistics_meta WHERE statistic_id = (SELECT sensor_clouds FROM vars)) as w_id_stats,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_clouds FROM vars)) as w_id_states,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_forecast FROM vars) LIMIT 1) as f_id,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT weather_entity FROM vars)) as w_entity_id,
        (SELECT metadata_id FROM states_meta WHERE entity_id = 'sun.sun') as sun_id,
        (SELECT id FROM statistics_meta WHERE statistic_id = (SELECT sensor_uv FROM vars)) as uv_id_stats,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_uv FROM vars)) as uv_id_states
),

pv_stat_ids AS (
    /* IDs and units of all configured PV energy sensor statistics (supports 1…n panels) */
    SELECT id,
           CASE WHEN unit_of_measurement = 'Wh' THEN 1000.0 ELSE 1.0 END as divisor
    FROM statistics_meta
    WHERE statistic_id IN ({sensor_pv_list})
),

pv_activity AS (
    /* sunrise = first above_horizon transition yesterday (UTC epoch stored, displayed as UTC HH:MM) */
    /* sunset  = first below_horizon transition AFTER sunrise                                       */
    /* _local columns = same instants displayed in local time – used for phase detection only       */
    /* Forecast datetimes are UTC (+00:00), so sun_start/sun_end (UTC) are used for BETWEEN.        */
    SELECT 
        COALESCE((
            SELECT strftime('%H:%M', last_updated_ts, 'unixepoch')
            FROM states
            WHERE metadata_id = (SELECT sun_id FROM ids)
              AND date(last_updated_ts, 'unixepoch', (SELECT offset FROM vars)) = date('now', (SELECT offset FROM vars), '-1 day')
              AND state = 'above_horizon'
            ORDER BY last_updated_ts ASC LIMIT 1
        ), '05:30') as sun_start,
        COALESCE((
            SELECT strftime('%H:%M', last_updated_ts, 'unixepoch')
            FROM states
            WHERE metadata_id = (SELECT sun_id FROM ids)
              AND state = 'below_horizon'
              AND last_updated_ts > (
                  SELECT last_updated_ts FROM states
                  WHERE metadata_id = (SELECT sun_id FROM ids)
                    AND date(last_updated_ts, 'unixepoch', (SELECT offset FROM vars)) = date('now', (SELECT offset FROM vars), '-1 day')
                    AND state = 'above_horizon'
                  ORDER BY last_updated_ts ASC LIMIT 1
              )
            ORDER BY last_updated_ts ASC LIMIT 1
        ), '17:30') as sun_end,
        COALESCE((
            SELECT strftime('%H:%M', last_updated_ts, 'unixepoch', (SELECT offset FROM vars))
            FROM states
            WHERE metadata_id = (SELECT sun_id FROM ids)
              AND date(last_updated_ts, 'unixepoch', (SELECT offset FROM vars)) = date('now', (SELECT offset FROM vars), '-1 day')
              AND state = 'above_horizon'
            ORDER BY last_updated_ts ASC LIMIT 1
        ), '06:30') as sun_start_local,
        COALESCE((
            SELECT strftime('%H:%M', last_updated_ts, 'unixepoch', (SELECT offset FROM vars))
            FROM states
            WHERE metadata_id = (SELECT sun_id FROM ids)
              AND state = 'below_horizon'
              AND last_updated_ts > (
                  SELECT last_updated_ts FROM states
                  WHERE metadata_id = (SELECT sun_id FROM ids)
                    AND date(last_updated_ts, 'unixepoch', (SELECT offset FROM vars)) = date('now', (SELECT offset FROM vars), '-1 day')
                    AND state = 'above_horizon'
                  ORDER BY last_updated_ts ASC LIMIT 1
              )
            ORDER BY last_updated_ts ASC LIMIT 1
        ), '18:30') as sun_end_local
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
                         /* Forecast slots are UTC: compare against UTC sun_start/sun_end.          */
                         /* Only the start of the window shifts: during the day use current UTC     */
                         /* time (remaining today); before local sunrise or after local sunset use  */
                         /* full day window (midnight use-case: forecast for the whole coming day). */
                         WHEN strftime('%H:%M', 'now', (SELECT offset FROM vars))
                              BETWEEN (SELECT sun_start_local FROM pv_activity)
                                  AND (SELECT sun_end_local   FROM pv_activity)
                             THEN strftime('%H:%M', 'now')
                         ELSE (SELECT sun_start FROM pv_activity)
                       END
               AND (SELECT sun_end FROM pv_activity)
        ), 50.0) as f_avg,
        COALESCE(
        (SELECT AVG(CAST(json_extract(f.value, '$.uv_index') AS FLOAT)) 
         FROM states s 
         JOIN state_attributes a ON s.attributes_id = a.attributes_id, 
         json_each(a.shared_attrs, '$.forecast') f 
         WHERE s.metadata_id = (SELECT f_id FROM ids) 
           AND s.last_updated_ts = (SELECT MAX(last_updated_ts) FROM states WHERE metadata_id = (SELECT f_id FROM ids)) 
           AND substr(json_extract(f.value, '$.datetime'), 1, 10) = date('now', (SELECT offset FROM vars))
           AND substr(json_extract(f.value, '$.datetime'), 12, 5)
               BETWEEN CASE
                         WHEN strftime('%H:%M', 'now', (SELECT offset FROM vars))
                              BETWEEN (SELECT sun_start_local FROM pv_activity)
                                  AND (SELECT sun_end_local   FROM pv_activity)
                             THEN strftime('%H:%M', 'now')
                         ELSE (SELECT sun_start FROM pv_activity)
                       END
               AND (SELECT sun_end FROM pv_activity)
        ), 0.0) as uv_avg
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
    ), 50.0) as f_avg_tomorrow,
    COALESCE((
        SELECT AVG(CAST(json_extract(f.value, '$.uv_index') AS FLOAT)) 
        FROM states s 
        JOIN state_attributes a ON s.attributes_id = a.attributes_id, 
        json_each(a.shared_attrs, '$.forecast') f 
        WHERE s.metadata_id = (SELECT f_id FROM ids) 
          AND s.last_updated_ts = (SELECT MAX(last_updated_ts) FROM states WHERE metadata_id = (SELECT f_id FROM ids)) 
          AND substr(json_extract(f.value, '$.datetime'), 1, 10) = date('now', (SELECT offset FROM vars), '+1 day') 
          AND substr(json_extract(f.value, '$.datetime'), 12, 5) BETWEEN (SELECT sun_start FROM pv_activity) AND (SELECT sun_end FROM pv_activity)
    ), 0.0) as uv_avg_tomorrow
),

cloud_history AS (
    /* Cloud coverage history — uv_val is NULL here; UV comes from the UV-sensor branches below */
    SELECT start_ts as ts, CAST(COALESCE(mean, state) AS FLOAT) as val, NULL as uv_val
    FROM statistics
    WHERE metadata_id = (SELECT w_id_stats FROM ids)
      AND start_ts > strftime('%s', 'now', '-{history_days} days')
    UNION ALL
    SELECT s.last_updated_ts as ts,
      CASE WHEN (SELECT sensor_clouds FROM vars) LIKE 'weather.%'
           THEN CAST(json_extract(a.shared_attrs, '$.cloud_coverage') AS FLOAT)
           ELSE CAST(s.state AS FLOAT)
      END as val,
      CASE WHEN (SELECT sensor_clouds FROM vars) LIKE 'weather.%'
           THEN CAST(json_extract(a.shared_attrs, '$.uv_index') AS FLOAT)
           ELSE NULL
      END as uv_val
    FROM states s
    LEFT JOIN state_attributes a ON s.attributes_id = a.attributes_id
    WHERE s.metadata_id = (SELECT w_id_states FROM ids)
      AND ((SELECT sensor_clouds FROM vars) LIKE 'weather.%' OR NOT EXISTS (SELECT 1 FROM statistics WHERE metadata_id = (SELECT w_id_stats FROM ids)))
      AND s.last_updated_ts > strftime('%s', 'now', '-10 days')
      AND s.state NOT IN ('unknown', 'unavailable', '')
    UNION ALL
    SELECT s.last_updated_ts as ts,
        CAST(json_extract(a.shared_attrs, '$.cloud_coverage') AS FLOAT) as val,
        CAST(json_extract(a.shared_attrs, '$.uv_index') AS FLOAT) as uv_val
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
    UNION ALL
    /* UV sensor long-term statistics */
    SELECT start_ts as ts, NULL as val, CAST(COALESCE(mean, state) AS FLOAT) as uv_val
    FROM statistics
    WHERE metadata_id = (SELECT uv_id_stats FROM ids)
      AND (SELECT uv_id_stats FROM ids) IS NOT NULL
      AND start_ts > strftime('%s', 'now', '-{history_days} days')
    UNION ALL
    /* UV sensor recent states (covers last 10 days before LTS has built up) */
    SELECT s.last_updated_ts as ts, NULL as val, CAST(s.state AS FLOAT) as uv_val
    FROM states s
    WHERE s.metadata_id = (SELECT uv_id_states FROM ids)
      AND (SELECT uv_id_states FROM ids) IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM statistics WHERE metadata_id = (SELECT uv_id_stats FROM ids)
            AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) = date(s.last_updated_ts, 'unixepoch', (SELECT offset FROM vars)))
      AND s.last_updated_ts > strftime('%s', 'now', '-10 days')
      AND s.state NOT IN ('unknown', 'unavailable', '')
),

matching_days AS (
    SELECT 
        date(ts, 'unixepoch', (SELECT offset FROM vars)) as day, 
        AVG(CASE WHEN strftime('%H:%M', ts, 'unixepoch') BETWEEN (SELECT sun_start FROM pv_activity) AND (SELECT sun_end FROM pv_activity) THEN val END) as h_avg_total_val,
    AVG(CASE WHEN strftime('%H:%M', ts, 'unixepoch') >= strftime('%H:00', 'now') AND strftime('%H:%M', ts, 'unixepoch') <= (SELECT sun_end FROM pv_activity) THEN val END) as h_avg_rest_val,
    AVG(CASE WHEN strftime('%H:%M', ts, 'unixepoch') BETWEEN (SELECT sun_start FROM pv_activity) AND (SELECT sun_end FROM pv_activity) THEN uv_val END) as uv_avg_total_val,
    AVG(CASE WHEN strftime('%H:%M', ts, 'unixepoch') >= strftime('%H:00', 'now') AND strftime('%H:%M', ts, 'unixepoch') <= (SELECT sun_end FROM pv_activity) THEN uv_val END) as uv_avg_rest_val
    FROM cloud_history 
    WHERE date(ts, 'unixepoch', (SELECT offset FROM vars)) < date('now', (SELECT offset FROM vars)) 
    GROUP BY 1 
    HAVING h_avg_total_val IS NOT NULL
    ORDER BY (
        ABS(COALESCE(h_avg_rest_val, h_avg_total_val) - (SELECT f_avg FROM forecast_val)) * 0.7
        + ABS(COALESCE(uv_avg_rest_val, uv_avg_total_val, 0) - (SELECT uv_avg FROM forecast_val)) * 8.0 * 0.3
    ) ASC
),

pv_per_sensor AS (
    /* Per-sensor per-day aggregates normalised to kWh (Wh ÷ 1000, kWh ÷ 1).
       Normalising here means sensors with different units (Wh vs kWh) are summed
       correctly in pv_daily even when panels report in different units. */
    SELECT
        psi.id as meta_id,
        date(s.start_ts, 'unixepoch', (SELECT offset FROM vars)) as day,
        MAX(CAST(s.state AS FLOAT)) / psi.divisor as s_max,
        MIN(CASE WHEN CAST(s.state AS FLOAT) > 0 THEN CAST(s.state AS FLOAT) ELSE NULL END) / psi.divisor as s_min,
        MAX(CASE WHEN strftime('%H', s.start_ts, 'unixepoch') = strftime('%H', 'now')
                 THEN CAST(s.state AS FLOAT) ELSE NULL END) / psi.divisor as s_curr,
        MAX(CASE WHEN strftime('%H', s.start_ts, 'unixepoch') = strftime('%H', 'now', '-1 hour')
                 THEN CAST(s.state AS FLOAT) ELSE NULL END) / psi.divisor as s_prev
    FROM statistics s
    JOIN pv_stat_ids psi ON s.metadata_id = psi.id
    GROUP BY psi.id, date(s.start_ts, 'unixepoch', (SELECT offset FROM vars))
),

pv_daily AS (
    /* Sum all panels for each historical day */
    SELECT
        day,
        SUM(s_max) as day_max,
        SUM(s_min) as day_min,
        /* COALESCE order: s_curr (current hour bucket, may not exist yet at hour boundary)    */
        /* → s_prev (previous hour bucket, close to current level if s_curr is missing)        */
        /* → s_min (first reading of the day, last resort before sunrise or first install).    */
        /* Using s_min directly caused a spike to the full day yield at the sunset hour flip.  */
        SUM(COALESCE(s_curr, s_prev, s_min)) as h_hour_curr,
        SUM(COALESCE(s_prev, s_min)) as h_hour_prev
    FROM pv_per_sensor
    WHERE s_max IS NOT NULL AND day IS NOT NULL
    GROUP BY day
),

final_data AS (
    SELECT
        md.*,
        pd.day_max,
        pd.day_min,
        pd.h_hour_curr,
        pd.h_hour_prev
    FROM matching_days md
    LEFT JOIN pv_daily pd ON pd.day = md.day
)

SELECT COALESCE(json_group_array(
    json_object(
        'date', day,
        'f_avg_today_remaining', (SELECT ROUND(f_avg, 1) FROM forecast_val),        
        'f_avg_tomorrow', (SELECT ROUND(f_avg_tomorrow, 1) FROM forecast_next_day),
    'uv_avg_today_remaining', (SELECT ROUND(uv_avg, 1) FROM forecast_val),
    'uv_avg_tomorrow', (SELECT ROUND(uv_avg_tomorrow, 1) FROM forecast_next_day),
        'h_avg_total', ROUND(h_avg_total_val, 1),
        /* COALESCE: before sunrise h_avg_rest_val is NULL (UTC window '23:xx'..'17:xx' empty) */
        /* Fall back to h_avg_total_val so Jinja cloud-matching works correctly at midnight.   */
        'h_avg_remaining', ROUND(COALESCE(h_avg_rest_val, h_avg_total_val), 1),
    'uv_avg_total', ROUND(uv_avg_total_val, 1),
    'uv_avg_remaining', ROUND(COALESCE(uv_avg_rest_val, uv_avg_total_val), 1),
        'yield_day_total', ROUND((day_max - day_min), 2),
        'yield_day_remaining', ROUND(CASE
            /* Phase detection must use LOCAL time: UTC HH:MM fails between local midnight        */
            /* and 00:00 UTC (e.g. 23:00 UTC > sun_end 17:32 UTC → wrongly returns 0.0).         */
            WHEN strftime('%H:%M', 'now', (SELECT offset FROM vars)) > (SELECT sun_end_local   FROM pv_activity)
                THEN 0.0
            WHEN strftime('%H:%M', 'now', (SELECT offset FROM vars)) < (SELECT sun_start_local FROM pv_activity)
                THEN (day_max - day_min)
            ELSE MAX(0, 
                ((h_hour_curr - h_hour_prev) * (1.0 - (CAST(strftime('%M', 'now') AS FLOAT) / 60.0)) * 
                  CASE 
                    WHEN strftime('%H', 'now') = strftime('%H', (SELECT sun_start FROM pv_activity)) THEN 0.85 
                    WHEN strftime('%H', 'now') = strftime('%H', (SELECT sun_end FROM pv_activity)) THEN 0.70 
                    ELSE 1.0 
                  END) 
                + (day_max - h_hour_curr)
            ) -- values already in kWh (normalised per-sensor in pv_per_sensor CTE)
        END, 2),
        'pv_start', (SELECT sun_start FROM pv_activity),
        'pv_end', (SELECT sun_end FROM pv_activity)
    )
), '[]') as json 
FROM (
    SELECT day, h_avg_total_val, h_avg_rest_val, uv_avg_total_val, uv_avg_rest_val,
           day_max, day_min, h_hour_curr, h_hour_prev
    FROM final_data WHERE day_max > 0
    UNION ALL
    /* Fallback row: provides forecast values even when no historical data exists (new install). */
    /* date='forecast_only' → as_datetime() returns None → skipped in all Jinja loops.         */
    SELECT 'forecast_only', NULL, NULL, NULL, NULL, 1, NULL, NULL, NULL
    WHERE NOT EXISTS (SELECT 1 FROM final_data WHERE day_max > 0))"""

DEFAULT_LOVELACE_TEMPLATE_REMAINING_TODAY = """{#- Remaining-today table only (no headlines) -#}
{% if raw_json and raw_json != '[]' and raw_json is not none %}
  {% set data = raw_json | from_json %}
  {% if data | length > 0 %}
    {% set f_avg = data[0].f_avg_today_remaining | float(default=50.0) %}
    {% set f_uv_avg = data[0].uv_avg_today_remaining | float(default=0.0) %}
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
    {% set latitude = state_attr('zone.home', 'latitude') | float(48.0) %}
    {% set doy = now().strftime('%j') | int(default=1) %}
    {% set lat_rad = latitude * pi / 180 %}
    {% set decl = -0.4093 * cos(2 * pi * (doy + 10) / 365) %}
    {% set dl_today = 24 / pi * acos([[(-tan(lat_rad) * tan(decl)), -1.0] | max, 1.0] | min) %}
    {% set sun_today = 0.80 + 0.20 * cos((doy - 172) * 2 * pi / 365) %}
    {% set ns_pool = namespace(items=[], total_w=0) %}
    {% for item in data %}
      {% set yield_raw = item.yield_day_remaining | float(default=0) %}
      {% set clouds = item.h_avg_remaining | float(default=0) %}
      {% set uv = item.uv_avg_remaining | float(default=0) %}
      {% set item_dt = as_datetime(item.date) %}
      {% if item_dt is not none %}
        {% set item_day = item_dt.strftime('%j') | int(default=1) %}
        {% set decl_i = -0.4093 * cos(2 * pi * (item_day + 10) / 365) %}
        {% set dl_item = 24 / pi * acos([[(-tan(lat_rad) * tan(decl_i)), -1.0] | max, 1.0] | min) %}
        {% set sun_item = 0.80 + 0.20 * cos((item_day - 172) * 2 * pi / 365) %}
        {% set s_korr = (sun_today / sun_item) * (dl_today / dl_item) %}
        {% set diff_c = (clouds - f_avg) | abs %}
        {% if f_uv_avg > 0 %}
          {% set uv_w = [0.3 + 0.4 * (f_avg / 100.0), 0.7] | min %}
          {% set diff = diff_c * (1.0 - uv_w) + (uv - f_uv_avg) | abs * 8.0 * uv_w %}
        {% else %}
          {% set diff = diff_c %}
        {% endif %}
        {% set days_ago = ((now().timestamp() - item_dt.timestamp()) / 86400) | int(0) %}
        {% set w = (1 / ([diff, 0.5] | max)) * (1.0 + 0.3 * ([1.0 - days_ago / 30.0, 0.0] | max)) %}
        {% if yield_raw > 0.05 or clouds > 95 or current_month in [12, 1, 2] %}
          {% set ns_pool.total_w = ns_pool.total_w + w %}
          {% set ns_pool.items = ns_pool.items + [{'date': item.date, 'h_avg': clouds, 'uv_avg': uv, 'y_korr': yield_raw * s_korr, 's_fakt': s_korr, 'w': w, 'filtered': false}] %}
        {% else %}
          {% set ns_pool.items = ns_pool.items + [{'date': item.date, 'h_avg': clouds, 'uv_avg': uv, 'y_korr': yield_raw * s_korr, 's_fakt': s_korr, 'w': 0, 'filtered': true}] %}
        {% endif %}
      {% endif %}
    {% endfor %}
    {% set top15 = (ns_pool.items | sort(attribute='w', reverse=True))[:15] %}
    {% set ns_top = namespace(total_w=0) %}
    {% for item in top15 %}{% if not item.filtered %}{% set ns_top.total_w = ns_top.total_w + item.w %}{% endif %}{% endfor %}
    {% set pool = top15 | selectattr('filtered', 'equalto', false) | list %}
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
| date | clouds | uv | rem. yield | weight |
| :--- | :---: | :---: | :---: | :---: |
{%- for item in top15 %}
| {{ item.date }} | {{ item.h_avg }}%{% if item.filtered %} ❌{% endif %} | {{ item.uv_avg | round(1) }} | **{{ (item.y_korr * snow_factor_today) | round(2) }} kWh** <small>({{ item.s_fakt | round(2) }}x)</small> | {{ (((item.w / ns_top.total_w) * 100) if ns_top.total_w > 0 else 0) | round(1) }}% |
{%- endfor %}
  {% endif %}
{% endif %}{% if data | length > 0 %}Info: showing {{ top15 | length }} out of {{ data | length }} days. Forecast basis: {{ f_avg }}% clouds, {{ f_uv_avg | round(1) }} uv. {% endif %}"""

DEFAULT_LOVELACE_TEMPLATE_TOMORROW = """{#- Tomorrow full-day table only (no headlines) -#}
{% if raw_json and raw_json != '[]' and raw_json is not none %}
  {% set data = raw_json | from_json %}
  {% if data | length > 0 %}
    {% set f_avg_tomorrow = data[0].f_avg_tomorrow | float(default=50.0) %}
    {% set f_uv_avg_tomorrow = data[0].uv_avg_tomorrow | float(default=0.0) %}
    {% set latitude = state_attr('zone.home', 'latitude') | float(48.0) %}
    {% set doy_tomorrow = (now() + timedelta(days=1)).strftime('%j') | int(default=1) %}
    {% set lat_rad = latitude * pi / 180 %}
    {% set decl_tomorrow = -0.4093 * cos(2 * pi * (doy_tomorrow + 10) / 365) %}
    {% set dl_tomorrow = 24 / pi * acos([[(-tan(lat_rad) * tan(decl_tomorrow)), -1.0] | max, 1.0] | min) %}
    {% set sun_tomorrow = 0.80 + 0.20 * cos((doy_tomorrow - 172) * 2 * pi / 365) %}
    {% set ns_pool = namespace(items=[], total_w=0) %}
    {% for item in data %}
      {% set yield_total = item.yield_day_total | float(default=0) %}
      {% set clouds_hist = item.h_avg_total | float(default=0) %}
      {% set uv_hist = item.uv_avg_total | float(default=0) %}
      {% set dt_item = as_datetime(item.date) %}
      {% if dt_item is not none %}
        {% set item_day = dt_item.strftime('%j') | int(default=1) %}
        {% set decl_i = -0.4093 * cos(2 * pi * (item_day + 10) / 365) %}
        {% set dl_item = 24 / pi * acos([[(-tan(lat_rad) * tan(decl_i)), -1.0] | max, 1.0] | min) %}
        {% set sun_item = 0.80 + 0.20 * cos((item_day - 172) * 2 * pi / 365) %}
        {% set s_korr = (sun_tomorrow / sun_item) * (dl_tomorrow / dl_item) %}
        {% set diff_c = (clouds_hist - f_avg_tomorrow) | abs %}
        {% if f_uv_avg_tomorrow > 0 %}
          {% set uv_w = [0.3 + 0.4 * (f_avg_tomorrow / 100.0), 0.7] | min %}
          {% set diff = diff_c * (1.0 - uv_w) + (uv_hist - f_uv_avg_tomorrow) | abs * 8.0 * uv_w %}
        {% else %}
          {% set diff = diff_c %}
        {% endif %}
        {% set days_ago = ((now().timestamp() - dt_item.timestamp()) / 86400) | int(0) %}
        {% set w = (1 / ([diff, 0.5] | max)) * (1.0 + 0.3 * ([1.0 - days_ago / 30.0, 0.0] | max)) %}
        {% set ns_pool.total_w = ns_pool.total_w + w %}
        {% set ns_pool.items = ns_pool.items + [{'date': item.date, 'h_avg': clouds_hist, 'uv_avg': uv_hist, 'y_korr': yield_total * s_korr, 's_fakt': s_korr, 'w': w}] %}
      {% endif %}
    {% endfor %}
    {% set top15 = (ns_pool.items | sort(attribute='w', reverse=True))[:15] %}
    {% set ns_top = namespace(total_w=0) %}
    {% for item in top15 %}{% set ns_top.total_w = ns_top.total_w + item.w %}{% endfor %}
    {% set pool = top15 %}
    {% set brighter = pool | selectattr('h_avg', 'le', f_avg_tomorrow) | list %}
    {% set darker = pool | selectattr('h_avg', 'ge', f_avg_tomorrow) | list %}
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
      {% set res = ns_mix.ws / (ns_top.total_w if ns_top.total_w > 0 else 1) %}
    {% endif %}
| date | clouds | uv | day yield  | weight |
| :--- | :---: | :---: | :---:  | :---: |
{%- for item in top15 %}
| {{ item.date }} | {{ item.h_avg }}% | {{ item.uv_avg | round(1) }} | **{{ item.y_korr | round(2) }} kWh** <small><small>({{ item.s_fakt | round(2) }}x)</small></small> | {{ (((item.w / ns_top.total_w) * 100) if ns_top.total_w > 0 else 0) | round(1) }}% |
{%- endfor %}
  {% endif %}
{% endif %}{% if data | length > 0 %}Info: showing {{ top15 | length }} out of {{ data | length }} days. Forecast basis: {{ f_avg_tomorrow }}% clouds, {{ f_uv_avg_tomorrow | round(1) }} uv. {% endif %}"""
