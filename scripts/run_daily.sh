#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

if [ -n "${PYTHON_BIN:-}" ]; then
  PYTHON_BIN="$PYTHON_BIN"
elif [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python)"
else
  echo "No usable Python interpreter found." >&2
  exit 1
fi

SOURCES=(${DAILY_SOURCES:-arxiv semanticscholar huggingface})
ARXIV_CATEGORIES=(${ARXIV_CATEGORIES:-cs.AI cs.CL cs.LG})
GH_LANGUAGES=(${GH_LANGUAGES:-all})
HF_CONTENT_TYPES=(${HF_CONTENT_TYPES:-papers})

IDEA_ARGS=()
REPORT_ARGS=()
SS_QUERY_ARGS=()
SS_FIELD_ARGS=()
SOURCE_EMAIL_ARGS=()
ZOTERO_ARGS=()

if [ -n "${SS_QUERIES:-}" ]; then
  IFS='|' read -r -a SS_QUERY_VALUES <<< "${SS_QUERIES}"
  SS_QUERY_ARGS=(--ss_queries "${SS_QUERY_VALUES[@]}")
fi

if [ -n "${SS_FIELDS_OF_STUDY:-Computer Science}" ]; then
  IFS='|' read -r -a SS_FIELD_VALUES <<< "${SS_FIELDS_OF_STUDY:-Computer Science}"
  SS_FIELD_ARGS=(--ss_fields_of_study "${SS_FIELD_VALUES[@]}")
fi

if [ "${GENERATE_IDEAS:-0}" = "1" ]; then
  IDEA_ARGS+=(
    --generate_ideas
    --researcher_profile "${RESEARCHER_PROFILE:-profiles/researcher_profile.md}"
    --idea_min_score "${IDEA_MIN_SCORE:-7}"
    --idea_max_items "${IDEA_MAX_ITEMS:-15}"
    --idea_count "${IDEA_COUNT:-5}"
  )
fi

if [ "${GENERATE_REPORT:-0}" = "1" ]; then
  REPORT_ARGS+=(--generate_report)
  if [ -n "${REPORT_PROFILE_FILE:-}" ]; then
    REPORT_ARGS+=(--report_profile "${REPORT_PROFILE_FILE}")
  fi
  if [ -n "${REPORT_TITLE:-}" ]; then
    REPORT_ARGS+=(--report_title "${REPORT_TITLE}")
  fi
  REPORT_ARGS+=(
    --report_min_score "${REPORT_MIN_SCORE:-4.0}"
    --report_max_items "${REPORT_MAX_ITEMS:-18}"
    --report_theme_count "${REPORT_THEME_COUNT:-4}"
    --report_prediction_count "${REPORT_PREDICTION_COUNT:-4}"
    --report_idea_count "${REPORT_IDEA_COUNT:-4}"
  )
  if [ "${SEND_REPORT_EMAIL:-0}" = "1" ]; then
    REPORT_ARGS+=(--send_report_email)
  fi
fi

if [ "${SKIP_SOURCE_EMAILS:-0}" = "1" ]; then
  SOURCE_EMAIL_ARGS+=(--skip_source_emails)
fi

# =========================
# Zotero auto-sync options
# =========================
# Enable with:
#   SYNC_ZOTERO=1
#
# Optional:
#   ZOTERO_MIN_SCORE=7
#   ZOTERO_COLLECTION="iDeer Daily Papers"
#
# Note:
#   main.py currently uses a hard-coded zotero_save.py path:
#   ~/.claude/skills/zotero-mcp/scripts/zotero_save.py
if [ "${SYNC_ZOTERO:-0}" = "1" ]; then
  ZOTERO_ARGS+=(--sync_zotero)
  ZOTERO_ARGS+=(--zotero_min_score "${ZOTERO_MIN_SCORE:-7}")

  if [ -n "${ZOTERO_COLLECTION:-}" ]; then
    ZOTERO_ARGS+=(--zotero_collection "${ZOTERO_COLLECTION}")
  fi
fi

CMD=(
  "$PYTHON_BIN" main.py
  --sources "${SOURCES[@]}"
  --description "${DESCRIPTION_FILE:-profiles/description.txt}"
  --num_workers "${NUM_WORKERS:-8}"
  --temperature "${TEMPERATURE:-0.5}"
  --save
  --arxiv_categories "${ARXIV_CATEGORIES[@]}"
  --arxiv_max_entries "${ARXIV_MAX_ENTRIES:-100}"
  --arxiv_max_papers "${ARXIV_MAX_PAPERS:-60}"
  --ss_max_results "${SS_MAX_RESULTS:-60}"
  --ss_max_papers "${SS_MAX_PAPERS:-30}"
  --ss_year "${SS_YEAR:-}"
  --ss_api_key "${SS_API_KEY:-}"
)

if [ "${#SS_QUERY_ARGS[@]}" -gt 0 ]; then
  CMD+=("${SS_QUERY_ARGS[@]}")
fi

if [ "${#SS_FIELD_ARGS[@]}" -gt 0 ]; then
  CMD+=("${SS_FIELD_ARGS[@]}")
fi

CMD+=(
  --gh_languages "${GH_LANGUAGES[@]}"
  --gh_since "${GH_SINCE:-daily}"
  --gh_max_repos "${GH_MAX_REPOS:-30}"
  --hf_content_type "${HF_CONTENT_TYPES[@]}"
  --hf_max_papers "${HF_MAX_PAPERS:-30}"
  --hf_max_models "${HF_MAX_MODELS:-15}"
)

if [ "${#SOURCE_EMAIL_ARGS[@]}" -gt 0 ]; then
  CMD+=("${SOURCE_EMAIL_ARGS[@]}")
fi

if [ "${#REPORT_ARGS[@]}" -gt 0 ]; then
  CMD+=("${REPORT_ARGS[@]}")
fi

if [ "${#IDEA_ARGS[@]}" -gt 0 ]; then
  CMD+=("${IDEA_ARGS[@]}")
fi

if [ "${#ZOTERO_ARGS[@]}" -gt 0 ]; then
  CMD+=("${ZOTERO_ARGS[@]}")
fi

printf '[run_daily] Command: '
printf '%q ' "${CMD[@]}"
echo

"${CMD[@]}"
# =========================
# Import today's history JSON to Zotero
# =========================
if [ "${IMPORT_ZOTERO_HISTORY:-0}" = "1" ]; then
  echo
  echo "============================================================"
  echo "Importing iDeer history to Zotero..."
  echo "============================================================"

  if [ ! -f "scripts/zotero_save.py" ]; then
    echo "[zotero_import] scripts/zotero_save.py not found." >&2
    exit 1
  fi

  "$PYTHON_BIN" scripts/zotero_save.py \
    --import_history \
    --sources "${SOURCES[@]}" \
    --parent_collection "${ZOTERO_PARENT_COLLECTION:-iDeer Daily Papers}" \
    --min_score "${ZOTERO_MIN_SCORE:-7}" \

fi