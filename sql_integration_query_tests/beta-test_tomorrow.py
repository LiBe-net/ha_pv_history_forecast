{# =================================================================
   PV full-day yield tomorrow – Lovelace Markdown Card
   Source sensor:  sensor.pv_remaining_states  (attribute: json)
   Forecast field: f_avg_tomorrow / uv_avg_tomorrow  (full-day)

   Accuracy column (LOO = Leave-One-Out):
     Acc. — how each day's full-day yield compares to the consensus of all other pool days
     100 % = perfectly in line · <100 % = lower than others · >100 % = higher than others

   Corrections applied in order:
     +LOO  — individual outlier down-weighting via leave-one-out cross-validation
     ↓td   — trend damping when recent days are systematically higher than older ones
     +TC   — today's remaining conditions regime signal (20 % blend, fires when >3 % diff)
     ↓yp   — yesterday penalty when most recent completed day was a moderate shortfall
   ================================================================= #}
{% set raw_json = state_attr('sensor.pv_hist_remaining_today', 'json') %}
{% if raw_json and raw_json != '[]' and raw_json is not none %}
  {% set data = raw_json | from_json %}

  {% if data | length > 0 %}
    {% set f_avg    = data[0].f_avg_tomorrow    | float(default=50.0) %}
    {% set f_uv_avg = data[0].uv_avg_tomorrow   | float(default=0.0) %}

    {# 0. TOMORROW DATE (display only) #}
    {% set tomorrow_label = (now() + timedelta(days=1)).strftime('%A, %Y-%m-%d') %}

    {# 1. SEASONAL SNOW DETECTION (Dec / Jan / Feb)                              #}
    {# Use the most recent entry in the dataset as the snow reference.            #}
    {# If that day's yield per cloud-free potential was < 2 % → snow suspected.  #}
    {% set current_month = now().month %}
    {% set snow_factor = 1.0 %}
    {% if current_month in [12, 1, 2] %}
      {% set ref_yield  = data[0].yield_day_total  | float(default=0) %}
      {% set ref_clouds = data[0].h_avg_total       | float(default=0) %}
      {% set ref_perf   = ref_yield / ([105 - ref_clouds, 5] | max) %}
      {% if ref_perf < 0.02 %}{% set snow_factor = 0.1 %}{% endif %}
    {% endif %}

    {# 2. ASTRONOMICAL BASE DATA FOR TOMORROW #}
    {% set latitude = state_attr('zone.home', 'latitude') | float(48.0) %}
    {% set doy      = (now() + timedelta(days=1)).strftime('%j') | int(default=1) %}
    {% set lat_rad  = latitude * pi / 180 %}
    {% set decl     = -0.4093 * cos(2 * pi * (doy + 10) / 365) %}
    {% set cos_ha   = -tan(lat_rad) * tan(decl) %}
    {% set dl_ref   = 24 / pi * acos([[cos_ha, -1.0] | max, 1.0] | min) %}
    {% set sun_ref  = 0.80 + 0.20 * cos((doy - 172) * 2 * pi / 365) %}

    {# 3. POOL BUILD                                                              #}
    {# Full-day comparison: h_avg_total / uv_avg_total / yield_day_total.        #}
    {# y_korr = yield_day_total × seasonal correction factor (s_korr).           #}
    {# Filtered = yield near-zero (sensor/data error) → excluded from forecast.  #}
    {% set ns_pool = namespace(items=[], total_w=0) %}
    {% for item in data %}
      {% set yield_day = item.yield_day_total | float(default=0) %}
      {% set clouds    = item.h_avg_total     | float(default=0) %}
      {% set uv        = item.uv_avg_total    | float(default=0) %}
      {% set item_dt   = as_datetime(item.date) %}
      {% if item_dt is not none %}
        {% set item_day = item_dt.strftime('%j') | int(default=1) %}
        {% set decl_i   = -0.4093 * cos(2 * pi * (item_day + 10) / 365) %}
        {% set cos_ha_i = -tan(lat_rad) * tan(decl_i) %}
        {% set dl_item  = 24 / pi * acos([[cos_ha_i, -1.0] | max, 1.0] | min) %}
        {% set sun_item = 0.80 + 0.20 * cos((item_day - 172) * 2 * pi / 365) %}
        {% set s_korr   = (sun_ref / sun_item) * (dl_ref / dl_item) %}
        {% set diff_c   = (clouds - f_avg) | abs %}
        {% if f_uv_avg > 0 %}
          {% set uv_w = [0.3 + 0.4 * (f_avg / 100.0), 0.7] | min %}
          {% set diff = diff_c * (1.0 - uv_w) + (uv - f_uv_avg) | abs * 8.0 * uv_w %}
        {% else %}
          {% set diff = diff_c %}
        {% endif %}
        {% set days_ago = ((now().timestamp() - item_dt.timestamp()) / 86400) | int(0) %}
        {% set w = (1 / ([diff, 0.5] | max)) * (1.0 + 0.3 * ([1.0 - days_ago / 30.0, 0.0] | max)) %}
        {% if yield_day > 0.05 %}
          {% set ns_pool.total_w = ns_pool.total_w + w %}
          {% set ns_pool.items = ns_pool.items + [{'date': item.date, 'h_avg': clouds, 'uv_avg': uv, 'y_korr': yield_day * s_korr, 'y_day_korr': yield_day * s_korr, 's_fakt': s_korr, 'w': w, 'yield_day_total': yield_day, 'days_ago': days_ago, 'filtered': false}] %}
        {% else %}
          {% set ns_pool.items = ns_pool.items + [{'date': item.date, 'h_avg': clouds, 'uv_avg': uv, 'y_korr': yield_day * s_korr, 'y_day_korr': yield_day * s_korr, 's_fakt': s_korr, 'w': 0, 'yield_day_total': yield_day, 'days_ago': days_ago, 'filtered': true}] %}
        {% endif %}
      {% endif %}
    {% endfor %}

    {% set top15 = (ns_pool.items | sort(attribute='w', reverse=True))[:15] %}
    {% set ns_top = namespace(total_w=0) %}
    {% for item in top15 %}{% if not item.filtered %}{% set ns_top.total_w = ns_top.total_w + item.w %}{% endif %}{% endfor %}
    {% set pool     = top15 | selectattr('filtered', 'equalto', false) | list %}
    {% set brighter = pool  | selectattr('h_avg', 'le', f_avg) | list %}
    {% set darker   = pool  | selectattr('h_avg', 'ge', f_avg) | list %}
    {% set res    = 0 %}
    {% set method = "No data" %}

    {# 4. DECISION LOGIC #}
    {% if brighter | count > 0 and darker | count == 0 %}
      {% set method    = "Light reduction" %}
      {% set worst_day = brighter | sort(attribute='y_korr') | first %}
      {% set res = worst_day.y_korr * ([120 - f_avg, 5.0] | max / [120 - worst_day.h_avg, 5.0] | max) %}
    {% elif darker | count > 0 and pool | selectattr('h_avg', 'le', f_avg) | list | count == 0 %}
      {% set method = "Max assumption" %}
      {% set res    = darker | map(attribute='y_korr') | max %}
    {% elif pool | count > 0 %}
      {% set method  = "Weighted average" %}
      {% set ns_mix  = namespace(ws=0) %}
      {% for item in pool %}
        {% set ns_mix.ws = ns_mix.ws + (item.y_korr * item.w) %}
      {% endfor %}
      {% set res = ns_mix.ws / (ns_top.total_w if ns_top.total_w > 0 else 1) %}
    {% endif %}

    {# 5. CROSS-VALIDATION (LOO)                                                  #}
    {# Tomorrow = full-day only → single accuracy metric (acc).                   #}
    {# For each pool day D: weighted forecast from all OTHER pool days → compare  #}
    {# D's actual y_korr to that prediction. 100 % = perfect consensus match.    #}
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
        {% set pred = ns_loo.wy / ns_loo.w %}
        {% set acc  = ((item_i.y_korr / ([pred, 0.001] | max)) * 100) | round(0) | int %}
      {% else %}
        {% set acc = 0 %}
      {% endif %}
      {% set ns_cv.items = ns_cv.items + [{'date': item_i.date, 'acc': acc}] %}
    {% endfor %}

    {# 6. LOO WEIGHT CORRECTION                                                   #}
    {# acc_factor = 1 / (1 + |acc − 100| / 100)                                  #}
    {# Perfect fit (acc = 100 %) → factor 1.0. 50 %-outlier → factor 0.67.      #}
    {% if pool | count > 1 %}
      {% set ns_corr = namespace(w=0, wy=0) %}
      {% for item_i in pool %}
        {% set cv = ns_cv.items | selectattr('date', 'equalto', item_i.date) | list %}
        {% if cv | length > 0 %}
          {% set dev        = ((cv[0].acc - 100) | abs) / 100.0 %}
          {% set acc_factor = 1.0 / (1.0 + dev) %}
          {% set ns_corr.w  = ns_corr.w  + item_i.w * acc_factor %}
          {% set ns_corr.wy = ns_corr.wy + item_i.y_korr * item_i.w * acc_factor %}
        {% else %}
          {% set ns_corr.w  = ns_corr.w  + item_i.w %}
          {% set ns_corr.wy = ns_corr.wy + item_i.y_korr * item_i.w %}
        {% endif %}
      {% endfor %}
      {% if ns_corr.w > 0 and ns_corr.wy > 0 %}
        {% set res    = ns_corr.wy / ns_corr.w %}
        {% set method = method ~ ' +LOO' %}
      {% endif %}
    {% endif %}

    {# 7. TREND DAMPING                                                           #}
    {# If recent pool days (≤14d) average >15 % above older ones, dampen 50 %.  #}
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
          {% set res    = res / (1.0 + 0.5 * (trend_ratio - 1.0)) %}
          {% set method = method ~ ' ↓td' %}
        {% endif %}
      {% endif %}
    {% endif %}

    {# 9. TODAY'S CONDITIONS ADJUSTMENT (TC)                                      #}
    {# Build a secondary pool using today's REMAINING cloud window:              #}
    {# match each historical day's h_avg_remaining (same time-of-day slice) vs  #}
    {# today's f_avg_today_remaining forecast.  Then take those days' full-day  #}
    {# yield, seasonally corrected to TOMORROW's DOY (same sun_ref/dl_ref).     #}
    {# This captures the "current weather regime" signal: if afternoons like    #}
    {# today's typically lead to better/worse full days, blend that into res.   #}
    {# Applied at α = 0.20 only when the TC estimate differs by > 3 %.         #}
    {% set tc_f_avg = data[0].f_avg_today_remaining | float(default=f_avg) %}
    {% set tc_uv    = data[0].uv_avg_today_remaining | float(default=0.0) %}
    {% set ns_tc = namespace(items=[]) %}
    {% for item in data %}
      {% set yield_day = item.yield_day_total | float(default=0) %}
      {% set item_dt   = as_datetime(item.date) %}
      {% if item_dt is not none and yield_day > 0.05 %}
        {% set item_day    = item_dt.strftime('%j') | int(default=1) %}
        {% set decl_tc     = -0.4093 * cos(2 * pi * (item_day + 10) / 365) %}
        {% set cos_ha_tc   = -tan(lat_rad) * tan(decl_tc) %}
        {% set dl_tc       = 24 / pi * acos([[cos_ha_tc, -1.0] | max, 1.0] | min) %}
        {% set sun_tc      = 0.80 + 0.20 * cos((item_day - 172) * 2 * pi / 365) %}
        {% set s_korr_tc   = (sun_ref / sun_tc) * (dl_ref / dl_tc) %}
        {% set clouds_rem  = item.h_avg_remaining | float(default=item.h_avg_total | float(default=0)) %}
        {% set uv_rem      = item.uv_avg_remaining | float(default=0.0) %}
        {% set days_ago_tc = ((now().timestamp() - item_dt.timestamp()) / 86400) | int(0) %}
        {% set diff_tc_c   = (clouds_rem - tc_f_avg) | abs %}
        {% if tc_uv > 0 %}
          {% set uv_w_tc = [0.3 + 0.4 * (tc_f_avg / 100.0), 0.7] | min %}
          {% set diff_tc = diff_tc_c * (1.0 - uv_w_tc) + (uv_rem - tc_uv) | abs * 8.0 * uv_w_tc %}
        {% else %}
          {% set diff_tc = diff_tc_c %}
        {% endif %}
        {% set w_tc = (1 / ([diff_tc, 0.5] | max)) * (1.0 + 0.3 * ([1.0 - days_ago_tc / 30.0, 0.0] | max)) %}
        {% set ns_tc.items = ns_tc.items + [{'w': w_tc, 'y_korr': yield_day * s_korr_tc}] %}
      {% endif %}
    {% endfor %}
    {% set tc_top15 = (ns_tc.items | sort(attribute='w', reverse=True))[:15] %}
    {% set ns_tc_sum = namespace(w=0.0, wy=0.0) %}
    {% for entry in tc_top15 %}
      {% set ns_tc_sum.w  = ns_tc_sum.w  + entry.w %}
      {% set ns_tc_sum.wy = ns_tc_sum.wy + entry.y_korr * entry.w %}
    {% endfor %}
    {% set res_tc = ns_tc_sum.wy / ([ns_tc_sum.w, 0.001] | max) %}
    {% set tc_delta_pct = ((res_tc - res) / ([res, 0.001] | max) * 100) | round(1) %}
    {% if (tc_delta_pct | abs) > 3.0 %}
      {% set res    = res * 0.80 + res_tc * 0.20 %}
      {% set method = method ~ ' +TC(' ~ ('+' if tc_delta_pct > 0 else '') ~ tc_delta_pct ~ '%)' %}
    {% endif %}

    {# BACK-TEST: consecutive-day carry-through over ALL data entries            #}
    {# Trigger range [40 %, 85 %) of mean: moderate shortfalls that tend to     #}
    {# persist. < 40 % = extreme low → usually rebounds (❌ noise).             #}
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
          {% set item_j            = next_items[0] %}
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

    {# 8. YESTERDAY PENALTY                                                        #}
    {# "Yesterday" for tomorrow's forecast = the most recent completed day        #}
    {# present in ns_cv. If its acc is in [40 %, 85 %), moderate shortfall        #}
    {# likely persists into tomorrow. Strength = data-driven effective_carry.     #}
    {% if ns_bt.total > 0 and ns_bt.trigger_sum > 0 %}
      {% set carry_through   = ns_bt.carry_sum / ns_bt.trigger_sum %}
      {% set hit_rate_f      = ns_bt.useful / ns_bt.total %}
      {% set effective_carry = carry_through * hit_rate_f %}
    {% else %}
      {% set effective_carry = 0.3 %}
    {% endif %}
    {% set yesterday_date = (now() - timedelta(days=1)).strftime('%Y-%m-%d') %}
    {% set yest_cv = ns_cv.items | selectattr('date', 'equalto', yesterday_date) | list %}
    {# Penalty only when both yesterday AND tomorrow are ≥60 % cloudy:           #}
    {# shortfalls on clearer days are noise, not a persistent weather pattern.   #}
    {% set yest_pool_item = pool | selectattr('date', 'equalto', yesterday_date) | list %}
    {% set yest_clouds = yest_pool_item[0].h_avg if yest_pool_item | length > 0 else 0 %}
    {% if yest_cv | length > 0 %}
      {% set yest_acc = yest_cv[0].acc %}
      {% if yest_acc >= 40 and yest_acc < 85 and f_avg >= 60 and yest_clouds >= 60 %}
        {% set shortfall = (1.0 - yest_acc / 100.0) %}
        {% set penalty   = 1.0 - effective_carry * shortfall %}
        {% set res       = res * ([penalty, 0.5] | max) %}
        {% set method    = method ~ ' ↓yp(' ~ yest_acc ~ '%/' ~ (effective_carry * 100) | round(0) | int ~ '%)' %}
      {% endif %}
    {% endif %}

    {% set scale     = 1000 if res > 200 else 1 %}
    {% set final_val = (res / scale) * snow_factor %}

**Tomorrow's Forecast — {{ tomorrow_label }}:**
## {{ final_val | round(2) }} kWh
*Basis: **{{ f_avg }}%** clouds, **{{ f_uv_avg }}** uv | **{{ method }}***
*Today rem.: **{{ tc_f_avg }}%** clouds, **{{ tc_uv }}** uv → regime signal: **{{ ((res_tc / scale) * snow_factor) | round(2) }} kWh** ({{ ('+' if tc_delta_pct > 0 else '') ~ tc_delta_pct }}%)*
{% if snow_factor < 1.0 %}⚠️ **Snow suspected! ({{ (snow_factor * 100) | round(0) }}%)**{% endif %}

| Date | Clouds | Yield | UV | Yield scaled | Weight | Acc. |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
{%- for item in top15 %}
{%- set cv = ns_cv.items | selectattr('date', 'equalto', item.date) | list %}
| {{ item.date }} | {{ item.h_avg }}% | {{ item.yield_day_total | round(2) }} | {{ item.uv_avg | round(1) }} | **{{ ((item.y_korr * snow_factor) / scale) | round(2) }} <small><small>({{ item.s_fakt | round(2) }}x)</small></small>**{% if item.filtered %}❌{% endif %} | {{ (((item.w / ns_top.total_w) * 100) if ns_top.total_w > 0 else 0) | round(1) }}% | {{ cv[0].acc ~ '%' if cv | length > 0 else '—' }} |
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