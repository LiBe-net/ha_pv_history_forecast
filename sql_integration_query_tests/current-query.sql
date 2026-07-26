-- Current production query from custom_components/pv_history_forecast/const.py
-- Direct-test configuration copied from v2-better-query-01.sql.
-- History window: 90 days (DEFAULT_PV_HISTORY_DAYS).

WITH vars AS (
    SELECT
        'sensor.pv_hist_cloud_coverage' as sensor_clouds,
        'sensor.pv_hist_weather_forecast' as sensor_forecast,
        'sensor.pv_hist_uv' as sensor_uv,
        'sensor.pv_hist_temperature' as sensor_temp,
        'sensor.pv_hist_precipitation' as sensor_precip,
        'weather.home' as weather_entity,
        (strftime('%s', 'now', 'localtime') - strftime('%s', 'now')) || ' seconds' as offset
),

ids AS (
    SELECT
        (SELECT id FROM statistics_meta WHERE statistic_id = (SELECT sensor_clouds FROM vars)) as cloud_id_statistics,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_clouds FROM vars)) as cloud_id_states,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_forecast FROM vars) LIMIT 1) as forecast_id,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT weather_entity FROM vars)) as w_entity_id,
        (SELECT metadata_id FROM states_meta WHERE entity_id = 'sun.sun') as sun_id,
        (SELECT id FROM statistics_meta WHERE statistic_id = (SELECT sensor_uv FROM vars)) as uv_id_statistics,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_uv FROM vars)) as uv_id_states,
        (SELECT id FROM statistics_meta WHERE statistic_id = (SELECT sensor_temp FROM vars)) as temp_id_statistics,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_temp FROM vars)) as temp_id_states,
        (SELECT id FROM statistics_meta WHERE statistic_id = (SELECT sensor_precip FROM vars)) as precip_id_statistics,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_precip FROM vars)) as precip_id_states
),

/* Gets all configured PV sensors including their IDs from states_meta for real-time RAM queries */
pv_stat_ids AS (
    SELECT id,
           (SELECT metadata_id FROM states_meta WHERE entity_id = statistic_id) as states_metadata_id,
           CASE WHEN unit_of_measurement = 'Wh' THEN 1000.0 ELSE 1.0 END as divisor
    FROM statistics_meta
    WHERE statistic_id IN ('sensor.pv_panels_energy')
),

