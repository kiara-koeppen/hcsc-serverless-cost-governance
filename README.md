# Serverless Cost Governance: tag coverage, Genie cost, and query cost drivers

SQL and an AI/BI dashboard that answer three FinOps questions about serverless spend on Databricks:

1. **Tag coverage.** How much serverless spend carries an attribution tag, and which resources (and owners) are untagged?
2. **Genie cost.** What does Genie cost, by product surface, user, and Genie space, free versus billed?
3. **Query cost drivers.** Which queries, dashboards, Genie spaces, jobs, and BI tools drive SQL warehouse cost?

Everything reads **only GA system tables**, so the dashboard imports into any Unity Catalog workspace with no setup,
no custom tables, and no jobs.

## Contents

| Asset | What it is |
|---|---|
| `dashboard/serverless_cost_governance.lvdash.json` | The AI/BI dashboard. Import this file. |
| `sql/serverless_tag_coverage.sql` | Tag coverage audit (v2): coverage summary by resource type, then every resource, untagged first |
| `sql/genie_cost_and_query_attribution.sql` | Genie cost by surface / space / user, warehouse cost split across queries, total cost per Genie space |
| `dashboard/build_dashboard.py` | Generates the `.lvdash.json` from shared SQL fragments (edit here, not in the JSON) |
| `tests/` | Static + live tests for the dashboard, and a runner for the multi-statement SQL scripts |

## Architecture

```
                      system tables (GA, read-only)
 ┌───────────────────────────────────────────────────────────────────────┐
 │ system.billing.usage ─┬─ list_prices ──> estimated list cost (USD)      │
 │   (global)            │                                                 │
 │                       ├─> serverless filter + platform-tag removal      │
 │                       │     └─> per resource: tagged / untagged / Genie │
 │                       ├─> GENIE rows ──> surface / space / user          │
 │                       └─> SQL warehouse cost per warehouse-hour ─┐       │
 │ system.query.history ──> statements per warehouse-hour ──────────┴─> allocation
 │   (regional)               (source: Genie space, dashboard, job, ...)     │
 │ system.compute.warehouses ──> warehouse names                            │
 └───────────────────────────────────────────────────────────────────────┘
                 │
                 ▼
   AI/BI dashboard (6 pages) or SQL editor (2 scripts)
```

**How the numbers are built**

- **Serverless scope.** Usage where `product_features.is_serverless` is TRUE *or NULL*. It is NULL for
  serverless-only products such as Model Serving, AI Search, AI Runtime, and Genie, so filtering on `= true` misses
  most serverless spend. `ALL_PURPOSE` (classic) is excluded.
- **Tagged.** The billing record carries at least one *attribution* tag, or a specific required key if you pick one.
  Keys the platform adds (`EndpointId`, `ServingType`, `BudgetPolicyId`, `databricks-*`, `DatabricksScgw*`,
  `LakehouseMonitoring*`, and others) identify the resource, not who pays, so they don't count.
- **Cost.** `usage_quantity x effective list price` from `system.billing.list_prices`, in USD. Excludes negotiated
  discounts and credits, so use it for coverage and ranking, not invoice matching. Free SKUs are $0.
- **Genie.** All Genie AI usage has `billing_origin_product = 'GENIE'`. Free usage is on `GENIE_FREE_USAGE` (no list
  price, visible from 2026-07-20); billed usage is on the regional Serverless Real-Time Inference SKU.
  `usage_metadata.genie.surface` is `GENIE_CODE`, `GENIE_ONE`, or `GENIE_AGENTS`; `agent_id` is the Genie space.
  Genie usage only carries the tag `databricks-product: genie`, so it can't be tagged per team: attribute it by user
  and space instead. The dashboard shows it as *not taggable* and leaves it out of coverage %.
- **Warehouse query cost (allocation).** Billing records warehouse cost per warehouse per hour, not per query. Each
  hour's cost is split across the statements that started in that hour, weighted by `total_task_duration_ms`.
  Hours with spend and no statements are reported as **idle**. Per-query numbers are an estimate.

Genie pricing facts (150 free DBUs per user per month, Genie One and Genie Agents free for users until
2027-01-31, service principals always billed, 25% promotional discount already in `usage_quantity`) are from the
Azure Databricks documentation page *Monitor and understand your Genie cost*.

## Dashboard pages

| Page | What it shows |
|---|---|
| Tag coverage overview | Tagged % of taggable spend, untagged spend and resources, daily spend by tag status, spend and coverage by resource type |
| Untagged resources | Top 15 owners by untagged spend; every resource (untagged first) with owner, untagged principals, latest tags |
| Model Serving & AI | Spend by model provider and destination model; endpoint-level tag status and who ran untagged usage |
| Genie cost | Billed cost, Genie Code share, service principal spend, free vs billed DBUs by surface, per user, per space (AI + allocated warehouse) |
| Warehouse query cost | Warehouse spend, attributed vs idle, Genie share, cost by source, top drivers, 100 most expensive statements |
| About & definitions | The definitions above |

