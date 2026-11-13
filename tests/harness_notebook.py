# Databricks notebook source
# Test harness: runs a .sql script statement-by-statement in ONE Spark session
# (same semantics as pasting it into the SQL editor), with optional DECLARE
# default overrides, then runs validation checks against the temp view and
# returns everything as JSON via dbutils.notebook.exit.

# COMMAND ----------
import json, re

dbutils.widgets.text("sql_path", "")
dbutils.widgets.text("overrides", "{}")   # JSON: {"lookback_days": "14"}
dbutils.widgets.text("checks_path", "")   # optional .sql of ;-separated check queries

sql_path = dbutils.widgets.get("sql_path")
overrides = json.loads(dbutils.widgets.get("overrides"))
checks_path = dbutils.widgets.get("checks_path")

def read(path):
    with open(path) as f:
        return f.read()

def statements(text):
    # Strip -- comments (the script has no '--' inside string literals)
    body = "\n".join(re.sub(r"--.*$", "", l) for l in text.splitlines())
    return [s.strip() for s in re.split(r";\s*\n", body) if s.strip().rstrip(";").strip()]

def override(stmt):
    for name, value in overrides.items():
        stmt = re.sub(rf"(DECLARE OR REPLACE VARIABLE {name}\s+[\w<>]+\s+DEFAULT\s+)(.+)$",
                      lambda m: m.group(1) + value, stmt, flags=re.S)
    return stmt

def rows(df, limit=200):
    return [r.asDict(recursive=True) for r in df.limit(limit).collect()]

# COMMAND ----------
out = {"results": [], "checks": []}
for s in statements(read(sql_path)):
    df = spark.sql(override(s))
    if s.upper().lstrip().startswith(("SELECT", "WITH")):
        out["results"].append({"sql": s[:120], "rows": rows(df)})

if checks_path:
    for s in statements(read(checks_path)):
        out["checks"].append({"sql": s, "rows": rows(spark.sql(s))})

dbutils.notebook.exit(json.dumps(out, default=str))