pv_activity AS (
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

latest_forecast_ts AS (
    SELECT MAX(s.last_updated_ts) as ts
    FROM states s
    JOIN state_attributes a ON s.attributes_id = a.attributes_id
    WHERE s.metadata_id = (SELECT forecast_id FROM ids)
      AND json_extract(a.shared_attrs, '$.forecast') IS NOT NULL
      AND json_extract(a.shared_attrs, '$.forecast') != '[]'
      AND s.last_updated_ts > strftime('%s', 'now', '-6 hours')
),
pv_live_current_hour_delta AS (
    SELECT
        COALESCE(SUM(
            /* (current live state) - (counter value before the start of the current UTC hour) */
            (CAST(s_now.state AS FLOAT) - CAST(s_hour.state AS FLOAT)) / pvi.divisor
        ), 0.0) as live_hour_delta
    FROM pv_stat_ids pvi
    /* 1. Holen des aktuellen Live-Zustands im RAM (Letzter State in der Tabelle) */
    JOIN states s_now ON s_now.metadata_id = pvi.states_metadata_id
      AND s_now.state_id = (
          SELECT MAX(state_id) FROM states
          WHERE metadata_id = pvi.states_metadata_id
            AND state NOT IN ('unknown', 'unavailable', '')
      )
    /* 2. Get the real counter value from states from exactly before the start of the current hour */
    JOIN states s_hour ON s_hour.metadata_id = pvi.states_metadata_id
      AND s_hour.state_id = (
          SELECT state_id FROM states
          WHERE metadata_id = pvi.states_metadata_id
            /* MATHEMATISCHER STUNDENSCHNITT: Rundet die aktuelle Zeit auf die vollendete Stunde ab */
            AND last_updated_ts <= (strftime('%s', 'now') / 3600) * 3600
            AND state NOT IN ('unknown', 'unavailable', '')
          ORDER BY last_updated_ts DESC LIMIT 1
      )
),

weather_history_raw AS (
    SELECT (CAST(start_ts AS INT) / 3600) * 3600 as ts, CAST(COALESCE(mean, state) AS FLOAT) as cloud_val, NULL as uv_val, NULL as temp_val, NULL as precip_val
    FROM statistics
    WHERE metadata_id = (SELECT cloud_id_statistics FROM ids) AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) > date('now', (SELECT offset FROM vars), '-90 days')

    UNION ALL
    SELECT (CAST(s.last_updated_ts AS INT) / 3600) * 3600 as ts,
      CASE WHEN (SELECT sensor_clouds FROM vars) LIKE 'weather.%' THEN CAST(json_extract(a.shared_attrs, '$.cloud_coverage') AS FLOAT) ELSE CAST(s.state AS FLOAT) END as cloud_val,
      CASE WHEN (SELECT sensor_clouds FROM vars) LIKE 'weather.%' THEN CAST(json_extract(a.shared_attrs, '$.uv_index') AS FLOAT) ELSE NULL END as uv_val,
      NULL as temp_val, NULL as precip_val
    FROM states s
    LEFT JOIN state_attributes a ON s.attributes_id = a.attributes_id
    WHERE s.metadata_id = (SELECT cloud_id_states FROM ids)
      AND ((SELECT sensor_clouds FROM vars) LIKE 'weather.%' OR NOT EXISTS (SELECT 1 FROM statistics WHERE metadata_id = (SELECT cloud_id_statistics FROM ids) AND (CAST(start_ts AS INT) / 3600) = (CAST(s.last_updated_ts AS INT) / 3600)))
      AND s.last_updated_ts > strftime('%s', 'now', '-10 days')
      AND s.state NOT IN ('unknown', 'unavailable', '')

    UNION ALL
    SELECT (CAST(s.last_updated_ts AS INT) / 3600) * 3600 as ts,
        CAST(json_extract(a.shared_attrs, '$.cloud_coverage') AS FLOAT) as cloud_val, CAST(json_extract(a.shared_attrs, '$.uv_index') AS FLOAT) as uv_val,
        NULL as temp_val, CAST(json_extract(a.shared_attrs, '$.precipitation') AS FLOAT) as precip_val
    FROM states s
    LEFT JOIN state_attributes a ON s.attributes_id = a.attributes_id
    WHERE s.metadata_id = (SELECT w_entity_id FROM ids)
      AND ((SELECT sensor_clouds FROM vars) LIKE 'weather.%' OR NOT EXISTS (SELECT 1 FROM statistics WHERE metadata_id = (SELECT cloud_id_statistics FROM ids) AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) = date(s.last_updated_ts, 'unixepoch', (SELECT offset FROM vars))))
      AND s.last_updated_ts > strftime('%s', 'now', '-10 days')
      AND json_extract(a.shared_attrs, '$.cloud_coverage') IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM statistics WHERE metadata_id = (SELECT cloud_id_statistics FROM ids)  AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) = date(s.last_updated_ts, 'unixepoch', (SELECT offset FROM vars)))

    UNION ALL
    SELECT (CAST(start_ts AS INT) / 3600) * 3600 as ts, NULL as cloud_val, CAST(COALESCE(mean, state) AS FLOAT) as uv_val, NULL as temp_val, NULL as precip_val
    FROM statistics
    WHERE metadata_id = (SELECT uv_id_statistics FROM ids) AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) > date('now', (SELECT offset FROM vars), '-90 days')

    UNION ALL
    SELECT (CAST(s.last_updated_ts AS INT) / 3600) * 3600 as ts, NULL as cloud_val, CAST(s.state AS FLOAT) as uv_val, NULL as temp_val, NULL as precip_val
    FROM states s
    WHERE s.metadata_id = (SELECT uv_id_states FROM ids)
      AND NOT EXISTS (SELECT 1 FROM statistics WHERE metadata_id = (SELECT uv_id_statistics FROM ids)  AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) = date(s.last_updated_ts, 'unixepoch', (SELECT offset FROM vars)))
      AND s.last_updated_ts > strftime('%s', 'now', '-10 days')
      AND s.state NOT IN ('unknown', 'unavailable', '')

    UNION ALL
    SELECT (CAST(start_ts AS INT) / 3600) * 3600 as ts, NULL as cloud_val, NULL as uv_val, CAST(COALESCE(mean, state) AS FLOAT) as temp_val, NULL as precip_val
    FROM statistics
    WHERE metadata_id = (SELECT temp_id_statistics FROM ids) AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) > date('now', (SELECT offset FROM vars), '-90 days')

    UNION ALL
    SELECT (CAST(s.last_updated_ts AS INT) / 3600) * 3600 as ts, NULL as cloud_val, NULL as uv_val, CAST(s.state AS FLOAT) as temp_val, NULL as precip_val
    FROM states s
    WHERE s.metadata_id = (SELECT temp_id_states FROM ids)
      AND NOT EXISTS (SELECT 1 FROM statistics WHERE metadata_id = (SELECT temp_id_statistics FROM ids)  AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) = date(s.last_updated_ts, 'unixepoch', (SELECT offset FROM vars)))
      AND s.last_updated_ts > strftime('%s', 'now', '-10 days')
      AND s.state NOT IN ('unknown', 'unavailable', '')

    UNION ALL
    SELECT (CAST(start_ts AS INT) / 3600) * 3600 as ts, NULL as cloud_val, NULL as uv_val, NULL as temp_val, CAST(COALESCE(mean, state) AS FLOAT) as precip_val
    FROM statistics
    WHERE metadata_id = (SELECT precip_id_statistics FROM ids) AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) > date('now', (SELECT offset FROM vars), '-90 days')

    UNION ALL
    SELECT (CAST(s.last_updated_ts AS INT) / 3600) * 3600 as ts, NULL as cloud_val, NULL as uv_val, NULL as temp_val, CAST(s.state AS FLOAT) as precip_val
    FROM states s
    WHERE s.metadata_id = (SELECT precip_id_states FROM ids)
      AND NOT EXISTS (SELECT 1 FROM statistics WHERE metadata_id = (SELECT precip_id_statistics FROM ids) AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) = date(s.last_updated_ts, 'unixepoch', (SELECT offset FROM vars)))
      AND s.last_updated_ts > strftime('%s', 'now', '-10 days')
      AND s.state NOT IN ('unknown', 'unavailable', '')
),

