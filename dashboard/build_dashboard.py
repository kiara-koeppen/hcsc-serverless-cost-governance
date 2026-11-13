"""Generate serverless_cost_governance.lvdash.json.

The dashboard reads ONLY system tables, so a customer can import the
.lvdash.json file into any workspace and it works, no setup:
  system.billing.usage, system.billing.list_prices, system.compute.warehouses,
  system.query.history

Logic mirrors sql/serverless_tag_coverage.sql (v2) and
sql/genie_cost_and_query_attribution.sql. Edit SQL fragments here, then run:
  python3 dashboard/build_dashboard.py && python3 tests/test_dashboard.py --live
"""
import json
from pathlib import Path

OUT = Path(__file__).parent / "serverless_cost_governance.lvdash.json"

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
P_TIME = {
    "displayName": "Date range", "keyword": "time_range", "dataType": "DATE",
    "complexType": "RANGE",
    "defaultSelection": {"range": {"dataType": "DATE",
                                   "min": {"value": "now-30d/d"}, "max": {"value": "now/d"}}},
}
P_WS = {
    "displayName": "Workspace", "keyword": "param_workspace", "dataType": "STRING",
    "defaultSelection": {"values": {"dataType": "STRING", "values": [{"value": "ALL"}]}},
}
P_TAG = {
    "displayName": "Required tag key", "keyword": "param_tag_key", "dataType": "STRING",
    "defaultSelection": {"values": {"dataType": "STRING", "values": [{"value": "ANY"}]}},
}

# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------
WS_FILTER = "(:param_workspace = 'ALL' OR {col} = :param_workspace)"

# Keys the platform adds to custom_tags. They identify the resource, not who
# pays, so they don't count as tagging.
PLATFORM_TAG_FILTER = """map_filter(COALESCE(custom_tags, map()), (k, v) ->
      NOT (array_contains(array('EndpointId', 'ServingType', 'BudgetPolicyId', 'MONITOR_EXPERIMENT_ID',
             'propagate_platform_channel', 'DataClassification', 'Predictive Optimization',
             'LakehouseMonitoring', 'LakehouseMonitoringMetastoreId', 'LakehouseMonitoringTableId',
             'LakehouseMonitoringWorkspaceId'), k)
           OR startswith(k, 'DatabricksScgw') OR startswith(k, 'databricks.')
           OR startswith(k, 'databricks-')))"""

PRICED = f"""priced AS (   -- usage in range, priced at list (free SKUs have no price -> $0)
  SELECT u.*,
         COALESCE(u.usage_quantity * lp.pricing.effective_list.default, 0) AS list_cost_usd
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices lp
    ON  u.sku_name = lp.sku_name AND u.cloud = lp.cloud AND u.usage_unit = lp.usage_unit
    AND u.usage_end_time >= lp.price_start_time
    AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
    AND lp.currency_code = 'USD'
  WHERE u.usage_date BETWEEN :time_range.min AND :time_range.max
    AND {WS_FILTER.format(col='u.workspace_id')}
)"""

# One row per serverless usage record, classified + tag-checked.
TYPED = f"""typed AS (
  SELECT
    workspace_id, billing_origin_product, usage_date, usage_end_time, record_type,
    usage_unit, usage_quantity, list_cost_usd,
    CASE
      WHEN billing_origin_product = 'MODEL_SERVING'
           AND usage_metadata.ai_gateway.endpoint_name IS NOT NULL THEN 'Unity Gateway model service'
      WHEN billing_origin_product = 'MODEL_SERVING'              THEN 'Model serving endpoint'
      WHEN billing_origin_product = 'GENIE'                      THEN 'Genie'
      WHEN billing_origin_product = 'INTERACTIVE'                THEN 'Serverless notebook'
      WHEN billing_origin_product = 'JOBS'                       THEN 'Serverless job'
      WHEN billing_origin_product IN ('DLT', 'LAKEFLOW_CONNECT') THEN 'Lakeflow pipeline / connector'
      WHEN billing_origin_product = 'SQL'
           AND usage_metadata.warehouse_id IS NOT NULL           THEN 'Serverless SQL warehouse'
      WHEN billing_origin_product = 'SQL'                        THEN 'Materialized view / streaming table'
      WHEN billing_origin_product IN ('DATABASE', 'LAKEBASE')    THEN 'Lakebase'
      WHEN billing_origin_product = 'APPS'                       THEN 'Databricks App'
      WHEN billing_origin_product = 'VECTOR_SEARCH'              THEN 'AI Search endpoint'
      WHEN billing_origin_product = 'AI_RUNTIME'                 THEN 'Serverless GPU (AI Runtime)'
      ELSE initcap(replace(billing_origin_product, '_', ' '))
    END AS resource_type,
    CASE
      WHEN billing_origin_product IN ('MODEL_SERVING', 'AI_GATEWAY', 'AI_FUNCTIONS') THEN
        COALESCE(usage_metadata.ai_gateway.endpoint_name, usage_metadata.endpoint_name, usage_metadata.endpoint_id)
      -- Genie: spaces carry an id; Genie Code / Genie One are attributed per user
      WHEN billing_origin_product = 'GENIE' THEN
        COALESCE(usage_metadata.genie.agent_id,
                 concat(usage_metadata.genie.surface, ' / ', identity_metadata.run_as))
      WHEN billing_origin_product = 'INTERACTIVE'   THEN usage_metadata.notebook_id
      WHEN billing_origin_product = 'JOBS'          THEN usage_metadata.job_id
      WHEN billing_origin_product = 'SQL'           THEN COALESCE(usage_metadata.warehouse_id, usage_metadata.dlt_pipeline_id)
      WHEN billing_origin_product = 'APPS'          THEN COALESCE(usage_metadata.app_id, usage_metadata.app_name)
      WHEN billing_origin_product = 'VECTOR_SEARCH' THEN COALESCE(usage_metadata.endpoint_name, usage_metadata.endpoint_id, usage_metadata.dlt_pipeline_id)
      WHEN billing_origin_product IN ('DATABASE', 'LAKEBASE') THEN
        COALESCE(usage_metadata.database_instance_id, usage_metadata.project_id)
      WHEN billing_origin_product = 'AI_RUNTIME'    THEN COALESCE(usage_metadata.ai_runtime_workload_id, usage_metadata.ai_runtime_pool_id)
      ELSE COALESCE(usage_metadata.dlt_pipeline_id, usage_metadata.job_id, usage_metadata.notebook_id,
                    usage_metadata.endpoint_name, usage_metadata.agent_bricks_id,
                    usage_metadata.supervisor_agent_id)
    END AS resource_id,
    COALESCE(usage_metadata.ai_gateway.endpoint_name, usage_metadata.endpoint_name,
             usage_metadata.job_name, usage_metadata.notebook_path, usage_metadata.app_name,
             IF(billing_origin_product = 'GENIE', usage_metadata.genie.surface, NULL)) AS resource_name,
    usage_metadata.ai_gateway.destination_model                          AS destination_model,
    NULLIF(regexp_extract(sku_name, '_([A-Z]+)_MODEL_SERVING$', 1), '')  AS model_provider,
    product_features.serving_type                                        AS serving_type,
    COALESCE(identity_metadata.run_as, identity_metadata.run_by,
             identity_metadata.owned_by, identity_metadata.created_by)   AS owner,
    usage_metadata.warehouse_id                                          AS warehouse_id,
    -- Genie usage only carries the platform tag databricks-product: genie,
    -- so it can't be tagged per team. Attribute it by user / space instead.
    billing_origin_product <> 'GENIE'                                    AS taggable,
    {PLATFORM_TAG_FILTER} AS attribution_tags
  FROM priced
  WHERE COALESCE(product_features.is_serverless, TRUE)   -- NULL for serverless-only products
    AND billing_origin_product <> 'ALL_PURPOSE'
),
flagged AS (
  SELECT *,
    taggable AND CASE
      WHEN :param_tag_key <> 'ANY' THEN COALESCE(attribution_tags[:param_tag_key], '') <> ''
      ELSE size(attribution_tags) > 0
    END AS is_tagged
  FROM typed
)"""

