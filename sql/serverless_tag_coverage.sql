-- =============================================================================
-- Serverless tag coverage audit (v2)
--
-- Builds a temp view with one row per serverless resource (for Model Serving,
-- per destination model / provider), then returns:
--   1. Coverage summary by resource type   (tagging coverage % of spend)
--   2. Resource detail, untagged first      (what to go fix)
--
-- Source tables (all GA): system.billing.usage, system.billing.list_prices,
-- system.lakeflow.jobs, system.lakeflow.pipelines, system.compute.warehouses
--
-- Changes from v1:
--   * product_features.is_serverless is NULL (not TRUE) for serverless-only
--     products such as Model Serving, AI Search, AI Runtime, and Genie, and for
--     some serverless notebook / job / pipeline usage. v1's
--     `is_serverless = true` filter dropped all of that. v2 keeps NULL rows
--     and excludes only usage explicitly flagged classic.
--   * Model Serving is split by destination model (usage_metadata.ai_gateway,
--     populated for Unity Gateway model services) and by model provider
--     (parsed from the SKU, e.g. *_ANTHROPIC_MODEL_SERVING).
--   * Platform-added keys (EndpointId, ServingType, BudgetPolicyId, ...) are
--     ignored when deciding whether a resource is tagged. v1 counted them,
--     so nearly every Model Serving endpoint showed as TAGGED.
--   * tag_status is based on the tags on the billing record (what chargeback
--     sees). Job / pipeline definition tags are shown alongside for context.
--   * Coverage is measured in estimated list-price USD so DBU, DSU, and GB
--     usage can be compared. List price excludes negotiated discounts.
-- =============================================================================

-- Parameters ------------------------------------------------------------------
DECLARE OR REPLACE VARIABLE ws_id            STRING DEFAULT NULL; -- NULL = all workspaces in the account
DECLARE OR REPLACE VARIABLE lookback_days    INT    DEFAULT 90;
DECLARE OR REPLACE VARIABLE required_tag_key STRING DEFAULT NULL; -- e.g. 'cost_center'. NULL = any non-platform tag counts

-- Keys the platform adds to custom_tags. They identify the resource, not who
-- pays for it, so they don't count as tagging. Keys starting with
-- 'DatabricksScgw', 'databricks.' or 'databricks-' are also ignored.
DECLARE OR REPLACE VARIABLE platform_tag_keys ARRAY<STRING> DEFAULT array(
  'EndpointId', 'ServingType', 'BudgetPolicyId', 'MONITOR_EXPERIMENT_ID',
  'propagate_platform_channel', 'DataClassification', 'Predictive Optimization',
  'LakehouseMonitoring', 'LakehouseMonitoringMetastoreId',
  'LakehouseMonitoringTableId', 'LakehouseMonitoringWorkspaceId');

CREATE OR REPLACE TEMPORARY VIEW serverless_tag_coverage AS
WITH usage AS (
  SELECT u.*,
         -- Free-usage SKUs have no list price; count them as $0
         COALESCE(u.usage_quantity * lp.pricing.effective_list.default, 0) AS list_cost_usd
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices lp
    ON  u.sku_name   = lp.sku_name
    AND u.cloud      = lp.cloud
    AND u.usage_unit = lp.usage_unit
    AND u.usage_end_time >= lp.price_start_time
    AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
    AND lp.currency_code = 'USD'
  WHERE (ws_id IS NULL OR u.workspace_id = ws_id)
    AND u.usage_date >= current_date() - lookback_days
    -- Serverless only: is_serverless is FALSE for classic compute and NULL for
    -- serverless-only products. Don't filter on cluster_id: serverless model
    -- serving called from a classic cluster (e.g. ai_query) carries one.
    AND COALESCE(u.product_features.is_serverless, TRUE)
    AND u.billing_origin_product <> 'ALL_PURPOSE'
),

