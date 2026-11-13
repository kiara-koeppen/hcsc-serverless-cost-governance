-- =============================================================================
-- Genie cost + query-level cost drivers (companion to serverless_tag_coverage.sql)
--
-- Answers two questions the tag-coverage audit can't:
--   A. What does Genie cost?  Genie's AI (LLM) usage, by surface / space / user,
--      free vs billed.                                   (exact, from billing)
--   B. Which queries drive warehouse cost?  Each warehouse's hourly cost is
--      split across the statements that ran on it in that hour, weighted by
--      total_task_duration_ms, and rolled up by source (Genie space, dashboard,
--      job, notebook, BI tool / ad hoc).                 (ALLOCATION, estimate)
--
-- Genie total cost = A (Genie AI usage) + B rows where source = Genie space.
--
-- Source tables (all GA on Azure per the system tables reference, Nov 2026):
-- system.billing.usage, system.billing.list_prices (global), system.query.history
-- (regional: covers workspaces in the same region as the workspace you run it in).
--
-- Facts this relies on (Azure docs "Monitor and understand your Genie cost"):
--   * All Genie AI usage has billing_origin_product = 'GENIE'.
--   * Free usage -> sku GENIE_FREE_USAGE (no list price, appears from 2026-07-20).
--     Billed usage -> regional SERVERLESS_REAL_TIME_INFERENCE SKU.
--   * 150 free DBUs per user per month (service principals excluded).
--     Genie One + Genie Agents are free for users until 2027-01-31; service
--     principal usage is billed. 25% promo discount is already in usage_quantity.
--   * usage_metadata.genie.surface = GENIE_CODE | GENIE_ONE | GENIE_AGENTS;
--     agent_id = Genie space id (GENIE_AGENTS only).
--   * Genie rows only carry custom_tags {"databricks-product": "genie"}, so
--     Genie AI usage can't be tagged per team. Attribute it by user and space.
--   * SQL that Genie generates runs on the space's warehouse and bills as SQL
--     warehouse usage, not on the Genie SKU.
-- =============================================================================

-- Parameters ------------------------------------------------------------------
DECLARE OR REPLACE VARIABLE ws_id         STRING DEFAULT NULL; -- NULL = all workspaces
DECLARE OR REPLACE VARIABLE lookback_days INT    DEFAULT 30;

-- A1. Genie AI usage by surface (free vs billed) -----------------------------
SELECT
  u.usage_metadata.genie.surface                         AS genie_surface,
  COUNT(DISTINCT u.identity_metadata.run_as)             AS users,
  ROUND(SUM(IF(u.sku_name =  'GENIE_FREE_USAGE', u.usage_quantity, 0)), 1) AS free_dbus,
  ROUND(SUM(IF(u.sku_name <> 'GENIE_FREE_USAGE', u.usage_quantity, 0)), 1) AS billed_dbus,
  ROUND(SUM(u.usage_quantity * COALESCE(lp.pricing.effective_list.default, 0)), 2) AS billed_list_cost_usd
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices lp
  ON  u.sku_name = lp.sku_name AND u.cloud = lp.cloud AND u.usage_unit = lp.usage_unit
  AND u.usage_end_time >= lp.price_start_time
  AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  AND lp.currency_code = 'USD'
WHERE u.billing_origin_product = 'GENIE'
  AND (ws_id IS NULL OR u.workspace_id = ws_id)
  AND u.usage_date >= current_date() - lookback_days
GROUP BY ALL
ORDER BY billed_list_cost_usd DESC;

