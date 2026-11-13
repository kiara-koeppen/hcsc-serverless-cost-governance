"""Tests for dashboard/serverless_cost_governance.lvdash.json.

Static checks (always):
  * every widget query points at an existing dataset
  * every widget encoding field is selected by its query
  * every field a query selects is a column the dataset returns (checked live)
  * every filter encoding points at a query defined on that widget
  * every parameter a filter binds is declared on the target dataset
  * only system.* tables are referenced (so it imports anywhere)
  * no layout overlaps on a page

Live checks (--live):
  * every dataset runs on a real workspace (last 7 days, all workspaces,
    any tag) and returns >= 1 row with the columns the widgets use

usage: python3 tests/test_dashboard.py
       python3 tests/test_dashboard.py --live --profile <cli profile> --warehouse <warehouse id>
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_sql import run  # noqa: E402

DASH = Path(__file__).parent.parent / "dashboard" / "serverless_cost_governance.lvdash.json"
FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


def field_names(expr_fields):
    return {f["name"] for f in expr_fields}


def static_checks(d):
    datasets = {ds["name"]: ds for ds in d["datasets"]}
    for ds in d["datasets"]:
        sql = "".join(ds["queryLines"])
        tables = set(re.findall(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w.]*)", sql, flags=re.I))
        cte_names = set(re.findall(r"(\w+)\s+AS\s*\(", sql, flags=re.I))
        for t in tables:
            check(t.startswith("system.") or t in cte_names,
                  f"{ds['name']}: references non-system table {t}")
        for kw in set(re.findall(r":(\w+)", sql)):
            check(any(p["keyword"] == kw for p in ds.get("parameters", [])),
                  f"{ds['name']}: uses :{kw} but does not declare it")

    for page in d["pages"]:
        cells = set()
        for item in page["layout"]:
            w, pos = item["widget"], item["position"]
            check(pos["x"] + pos["width"] <= 6, f"{w['name']}: wider than grid")
            for x in range(pos["x"], pos["x"] + pos["width"]):
                for y in range(pos["y"], pos["y"] + pos["height"]):
                    check((x, y) not in cells, f"{page['displayName']}: overlap at {w['name']} ({x},{y})")
                    cells.add((x, y))
            if "multilineTextboxSpec" in w:
                continue
            queries = {q["name"]: q["query"] for q in w["queries"]}
            for qname, q in queries.items():
                check(q["datasetName"] in datasets, f"{w['name']}: unknown dataset {q['datasetName']}")
                for p in q.get("parameters", []):
                    ds = datasets.get(q["datasetName"], {})
                    check(any(dp["keyword"] == p["keyword"] for dp in ds.get("parameters", [])),
                          f"{w['name']}: binds :{p['keyword']} not declared on {q['datasetName']}")
            spec = w["spec"]
            enc = spec.get("encodings", {})
            if spec["widgetType"].startswith("filter"):
                for f in enc.get("fields", []):
                    check(f["queryName"] in queries, f"{w['name']}: filter queryName {f['queryName']} missing")
                continue
            selected = field_names(next(iter(queries.values()))["fields"])
            used = []
            for k, v in enc.items():
                if isinstance(v, dict) and "fieldName" in v:
                    used.append(v["fieldName"])
                if isinstance(v, dict) and "fields" in v:
                    used += [f["fieldName"] for f in v["fields"]]
                if k == "columns":
                    used += [c["fieldName"] for c in v]
            for u in used:
                check(u in selected, f"{w['name']}: encoding uses {u} not selected by query")


def default_sql(ds):
    sql = "".join(ds["queryLines"])
    for p in ds.get("parameters", []):
        kw, sel = p["keyword"], p["defaultSelection"]
        if p.get("complexType") == "RANGE":
            sql = sql.replace(f":{kw}.min", "date_sub(current_date(), 7)")
            sql = sql.replace(f":{kw}.max", "current_date()")
        else:
            sql = sql.replace(f":{kw}", "'" + sel["values"]["values"][0]["value"] + "'")
    return sql


def live_checks(d, profile, warehouse):
    used_cols = {}
    for page in d["pages"]:
        for item in page["layout"]:
            w = item["widget"]
            for q in w.get("queries", []):
                for f in q["query"].get("fields", []):
                    for col in re.findall(r"`([^`]+)`", f["expression"]):
                        used_cols.setdefault(q["query"]["datasetName"], set()).add(col)
    for ds in d["datasets"]:
        try:
            cols, rows = run(profile, warehouse, default_sql(ds))
        except Exception as e:  # noqa: BLE001
            check(False, f"{ds['name']}: query failed: {str(e)[:400]}")
            continue
        check(len(rows) > 0, f"{ds['name']}: returned 0 rows")
        for c in used_cols.get(ds["name"], set()):
            check(c in cols, f"{ds['name']}: widget uses column {c} the dataset does not return")
        print(f"  {ds['name']}: {len(rows)} rows, {len(cols)} cols")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--profile")
    ap.add_argument("--warehouse")
    a = ap.parse_args()
    if a.live and not (a.profile and a.warehouse):
        ap.error("--live needs --profile and --warehouse")
    check(DASH.exists(), f"missing {DASH}")
    if DASH.exists():
        d = json.loads(DASH.read_text())
        static_checks(d)
        if a.live and not FAILS:
            live_checks(d, a.profile, a.warehouse)
    print("\n".join("FAIL: " + f for f in FAILS) or "ALL CHECKS PASSED")
    sys.exit(1 if FAILS else 0)
