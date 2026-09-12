#!/bin/zsh

# Resume and complete one all-history MetaVir screening run. Every Python stage
# is digest-bound and idempotent, so restarting this script reuses completed
# responses rather than rebilling them. Never add --force here.

set -euo pipefail

if (( $# != 1 )); then
  print -u2 "usage: $0 YYYY-MM-DD"
  exit 2
fi

end_date="$1"
if [[ "$end_date" != [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] ]]; then
  print -u2 "end date must use YYYY-MM-DD"
  exit 2
fi

script_dir="${0:A:h}"
repo_root="${script_dir:h}"
cd "$repo_root"

export MPLCONFIGDIR="${MPLCONFIGDIR:-/tmp/metavir-mpl}"
export PYTHONUNBUFFERED=1
second_screen_batch_size="${SECOND_SCREEN_BATCH_SIZE:-8}"

if [[ "$second_screen_batch_size" != <-> ]] || (( second_screen_batch_size < 1 )); then
  print -u2 "SECOND_SCREEN_BATCH_SIZE must be a positive integer"
  exit 2
fi

if [[ -n "${WAIT_FOR_PID:-}" ]]; then
  while kill -0 "$WAIT_FOR_PID" 2>/dev/null; do
    sleep 30
  done
fi

common=(
  --all-history-through "$end_date"
  --report-dir data/monthly_reports
  --monthly-report-subdir
)

python scripts/monthly_evidence_update.py "${common[@]}" --stage discover
python scripts/monthly_evidence_update.py "${common[@]}" --stage cost-report
python scripts/monthly_evidence_update.py "${common[@]}" --stage first-screen --batch-size 20
python scripts/monthly_evidence_update.py "${common[@]}" --stage second-screen --batch-size "$second_screen_batch_size"
python scripts/monthly_evidence_update.py "${common[@]}" --stage report
python scripts/monthly_evidence_update.py "${common[@]}" --stage full-text
python scripts/monthly_evidence_update.py "${common[@]}" --stage finalize
python scripts/monthly_evidence_update.py "${common[@]}" --stage apply
python scripts/monthly_evidence_update.py "${common[@]}" --stage cost-report
python scripts/validate_monthly_artifacts.py --all-history-through "$end_date"