pv_hourly_states AS (
    /* Fallback, falls noch keine Statistik vorhanden ist */
    SELECT
        date(s.last_updated_ts, 'unixepoch', (SELECT offset FROM vars)) as day_string,
        pvi.id as metadata_id,
        pvi.divisor,
        strftime('%H:00', s.last_updated_ts, 'unixepoch', (SELECT offset FROM vars)) as hour_string,
        MAX(CAST(s.state AS FLOAT)) as max_state_hourly
    FROM states s
    JOIN pv_stat_ids pvi ON s.metadata_id = pvi.states_metadata_id
    WHERE s.last_updated_ts > strftime('%s', 'now', '-10 days')
      AND s.state NOT IN ('unknown', 'unavailable', '')
    GROUP BY 1, 2, 4
),

pv_history_per_sensor AS (
    SELECT
        date(start_ts, 'unixepoch', (SELECT offset FROM vars)) as day_string,
        metadata_id,
        (MAX(CAST(state AS FLOAT)) - MIN(CAST(state AS FLOAT))) / (SELECT divisor FROM pv_stat_ids WHERE id = metadata_id) as single_yield_total,
        (
            MAX(CAST(state AS FLOAT))
            -
            COALESCE(

                MAX(CASE
                    WHEN CAST(strftime('%H', start_ts, 'unixepoch', (SELECT offset FROM vars)) AS INT)
                         < CAST(strftime('%H', 'now', (SELECT offset FROM vars)) AS INT)
                    THEN CAST(state AS FLOAT)
                END),
                MIN(CAST(state AS FLOAT))
            )
        ) / (SELECT divisor FROM pv_stat_ids WHERE id = metadata_id) as single_yield_remaining
    FROM statistics
    WHERE metadata_id IN (SELECT id FROM pv_stat_ids)
      AND date(start_ts, 'unixepoch', (SELECT offset FROM vars)) > date('now', (SELECT offset FROM vars), '-90 days')
    GROUP BY 1, 2

    UNION ALL

    SELECT
        day_string,
        metadata_id,
        (MAX(max_state_hourly) - MIN(max_state_hourly)) / divisor as single_yield_total,

        (
            MAX(max_state_hourly)
            -
            COALESCE(
                MAX(CASE
                    WHEN CAST(SUBSTR(hour_string, 1, 2) AS INT)
                         < CAST(strftime('%H', 'now', (SELECT offset FROM vars)) AS INT)
                    THEN max_state_hourly
                END),
                MIN(max_state_hourly)
            )
        ) / divisor as single_yield_remaining
    FROM pv_hourly_states f
    WHERE NOT EXISTS (
          SELECT 1 FROM statistics st
          WHERE st.metadata_id = f.metadata_id
            AND date(st.start_ts, 'unixepoch', (SELECT offset FROM vars)) = f.day_string
      )
    GROUP BY 1, 2, divisor
),