Each page has a **date range** filter (default last 30 days) and a **workspace** filter (default ALL). Coverage pages
also have **required tag key** (default ANY), whose options are the attribution tag keys found in your billing data.

## Prerequisites

- A Unity Catalog-enabled workspace with system tables enabled.
- A SQL warehouse (serverless or pro) to run the dashboard or scripts.
- Read access to the system tables used:
  ```sql
  GRANT USE CATALOG ON CATALOG system TO `<group>`;
  GRANT USE SCHEMA, SELECT ON SCHEMA system.billing TO `<group>`;
  GRANT USE SCHEMA, SELECT ON SCHEMA system.query   TO `<group>`;
  GRANT USE SCHEMA, SELECT ON SCHEMA system.compute TO `<group>`;
  ```
  `system.billing` contains account-wide spend, and `system.query.history` contains query text. Grant accordingly.
- For development and tests only: a recent Databricks CLI with a configured profile, and Python 3.9+.

**Feature maturity (Azure Databricks docs, November 2026):** `system.billing.usage`, `system.billing.list_prices`,
`system.compute.warehouses`, `system.query.history`, and AI/BI dashboard parameters are all GA. Nothing in this repo
uses a Preview or Beta table.

## Deploy and run

### Dashboard (no code)

1. In the workspace sidebar: **Dashboards > Create dashboard (arrow) > Import dashboard from file**.
2. Choose `dashboard/serverless_cost_governance.lvdash.json`.
3. Pick a SQL warehouse, then **Publish**. Share it with the group that has the grants above.

Or with the CLI:

```bash
python3 - <<'EOF' > /tmp/body.json
import json
print(json.dumps({
  "display_name": "Serverless Cost Governance",
  "warehouse_id": "<warehouse-id>",
  "parent_path": "/Workspace/Shared",
  "serialized_dashboard": open("dashboard/serverless_cost_governance.lvdash.json").read()}))
EOF
databricks api post /api/2.0/lakeview/dashboards --json @/tmp/body.json -p <profile>
```

### SQL scripts

Open either file in the SQL editor, set the parameters at the top, and **Run all**:

| Script | Parameters |
|---|---|
| `serverless_tag_coverage.sql` | `ws_id` (NULL = all), `lookback_days` (90), `required_tag_key` (NULL = any non-platform tag) |
| `genie_cost_and_query_attribution.sql` | `ws_id` (NULL = all), `lookback_days` (30) |

### Tests

```bash
python3 dashboard/build_dashboard.py           # regenerate the .lvdash.json after editing SQL or widgets
python3 tests/test_dashboard.py                # static: fields, filters, parameters, system-tables-only, layout
python3 tests/test_dashboard.py --live --profile <profile> --warehouse <warehouse-id>
                                               # run every dataset (last 7 days) and check columns
tests/submit_script.sh <profile> sql/genie_cost_and_query_attribution.sql '{"lookback_days": "7"}'
                                               # run a multi-statement script in one session (serverless job)
```

`submit_script.sh` exists because scripts with `DECLARE` variables and temp views need one session, which the
Statement Execution API doesn't provide. It uploads `tests/harness_notebook.py` and runs it as a one-off serverless run.

## Folder structure

```
.
├── README.md
├── dashboard/
│   ├── build_dashboard.py                    # source of truth for the dashboard (SQL fragments + layout)
│   └── serverless_cost_governance.lvdash.json # generated; import this
├── sql/
│   ├── serverless_tag_coverage.sql           # tag coverage audit (v2)
│   └── genie_cost_and_query_attribution.sql  # Genie cost + warehouse query cost drivers
└── tests/
    ├── test_dashboard.py                     # static + live dashboard tests
    ├── run_sql.py                            # Statement Execution API helper used by the tests
    ├── harness_notebook.py                   # runs a .sql script statement by statement in one session
    └── submit_script.sh                      # submits the harness as a serverless run
```

## Configuration

There are no environment variables. Everything is set through dashboard filters or the parameters at the top of
each script. To change which tag keys count as platform keys, edit `PLATFORM_TAG_FILTER` in
`dashboard/build_dashboard.py` (and `platform_tag_keys` in `sql/serverless_tag_coverage.sql`), then regenerate.

## Known limitations

- **Regional tables.** `system.billing.usage` is global; `system.query.history` and `system.compute.warehouses` only
  include workspaces in the same cloud region as the workspace running the query. In a multi-region account, run the
  warehouse page from each region.
- **List price, not invoice.** Negotiated discounts and credits aren't applied.
- **Allocation.** Warehouse cost per query is an estimate; statements count toward the hour they started in, and
  partial-hour idle time is folded into attributed cost.
- **Query text.** The most-expensive-statements table shows the first 400 characters of each statement. Query text
  can contain literal values; restrict the published dashboard if that's sensitive.
- **Large accounts.** Very broad system table queries can return *System Table query returned too much data*. Pick
  one workspace or a shorter date range.
- **Service principal detection** for Genie relies on service principals appearing as an application ID (UUID) in
  `identity_metadata.run_as`.