RESOURCES = """resources AS (
  SELECT
    workspace_id, resource_type, billing_origin_product, resource_id,
    destination_model, model_provider,
    MAX_BY(resource_name,    IF(record_type = 'RETRACTION', NULL, usage_end_time)) AS resource_name,
    MAX_BY(owner,            IF(record_type = 'RETRACTION', NULL, usage_end_time)) AS owner,
    MAX_BY(attribution_tags, IF(record_type = 'RETRACTION', NULL, usage_end_time)) AS latest_tags,
    MAX_BY(is_tagged,        IF(record_type = 'RETRACTION', NULL, usage_end_time)) AS latest_is_tagged,
    BOOL_AND(taggable)                                   AS taggable,
    MAX(serving_type)                                    AS serving_type,
    MAX(warehouse_id)                                    AS warehouse_id,
    MIN(usage_date)                                      AS first_seen,
    MAX(usage_date)                                      AS last_seen,
    SUM(list_cost_usd)                                   AS list_cost_usd,
    SUM(IF(is_tagged, list_cost_usd, 0))                 AS tagged_cost_usd,
    slice(array_sort(collect_set(IF(is_tagged, NULL, owner))), 1, 5) AS untagged_principals
  FROM flagged
  GROUP BY ALL
),
resources_status AS (
  SELECT *,
    CASE
      WHEN NOT taggable                                        THEN 'NOT TAGGABLE (Genie)'
      WHEN latest_is_tagged AND tagged_cost_usd >= list_cost_usd THEN 'TAGGED'
      WHEN latest_is_tagged                                    THEN 'TAGGED (earlier usage untagged)'
      WHEN tagged_cost_usd > 0                                 THEN 'UNTAGGED (was tagged earlier)'
      ELSE 'UNTAGGED'
    END AS tag_status,
    IF(taggable, list_cost_usd - tagged_cost_usd, 0)            AS untagged_cost_usd
  FROM resources
)"""

WAREHOUSES = f"""warehouses AS (
  SELECT workspace_id, warehouse_id, warehouse_name
  FROM system.compute.warehouses
  WHERE {WS_FILTER.format(col='workspace_id')}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time DESC) = 1
)"""

# Warehouse cost split across the statements that ran in each hour.
WH_ALLOC = f"""wh_hour_cost AS (
  SELECT workspace_id, usage_metadata.warehouse_id AS warehouse_id,
         date_trunc('HOUR', usage_start_time) AS hour,
         SUM(list_cost_usd) AS hour_cost_usd
  FROM priced
  WHERE billing_origin_product = 'SQL' AND usage_metadata.warehouse_id IS NOT NULL
  GROUP BY ALL
),
q AS (
  SELECT
    workspace_id, compute.warehouse_id AS warehouse_id,
    date_trunc('HOUR', start_time) AS hour,
    statement_id, executed_by,
    CASE
      WHEN query_source.genie_space_id IS NOT NULL            THEN 'Genie space'
      -- Genie SQL with no space id (for example Genie One / Genie Code)
      WHEN client_application ILIKE '%genie%'                 THEN 'Genie (no space id)'
      WHEN query_source.dashboard_id IS NOT NULL
        OR query_source.legacy_dashboard_id IS NOT NULL       THEN 'Dashboard'
      WHEN query_source.alert_id IS NOT NULL                  THEN 'Alert'
      WHEN query_source.job_info.job_id IS NOT NULL           THEN 'Job'
      WHEN query_source.pipeline_info.pipeline_id IS NOT NULL THEN 'Pipeline'
      WHEN query_source.notebook_id IS NOT NULL               THEN 'Notebook'
      WHEN query_source.sql_query_id IS NOT NULL              THEN 'Saved query'
      WHEN client_application IS NULL OR client_application = 'unknown' THEN 'Other / ad hoc'
      ELSE concat('Client: ', client_application)
    END AS source_type,
    COALESCE(query_source.genie_space_id, query_source.dashboard_id,
             query_source.legacy_dashboard_id, query_source.alert_id,
             query_source.job_info.job_id, query_source.pipeline_info.pipeline_id,
             query_source.notebook_id, query_source.sql_query_id) AS source_id,
    statement_text, total_duration_ms,
    COALESCE(total_task_duration_ms, 0) AS task_ms
  FROM system.query.history
  WHERE compute.warehouse_id IS NOT NULL
    AND start_time >= :time_range.min AND start_time < date_add(:time_range.max, 1)
    AND {WS_FILTER.format(col='workspace_id')}
),
hour_ms AS (
  SELECT workspace_id, warehouse_id, hour, SUM(task_ms) AS hour_task_ms, COUNT(*) AS hour_statements
  FROM q GROUP BY ALL
),
alloc AS (
  SELECT q.*,
         -- hours where every statement had 0 task time: split cost evenly
         c.hour_cost_usd * IF(h.hour_task_ms > 0, q.task_ms / h.hour_task_ms, 1 / h.hour_statements)
           AS allocated_cost_usd
  FROM q
  JOIN hour_ms h      USING (workspace_id, warehouse_id, hour)
  JOIN wh_hour_cost c USING (workspace_id, warehouse_id, hour)
)"""

