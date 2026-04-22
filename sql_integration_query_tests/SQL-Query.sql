WITH vars AS (
    SELECT 
        'weather.forecast_home' as sensor_clouds,   -- Weather entity direct: cloud_coverage from state_attributes
        'weather.forecast_home' as sensor_uv,   -- Weather entity direct: uv_index from state_attributes
        'sensor.pv_panels_energy' as sensor_pv,
        'sensor.weather_forecast_hourly' as sensor_forecast,
        -- Calculates the offset between local time and UTC (e.g. '+3600 seconds')
        -- Used to trigger the date change (00:00) locally
        (strftime('%s', 'now', 'localtime') - strftime('%s', 'now')) || ' seconds' as offset
),

ids AS (
    /* Retrieves all required internal IDs for statistics and states from the HA database */
    SELECT 
        (SELECT id FROM statistics_meta WHERE statistic_id = (SELECT sensor_clouds FROM vars)) as w_id_stats,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_clouds FROM vars)) as w_id_states,
        (SELECT id FROM statistics_meta WHERE statistic_id = (SELECT sensor_uv FROM vars)) as uv_id_stats,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_uv FROM vars)) as uv_id_states,
        (SELECT id FROM statistics_meta WHERE statistic_id = (SELECT sensor_pv FROM vars) LIMIT 1) as p_id,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_pv FROM vars) LIMIT 1) as p_id_states,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_forecast FROM vars) LIMIT 1) as f_id,
        (SELECT metadata_id FROM states_meta WHERE entity_id = 'sun.sun') as sun_id
),