-- A2. Genie AI usage by space and user (who is driving billed Genie cost) -----
SELECT
  u.workspace_id,
  u.usage_metadata.genie.surface                         AS genie_surface,
  u.usage_metadata.genie.agent_id                        AS genie_space_id,  -- NULL for Genie Code / Genie One
  u.usage_metadata.genie.channel                         AS channel,         -- UI or API
  u.identity_metadata.run_as                             AS run_as,
  ROUND(SUM(IF(u.sku_name =  'GENIE_FREE_USAGE', u.usage_quantity, 0)), 1) AS free_dbus,
  ROUND(SUM(IF(u.sku_name <> 'GENIE_FREE_USAGE', u.usage_quantity, 0)), 1) AS billed_dbus,
  ROUND(SUM(u.usage_quantity * COALESCE(lp.pricing.effective_list.default, 0)), 2) AS billed_list_cost_usd
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices lp
  ON  u.sku_name = lp.sku_name AND u.cloud = lp.cloud AND u.usage_unit = lp.usage_unit
  AND u.usage_end_time >= lp.price_start_time
  AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  AND lp.currency_code = 'USD'
WHERE u.billing_origin_product = 'GENIE'
  AND (ws_id IS NULL OR u.workspace_id = ws_id)
  AND u.usage_date >= current_date() - lookback_days
GROUP BY ALL
ORDER BY billed_list_cost_usd DESC, billed_dbus + free_dbus DESC
LIMIT 200;

-- B. Warehouse cost allocated to the statements that ran on it ---------------
CREATE OR REPLACE TEMPORARY VIEW warehouse_query_cost AS
WITH wh_hour_cost AS (     -- what each warehouse cost, per hour
  SELECT
    u.workspace_id,
    u.usage_metadata.warehouse_id                        AS warehouse_id,
    date_trunc('HOUR', u.usage_start_time)               AS hour,
    SUM(u.usage_quantity * COALESCE(lp.pricing.effective_list.default, 0)) AS hour_cost_usd
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices lp
    ON  u.sku_name = lp.sku_name AND u.cloud = lp.cloud AND u.usage_unit = lp.usage_unit
    AND u.usage_end_time >= lp.price_start_time
    AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
    AND lp.currency_code = 'USD'
  WHERE u.billing_origin_product = 'SQL'
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND (ws_id IS NULL OR u.workspace_id = ws_id)
    AND u.usage_date >= current_date() - lookback_days
  GROUP BY ALL
),
q AS (                     -- statements, tagged with what triggered them
  SELECT
    workspace_id,
    compute.warehouse_id                                 AS warehouse_id,
    date_trunc('HOUR', start_time)                       AS hour,
    statement_id,
    executed_by,
    client_application,
    CASE
      WHEN query_source.genie_space_id IS NOT NULL       THEN 'Genie space'
      -- Genie SQL with no space id (for example Genie One / Genie Code)
      WHEN client_application ILIKE '%genie%'            THEN 'Genie (no space id)'
      WHEN query_source.dashboard_id IS NOT NULL
        OR query_source.legacy_dashboard_id IS NOT NULL  THEN 'Dashboard'
      WHEN query_source.alert_id IS NOT NULL             THEN 'Alert'
      WHEN query_source.job_info.job_id IS NOT NULL      THEN 'Job'
      WHEN query_source.pipeline_info.pipeline_id IS NOT NULL THEN 'Pipeline'
      WHEN query_source.notebook_id IS NOT NULL          THEN 'Notebook'
      WHEN query_source.sql_query_id IS NOT NULL         THEN 'Saved query'
      ELSE COALESCE(client_application, 'Other / ad hoc')
    END                                                  AS source_type,
    COALESCE(query_source.genie_space_id, query_source.dashboard_id,
             query_source.legacy_dashboard_id, query_source.alert_id,
             query_source.job_info.job_id, query_source.pipeline_info.pipeline_id,
             query_source.notebook_id, query_source.sql_query_id) AS source_id,
    statement_text,
    total_duration_ms,
    COALESCE(total_task_duration_ms, 0)                  AS task_ms
  FROM system.query.history
  WHERE compute.warehouse_id IS NOT NULL
    AND (ws_id IS NULL OR workspace_id = ws_id)
    AND start_time >= current_date() - lookback_days
),
hour_ms AS (
  SELECT workspace_id, warehouse_id, hour, SUM(task_ms) AS hour_task_ms
  FROM q GROUP BY ALL
)
SELECT
  q.*,
  c.hour_cost_usd * q.task_ms / NULLIF(h.hour_task_ms, 0) AS allocated_cost_usd