pv_daily_totals AS (
    SELECT
        day_string,
        ROUND(SUM(COALESCE(single_yield_total, 0.0)), 1) as pv_yield_total,
        ROUND(SUM(COALESCE(single_yield_remaining, 0.0)), 1) as pv_yield_remaining
    FROM pv_history_per_sensor
    GROUP BY 1
),
forecast AS (
    SELECT
        -- Today's remaining values (now uses local sun times!)
        ROUND(COALESCE(AVG(CASE
            WHEN substr(json_extract(f.value, '$.datetime'), 1, 10) = date('now', (SELECT offset FROM vars))
             AND substr(json_extract(f.value, '$.datetime'), 12, 5) >= MAX(strftime('%H:00', 'now', (SELECT offset FROM vars)), (SELECT sun_start_local FROM pv_activity))
             AND substr(json_extract(f.value, '$.datetime'), 12, 5) <= (SELECT sun_end_local FROM pv_activity)
            THEN CAST(json_extract(f.value, '$.cloud_coverage') AS FLOAT) END), 0.0), 1) as cloud_remaining,

        ROUND(COALESCE(AVG(CASE
            WHEN substr(json_extract(f.value, '$.datetime'), 1, 10) = date('now', (SELECT offset FROM vars))
             AND substr(json_extract(f.value, '$.datetime'), 12, 5) >= MAX(strftime('%H:00', 'now', (SELECT offset FROM vars)), (SELECT sun_start_local FROM pv_activity))
             AND substr(json_extract(f.value, '$.datetime'), 12, 5) <= (SELECT sun_end_local FROM pv_activity)
            THEN CAST(json_extract(f.value, '$.uv_index') AS FLOAT) END), 0.0), 1) as uv_remaining,

        ROUND(COALESCE(AVG(CASE
            WHEN substr(json_extract(f.value, '$.datetime'), 1, 10) = date('now', (SELECT offset FROM vars))
             AND substr(json_extract(f.value, '$.datetime'), 12, 5) >= MAX(strftime('%H:00', 'now', (SELECT offset FROM vars)), (SELECT sun_start_local FROM pv_activity))
             AND substr(json_extract(f.value, '$.datetime'), 12, 5) <= (SELECT sun_end_local FROM pv_activity)
            THEN CAST(json_extract(f.value, '$.temperature') AS FLOAT) END), 0.0), 1) as temp_remaining,

        ROUND(COALESCE(AVG(CASE
            WHEN substr(json_extract(f.value, '$.datetime'), 1, 10) = date('now', (SELECT offset FROM vars))
             AND substr(json_extract(f.value, '$.datetime'), 12, 5) >= MAX(strftime('%H:00', 'now', (SELECT offset FROM vars)), (SELECT sun_start_local FROM pv_activity))
             AND substr(json_extract(f.value, '$.datetime'), 12, 5) <= (SELECT sun_end_local FROM pv_activity)
            THEN CASE WHEN CAST(json_extract(f.value, '$.precipitation') AS FLOAT) <= 0.05 THEN 0.0 WHEN CAST(json_extract(f.value, '$.precipitation') AS FLOAT) >= 1.05 THEN 100.0 ELSE (CAST(json_extract(f.value, '$.precipitation') AS FLOAT) - 0.05) * 100.0 END END), 0.0), 1) as precip_remaining,

        -- Values for the following day (now also uses local sun times!)
        ROUND(COALESCE(AVG(CASE
            WHEN substr(json_extract(f.value, '$.datetime'), 1, 10) = date('now', (SELECT offset FROM vars), '+1 day')
             AND substr(json_extract(f.value, '$.datetime'), 12, 5) >= (SELECT sun_start_local FROM pv_activity)
             AND substr(json_extract(f.value, '$.datetime'), 12, 5) <= (SELECT sun_end_local FROM pv_activity)
            THEN CAST(json_extract(f.value, '$.cloud_coverage') AS FLOAT) END), 0.0), 1) as next_cloud_total,

        ROUND(COALESCE(AVG(CASE
            WHEN substr(json_extract(f.value, '$.datetime'), 1, 10) = date('now', (SELECT offset FROM vars), '+1 day')
             AND substr(json_extract(f.value, '$.datetime'), 12, 5) >= (SELECT sun_start_local FROM pv_activity)
             AND substr(json_extract(f.value, '$.datetime'), 12, 5) <= (SELECT sun_end_local FROM pv_activity)
            THEN CAST(json_extract(f.value, '$.uv_index') AS FLOAT) END), 0.0), 1) as next_uv_total,

        ROUND(COALESCE(AVG(CASE
            WHEN substr(json_extract(f.value, '$.datetime'), 1, 10) = date('now', (SELECT offset FROM vars), '+1 day')
             AND substr(json_extract(f.value, '$.datetime'), 12, 5) >= (SELECT sun_start_local FROM pv_activity)
             AND substr(json_extract(f.value, '$.datetime'), 12, 5) <= (SELECT sun_end_local FROM pv_activity)
            THEN CAST(json_extract(f.value, '$.temperature') AS FLOAT) END), 0.0), 1) as next_temp_total,

        ROUND(COALESCE(AVG(CASE
            WHEN substr(json_extract(f.value, '$.datetime'), 1, 10) = date('now', (SELECT offset FROM vars), '+1 day')
             AND substr(json_extract(f.value, '$.datetime'), 12, 5) >= (SELECT sun_start_local FROM pv_activity)
             AND substr(json_extract(f.value, '$.datetime'), 12, 5) <= (SELECT sun_end_local FROM pv_activity)
            THEN CASE WHEN CAST(json_extract(f.value, '$.precipitation') AS FLOAT) <= 0.05 THEN 0.0 WHEN CAST(json_extract(f.value, '$.precipitation') AS FLOAT) >= 1.05 THEN 100.0 ELSE (CAST(json_extract(f.value, '$.precipitation') AS FLOAT) - 0.05) * 100.0 END END), 0.0), 1) as next_precip_total
    FROM states s
    JOIN state_attributes a ON s.attributes_id = a.attributes_id
    CROSS JOIN json_each(a.shared_attrs, '$.forecast') f
    WHERE s.metadata_id = (SELECT forecast_id FROM ids) AND s.last_updated_ts = (SELECT ts FROM latest_forecast_ts)
),

