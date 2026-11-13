#!/bin/bash
# Run a multi-statement .sql script (DECLARE variables + temp views) in ONE
# session by submitting tests/harness_notebook.py as a one-off serverless run.
# The Statement Execution API has no session, so this is how the scripts in
# sql/ are tested end to end.
#
# usage: tests/submit_script.sh <profile> <sql file> ['<overrides json>']
#   e.g. tests/submit_script.sh e2-demo-field-eng sql/genie_cost_and_query_attribution.sql \
#          '{"lookback_days": "7"}'
set -euo pipefail
P=$1; SQL=$2; OVR=${3:-'{}'}
ME=$(databricks current-user me -p "$P" -o json | python3 -c "import json,sys;print(json.load(sys.stdin)['userName'])")
D="/Workspace/Users/$ME/serverless_cost_governance_test"
databricks workspace mkdirs "$D" -p "$P"
databricks workspace import "$D/harness" --file tests/harness_notebook.py --language PYTHON --format SOURCE --overwrite -p "$P" >/dev/null
databricks workspace import "$D/$(basename "$SQL")" --file "$SQL" --format AUTO --overwrite -p "$P" >/dev/null
BODY=$(python3 -c "import json,sys;print(json.dumps({'run_name':'serverless_cost_governance_test','tasks':[{'task_key':'t','notebook_task':{'notebook_path':sys.argv[1]+'/harness','base_parameters':{'sql_path':sys.argv[1]+'/'+sys.argv[2],'checks_path':'','overrides':sys.argv[3]}}}]}))" "$D" "$(basename "$SQL")" "$OVR")
RUN=$(databricks api post /api/2.2/jobs/runs/submit -p "$P" --json "$BODY" | python3 -c "import json,sys;print(json.load(sys.stdin)['run_id'])")
echo "run_id=$RUN" >&2
while true; do
  S=$(databricks api get "/api/2.2/jobs/runs/get?run_id=$RUN" -p "$P")
  LC=$(echo "$S" | python3 -c "import json,sys;print(json.load(sys.stdin)['state']['life_cycle_state'])")
  case $LC in TERMINATED|INTERNAL_ERROR|SKIPPED) break;; esac
  sleep 15
done
TID=$(echo "$S" | python3 -c "import json,sys;print(json.load(sys.stdin)['tasks'][0]['run_id'])")
databricks api get "/api/2.2/jobs/runs/get-output?run_id=$TID" -p "$P" | python3 -c "
import json,sys
d=json.load(sys.stdin)
if 'error' in d: print('FAILED:', d['error']); sys.exit(1)
r=json.loads(d['notebook_output']['result'])
for x in r['results']:
    print('##', ' '.join(x['sql'].split())[:90], '| rows:', len(x['rows']))
"