GENIE = """genie AS (
  SELECT
    workspace_id, usage_date,
    usage_metadata.genie.surface  AS genie_surface,
    usage_metadata.genie.agent_id AS genie_space_id,
    usage_metadata.genie.channel  AS channel,
    identity_metadata.run_as      AS run_as,
    -- service principals appear as an application id (UUID); they get no free allowance
    IF(identity_metadata.run_as RLIKE '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
       'Service principal', 'User') AS principal_type,
    IF(sku_name =  'GENIE_FREE_USAGE', usage_quantity, 0) AS free_dbus,
    IF(sku_name <> 'GENIE_FREE_USAGE', usage_quantity, 0) AS billed_dbus,
    list_cost_usd                                         AS billed_cost_usd
  FROM priced
  WHERE billing_origin_product = 'GENIE'
)"""


def with_(*ctes):
    return "WITH " + ",\n".join(ctes) + "\n"


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------
DATASETS = []


def dataset(name, display, sql, params=(P_TIME, P_WS)):
    DATASETS.append({
        "name": name, "displayName": display,
        "queryLines": [l + "\n" for l in sql.strip().split("\n")],
        **({"parameters": list(params)} if params else {}),
    })


dataset("ds_workspaces", "Workspace picker", """
SELECT 'ALL' AS workspace_id
UNION ALL
SELECT DISTINCT workspace_id FROM system.billing.usage WHERE usage_date >= current_date() - 90
""", params=())

dataset("ds_tag_keys", "Tag key picker", f"""
SELECT 'ANY' AS tag_key
UNION ALL
SELECT DISTINCT k AS tag_key
FROM (
  SELECT explode(map_keys({PLATFORM_TAG_FILTER})) AS k
  FROM system.billing.usage
  WHERE usage_date >= current_date() - 90
    AND COALESCE(product_features.is_serverless, TRUE)
    AND billing_origin_product <> 'ALL_PURPOSE'
)
""", params=())

ALL3 = (P_TIME, P_WS, P_TAG)

dataset("ds_kpis", "Coverage KPIs", with_(PRICED, TYPED, RESOURCES) + """
SELECT
  ROUND(SUM(list_cost_usd), 2)                                         AS total_cost_usd,
  ROUND(SUM(IF(taggable, list_cost_usd, 0)), 2)                        AS taggable_cost_usd,
  ROUND(SUM(tagged_cost_usd), 2)                                       AS tagged_cost_usd,
  ROUND(SUM(untagged_cost_usd), 2)                                     AS untagged_cost_usd,
  SUM(tagged_cost_usd) / NULLIF(SUM(IF(taggable, list_cost_usd, 0)), 0) AS tagged_pct,
  COUNT_IF(taggable)                                                   AS taggable_resources,
  COUNT_IF(tag_status LIKE 'UNTAGGED%')                                AS untagged_resources,
  ROUND(SUM(IF(NOT taggable, list_cost_usd, 0)), 2)                    AS genie_cost_usd
FROM resources_status
""", ALL3)

dataset("ds_coverage_by_type", "Coverage by resource type", with_(PRICED, TYPED, RESOURCES) + """
SELECT
  resource_type,
  COUNT(*)                                                    AS resources,
  COUNT_IF(tag_status LIKE 'UNTAGGED%')                       AS untagged_resources,
  ROUND(SUM(list_cost_usd), 2)                                AS list_cost_usd,
  ROUND(SUM(tagged_cost_usd), 2)                              AS tagged_cost_usd,
  ROUND(SUM(untagged_cost_usd), 2)                            AS untagged_cost_usd,
  IF(BOOL_AND(taggable), SUM(tagged_cost_usd) / NULLIF(SUM(list_cost_usd), 0), NULL) AS tagged_pct
FROM resources_status
GROUP BY resource_type
ORDER BY untagged_cost_usd DESC, list_cost_usd DESC
""", ALL3)

dataset("ds_coverage_type_long", "Cost by resource type and tag status", with_(PRICED, TYPED) + """
SELECT
  resource_type,
  CASE WHEN NOT taggable THEN 'Not taggable (Genie)' WHEN is_tagged THEN 'Tagged' ELSE 'Untagged' END AS tag_group,
  ROUND(SUM(list_cost_usd), 2) AS list_cost_usd
FROM flagged
GROUP BY ALL
""", ALL3)

dataset("ds_daily_coverage", "Daily cost by tag status", with_(PRICED, TYPED) + """
SELECT
  usage_date,
  CASE WHEN NOT taggable THEN 'Not taggable (Genie)' WHEN is_tagged THEN 'Tagged' ELSE 'Untagged' END AS tag_group,
  ROUND(SUM(list_cost_usd), 2) AS list_cost_usd
FROM flagged
GROUP BY ALL
ORDER BY usage_date
""", ALL3)

dataset("ds_resources", "Serverless resources (detail)",
        with_(PRICED, TYPED, RESOURCES, WAREHOUSES) + """
SELECT
  r.tag_status, r.resource_type, r.workspace_id, r.resource_id,
  COALESCE(w.warehouse_name, r.resource_name)      AS resource_name,
  r.owner,
  array_join(r.untagged_principals, ', ')          AS untagged_principals,
  to_json(r.latest_tags)                           AS latest_tags,
  ROUND(r.list_cost_usd, 2)                        AS list_cost_usd,
  ROUND(r.untagged_cost_usd, 2)                    AS untagged_cost_usd,
  r.first_seen, r.last_seen
FROM resources_status r
LEFT JOIN warehouses w ON r.workspace_id = w.workspace_id AND r.warehouse_id = w.warehouse_id
WHERE r.list_cost_usd > 0 OR r.tag_status LIKE 'UNTAGGED%'
ORDER BY r.tag_status LIKE 'UNTAGGED%' DESC, r.untagged_cost_usd DESC, r.list_cost_usd DESC
LIMIT 1000
""", ALL3)