weather_history_hourly AS (
    SELECT
        ts,
        date(ts, 'unixepoch', (SELECT offset FROM vars)) as day_string,
        strftime('%H:%M', ts, 'unixepoch') as hour_string,
        MAX(cloud_val) as cloud_val,
        MAX(uv_val) as uv_val,
        MAX(temp_val) as temp_val,
        COALESCE(MAX(precip_val), 0.0) as precip_val
    FROM weather_history_raw
    GROUP BY ts
),

daily_metrics AS (
    SELECT
        h.day_string,
        ROUND(COALESCE(pvt.pv_yield_total, 0.0), 1) as pv_yield_total,
        ROUND(COALESCE(pvt.pv_yield_remaining, 0.0), 1) as pv_yield_remaining,

        -- REPARATUR: Wenn temp_remaining NULL ist, nimm den Tagesschnitt (temp_total). Ist dieser auch NULL, nimm 15.0
        ROUND(COALESCE(
            AVG(CASE
                WHEN h.hour_string >= MAX(strftime('%H:00', 'now', (SELECT offset FROM vars)), (SELECT sun_start_local FROM pv_activity))
                 AND h.hour_string <= (SELECT sun_end_local FROM pv_activity)
                THEN h.temp_val END),
            AVG(CASE
                WHEN h.hour_string >= (SELECT sun_start_local FROM pv_activity)
                 AND h.hour_string <= (SELECT sun_end_local FROM pv_activity)
                THEN h.temp_val END),
            15.0
        ), 1) as temp_remaining,

        -- Remaining values for the current day (dynamic from sunrise or current time)
        ROUND(COALESCE(AVG(CASE
            WHEN h.hour_string >= MAX(strftime('%H:00', 'now', (SELECT offset FROM vars)), (SELECT sun_start_local FROM pv_activity))
             AND h.hour_string <= (SELECT sun_end_local FROM pv_activity)
            THEN h.cloud_val END), 0.0), 1) as cloud_remaining,

        ROUND(COALESCE(AVG(CASE
            WHEN h.hour_string >= MAX(strftime('%H:00', 'now', (SELECT offset FROM vars)), (SELECT sun_start_local FROM pv_activity))
             AND h.hour_string <= (SELECT sun_end_local FROM pv_activity)
            THEN h.uv_val END), 0.0), 1) as uv_remaining,

        ROUND(COALESCE(AVG(CASE
            WHEN h.hour_string >= MAX(strftime('%H:00', 'now', (SELECT offset FROM vars)), (SELECT sun_start_local FROM pv_activity))
             AND h.hour_string <= (SELECT sun_end_local FROM pv_activity)
            THEN h.precip_val END), 0.0), 1) as precip_remaining,

        -- Full-day values (strictly between local sunrise and sunset)
        ROUND(COALESCE(AVG(CASE
            WHEN h.hour_string >= (SELECT sun_start_local FROM pv_activity)
             AND h.hour_string <= (SELECT sun_end_local FROM pv_activity)
            THEN h.cloud_val END), 0.0), 1) as cloud_total,

        ROUND(COALESCE(AVG(CASE
            WHEN h.hour_string >= (SELECT sun_start_local FROM pv_activity)
             AND h.hour_string <= (SELECT sun_end_local FROM pv_activity)
            THEN h.uv_val END), 0.0), 1) as uv_total,

        ROUND(COALESCE(AVG(CASE
            WHEN h.hour_string >= (SELECT sun_start_local FROM pv_activity)
             AND h.hour_string <= (SELECT sun_end_local FROM pv_activity)
            THEN h.temp_val END), 15.0), 1) as temp_total,

        ROUND(COALESCE(AVG(CASE
            WHEN h.hour_string >= (SELECT sun_start_local FROM pv_activity)
             AND h.hour_string <= (SELECT sun_end_local FROM pv_activity)
            THEN h.precip_val END), 0.0), 1) as precip_total

    FROM weather_history_hourly h
    LEFT JOIN pv_daily_totals pvt ON h.day_string = pvt.day_string
    WHERE h.day_string != date('now', (SELECT offset FROM vars))
    GROUP BY h.day_string
    HAVING pv_yield_total >= 0.0
       AND COUNT(h.ts) >= 1
       AND AVG(h.cloud_val) IS NOT NULL
),