typed AS (
  SELECT
    workspace_id,
    billing_origin_product,
    CASE
      WHEN billing_origin_product = 'MODEL_SERVING'
           AND usage_metadata.ai_gateway.endpoint_name IS NOT NULL THEN 'Unity Gateway model service'
      WHEN billing_origin_product = 'MODEL_SERVING'                THEN 'Model serving endpoint'
      WHEN billing_origin_product = 'INTERACTIVE'                  THEN 'Serverless notebook'
      WHEN billing_origin_product = 'JOBS'                         THEN 'Serverless job'
      WHEN billing_origin_product IN ('DLT', 'LAKEFLOW_CONNECT')   THEN 'Lakeflow pipeline / connector'
      WHEN billing_origin_product = 'SQL'
           AND usage_metadata.warehouse_id IS NOT NULL             THEN 'Serverless SQL warehouse'
      WHEN billing_origin_product = 'SQL'                          THEN 'Materialized view / streaming table'
      WHEN billing_origin_product IN ('DATABASE', 'LAKEBASE')      THEN 'Lakebase'
      WHEN billing_origin_product = 'APPS'                         THEN 'Databricks App'
      WHEN billing_origin_product = 'VECTOR_SEARCH'                THEN 'AI Search endpoint'
      WHEN billing_origin_product = 'AI_RUNTIME'                   THEN 'Serverless GPU (AI Runtime)'
      ELSE billing_origin_product
    END AS resource_type,
    CASE
      WHEN billing_origin_product IN ('MODEL_SERVING', 'AI_GATEWAY', 'AI_FUNCTIONS') THEN
        COALESCE(usage_metadata.ai_gateway.endpoint_name, usage_metadata.endpoint_name, usage_metadata.endpoint_id)
      WHEN billing_origin_product = 'INTERACTIVE'   THEN usage_metadata.notebook_id
      WHEN billing_origin_product = 'JOBS'          THEN usage_metadata.job_id
      WHEN billing_origin_product = 'SQL'           THEN COALESCE(usage_metadata.warehouse_id, usage_metadata.dlt_pipeline_id)
      WHEN billing_origin_product = 'APPS'          THEN COALESCE(usage_metadata.app_id, usage_metadata.app_name)
      WHEN billing_origin_product = 'VECTOR_SEARCH' THEN COALESCE(usage_metadata.endpoint_name, usage_metadata.endpoint_id, usage_metadata.dlt_pipeline_id)
      WHEN billing_origin_product IN ('DATABASE', 'LAKEBASE') THEN
        COALESCE(usage_metadata.database_instance_id, usage_metadata.project_id)
      WHEN billing_origin_product = 'AI_RUNTIME'    THEN COALESCE(usage_metadata.ai_runtime_workload_id, usage_metadata.ai_runtime_pool_id)
      ELSE COALESCE(
        usage_metadata.dlt_pipeline_id, usage_metadata.job_id, usage_metadata.notebook_id,
        usage_metadata.endpoint_name, usage_metadata.agent_bricks_id,
        usage_metadata.supervisor_agent_id, usage_metadata.genie.agent_id)
    END AS resource_id,
    usage_metadata.ai_gateway.destination_model           AS destination_model,
    NULLIF(regexp_extract(sku_name, '_([A-Z]+)_MODEL_SERVING$', 1), '') AS model_provider,  -- e.g. ANTHROPIC, OPENAI, GEMINI
    product_features.serving_type                          AS serving_type,
    COALESCE(usage_metadata.ai_gateway.endpoint_name, usage_metadata.endpoint_name,
             usage_metadata.job_name, usage_metadata.notebook_path,
             usage_metadata.app_name)                      AS resource_name,
    COALESCE(identity_metadata.run_as, identity_metadata.run_by,
             identity_metadata.owned_by, identity_metadata.created_by) AS owner,
    usage_metadata.job_id,
    usage_metadata.dlt_pipeline_id,
    usage_metadata.warehouse_id,
    usage_metadata.usage_policy_id,
    -- Tags with platform-added keys removed
    map_filter(COALESCE(custom_tags, map()), (k, v) ->
      NOT (array_contains(platform_tag_keys, k)
           OR startswith(k, 'DatabricksScgw')
           OR startswith(k, 'databricks.')
           OR startswith(k, 'databricks-'))) AS attribution_tags,
    usage_unit,
    record_type,
    usage_end_time,
    usage_date,
    usage_quantity,
    list_cost_usd
  FROM usage
),

flagged AS (
  SELECT *,
    CASE
      WHEN required_tag_key IS NOT NULL THEN COALESCE(attribution_tags[required_tag_key], '') <> ''
      ELSE size(attribution_tags) > 0
    END AS is_tagged
  FROM typed
),