dataset("ds_untagged_owners", "Untagged cost by owner", with_(PRICED, TYPED, RESOURCES) + """
SELECT
  COALESCE(owner, '(unknown)')                 AS owner,
  COUNT_IF(tag_status LIKE 'UNTAGGED%')        AS untagged_resources,
  ROUND(SUM(untagged_cost_usd), 2)             AS untagged_cost_usd
FROM resources_status
WHERE taggable AND untagged_cost_usd > 0
GROUP BY ALL
ORDER BY untagged_cost_usd DESC
LIMIT 15
""", ALL3)

dataset("ds_model_serving", "Model Serving by provider / model", with_(PRICED, TYPED, RESOURCES) + """
SELECT
  COALESCE(model_provider, 'Databricks / custom')  AS model_provider,
  COALESCE(destination_model, '(not via Unity Gateway)') AS destination_model,
  resource_type, resource_id AS endpoint, serving_type, tag_status, owner,
  array_join(untagged_principals, ', ')            AS untagged_principals,
  ROUND(list_cost_usd, 2)                          AS list_cost_usd,
  ROUND(untagged_cost_usd, 2)                      AS untagged_cost_usd
FROM resources_status
WHERE billing_origin_product = 'MODEL_SERVING'
ORDER BY list_cost_usd DESC
LIMIT 500
""", ALL3)

dataset("ds_genie_kpis", "Genie KPIs", with_(PRICED, GENIE) + """
SELECT
  ROUND(SUM(billed_cost_usd), 2)                                     AS billed_cost_usd,
  ROUND(SUM(billed_dbus), 1)                                         AS billed_dbus,
  ROUND(SUM(free_dbus), 1)                                           AS free_dbus,
  COUNT(DISTINCT IF(principal_type = 'User', run_as, NULL))          AS users,
  ROUND(SUM(IF(principal_type = 'Service principal', billed_cost_usd, 0)), 2) AS sp_billed_cost_usd,
  SUM(IF(genie_surface = 'GENIE_CODE', billed_cost_usd, 0)) / NULLIF(SUM(billed_cost_usd), 0) AS genie_code_share
FROM genie
""")

dataset("ds_genie_daily", "Genie daily usage by surface", with_(PRICED, GENIE) + """
SELECT usage_date, genie_surface,
       ROUND(SUM(billed_cost_usd), 2) AS billed_cost_usd,
       ROUND(SUM(billed_dbus), 1)     AS billed_dbus,
       ROUND(SUM(free_dbus), 1)       AS free_dbus
FROM genie
GROUP BY ALL
ORDER BY usage_date
""")

dataset("ds_genie_surface", "Genie free vs billed by surface", with_(PRICED, GENIE) + """
SELECT genie_surface, 'Free DBUs' AS usage_type, ROUND(SUM(free_dbus), 1) AS dbus FROM genie GROUP BY ALL
UNION ALL
SELECT genie_surface, 'Billed DBUs', ROUND(SUM(billed_dbus), 1) FROM genie GROUP BY ALL
""")

dataset("ds_genie_users", "Genie usage by user", with_(PRICED, GENIE) + """
SELECT
  run_as, principal_type,
  concat_ws(', ', array_sort(collect_set(genie_surface)))  AS surfaces,
  ROUND(SUM(free_dbus), 1)                                 AS free_dbus,
  ROUND(SUM(billed_dbus), 1)                               AS billed_dbus,
  ROUND(SUM(billed_cost_usd), 2)                           AS billed_cost_usd
FROM genie
GROUP BY ALL
ORDER BY billed_cost_usd DESC, free_dbus DESC
LIMIT 200
""")

dataset("ds_genie_spaces", "Genie spaces: AI + warehouse cost",
        with_(PRICED, GENIE, WH_ALLOC) + """,
ai AS (
  SELECT workspace_id, genie_space_id,
         SUM(free_dbus + billed_dbus) AS ai_dbus, SUM(billed_cost_usd) AS ai_cost_usd,
         COUNT(DISTINCT run_as) AS ai_users
  FROM genie WHERE genie_space_id IS NOT NULL GROUP BY ALL
),
wh AS (
  SELECT workspace_id, source_id AS genie_space_id,
         SUM(allocated_cost_usd) AS warehouse_cost_usd, COUNT(*) AS statements
  FROM alloc WHERE source_type = 'Genie space' GROUP BY ALL
)
SELECT
  workspace_id, genie_space_id,
  COALESCE(ai.ai_users, 0)                               AS users,
  ROUND(COALESCE(ai.ai_dbus, 0), 1)                      AS ai_dbus,
  ROUND(COALESCE(ai.ai_cost_usd, 0), 2)                  AS ai_billed_cost_usd,
  COALESCE(wh.statements, 0)                             AS sql_statements,
  ROUND(COALESCE(wh.warehouse_cost_usd, 0), 2)           AS warehouse_cost_usd,
  ROUND(COALESCE(ai.ai_cost_usd, 0) + COALESCE(wh.warehouse_cost_usd, 0), 2) AS total_cost_usd
FROM ai FULL OUTER JOIN wh USING (workspace_id, genie_space_id)
ORDER BY total_cost_usd DESC, ai_dbus DESC
LIMIT 200
""")

dataset("ds_wh_kpis", "Warehouse attribution KPIs", with_(PRICED, WH_ALLOC) + """
SELECT
  ROUND((SELECT SUM(hour_cost_usd) FROM wh_hour_cost), 2)                     AS warehouse_cost_usd,
  ROUND(SUM(allocated_cost_usd), 2)                                           AS attributed_cost_usd,
  1 - SUM(allocated_cost_usd) / NULLIF((SELECT SUM(hour_cost_usd) FROM wh_hour_cost), 0) AS idle_pct,
  SUM(IF(source_type LIKE 'Genie%', allocated_cost_usd, 0))
    / NULLIF(SUM(allocated_cost_usd), 0)                                      AS genie_share,
  COUNT(*)                                                                    AS statements
FROM alloc
""")

