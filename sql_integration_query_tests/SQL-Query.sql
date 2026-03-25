WITH vars AS (
    SELECT 
        'weather.forecast_home' as sensor_clouds,   -- Weather Entity direkt: cloud_coverage aus state_attributes
        'sensor.pv_panels_energy' as sensor_pv,
        'sensor.weather_forecast_hourly' as sensor_forecast,
        -- Berechnet den Versatz zwischen Lokalzeit und UTC (z.B. '+3600 seconds')
        -- Wird genutzt, um den Datumswechsel (00:00 Uhr) lokal zu triggern
        (strftime('%s', 'now', 'localtime') - strftime('%s', 'now')) || ' seconds' as offset
),

ids AS (
    /* Holt alle benötigten internen IDs für Statistiken und States aus der HA-Datenbank */
    SELECT 
        (SELECT id FROM statistics_meta WHERE statistic_id = (SELECT sensor_clouds FROM vars)) as w_id_stats,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_clouds FROM vars)) as w_id_states,
        (SELECT id FROM statistics_meta WHERE statistic_id = (SELECT sensor_pv FROM vars) LIMIT 1) as p_id,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_pv FROM vars) LIMIT 1) as p_id_states,
        (SELECT metadata_id FROM states_meta WHERE entity_id = (SELECT sensor_forecast FROM vars) LIMIT 1) as f_id
),

pv_activity AS (
    /* Ermittelt die Sonnenauf- und Untergangszeiten basierend auf der gestrigen PV-Produktion */
    SELECT 
        COALESCE((
            SELECT strftime('%H:%M', last_updated_ts, 'unixepoch') 
            FROM states 
            WHERE metadata_id = (SELECT p_id_states FROM ids) 
              AND date(last_updated_ts, 'unixepoch', (SELECT offset FROM vars)) = date('now', (SELECT offset FROM vars), '-1 day') 
              AND state NOT IN ('unknown','0','0.0','unavailable') 
            ORDER BY last_updated_ts ASC LIMIT 1
        ), '05:30') as sun_start,
        COALESCE((
            -- Letzter Zeitpunkt, an dem der kumulative Sensor noch UNTER seinem Tagesmaximum lag
            -- = letzter aktiver Produktionszeitpunkt (Sonnenuntergang).
            -- state DESC LIMIT 1 wäre falsch: kumulativer Sensor bleibt bis Mitternacht auf Max-Wert.
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
    /* Berechnet die durchschnittliche Bewölkung für den verbleibenden Teil des aktuellen Tages */
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
                         WHEN strftime('%H:%M', 'now') > (SELECT sun_start FROM pv_activity) THEN strftime('%H:%M', 'now') 
                         ELSE (SELECT sun_start FROM pv_activity) 
                       END
               AND (SELECT sun_end FROM pv_activity)
        ), 50.0) as f_avg
),

forecast_next_day AS (
    /* Berechnet die durchschnittliche Bewölkung für den gesamten nächsten Tag */
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
    /* Kombiniert Langzeit-Statistiken und kurzfristige States der Bewölkung für den historischen Vergleich */
    SELECT start_ts as ts, CAST(COALESCE(mean, state) AS FLOAT) as val 
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

matching_days AS (
    /* Findet vergangene Tage, deren Bewölkungsprofil dem heutigen Forecast am nächsten kommt */
    SELECT 
        date(ts, 'unixepoch') as day, 
        AVG(CASE WHEN strftime('%H:%M', ts, 'unixepoch') BETWEEN (SELECT sun_start FROM pv_activity) AND (SELECT sun_end FROM pv_activity) THEN val END) as h_avg_total_val,
        AVG(CASE WHEN strftime('%H:%M', ts, 'unixepoch') >= strftime('%H:00', 'now') AND strftime('%H:%M', ts, 'unixepoch') <= (SELECT sun_end FROM pv_activity) THEN val END) as h_avg_rest_val
    FROM cloud_history 
    -- Filtert die Historie: Alles vor dem heutigen lokalen Tag (Offset-gesteuert)
    WHERE date(ts, 'unixepoch') < date('now', (SELECT offset FROM vars)) 
    GROUP BY 1 
    HAVING h_avg_total_val IS NOT NULL AND h_avg_total_val > 0
    ORDER BY ABS(
    COALESCE(h_avg_rest_val, h_avg_total_val) -- Fallback auf Gesamt-Schnitt wenn Rest null ist
    - (SELECT f_avg FROM forecast_val)
) ASC
),

final_data AS (
    /* Ermittelt die realen PV-Erträge der passendsten historischen Tage */
    SELECT 
        md.*,
        (SELECT MAX(state) FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch') = md.day) as day_max,
        (SELECT MIN(state) FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch') = md.day AND state > 0) as day_min,
        COALESCE((SELECT state FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch') = md.day AND strftime('%H', start_ts, 'unixepoch') = strftime('%H', 'now') LIMIT 1), (SELECT MIN(state) FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch') = md.day AND state > 0)) as h_hour_curr,
        COALESCE((SELECT state FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch') = md.day AND strftime('%H', start_ts, 'unixepoch') = strftime('%H', 'now', '-1 hour') LIMIT 1), (SELECT MIN(state) FROM statistics WHERE metadata_id = (SELECT p_id FROM ids) AND date(start_ts, 'unixepoch') = md.day AND state > 0)) as h_hour_prev
    FROM matching_days md
)

/* Generiert das finale JSON-Objekt für Home Assistant */
SELECT json_group_array(
    json_object(
        'date', day,
        'f_avg_today_remaining', (SELECT ROUND(f_avg, 1) FROM forecast_val),        
        'f_avg_tomorrow', (SELECT ROUND(f_avg_tomorrow, 1) FROM forecast_next_day),
        'h_avg_total', ROUND(h_avg_total_val, 1),
        'h_avg_remaining', ROUND(h_avg_rest_val, 1),
        'yield_day_total', ROUND(day_max - day_min, 2),
        -- Dreistufige Logik für den Restertrag (alle Zeiten in UTC):
        -- 1. Vor Sonnenaufgang (00:00 bis sun_start): Gesamter Tagesertrag als Prognose.
        -- 2. Im PV-Fenster (sun_start bis sun_end): Verbleibender Restertrag ab jetzt.
        -- 3. Nach Sonnenuntergang (sun_end bis 23:59): 0.0 (Tag abgeschlossen).
        'yield_day_remaining', ROUND(CASE 
            WHEN strftime('%H:%M', 'now') > (SELECT sun_end FROM pv_activity)
                THEN 0.0
            WHEN strftime('%H:%M', 'now') < (SELECT sun_start FROM pv_activity)
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