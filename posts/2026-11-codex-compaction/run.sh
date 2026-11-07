#!/bin/sh
# Run the compaction experiment once with `codex exec` and check the outcome.
#
# Usage: run.sh MODEL EFFORT OUT_DIR
# Environment (all optional):
#   COMPACT_LIMIT    model_auto_compact_token_limit, default 40000
#   COMPACT_PROMPT   compact_prompt override, e.g. "Reply with exactly the single word: EMPTY" to cripple the summary
#   CONTEXT_WINDOW   model_context_window override, e.g. 872000
set -eu

if [ "$#" -ne 3 ]; then
  echo "usage: $0 MODEL EFFORT OUT_DIR" >&2
  exit 2
fi
MODEL=$1
EFFORT=$2
OUT=$3
COMPACT_LIMIT=${COMPACT_LIMIT:-40000}
HERE=$(cd "$(dirname "$0")" && pwd)

mkdir -p "$OUT"
python3 "$HERE/make_samples.py" "$OUT" > /dev/null
cp "$HERE/prompt.txt" "$OUT/prompt.txt"
cd "$OUT"
rm -f report.md

set -- exec --skip-git-repo-check -s workspace-write -C . -m "$MODEL" \
  -c "model_reasoning_effort=$EFFORT" \
  -c "model_auto_compact_token_limit=$COMPACT_LIMIT"
if [ -n "${COMPACT_PROMPT:-}" ]; then
  set -- "$@" -c "compact_prompt=\"$COMPACT_PROMPT\""
fi
if [ -n "${CONTEXT_WINDOW:-}" ]; then
  set -- "$@" -c "model_context_window=$CONTEXT_WINDOW"
fi
set -- "$@" --json -o last.txt -

codex "$@" < prompt.txt > events.jsonl 2> run.log || echo "codex exit status: $?" >> run.log

if [ -s report.md ] && [ "$(tail -n 1 report.md | tr -d '[:space:]')" = "ORCHID-7731" ]; then
  echo "RESULT: complete (report.md ends with the codeword)"
else
  echo "RESULT: NOT complete (report.md missing or last line is not ORCHID-7731)"
fi
echo "Next: find the rollout and check it, for example"
echo "  python3 $HERE/../../tools/codex-compaction-check/compaction_check.py --latest --cwd $OUT --task-file $OUT/prompt.txt"
echo "Token usage per turn is in $OUT/events.jsonl (turn.completed events)."