dataset("ds_wh_by_source", "Warehouse cost by source", with_(PRICED, WH_ALLOC) + """
SELECT source_type,
       COUNT(*)                          AS statements,
       COUNT(DISTINCT executed_by)       AS users,
       ROUND(SUM(allocated_cost_usd), 2) AS allocated_cost_usd
FROM alloc
GROUP BY source_type
ORDER BY allocated_cost_usd DESC
""")

dataset("ds_wh_utilization", "Warehouse cost: attributed vs idle", with_(PRICED, WH_ALLOC, WAREHOUSES) + """,
per_wh AS (
  SELECT c.workspace_id, c.warehouse_id,
         SUM(c.hour_cost_usd)                         AS warehouse_cost_usd,
         SUM(IF(h.hour IS NULL, c.hour_cost_usd, 0))  AS idle_cost_usd
  FROM wh_hour_cost c
  LEFT JOIN hour_ms h USING (workspace_id, warehouse_id, hour)
  GROUP BY ALL
)
SELECT COALESCE(w.warehouse_name, p.warehouse_id) AS warehouse,
       p.workspace_id, p.warehouse_id,
       'Attributed to queries' AS cost_type, ROUND(p.warehouse_cost_usd - p.idle_cost_usd, 2) AS cost_usd,
       ROUND(p.warehouse_cost_usd, 2) AS warehouse_cost_usd
FROM per_wh p LEFT JOIN warehouses w USING (workspace_id, warehouse_id)
UNION ALL
SELECT COALESCE(w.warehouse_name, p.warehouse_id), p.workspace_id, p.warehouse_id,
       'Idle (no queries that hour)', ROUND(p.idle_cost_usd, 2), ROUND(p.warehouse_cost_usd, 2)
FROM per_wh p LEFT JOIN warehouses w USING (workspace_id, warehouse_id)
ORDER BY warehouse_cost_usd DESC
LIMIT 60
""")

dataset("ds_wh_drivers", "Top cost drivers (source x warehouse)", with_(PRICED, WH_ALLOC, WAREHOUSES) + """
SELECT
  a.source_type, a.source_id,
  COALESCE(w.warehouse_name, a.warehouse_id)                      AS warehouse,
  a.workspace_id,
  COUNT(*)                                                        AS statements,
  array_join(slice(array_sort(collect_set(a.executed_by)), 1, 5), ', ') AS top_users,
  ROUND(SUM(a.allocated_cost_usd), 2)                             AS allocated_cost_usd,
  ROUND(SUM(a.total_duration_ms) / 60000, 1)                      AS total_minutes
FROM alloc a LEFT JOIN warehouses w USING (workspace_id, warehouse_id)
GROUP BY ALL
ORDER BY allocated_cost_usd DESC
LIMIT 100
""")

dataset("ds_wh_statements", "Most expensive statements", with_(PRICED, WH_ALLOC, WAREHOUSES) + """
SELECT
  a.source_type, a.source_id,
  COALESCE(w.warehouse_name, a.warehouse_id)   AS warehouse,
  a.executed_by, a.statement_id,
  ROUND(a.allocated_cost_usd, 4)               AS allocated_cost_usd,
  ROUND(a.total_duration_ms / 1000, 1)         AS duration_s,
  left(regexp_replace(a.statement_text, '\\\\s+', ' '), 400) AS statement_preview
FROM alloc a LEFT JOIN warehouses w USING (workspace_id, warehouse_id)
ORDER BY a.allocated_cost_usd DESC
LIMIT 100
""")

# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------
USD = {"type": "number-currency", "currencyCode": "USD", "abbreviation": "compact",
       "decimalPlaces": {"type": "max", "places": 2}}
PCT = {"type": "number-percent", "decimalPlaces": {"type": "max", "places": 1}}
NUM = {"type": "number-plain", "abbreviation": "compact", "decimalPlaces": {"type": "max", "places": 1}}

TAG_COLORS = [{"value": "Tagged", "color": "#00A972"},
              {"value": "Untagged", "color": "#FF3621"},
              {"value": "Not taggable (Genie)", "color": "#919191"}]


def pos(x, y, w, h):
    return {"x": x, "y": y, "width": w, "height": h}


def text(name, lines, p):
    return {"widget": {"name": name, "multilineTextboxSpec": {"lines": lines}}, "position": p}


def q(ds, fields, disagg=True):
    return [{"name": "main_query", "query": {
        "datasetName": ds, "disaggregated": disagg,
        "fields": [{"name": n, "expression": e} for n, e in fields]}}]


def frame(title, desc=None):
    f = {"showTitle": True, "title": title}
    if desc:
        f.update({"showDescription": True, "description": desc})
    return f


def counter(name, ds, col, title, fmt, p, desc=None):
    return {"widget": {"name": name, "queries": q(ds, [(col, f"`{col}`")]),
                       "spec": {"version": 2, "widgetType": "counter",
                                "encodings": {"value": {"fieldName": col, "displayName": title, "format": fmt}},
                                "frame": frame(title, desc)}}, "position": p}


def table(name, ds, cols, title, p, desc=None):
    """cols: list of (field, display, format or None)"""
    columns = []
    for f, disp, fmt in cols:
        c = {"fieldName": f, "displayName": disp}
        if fmt:
            c["numberFormat"] = fmt
        columns.append(c)
    return {"widget": {"name": name, "queries": q(ds, [(f, f"`{f}`") for f, _, _ in cols]),
                       "spec": {"version": 2, "widgetType": "table",
                                "encodings": {"columns": columns},
                                "frame": frame(title, desc)}}, "position": p}


