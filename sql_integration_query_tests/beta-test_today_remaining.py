{# =================================================================
   PV remaining yield today – Lovelace Markdown Card (Option B: Inline template)
   Source sensor:   sensor.pv_hist_remaining_today  (attribute: json)
   Forecast sensor: sensor.pv_hist_weather_forecast (attribute: forecast)

   RECOMMENDED: Use Option A instead of this inline template:
   {{ state_attr('sensor.pv_hist_remaining_today', 'lovelace_card_card_remaining_today') }}

   Option B: Use this content directly as a Lovelace Markdown card.

   Cross-validation accuracy columns (LOO = Leave-One-Out):
     Acc.Rem — how the day's remaining yield compares to the consensus of all other pool days
     Acc.Day — how the day's full-day yield compares to the consensus of all other pool days
     100 % = perfectly in line · <100 % = lower than others · >100 % = higher than others
   ================================================================= #}
{% set raw_json = state_attr('sensor.pv_hist_remaining_today', 'json') %}
{% if raw_json and raw_json != '[]' and raw_json is not none %}
  {% set data = raw_json | from_json %}

  {% if data | length > 0 %}
    {% set f_avg = data[0].f_avg_today_remaining | float(default=50.0) %}
    {% set f_uv_avg = data[0].uv_avg_today_remaining | float(default=0.0) %}

    {# 0. NIGHT-CHECK: 0.0 only after local sunset until midnight. Midnight→sunrise: full-day forecast. #}
    {% set offset_min = (now().utcoffset().total_seconds() / 60) | int %}
    {% set pv_end_utc = data[0].pv_end | default('17:30') %}
    {% set end_min_local = ((pv_end_utc.split(':')[0] | int) * 60 + (pv_end_utc.split(':')[1] | int) + offset_min) % 1440 %}
    {% set is_night = (now().hour * 60 + now().minute) > end_min_local %}

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

    {# 2. ASTRONOMISCHE BASISDATEN (Breitengrad aus zone.home) #}
    {% set latitude = state_attr('zone.home', 'latitude') | float(48.0) %}
    {% set doy = now().strftime('%j') | int(default=1) %}
    {% set lat_rad = latitude * pi / 180 %}
    {% set decl = -0.4093 * cos(2 * pi * (doy + 10) / 365) %}
    {% set cos_ha = -tan(lat_rad) * tan(decl) %}
    {% set dl_today = 24 / pi * acos([[cos_ha, -1.0] | max, 1.0] | min) %}
    {% set sun_today = 0.80 + 0.20 * cos((doy - 172) * 2 * pi / 365) %}

    {# 3. POOL AUFBAUEN #}
    {% set ns_pool = namespace(items=[], total_w=0) %}
    {% for item in data %}
      {% set yield_raw = item.yield_day_remaining | float(default=0) %}
      {% set clouds = item.h_avg_remaining | float(default=0) %}
      {% set clouds_total = item.h_avg_total | float(default=0) %}
      {% set uv = item.uv_avg_remaining | float(default=0) %}
      {% set item_dt = as_datetime(item.date) %}
      {% if item_dt is not none %}
        {% set item_day = item_dt.strftime('%j') | int(default=1) %}
        {% set decl_i = -0.4093 * cos(2 * pi * (item_day + 10) / 365) %}
        {% set cos_ha_i = -tan(lat_rad) * tan(decl_i) %}
        {% set dl_item = 24 / pi * acos([[cos_ha_i, -1.0] | max, 1.0] | min) %}
        {% set sun_item = 0.80 + 0.20 * cos((item_day - 172) * 2 * pi / 365) %}
        {% set s_korr = (sun_today / sun_item) * (dl_today / dl_item) %}
        {% set diff_c = (clouds - f_avg) | abs %}
        {% if f_uv_avg > 0 %}
          {# UV weight scales with forecast cloud coverage: at clear skies 0.3 (same as before), #}
          {# at 100% overcast 0.7 — UV index becomes the primary discriminator under thick clouds. #}
          {% set uv_w = [0.3 + 0.4 * (f_avg / 100.0), 0.7] | min %}
          {% set diff = diff_c * (1.0 - uv_w) + (uv - f_uv_avg) | abs * 8.0 * uv_w %}
        {% else %}
          {% set diff = diff_c %}
        {% endif %}
        {% set days_ago = ((now().timestamp() - item_dt.timestamp()) / 86400) | int(0) %}
        {% set w = (1 / ([diff, 0.5] | max)) * (1.0 + 0.3 * ([1.0 - days_ago / 30.0, 0.0] | max)) %}
        {% set yield_day = item.yield_day_total | float(default=0) %}
        {% if yield_raw > 0.05 or clouds > 95 or current_month in [12, 1, 2] %}
          {% set ns_pool.total_w = ns_pool.total_w + w %}
          {% set ns_pool.items = ns_pool.items + [{'date': item.date, 'h_avg': clouds, 'h_avg_total': clouds_total, 'uv_avg': uv, 'y_korr': yield_raw * s_korr, 'y_day_korr': yield_day * s_korr, 's_fakt': s_korr, 'w': w, 'yield_day_total': yield_day, 'days_ago': days_ago, 'filtered': false}] %}
        {% else %}
          {% set ns_pool.items = ns_pool.items + [{'date': item.date, 'h_avg': clouds, 'h_avg_total': clouds_total, 'uv_avg': uv, 'y_korr': yield_raw * s_korr, 'y_day_korr': yield_day * s_korr, 's_fakt': s_korr, 'w': 0, 'yield_day_total': yield_day, 'days_ago': days_ago, 'filtered': true}] %}
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
      {% set res = ns_mix.ws / (ns_top.total_w if ns_top.total_w > 0 else 1) %}
    {% endif %}

    {# 5. CROSS-VALIDATION (LOO) — every pool entry proved against all others #}
    {# For each day D: compute weighted forecast from all OTHER pool days,         #}
    {# then compare D's actual yield (remaining + full-day) to that prediction.   #}
    {# Result: acc_rem and acc_day as percentage (100 % = perfect consensus fit). #}
    {% set ns_cv = namespace(items=[]) %}
    {% for item_i in pool %}
      {% set ns_loo = namespace(w=0, wy_rem=0, wy_day=0) %}
      {% for item_j in pool %}
        {% if item_j.date != item_i.date %}
          {% set ns_loo.w     = ns_loo.w     + item_j.w %}
          {% set ns_loo.wy_rem = ns_loo.wy_rem + item_j.w * item_j.y_korr %}
          {% set ns_loo.wy_day = ns_loo.wy_day + item_j.w * item_j.y_day_korr %}
        {% endif %}
      {% endfor %}
      {% if ns_loo.w > 0 and ns_loo.wy_rem > 0 %}
        {% set pred_rem = ns_loo.wy_rem / ns_loo.w %}
        {% set pred_day = ns_loo.wy_day / ns_loo.w %}
        {% set acc_rem = ((item_i.y_korr      / ([pred_rem, 0.001] | max)) * 100) | round(0) | int %}
        {% set acc_day = ((item_i.y_day_korr  / ([pred_day, 0.001] | max)) * 100) | round(0) | int %}
      {% else %}
        {% set acc_rem = 0 %}
        {% set acc_day = 0 %}
      {% endif %}
      {% set ns_cv.items = ns_cv.items + [{'date': item_i.date, 'acc_rem': acc_rem, 'acc_day': acc_day}] %}
    {% endfor %}

    {# 6. CORRECTED FINAL FORECAST — Acc.Rem used as weight modifier                       #}
    {# acc_factor = 1 / (1 + |acc_rem − 100| / 100)                                        #}
    {# Day perfectly in line with others (acc_rem = 100 %) → acc_factor = 1.0 (no change)  #}
    {# Day is a 50 %-outlier (acc_rem = 50 % or 150 %)  → acc_factor = 0.67 (down-weighted) #}
    {# Replace res with this corrected weighted average when pool has ≥ 2 entries.           #}
    {% if pool | count > 1 %}
      {% set ns_corr = namespace(w=0, wy=0) %}
      {% for item_i in pool %}
        {%- set cv = ns_cv.items | selectattr('date', 'equalto', item_i.date) | list %}
        {% if cv | length > 0 %}
          {% set dev = ((cv[0].acc_rem - 100) | abs) / 100.0 %}
          {% set acc_factor = 1.0 / (1.0 + dev) %}
          {% set ns_corr.w  = ns_corr.w  + item_i.w * acc_factor %}
          {% set ns_corr.wy = ns_corr.wy + item_i.y_korr * item_i.w * acc_factor %}
        {% else %}
          {% set ns_corr.w  = ns_corr.w  + item_i.w %}
          {% set ns_corr.wy = ns_corr.wy + item_i.y_korr * item_i.w %}
        {% endif %}
      {% endfor %}
      {% if ns_corr.w > 0 and ns_corr.wy > 0 %}
        {% set res = ns_corr.wy / ns_corr.w %}
        {% set method = method ~ ' +LOO' %}
      {% endif %}
    {% endif %}

    {# 7. TREND DAMPING — detect if recent pool days (≤14d) are systematically higher     #}
    {# than older ones. LOO cannot see this bias because recent outliers confirm each other. #}
    {# trend_ratio = weighted avg of recent y_korr ÷ weighted avg of older y_korr.          #}
    {# Threshold 1.15: only dampen when recent exceeds older by >15%.                        #}
    {# Strength 0.5: absorb 50% of the excess → moderate, reversible correction.            #}
    {# Method label adds " ↓td" so the correction is visible in the card.                   #}
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
      {% if avg_old > 0 %}
        {% set trend_ratio = avg_rec / avg_old %}
        {% if trend_ratio > 1.15 %}
          {% set res = res / (1.0 + 0.5 * (trend_ratio - 1.0)) %}
          {% set method = method ~ ' ↓td' %}
        {% endif %}
      {% endif %}
    {% endif %}

    {# BACK-TEST: consecutive-day carry-through over ALL data entries                     #}
    {# Trigger range [40 %, 85 %) of mean: moderate shortfalls that tend to persist.       #}
    {# < 40 % = extreme low (storm/heavy rain) → usually rebounds next day (❌ noise).     #}
    {# ≥ 85 % = normal day → no penalty warranted.                                         #}
    {% set ns_all = namespace(sum_y=0.0, count_y=0) %}
    {% for item in ns_pool.items %}
      {% if not item.filtered and item.y_day_korr > 0 %}
        {% set ns_all.sum_y   = ns_all.sum_y   + item.y_day_korr %}
        {% set ns_all.count_y = ns_all.count_y + 1 %}
      {% endif %}
    {% endfor %}
    {% set mean_y = ns_all.sum_y / ([ns_all.count_y, 1] | max) %}

    {% set ns_bt = namespace(total=0, useful=0, trigger_sum=0.0, carry_sum=0.0, rows=[]) %}
    {% for item_i in ns_pool.items %}
      {% if not item_i.filtered and item_i.y_day_korr >= 0.40 * mean_y and item_i.y_day_korr < 0.85 * mean_y %}
        {% set next_date  = (as_datetime(item_i.date) + timedelta(days=1)).strftime('%Y-%m-%d') %}
        {% set next_items = ns_pool.items | selectattr('date', 'equalto', next_date) | selectattr('filtered', 'equalto', false) | list %}
        {% set rel_i = (item_i.y_day_korr / mean_y * 100) | round(0) | int %}
        {% if next_items | length > 0 %}
          {% set item_j = next_items[0] %}
          {% set ns_bt.total       = ns_bt.total + 1 %}
          {% set shf_i             = 1.0 - item_i.y_day_korr / mean_y %}
          {% set shf_j             = ([1.0 - item_j.y_day_korr / mean_y, 0.0] | max) %}
          {% set ns_bt.trigger_sum = ns_bt.trigger_sum + shf_i %}
          {% set ns_bt.carry_sum   = ns_bt.carry_sum   + shf_j %}
          {% set rel_j = (item_j.y_day_korr / mean_y * 100) | round(0) | int %}
          {% if item_j.y_day_korr < mean_y %}
            {% set ns_bt.useful = ns_bt.useful + 1 %}
            {% set row_label = '✅ yes (' ~ rel_j ~ '%)' %}
          {% else %}
            {% set row_label = '❌ no (' ~ rel_j ~ '%)' %}
          {% endif %}
          {% set ns_bt.rows = ns_bt.rows + [{'trigger': item_i.date, 'rel_i': rel_i, 'next': next_date, 'rel_j': rel_j, 'label': row_label}] %}
        {% endif %}
      {% endif %}
    {% endfor %}

    {# 8. YESTERDAY PENALTY — strength derived from pool back-test, not hardcoded.        #}
    {# Trigger range: acc_rem in [40 %, 85 %) — moderate shortfall only.                  #}
    {# < 40 %: extreme low (storm) → likely rebound today → back-test shows no carry.     #}
    {# ≥ 85 %: normal day → no penalty.                                                    #}
    {% if ns_bt.total > 0 and ns_bt.trigger_sum > 0 %}
      {% set carry_through   = ns_bt.carry_sum / ns_bt.trigger_sum %}
      {% set hit_rate_f      = ns_bt.useful / ns_bt.total %}
      {% set effective_carry = carry_through * hit_rate_f %}
    {% else %}
      {% set effective_carry = 0.3 %}
    {% endif %}
    {% set yesterday_date = (now() - timedelta(days=1)).strftime('%Y-%m-%d') %}
    {% set yest_cv = ns_cv.items | selectattr('date', 'equalto', yesterday_date) | list %}
    {# Penalty only fires when both yesterday AND today are overcast (≥60 % cloud):              #}
    {# a shortfall on a clear day is noise (shadowing, dust, etc.), not a weather pattern.       #}
    {# Requiring matching cloud levels prevents penalising today when conditions have changed.   #}
    {% set yest_item = pool | selectattr('date', 'equalto', yesterday_date) | list %}
    {% set yest_clouds = yest_item[0].h_avg if yest_item | length > 0 else 0 %}
    {% if yest_cv | length > 0 %}
      {% set yest_acc = yest_cv[0].acc_rem %}
      {% if yest_acc >= 40 and yest_acc < 85 and f_avg >= 60 and yest_clouds >= 60 %}
        {% set shortfall = (1.0 - yest_acc / 100.0) %}
        {% set penalty = 1.0 - effective_carry * shortfall %}
        {% set res = res * ([penalty, 0.5] | max) %}
        {% set method = method ~ ' ↓yp(' ~ yest_acc ~ '%/' ~ (effective_carry * 100) | round(0) | int ~ '%)' %}
      {% endif %}
    {% endif %}

    {% set scale = 1000 if res > 200 else 1 %}
    {% set final_val = (res / scale) * snow_factor_today %}

**Forecast:**
## {{ (0.0 if is_night else final_val) | round(2) }} kWh
*Basis: **{{ f_avg }}%** clouds, **{{ f_uv_avg }}** uv | **{{ method }}***
{% if snow_factor_today < 1.0 %}⚠️ **Snow suspected! ({{ (snow_factor_today * 100) | round(0) }}%)**{% endif %}

| Date | Day clouds | Day yield | Rem. clouds | Rem. uv | Rem. yield | Weight | Acc.Rem | Acc.Day |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
{%- for item in top15 %}
{%- set cv = ns_cv.items | selectattr('date', 'equalto', item.date) | list %}
| {{ item.date }} | {{ item.h_avg_total }}% | {{ item.yield_day_total | round(2) }} | **{{ item.h_avg }}%** | {{ item.uv_avg | round(1) }} | **{{ ((item.y_korr * snow_factor_today) / scale) | round(2) }} <small><small>({{ item.s_fakt | round(2) }}x)</small></small>**{% if item.filtered %}❌{% endif %} | {{ (((item.w / ns_top.total_w) * 100) if ns_top.total_w > 0 else 0) | round(1) }}% | {{ cv[0].acc_rem ~ '%' if cv | length > 0 else '—' }} | {{ cv[0].acc_day ~ '%' if cv | length > 0 else '—' }} |
{%- endfor %}

---
*Yesterday Penalty — back-test on all data (trigger: y\_day\_korr in [40 %, 85 %) of mean — moderate shortfalls only)*

| Trigger day | Rel.yield | Next day | Next rel. | Useful? |
| :--- | :---: | :--- | :---: | :---: |
{%- set bt_display = (ns_bt.rows | sort(attribute='trigger', reverse=True))[:10] %}
{%- for row in bt_display %}
| {{ row.trigger }} | {{ row.rel_i }}% | {{ row.next }} | {{ row.rel_j }}% | {{ row.label }} |
{%- endfor %}
{%- if ns_bt.rows | length > 10 %}
| *⋯* | | *+ {{ ns_bt.rows | length - 10 }} more pairs (counted in stats below)* | | |
{%- endif %}
{%- if ns_bt.total > 0 %}

*Pairs found: **{{ ns_bt.total }}** · Hit rate: **{{ ns_bt.useful }}/{{ ns_bt.total }}** ({{ ((ns_bt.useful / ns_bt.total) * 100) | round(0) | int }}%) · Carry-through: {{ (ns_bt.carry_sum / ns_bt.trigger_sum * 100) | round(0) | int }}% · Effective carry: **{{ (effective_carry * 100) | round(0) | int }}%*** — penalty is {% if (ns_bt.useful / ns_bt.total) >= 0.6 %}**supported** ✅{% elif (ns_bt.useful / ns_bt.total) >= 0.4 %}**inconclusive** ⚠️{% else %}**not supported** ❌{% endif %} by data*
{%- else %}

*No trigger pairs found in data. Fallback carry: {{ (effective_carry * 100) | round(0) | int }}%.*
{%- endif %}

  {% else %}
**No data in SQL result.**
  {% endif %}
{% else %}
**Waiting for SQL data...**
{% endif %}