FROM q
JOIN hour_ms h      USING (workspace_id, warehouse_id, hour)
JOIN wh_hour_cost c USING (workspace_id, warehouse_id, hour);

-- B1. Warehouse cost by source type (how much of SQL spend is Genie, dashboards, BI ...)
SELECT
  source_type,
  COUNT(*)                                       AS statements,
  COUNT(DISTINCT executed_by)                    AS users,
  ROUND(SUM(allocated_cost_usd), 2)              AS allocated_cost_usd,
  ROUND(100 * SUM(allocated_cost_usd) / SUM(SUM(allocated_cost_usd)) OVER (), 1) AS pct_of_allocated
FROM warehouse_query_cost
GROUP BY source_type
ORDER BY allocated_cost_usd DESC;

-- B2. Top cost drivers: source (Genie space / dashboard / job ...) x warehouse
SELECT
  workspace_id, warehouse_id, source_type, source_id,
  COUNT(*)                                       AS statements,
  slice(array_sort(collect_set(executed_by)), 1, 10) AS users,
  ROUND(SUM(allocated_cost_usd), 2)              AS allocated_cost_usd,
  ROUND(SUM(total_duration_ms) / 60000, 1)       AS total_minutes
FROM warehouse_query_cost
GROUP BY ALL
ORDER BY allocated_cost_usd DESC
LIMIT 100;

-- B3. Most expensive individual statements (the queries to tune first)
SELECT
  workspace_id, warehouse_id, source_type, source_id, executed_by, statement_id,
  ROUND(allocated_cost_usd, 4)                   AS allocated_cost_usd,
  ROUND(total_duration_ms / 1000, 1)             AS duration_s,
  left(statement_text, 300)                      AS statement_preview
FROM warehouse_query_cost
ORDER BY allocated_cost_usd DESC
LIMIT 50;

-- C. Total Genie cost per space: AI usage + allocated warehouse compute -----
WITH ai AS (
  SELECT u.workspace_id, u.usage_metadata.genie.agent_id AS genie_space_id,
         SUM(u.usage_quantity * COALESCE(lp.pricing.effective_list.default, 0)) AS ai_cost_usd,
         SUM(u.usage_quantity) AS ai_dbus
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices lp
    ON  u.sku_name = lp.sku_name AND u.cloud = lp.cloud AND u.usage_unit = lp.usage_unit
    AND u.usage_end_time >= lp.price_start_time
    AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
    AND lp.currency_code = 'USD'
  WHERE u.billing_origin_product = 'GENIE'
    AND u.usage_metadata.genie.agent_id IS NOT NULL
    AND (ws_id IS NULL OR u.workspace_id = ws_id)
    AND u.usage_date >= current_date() - lookback_days
  GROUP BY ALL
),
wh AS (
  SELECT workspace_id, source_id AS genie_space_id,
         SUM(allocated_cost_usd) AS warehouse_cost_usd, COUNT(*) AS statements
  FROM warehouse_query_cost
  WHERE source_type = 'Genie space'
  GROUP BY ALL
)
SELECT
  workspace_id, genie_space_id,
  ROUND(COALESCE(ai.ai_dbus, 0), 1)               AS ai_dbus,            -- includes free usage
  ROUND(COALESCE(ai.ai_cost_usd, 0), 2)           AS ai_billed_list_cost_usd,
  COALESCE(wh.statements, 0)                      AS warehouse_statements,
  ROUND(COALESCE(wh.warehouse_cost_usd, 0), 2)    AS warehouse_allocated_cost_usd,
  ROUND(COALESCE(ai.ai_cost_usd, 0) + COALESCE(wh.warehouse_cost_usd, 0), 2) AS total_est_cost_usd
FROM ai FULL OUTER JOIN wh USING (workspace_id, genie_space_id)
ORDER BY total_est_cost_usd DESC
LIMIT 100;