def bar(name, ds, x, ys, title, p, color=None, desc=None, x_type="categorical",
        horizontal=False, color_map=None, agg=False):
    """ys: list of (field, display). color: (field, display) for stacked long-format."""
    fields = [(x, f"`{x}`")]
    if agg:
        fields += [(f"sum({f})", f"SUM(`{f}`)") for f, _ in ys]
        ys = [(f"sum({f})", d) for f, d in ys]
    else:
        fields += [(f, f"`{f}`") for f, _ in ys]
    if color:
        fields.append((color[0], f"`{color[0]}`"))
    xe = {"fieldName": x, "scale": {"type": x_type}, "displayName": x.replace("_", " ")}
    if len(ys) == 1:
        ye = {"fieldName": ys[0][0], "scale": {"type": "quantitative"}, "displayName": ys[0][1]}
    else:
        ye = {"scale": {"type": "quantitative"},
              "fields": [{"fieldName": f, "displayName": d} for f, d in ys]}
    enc = {"x": ye, "y": xe} if horizontal else {"x": xe, "y": ye}
    if color:
        ce = {"fieldName": color[0], "scale": {"type": "categorical"}, "displayName": color[1]}
        if color_map:
            ce["scale"]["mappings"] = color_map
        enc["color"] = ce
    return {"widget": {"name": name, "queries": q(ds, fields, disagg=not agg),
                       "spec": {"version": 3, "widgetType": "bar", "encodings": enc,
                                "frame": frame(title, desc)}}, "position": p}


def line(name, ds, x, y, color, title, p, desc=None):
    fields = [(x, f"`{x}`"), (f"sum({y})", f"SUM(`{y}`)"), (color, f"`{color}`")]
    enc = {"x": {"fieldName": x, "scale": {"type": "temporal"}, "displayName": "Date"},
           "y": {"fieldName": f"sum({y})", "scale": {"type": "quantitative"}, "displayName": y.replace("_", " ")},
           "color": {"fieldName": color, "scale": {"type": "categorical"}, "displayName": color.replace("_", " ")}}
    return {"widget": {"name": name, "queries": q(ds, fields, disagg=False),
                       "spec": {"version": 3, "widgetType": "line", "encodings": enc,
                                "frame": frame(title, desc)}}, "position": p}


def param_filter(name, ds, field, keyword, title, targets, p):
    """Single-select whose options come from `ds.field` and that sets :keyword on targets."""
    queries = [{"name": f"{name}_options", "query": {
        "datasetName": ds, "disaggregated": False,
        "fields": [{"name": field, "expression": f"`{field}`"}]}}]
    fields = [{"fieldName": field, "displayName": title, "queryName": f"{name}_options"}]
    for t in targets:
        qn = f"{name}_{t}"
        queries.append({"name": qn, "query": {"datasetName": t, "disaggregated": False,
                                              "parameters": [{"name": keyword, "keyword": keyword}]}})
        fields.append({"parameterName": keyword, "queryName": qn})
    return {"widget": {"name": name, "queries": queries,
                       "spec": {"version": 2, "widgetType": "filter-single-select",
                                "encodings": {"fields": fields}, "frame": frame(title)}},
            "position": p}


def date_filter(name, targets, p):
    queries, fields = [], []
    for t in targets:
        qn = f"{name}_{t}"
        queries.append({"name": qn, "query": {"datasetName": t, "disaggregated": False,
                                              "parameters": [{"name": "time_range", "keyword": "time_range"}]}})
        fields.append({"parameterName": "time_range", "queryName": qn})
    return {"widget": {"name": name, "queries": queries,
                       "spec": {"version": 2, "widgetType": "filter-date-range-picker",
                                "encodings": {"fields": fields}, "frame": frame("Date range")}},
            "position": p}


def filters_row(prefix, targets, with_tag, y=2):
    """Date range + workspace (+ tag key) filters across the top of a page."""
    tag_targets = [t for t in targets if any(p["keyword"] == "param_tag_key"
                                             for p in next(d for d in DATASETS if d["name"] == t).get("parameters", []))]
    row = [date_filter(f"{prefix}_dates", targets, pos(0, y, 2, 1)),
           param_filter(f"{prefix}_ws", "ds_workspaces", "workspace_id", "param_workspace",
                        "Workspace", targets, pos(2, y, 2, 1))]
    if with_tag:
        row.append(param_filter(f"{prefix}_tag", "ds_tag_keys", "tag_key", "param_tag_key",
                                "Required tag key (ANY = any tag)", tag_targets, pos(4, y, 2, 1)))
    return row


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
pages = []

# 1. Overview ---------------------------------------------------------------
t1 = ["ds_kpis", "ds_coverage_type_long", "ds_daily_coverage", "ds_coverage_by_type"]
pages.append({"name": "overview", "displayName": "Tag coverage overview", "pageType": "PAGE_TYPE_CANVAS", "layout": [
    text("ov_title", [
        "## Serverless tag coverage\n\n",
        "How much serverless spend carries an attribution tag, by resource type. Costs are **estimated list price** "
        "(no negotiated discounts), so use them for coverage, not invoice matching. Platform-added keys "
        "(EndpointId, ServingType, BudgetPolicyId, databricks-*) don't count as tags. "
        "Genie usage can't be tagged per team, so it is shown as *not taggable* and excluded from coverage %.\n"],
        pos(0, 0, 6, 2)),
    *filters_row("ov", t1, with_tag=True),
    counter("ov_pct", "ds_kpis", "tagged_pct", "Tagged spend (taggable)", PCT, pos(0, 3, 1, 2)),
    counter("ov_untagged", "ds_kpis", "untagged_cost_usd", "Untagged spend", USD, pos(1, 3, 1, 2)),
    counter("ov_untagged_n", "ds_kpis", "untagged_resources", "Untagged resources", NUM, pos(2, 3, 1, 2)),
    counter("ov_total", "ds_kpis", "total_cost_usd", "Serverless spend", USD, pos(3, 3, 1, 2)),
    counter("ov_taggable_n", "ds_kpis", "taggable_resources", "Taggable resources", NUM, pos(4, 3, 1, 2)),
    counter("ov_genie", "ds_kpis", "genie_cost_usd", "Genie (not taggable)", USD, pos(5, 3, 1, 2),
            "Attribute by user and space on the Genie page"),
    bar("ov_daily", "ds_daily_coverage", "usage_date", [("list_cost_usd", "List cost (USD)")],
        "Daily serverless spend by tag status", pos(0, 5, 6, 5), color=("tag_group", "Tag status"),
        x_type="temporal", color_map=TAG_COLORS, agg=True),
    bar("ov_by_type", "ds_coverage_type_long", "resource_type", [("list_cost_usd", "List cost (USD)")],
        "Spend by resource type and tag status", pos(0, 10, 3, 7), color=("tag_group", "Tag status"),
        horizontal=True, color_map=TAG_COLORS, agg=True),
    table("ov_table", "ds_coverage_by_type", [
        ("resource_type", "Resource type", None), ("resources", "Resources", None),
        ("untagged_resources", "Untagged", None), ("list_cost_usd", "Spend", USD),
        ("untagged_cost_usd", "Untagged spend", USD), ("tagged_pct", "Tagged %", PCT)],
        "Coverage by resource type", pos(3, 10, 3, 7)),
]})