pv_activity AS (
    /* Sunrise = first 'above_horizon' entry yesterday (UTC epoch, directly correct)       */
    /* Sunset = first 'below_horizon' entry AFTER sunrise yesterday                         */
    /* sun_start/sun_end = UTC HH:MM  → used for BETWEEN with UTC forecast datetimes (+00:00) */
    /* sun_start_local/sun_end_local = local HH:MM → only for phase detection (before/after sunrise/sunset)  */
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
    /* Calculates the average cloud coverage + UV index for the remaining part of the current day */
    SELECT COALESCE(
        (SELECT AVG(CAST(json_extract(f.value, '$.cloud_coverage') AS FLOAT)) 
         FROM states s 
         JOIN state_attributes a ON s.attributes_id = a.attributes_id, 
         json_each(a.shared_attrs, '$.forecast') f 
         WHERE s.metadata_id = (SELECT f_id FROM ids) 
           AND s.last_updated_ts = (SELECT MAX(last_updated_ts) FROM states WHERE metadata_id = (SELECT f_id FROM ids)) 
           -- Match forecast date against local "today" (via UTC offset)
           AND substr(json_extract(f.value, '$.datetime'), 1, 10) = date('now', (SELECT offset FROM vars))
           AND substr(json_extract(f.value, '$.datetime'), 12, 5) 
               BETWEEN CASE
                         -- Forecast slots are UTC: compare against UTC sun_start/sun_end.
                         -- Only the window START shifts: during local day use current UTC time
                         -- (remaining today); before/after local daylight use full-day window
                         -- (midnight use-case: forecast for the whole coming day).
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
    /* Calculates the average cloud coverage + UV index for the entire next day */
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
        /* Combines long-term statistics and short-term states of cloud coverage for historical comparison */
        SELECT start_ts as ts,
                     CAST(COALESCE(mean, state) AS FLOAT) as val
        FROM statistics 
        WHERE metadata_id = (SELECT w_id_stats FROM ids) 
            AND start_ts > strftime('%s', 'now', '-60 days')
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
),

uv_history AS (
        /* Combines long-term statistics and short-term states of UV index for historical comparison */
        SELECT start_ts as ts,
                     CAST(COALESCE(mean, state) AS FLOAT) as uv_val
        FROM statistics 
        WHERE metadata_id = (SELECT uv_id_stats FROM ids) 
            AND start_ts > strftime('%s', 'now', '-60 days')
        UNION ALL
        SELECT s.last_updated_ts as ts, 
            CASE WHEN (SELECT sensor_uv FROM vars) LIKE 'weather.%' 
                     THEN CAST(json_extract(a.shared_attrs, '$.uv_index') AS FLOAT) 
                     ELSE CAST(s.state AS FLOAT) 
            END as uv_val
        FROM states s 
        LEFT JOIN state_attributes a ON s.attributes_id = a.attributes_id 
        WHERE s.metadata_id = (SELECT uv_id_states FROM ids) 
            AND ((SELECT sensor_uv FROM vars) LIKE 'weather.%' OR NOT EXISTS (SELECT 1 FROM statistics WHERE metadata_id = (SELECT uv_id_stats FROM ids)))
            AND s.last_updated_ts > strftime('%s', 'now', '-10 days') 
            AND s.state NOT IN ('unknown', 'unavailable', '')
),

matching_days AS (
    /* Finds past days whose cloud and UV profile most closely matches today's forecast */
    SELECT 
        date(c.ts, 'unixepoch') as day, 
        AVG(CASE WHEN strftime('%H:%M', c.ts, 'unixepoch') BETWEEN (SELECT sun_start FROM pv_activity) AND (SELECT sun_end FROM pv_activity) THEN c.val END) as h_avg_total_val,
        AVG(CASE WHEN strftime('%H:%M', c.ts, 'unixepoch') >= strftime('%H:00', 'now') AND strftime('%H:%M', c.ts, 'unixepoch') <= (SELECT sun_end FROM pv_activity) THEN c.val END) as h_avg_rest_val,
        AVG(CASE WHEN strftime('%H:%M', u.ts, 'unixepoch') BETWEEN (SELECT sun_start FROM pv_activity) AND (SELECT sun_end FROM pv_activity) THEN u.uv_val END) as uv_avg_total_val,
        AVG(CASE WHEN strftime('%H:%M', u.ts, 'unixepoch') >= strftime('%H:00', 'now') AND strftime('%H:%M', u.ts, 'unixepoch') <= (SELECT sun_end FROM pv_activity) THEN u.uv_val END) as uv_avg_rest_val
    FROM cloud_history c
    JOIN uv_history u ON date(c.ts, 'unixepoch') = date(u.ts, 'unixepoch')
    -- Filtert die Historie: Alles vor dem heutigen lokalen Tag (Offset-gesteuert)
    WHERE date(c.ts, 'unixepoch') < date('now', (SELECT offset FROM vars)) 
    GROUP BY 1 
    HAVING h_avg_total_val IS NOT NULL AND h_avg_total_val > 0
    ORDER BY ABS(
        COALESCE(h_avg_rest_val, h_avg_total_val) -- Fallback auf Gesamt-Schnitt wenn Rest null ist
        - (SELECT f_avg FROM forecast_val)
    ) ASC
),

final_data AS (
    /* Determines the actual PV yields of the best matching historical days */
    SELECT 
        md.*,
        (SELECT MAX(state) FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch') = md.day) as day_max,
        (SELECT MIN(state) FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch') = md.day AND state > 0) as day_min,
        COALESCE((SELECT state FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch') = md.day AND strftime('%H', start_ts, 'unixepoch') = strftime('%H', 'now') LIMIT 1), (SELECT MIN(state) FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch') = md.day AND state > 0)) as h_hour_curr,
        COALESCE((SELECT state FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch') = md.day AND strftime('%H', start_ts, 'unixepoch') = strftime('%H', 'now', '-1 hour') LIMIT 1), (SELECT MIN(state) FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch') = md.day AND state > 0)) as h_hour_prev
    FROM matching_days md
)

/* Generates the final JSON object for Home Assistant */
SELECT json_group_array(
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
        'yield_day_total', ROUND(day_max - day_min, 2),
        'yield_day_remaining', ROUND(CASE
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
            )
        END, 2),
        'pv_start', (SELECT sun_start FROM pv_activity),
        'pv_end', (SELECT sun_end FROM pv_activity)
    )
) as json FROM final_data WHERE day_max > 0;