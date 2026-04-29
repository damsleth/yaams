#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${YAAMS_CONFIG:-config.yaml}"
VENV_DIR="${YAAMS_VENV:-.venv}"
SPACY_MODEL="${YAAMS_SPACY_MODEL:-xx_ent_wiki_sm}"
DRY_RUN_SOURCE="${YAAMS_DRY_RUN_SOURCE:-all}"
REQUIRE_VEC="${YAAMS_REQUIRE_VEC:-1}"

cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Usage: scripts/install_phase_a.sh

Environment overrides:
  YAAMS_CONFIG=config.yaml          Config file to use
  YAAMS_VENV=.venv                 Virtualenv directory
  PYTHON=python3.12                Python executable for venv creation
  YAAMS_SPACY_MODEL=xx_ent_wiki_sm spaCy model to download
  YAAMS_DRY_RUN_SOURCE=all         Dry-run source: all, imessage, or email
  YAAMS_REQUIRE_VEC=1              Use 0 to skip --require-vec during init
EOF
}

log() {
  printf '\n==> %s\n' "$1"
}

run() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  "$@"
}

python_version_check() {
  "$1" - <<'PY'
import sys

if sys.version_info < (3, 11):
  raise SystemExit("Python 3.11 or newer is required")
PY
}

python_version() {
  "$1" - <<'PY'
import sys

print(".".join(str(part) for part in sys.version_info[:3]))
PY
}

python_is_compatible() {
  "$1" - <<'PY'
import sys

raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

select_python() {
  local candidate
  if [ -n "${PYTHON:-}" ]; then
    if command -v "$PYTHON" >/dev/null 2>&1 && python_is_compatible "$PYTHON"; then
      command -v "$PYTHON"
      return 0
    fi
    printf 'Configured PYTHON=%s is not Python 3.11 or newer.\n' "$PYTHON" >&2
    return 1
  fi

  for candidate in python3.12 python3.11 python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_is_compatible "$candidate"; then
      command -v "$candidate"
      return 0
    fi
  done

  cat >&2 <<'EOF'
Could not find Python 3.11 or newer.

On macOS, install one with Homebrew:
  brew install python@3.12

Then rerun:
  PYTHON=python3.12 scripts/install_phase_a.sh
EOF
  return 1
}

ensure_venv() {
  local python_bin="$1"

  if [ -x "$VENV_DIR/bin/python" ]; then
    if python_is_compatible "$VENV_DIR/bin/python"; then
      log "Reusing virtualenv at $VENV_DIR"
      return 0
    fi

    if [ -f "$VENV_DIR/pyvenv.cfg" ]; then
      log "Recreating incompatible virtualenv at $VENV_DIR"
      rm -rf "$VENV_DIR"
    else
      cat >&2 <<EOF
${VENV_DIR} exists but is not a compatible Python virtualenv.
Move it aside or set YAAMS_VENV to another directory.
EOF
      return 1
    fi
  fi

  log "Creating virtualenv at $VENV_DIR"
  run "$python_bin" -m venv "$VENV_DIR"
}

db_path() {
  "$VENV_DIR/bin/python" - "$CONFIG_PATH" <<'PY'
from yaams.config import get_db_path, load_config
import sys

print(get_db_path(load_config(sys.argv[1])))
PY
}

print_next_steps() {
  local dry_run_status="$1"
  local resolved_db_path
  resolved_db_path="$(db_path 2>/dev/null || printf '~/yaams/data.db')"

  cat <<EOF

Next steps for initial ingestion
--------------------------------

1. Review config before the real ingest:
   - db_path: where YAAMS writes the SQLite database
   - ingest.since: earliest item timestamp to ingest
   - ingest.imessage.chat_db_path: Messages database path
   - ingest.email.sources: Mail .emlx tree or .mbox export
   - entities.dictionary: important people, projects, places, and aliases
   - embed.device: mps on Apple Silicon, cpu if MPS causes issues

2. Activate the venv:
   source ${VENV_DIR}/bin/activate

3. Run real extraction one source at a time:
   python scripts/ingest.py --config ${CONFIG_PATH} --source imessage --require-vec
   python scripts/ingest.py --config ${CONFIG_PATH} --source email --require-vec

4. Spot-check the database:
   python -m yaams.cli stats --config ${CONFIG_PATH}
   sqlite3 ${resolved_db_path} "SELECT source, count(*) FROM items GROUP BY source;"
   sqlite3 ${resolved_db_path} "SELECT timestamp, sender, substr(content, 1, 80) FROM items ORDER BY timestamp DESC LIMIT 10;"

5. Troubleshooting Full Disk Access:
   - If iMessage or Mail dry-run/extraction fails with permission errors, grant Full Disk Access to the terminal app running these commands.
   - On macOS: System Settings -> Privacy & Security -> Full Disk Access.
   - Enable Terminal, iTerm, VS Code, Cursor, or whichever app launched this shell.
   - Restart that terminal app after changing the permission.
   - Retry:
     python scripts/ingest.py --config ${CONFIG_PATH} --source imessage --dry-run
     python scripts/ingest.py --config ${CONFIG_PATH} --source email --dry-run

6. Troubleshooting model/device issues:
   - The first real ingest may download BAAI/bge-m3. Let it finish.
   - If MPS fails, edit config.yaml and set embed.device: cpu.
   - If sqlite-vec init fails, keep --require-vec for the real run and fix the dependency rather than ingesting data that Phase B cannot vector-search.

EOF

  if [ "$dry_run_status" != "0" ]; then
    cat <<EOF
The dry-run did not complete successfully. Fix the issue above before running real extraction.
The most common cause on macOS is missing Full Disk Access for Messages or Mail data.

EOF
  fi
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage
  exit 0
fi

log "Checking Python"
PYTHON_BIN="$(select_python)"
run command -v "$PYTHON_BIN"
python_version_check "$PYTHON_BIN"
printf 'Using Python %s\n' "$(python_version "$PYTHON_BIN")"
ensure_venv "$PYTHON_BIN"

VENV_PY="$VENV_DIR/bin/python"
printf 'Using venv Python %s\n' "$(python_version "$VENV_PY")"

log "Installing Python dependencies"
run "$VENV_PY" -m pip install --upgrade pip
run "$VENV_PY" -m pip install -r requirements.txt

log "Installing spaCy model"
run "$VENV_PY" -m spacy download "$SPACY_MODEL"

log "Initializing YAAMS database"
INIT_ARGS=(scripts/init_db.py --config "$CONFIG_PATH")
if [ "$REQUIRE_VEC" != "0" ]; then
  INIT_ARGS+=(--require-vec)
fi
run "$VENV_PY" "${INIT_ARGS[@]}"

log "Running dry-run ingest"
DRY_RUN_ARGS=(scripts/ingest.py --config "$CONFIG_PATH" --source "$DRY_RUN_SOURCE" --dry-run)
if run "$VENV_PY" "${DRY_RUN_ARGS[@]}"; then
  print_next_steps 0
  exit 0
else
  status=$?
  print_next_steps "$status"
  exit "$status"
fi