# 2. Untagged resources ------------------------------------------------------
t2 = ["ds_resources", "ds_untagged_owners"]
pages.append({"name": "untagged", "displayName": "Untagged resources", "pageType": "PAGE_TYPE_CANVAS", "layout": [
    text("un_title", [
        "## What to go fix\n\n",
        "Every serverless resource with spend in the range, untagged first. **Owner** is the run-as / owner on the most "
        "recent usage. **Untagged principals** lists who ran untagged usage (useful for shared endpoints). "
        "*TAGGED (earlier usage untagged)* means a tag was added during the range.\n"], pos(0, 0, 6, 2)),
    *filters_row("un", t2, with_tag=True),
    bar("un_owners", "ds_untagged_owners", "owner", [("untagged_cost_usd", "Untagged spend (USD)")],
        "Top 15 owners by untagged spend", pos(0, 3, 6, 5), horizontal=True),
    table("un_table", "ds_resources", [
        ("tag_status", "Tag status", None), ("resource_type", "Type", None),
        ("resource_name", "Name", None), ("resource_id", "ID", None),
        ("owner", "Owner", None), ("untagged_principals", "Untagged principals", None),
        ("untagged_cost_usd", "Untagged spend", USD), ("list_cost_usd", "Spend", USD),
        ("latest_tags", "Latest tags", None), ("workspace_id", "Workspace", None),
        ("last_seen", "Last seen", None)],
        "Serverless resources (untagged first, top 1,000)", pos(0, 8, 6, 10)),
]})

# 3. Model Serving & AI -------------------------------------------------------
t3 = ["ds_model_serving"]
pages.append({"name": "ai", "displayName": "Model Serving & AI", "pageType": "PAGE_TYPE_CANVAS", "layout": [
    text("ai_title", [
        "## Model Serving by provider and model\n\n",
        "Provider is parsed from the SKU (for example *_ANTHROPIC_MODEL_SERVING). Destination model is populated for "
        "Unity Gateway model services. Pay-per-token usage by many callers shows up under **Untagged principals**.\n"],
        pos(0, 0, 6, 2)),
    *filters_row("ai", t3, with_tag=True),
    bar("ai_provider", "ds_model_serving", "model_provider", [("list_cost_usd", "List cost (USD)")],
        "Spend by provider and tag status", pos(0, 3, 3, 6), color=("tag_status", "Tag status"), agg=True),
    bar("ai_model", "ds_model_serving", "destination_model", [("list_cost_usd", "List cost (USD)")],
        "Spend by destination model", pos(3, 3, 3, 6), horizontal=True, agg=True),
    table("ai_table", "ds_model_serving", [
        ("tag_status", "Tag status", None), ("endpoint", "Endpoint / service", None),
        ("model_provider", "Provider", None), ("destination_model", "Destination model", None),
        ("serving_type", "Serving type", None), ("owner", "Owner", None),
        ("untagged_principals", "Untagged principals", None),
        ("untagged_cost_usd", "Untagged spend", USD), ("list_cost_usd", "Spend", USD)],
        "Model Serving endpoints and services", pos(0, 9, 6, 9)),
]})

# 4. Genie -------------------------------------------------------------------
t4 = ["ds_genie_kpis", "ds_genie_daily", "ds_genie_surface", "ds_genie_users", "ds_genie_spaces"]
pages.append({"name": "genie", "displayName": "Genie cost", "pageType": "PAGE_TYPE_CANVAS", "layout": [
    text("ge_title", [
        "## Genie cost\n\n",
        "Genie AI usage bills under `billing_origin_product = GENIE`: free usage on **GENIE_FREE_USAGE** (no list price), "
        "billed usage on the regional Serverless Real-Time Inference SKU. Each user gets **150 free DBUs per month**; "
        "until **Jan 31, 2027** Genie One and Genie Agents are free for users, and **service principals are always billed**. "
        "The SQL a Genie space generates runs on its warehouse and bills there; the space table adds that share.\n"],
        pos(0, 0, 6, 2)),
    *filters_row("ge", t4, with_tag=False),
    counter("ge_billed", "ds_genie_kpis", "billed_cost_usd", "Billed Genie AI cost", USD, pos(0, 3, 1, 2)),
    counter("ge_code", "ds_genie_kpis", "genie_code_share", "Share from Genie Code", PCT, pos(1, 3, 1, 2)),
    counter("ge_sp", "ds_genie_kpis", "sp_billed_cost_usd", "Billed to service principals", USD, pos(2, 3, 1, 2)),
    counter("ge_users", "ds_genie_kpis", "users", "Genie users", NUM, pos(3, 3, 1, 2)),
    counter("ge_free", "ds_genie_kpis", "free_dbus", "Free DBUs used", NUM, pos(4, 3, 1, 2)),
    counter("ge_billed_dbus", "ds_genie_kpis", "billed_dbus", "Billed DBUs", NUM, pos(5, 3, 1, 2)),
    line("ge_daily", "ds_genie_daily", "usage_date", "billed_cost_usd", "genie_surface",
         "Daily billed Genie cost by surface", pos(0, 5, 4, 5)),
    bar("ge_surface", "ds_genie_surface", "genie_surface", [("dbus", "DBUs")],
        "Free vs billed DBUs by surface", pos(4, 5, 2, 5), color=("usage_type", "Usage"), agg=True,
        color_map=[{"value": "Free DBUs", "color": "#8BCAE7"}, {"value": "Billed DBUs", "color": "#FF3621"}]),
    table("ge_spaces", "ds_genie_spaces", [
        ("genie_space_id", "Genie space", None), ("users", "Users", None),
        ("ai_dbus", "AI DBUs", NUM), ("ai_billed_cost_usd", "AI billed", USD),
        ("sql_statements", "SQL statements", None), ("warehouse_cost_usd", "Warehouse (alloc.)", USD),
        ("total_cost_usd", "Total est.", USD)],
        "Genie spaces: AI usage + allocated warehouse cost", pos(0, 10, 3, 8),
        "Warehouse cost is an allocation (see Warehouse query cost page)"),
    table("ge_users_t", "ds_genie_users", [
        ("run_as", "User / principal", None), ("principal_type", "Type", None),
        ("surfaces", "Surfaces", None), ("free_dbus", "Free DBUs", NUM),
        ("billed_dbus", "Billed DBUs", NUM), ("billed_cost_usd", "Billed", USD)],
        "Genie usage by user", pos(3, 10, 3, 8)),
]})