json_output_assembly AS (
    SELECT json_group_array(
        json_object(
            'day', day_string,
            'remaining', json_object(
                'cloud', cloud_remaining,
                'uv', uv_remaining,
                'temp', temp_remaining,
                'precip', precip_remaining,
                'pv_yield', pv_yield_remaining
            ),
            'total', json_object(
                'cloud', cloud_total,
                'uv', uv_total,
                'temp', temp_total,
                'precip', precip_total,
                'pv_yield', pv_yield_total
            )
        )
    ) as metrics_array
    FROM daily_metrics
)
SELECT json_object(
    'pv_activity', (SELECT json_object('sun_start', sun_start, 'sun_end', sun_end, 'sun_start_local', sun_start_local, 'sun_end_local', sun_end_local) FROM pv_activity),
    'forecast', (SELECT json_object('remaining', json_object('cloud', cloud_remaining, 'uv', uv_remaining, 'temp', temp_remaining, 'precip', precip_remaining), 'next_day_total', json_object('cloud', next_cloud_total, 'uv', next_uv_total, 'temp', next_temp_total, 'precip', next_precip_total)) FROM forecast),
    'live_hour_delta', (SELECT live_hour_delta FROM pv_live_current_hour_delta),
    'daily_summary', (SELECT metrics_array FROM json_output_assembly)
) as value;