resources AS (
  SELECT
    workspace_id, resource_type, billing_origin_product, resource_id,
    destination_model, model_provider,
    -- "Latest" values ignore retraction rows
    MAX_BY(resource_name,    IF(record_type = 'RETRACTION', NULL, usage_end_time)) AS resource_name,
    MAX_BY(owner,            IF(record_type = 'RETRACTION', NULL, usage_end_time)) AS owner,
    MAX_BY(attribution_tags, IF(record_type = 'RETRACTION', NULL, usage_end_time)) AS latest_tags,
    MAX_BY(is_tagged,        IF(record_type = 'RETRACTION', NULL, usage_end_time)) AS latest_is_tagged,
    MAX_BY(usage_policy_id,  IF(record_type = 'RETRACTION', NULL, usage_end_time)) AS usage_policy_id,
    MAX(serving_type)    AS serving_type,
    MAX(job_id)          AS job_id,
    MAX(dlt_pipeline_id) AS pipeline_id,
    MAX(warehouse_id)    AS warehouse_id,
    MIN(usage_date)      AS first_seen,
    MAX(usage_date)      AS last_seen,
    SUM(IF(usage_unit = 'DBU', usage_quantity, 0)) AS dbus,
    SUM(list_cost_usd)                             AS list_cost_usd,
    SUM(IF(is_tagged, list_cost_usd, 0))           AS tagged_list_cost_usd,
    -- Who ran the untagged usage (useful for shared pay-per-token endpoints)
    slice(array_sort(collect_set(IF(is_tagged, NULL, owner))), 1, 10) AS untagged_principals
  FROM flagged
  GROUP BY ALL
),

jobs AS (       -- current job definition (name, tags)
  SELECT workspace_id, job_id, name, tags
  FROM system.lakeflow.jobs
  WHERE (ws_id IS NULL OR workspace_id = ws_id)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),

pipelines AS (  -- current pipeline definition (name, tags)
  SELECT workspace_id, pipeline_id, name, tags
  FROM system.lakeflow.pipelines
  WHERE (ws_id IS NULL OR workspace_id = ws_id)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) = 1
),

warehouses AS ( -- current warehouse name
  SELECT workspace_id, warehouse_id, warehouse_name
  FROM system.compute.warehouses
  WHERE (ws_id IS NULL OR workspace_id = ws_id)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time DESC) = 1
)

SELECT
  r.workspace_id,
  r.resource_type,
  r.billing_origin_product,
  r.resource_id,
  COALESCE(j.name, p.name, w.warehouse_name, r.resource_name) AS resource_name,
  r.serving_type,
  r.model_provider,
  r.destination_model,                          -- Unity Gateway model services only
  r.owner,
  r.untagged_principals,
  CASE
    WHEN r.latest_is_tagged AND r.tagged_list_cost_usd >= r.list_cost_usd THEN 'TAGGED'
    WHEN r.latest_is_tagged                     THEN 'TAGGED (earlier usage untagged)'
    WHEN r.tagged_list_cost_usd > 0             THEN 'UNTAGGED (was tagged earlier)'
    ELSE 'UNTAGGED'
  END AS tag_status,
  r.latest_tags,                                -- attribution tags on the most recent billing record
  COALESCE(j.tags, p.tags) AS definition_tags,  -- jobs / pipelines only, for context
  r.usage_policy_id,
  ROUND(r.dbus, 2)                              AS dbus,
  ROUND(r.list_cost_usd, 2)                     AS list_cost_usd,
  ROUND(r.tagged_list_cost_usd, 2)              AS tagged_list_cost_usd,
  ROUND(100 * r.tagged_list_cost_usd / NULLIF(r.list_cost_usd, 0), 1) AS tagged_cost_pct,
  r.first_seen,
  r.last_seen
FROM resources r
LEFT JOIN jobs j       ON r.workspace_id = j.workspace_id AND r.job_id       = j.job_id
LEFT JOIN pipelines p  ON r.workspace_id = p.workspace_id AND r.pipeline_id  = p.pipeline_id
LEFT JOIN warehouses w ON r.workspace_id = w.workspace_id AND r.warehouse_id = w.warehouse_id;

-- 1. Coverage summary by resource type ---------------------------------------
SELECT
  resource_type,
  COUNT(*)                                       AS resources,
  COUNT_IF(tag_status LIKE 'UNTAGGED%')          AS untagged_resources,
  ROUND(SUM(list_cost_usd), 2)                   AS list_cost_usd,
  ROUND(SUM(list_cost_usd - tagged_list_cost_usd), 2) AS untagged_list_cost_usd,
  ROUND(100 * SUM(tagged_list_cost_usd) / NULLIF(SUM(list_cost_usd), 0), 1) AS tagged_cost_pct
FROM serverless_tag_coverage
GROUP BY resource_type
ORDER BY untagged_list_cost_usd DESC;

-- 2. Resource detail, untagged first -----------------------------------------
SELECT *
FROM serverless_tag_coverage
ORDER BY tag_status LIKE 'UNTAGGED%' DESC, list_cost_usd DESC;