# 5. Warehouse query cost ----------------------------------------------------
t5 = ["ds_wh_kpis", "ds_wh_by_source", "ds_wh_utilization", "ds_wh_drivers", "ds_wh_statements"]
pages.append({"name": "warehouse", "displayName": "Warehouse query cost", "pageType": "PAGE_TYPE_CANVAS", "layout": [
    text("wh_title", [
        "## Which queries drive SQL warehouse cost\n\n",
        "Billing records warehouse cost per warehouse per hour, never per query. Each hour's cost is **allocated** across the "
        "statements that ran in it, weighted by task time (`system.query.history`). Hours with no statements are shown as "
        "**idle**. Treat per-query numbers as an estimate.\n"], pos(0, 0, 6, 2)),
    *filters_row("wh", t5, with_tag=False),
    counter("wh_total", "ds_wh_kpis", "warehouse_cost_usd", "SQL warehouse spend", USD, pos(0, 3, 1, 2)),
    counter("wh_attr", "ds_wh_kpis", "attributed_cost_usd", "Attributed to queries", USD, pos(1, 3, 1, 2)),
    counter("wh_idle", "ds_wh_kpis", "idle_pct", "Idle (no queries)", PCT, pos(2, 3, 1, 2),
            "Spend in hours with no statements"),
    counter("wh_genie", "ds_wh_kpis", "genie_share", "Share from Genie", PCT, pos(3, 3, 1, 2)),
    counter("wh_stmts", "ds_wh_kpis", "statements", "Statements", NUM, pos(4, 3, 2, 2)),
    bar("wh_source", "ds_wh_by_source", "source_type", [("allocated_cost_usd", "Allocated cost (USD)")],
        "Warehouse cost by source", pos(0, 5, 3, 6), horizontal=True),
    bar("wh_util", "ds_wh_utilization", "warehouse", [("cost_usd", "List cost (USD)")],
        "Top warehouses: attributed vs idle", pos(3, 5, 3, 6), color=("cost_type", "Cost"), horizontal=True, agg=True,
        color_map=[{"value": "Attributed to queries", "color": "#2272B4"},
                   {"value": "Idle (no queries that hour)", "color": "#FFAB00"}]),
    table("wh_drivers_t", "ds_wh_drivers", [
        ("source_type", "Source", None), ("source_id", "Source ID", None),
        ("warehouse", "Warehouse", None), ("statements", "Statements", None),
        ("top_users", "Users", None), ("allocated_cost_usd", "Allocated cost", USD),
        ("total_minutes", "Minutes", NUM)],
        "Top cost drivers (source x warehouse)", pos(0, 11, 6, 7)),
    table("wh_stmts_t", "ds_wh_statements", [
        ("allocated_cost_usd", "Allocated cost", USD), ("duration_s", "Duration (s)", NUM),
        ("source_type", "Source", None), ("warehouse", "Warehouse", None),
        ("executed_by", "Run by", None), ("statement_preview", "Statement", None),
        ("statement_id", "Statement ID", None)],
        "Most expensive statements (tune these first)", pos(0, 18, 6, 9)),
]})

# 6. About ---------------------------------------------------------------------
pages.append({"name": "about", "displayName": "About & definitions", "pageType": "PAGE_TYPE_CANVAS", "layout": [
    text("ab_text", [
        "## About this dashboard\n\n",
        "**Data sources (system tables only):** system.billing.usage, system.billing.list_prices, "
        "system.compute.warehouses, system.query.history. Import the .lvdash.json file into any workspace with access to "
        "these tables; no setup is needed. Viewers need SELECT on the system tables (account-wide billing data).\n\n",
        "**Serverless scope:** usage where `product_features.is_serverless` is TRUE or NULL (NULL for serverless-only "
        "products such as Model Serving, AI Search, AI Runtime and Genie), excluding ALL_PURPOSE.\n\n",
        "**Tagged:** the billing record carries at least one non-platform tag, or the selected *Required tag key* with a "
        "non-empty value. Platform keys ignored: EndpointId, ServingType, BudgetPolicyId, MONITOR_EXPERIMENT_ID, "
        "propagate_platform_channel, DataClassification, Predictive Optimization, LakehouseMonitoring*, and keys starting "
        "with DatabricksScgw, databricks. or databricks-.\n\n",
        "**Cost:** usage_quantity x effective list price (USD). Excludes negotiated discounts and credits. Free SKUs "
        "(for example GENIE_FREE_USAGE) are $0.\n\n",
        "**Genie:** not taggable per team (only databricks-product: genie). Spaces are identified by "
        "usage_metadata.genie.agent_id; Genie Code and Genie One are attributed per user. Service principals are "
        "detected by an application-id (UUID) run_as.\n\n",
        "**Warehouse allocation:** hourly warehouse cost x (statement task time / all task time that hour). Statements "
        "are assigned to the hour they started. Idle = hours with spend and no statements; partial-hour idle time is "
        "included in attributed cost.\n\n",
        "**Freshness:** billing data typically lands within a few hours.\n"], pos(0, 0, 6, 12)),
]})

dash = {"datasets": DATASETS, "pages": pages,
        "uiSettings": {"theme": {"widgetHeaderAlignment": "LEFT"}}}
OUT.write_text(json.dumps(dash, indent=2))
print(f"wrote {OUT} ({len(DATASETS)} datasets, {len(pages)} pages)")
