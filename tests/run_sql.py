"""Run a SQL statement (or a .sql file of ;-separated statements) on a Databricks
SQL warehouse via the Databricks CLI and print the final result set.

Usage:
  python3 tests/run_sql.py --profile e2-demo-field-eng --warehouse <id> --sql "SELECT 1"
  python3 tests/run_sql.py --profile e2-demo-field-eng --warehouse <id> --file sql/x.sql
"""
import argparse
import json
import subprocess
import sys
import time


def api(profile, method, path, body=None):
    cmd = ["databricks", "api", method, path, "--profile", profile]
    if body is not None:
        cmd += ["--json", json.dumps(body)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(out.stderr or out.stdout)
    return json.loads(out.stdout)


def run(profile, warehouse, statement):
    """Execute one statement, poll to completion, return (columns, rows)."""
    d = api(profile, "post", "/api/2.0/sql/statements", {
        "warehouse_id": warehouse,
        "statement": statement,
        "wait_timeout": "50s",
        "on_wait_timeout": "CONTINUE",
        "format": "JSON_ARRAY",
        "disposition": "INLINE",
    })
    while d["status"]["state"] in ("PENDING", "RUNNING"):
        time.sleep(5)
        d = api(profile, "get", f"/api/2.0/sql/statements/{d['statement_id']}")
    if d["status"]["state"] != "SUCCEEDED":
        raise RuntimeError(json.dumps(d["status"]))
    cols = [c["name"] for c in d["manifest"]["schema"]["columns"]]
    rows = (d.get("result") or {}).get("data_array") or []
    return cols, rows


def split_statements(text):
    """Split a script on ';' at line ends, ignoring '--' comment-only lines."""
    body = "\n".join(l for l in text.splitlines() if not l.strip().startswith("--"))
    return [s.strip() for s in body.split(";\n") if s.strip().rstrip(";").strip()]


def run_script(profile, warehouse, text, overrides=None):
    """Run a multi-statement script in one session-like sequence.

    The Statement Execution API has no session, so SQL variables don't persist
    across calls. We therefore substitute DECLARE defaults into a single
    statement: each DECLARE becomes a CTE-free literal via SET VARIABLE-less
    inlining is not possible, so we send the whole script as one compound
    statement (BEGIN ... END) which DBSQL executes in one session.
    """
    stmts = split_statements(text)
    if overrides:
        stmts = [apply_overrides(s, overrides) for s in stmts]
    compound = "BEGIN\n" + ";\n".join(s.rstrip(";") for s in stmts) + ";\nEND"
    return run(profile, warehouse, compound)


def apply_overrides(stmt, overrides):
    import re
    for name, value in overrides.items():
        stmt = re.sub(
            rf"(DECLARE OR REPLACE VARIABLE {name}\s+\w+\s+DEFAULT\s+)([^;]+)",
            lambda m: m.group(1) + value, stmt)
    return stmt


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--warehouse", required=True)
    ap.add_argument("--sql")
    ap.add_argument("--file")
    ap.add_argument("--set", action="append", default=[], help="var=value override for DECLARE defaults")
    a = ap.parse_args()
    overrides = dict(kv.split("=", 1) for kv in a.set)
    if a.file:
        cols, rows = run_script(a.profile, a.warehouse, open(a.file).read(), overrides)
    else:
        cols, rows = run(a.profile, a.warehouse, a.sql)
    print(" | ".join(cols))
    for r in rows:
        print(" | ".join("" if x is None else str(x) for x in r))
    print(f"-- {len(rows)} rows", file=sys.stderr)
