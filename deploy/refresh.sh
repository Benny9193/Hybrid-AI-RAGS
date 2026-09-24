#!/usr/bin/env bash
# Weekly schema-drift check. Exit code 2 = drift found (report written).
# crontab:  0 6 * * 1  /opt/eds-rag/deploy/refresh.sh
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
: "${EDS_SQL_CONN:?set EDS_SQL_CONN to the read-only login connection string}"

stamp=$(date -u +%Y%m%d)
set +e
eds-rag refresh --live --apply \
  --report "reports/drift-${stamp}.md" \
  --snapshot-out "data/snapshots/snapshot-${stamp}.json"
status=$?
set -e

if [ "$status" -eq 2 ]; then
  echo "schema drift detected - see reports/drift-${stamp}.md" >&2
  # Hook your alerting here (email, Teams webhook, ticket...).
fi
exit "$status"
