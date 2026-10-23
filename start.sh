#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")" || exit 1
BASE="$(pwd)"

DOOR_STOP=0
DOOR_START=1
RUN_REL="Runs/Run 1"
_door_tmp="${TEMP:-${TMPDIR:-/tmp}}"
_door_tmp="$(printf %s "$_door_tmp" | tr '\\' '/')/loom-door.$$"
LOOM_DOOR_OUT="$_door_tmp" bash "$BASE/tools/start_door.sh" "$@" \
  || { rm -f "$_door_tmp"; exit 2; }
[ -f "$_door_tmp" ] || exit 0
while IFS=$'\t' read -r _k _v; do
  case "$_k" in
    workspace) WORKSPACE="$_v" ;;
    wsunix)    WS_UNIX="$_v" ;;
    run)       RUN_REL="$_v" ;;
    stop)      DOOR_STOP="$_v" ;;
    start)     DOOR_START="$_v" ;;
    models)    [ -n "$_v" ] && export LOOM_MODELS_DIR="$_v" ;;
    llama)     [ -n "$_v" ] && export LOOM_LLAMA_SERVER="$_v" ;;
  esac
done < "$_door_tmp"
rm -f "$_door_tmp"
if [ "$DOOR_START" = "0" ] && [ "$DOOR_STOP" = "0" ]; then exit 0; fi

PY="py -3.12"
LOOM_PORT=8888

_pycache_tmp="${TEMP:-${TMPDIR:-/tmp}}"
_pycache_tmp="$(printf %s "$_pycache_tmp" | tr '\\' '/')"
export PYTHONPYCACHEPREFIX="$_pycache_tmp/loom-pycache-$$"

LOOM_DATA="$WORKSPACE\\loom"
EXTRACT_DATA="$WORKSPACE\\extractor"
RUN="$WS_UNIX/logs/$(basename "$RUN_REL")"

snapshot_workspace() {
  command -v git >/dev/null 2>&1 || { ylw "  snapshots      git not found - none taken"; return 0; }
  local root out n
  root="$WS_UNIX"
  if [ ! -d "$root" ]; then
    ylw "  snapshots      NONE - the workspace is not there: $root"
    return 0
  fi
  if [ ! -d "$root/.git" ]; then
    out="$(git -C "$root" init -q 2>&1)"
    if [ ! -d "$root/.git" ]; then
      ylw "  snapshots      COULD NOT start a repo in $root - your work is NOT being snapshotted"
      [ -n "$out" ] && printf '    %s\n' "$out"
      return 0
    fi
    printf '%s\n' 'models/' 'data/' 'studio/' 'logs/' 'temp/' 'loom/trace/' \
      '*.log' '*.tmp' > "$root/.gitignore"
    grn "  snapshots      new git repo in $root (prose only - models/logs excluded)"
  fi
  for p in .gitignore loom extractor; do
    [ -e "$root/$p" ] && git -C "$root" add -A -- "$p" >/dev/null 2>&1
  done
  if git -C "$root" rev-parse --verify -q HEAD >/dev/null 2>&1 \
     && git -C "$root" diff-index --quiet HEAD -- 2>/dev/null; then
    n="$(git -C "$root" rev-list --count HEAD 2>/dev/null || echo '?')"
    grn "  snapshots      unchanged since last run ($n on record)"
    return 0
  fi
  if out="$(git -C "$root" -c user.name="story-loom" -c user.email="loom@localhost" \
              commit -q -m "snapshot before run $(date '+%Y-%m-%d %H:%M:%S')" 2>&1)"; then
    n="$(git -C "$root" rev-list --count HEAD 2>/dev/null || echo '?')"
    grn "  snapshots      $n on record ($root)"
  elif printf '%s' "$out" | grep -qi 'nothing to commit'; then
    grn "  snapshots      nothing new to record ($root)"
  else
    ylw "  snapshots      COULD NOT record this run - your work is NOT being snapshotted"
    printf '    %s\n' "$(printf '%s' "$out" | head -3)"
  fi
}

red()  { _SAID=1; printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }

_STEP='startup'
_SAID=0
step() { _STEP="$*"; printf '\n\033[1m== %s\033[0m\n' "$*"; }
_bail() {
  local rc=$?
  [ "$rc" -eq 0 ] && return 0
  [ "${_SAID:-0}" = "1" ] && return 0
  printf '\033[31m%s\033[0m\n' "" "  it stopped during: $_STEP  (exit $rc)"
  printf '\033[33m%s\033[0m\n' \
    "  Nothing above explained that, which is a bug in this script." \
    "  The last thing it did is named on the line above."
  printf '\n  %s\n  %s\n\n' \
    './start.sh "<folder>" --show   what that folder resolves to' \
    'bash -x ./start.sh        the whole trace, if you want to send it'
}
trap _bail EXIT

port_up() { $PY - "$1" <<'PYEOF' 2>/dev/null
import socket, sys
s = socket.socket(); s.settimeout(0.4)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PYEOF
}

wait_for() {  # wait_for <port> <label> <seconds>
  local p="$1" label="$2" secs="${3:-90}" i=0
  while [ "$i" -lt "$secs" ]; do
    if port_up "$p"; then grn "  $label is up on $p"; return 0; fi
    sleep 1; i=$((i+1))
  done
  red "  $label did NOT come up on $p within ${secs}s"; return 1
}

stop_all() {
  step "stopping"
  $PY - <<'PYEOF'
import subprocess
def ps_run(cmd):
    return subprocess.run(["powershell","-NoProfile","-Command",cmd],
                          capture_output=True, text=True).stdout.strip()

pids = ps_run(
  "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
  "Where-Object { $_.CommandLine -match 'loom-server.py|extractor-server.py' } | "
  "ForEach-Object { $_.ProcessId }").split()
for pid in pids:
    ps_run(f"Stop-Process -Id {pid} -Force")
    print("  stopped python", pid)

# llama-server does NOT die with story-studio when story-studio is force-killed:
# the shutdown hook never runs, and the orphan keeps port 8901, after which every
# generation fails with "port 8901 is already in use". Stop it explicitly.
llama = ps_run("Get-Process llama-server -ErrorAction SilentlyContinue | "
               "ForEach-Object { $_.Id }").split()
for pid in llama:
    ps_run(f"Stop-Process -Id {pid} -Force")
    print("  stopped llama-server", pid, "(frees port 8901)")

if not pids and not llama:
    print("  nothing was running")
PYEOF
}

clear_orphan_llama() {
  if port_up "$LOOM_PORT"; then return 0; fi
  if ! port_up 8901; then return 0; fi
  ylw "  an orphaned llama-server holds port 8901 (loom-server is not running)"
  $PY - <<'PYEOF'
import subprocess
def ps_run(c):
    return subprocess.run(["powershell","-NoProfile","-Command",c],
                          capture_output=True, text=True).stdout.strip()
for pid in ps_run("Get-Process llama-server -ErrorAction SilentlyContinue | "
                  "ForEach-Object { $_.Id }").split():
    ps_run(f"Stop-Process -Id {pid} -Force")
    print("  cleared orphan llama-server", pid)
PYEOF
  sleep 2
}
if [ "$DOOR_STOP" = "1" ]; then
  stop_all
  [ "$DOOR_START" = "1" ] || exit 0
fi

mkdir -p "$RUN" || { red "cannot create run folder: $RUN"; exit 1; }
step "workspace snapshot"
snapshot_workspace

step "run folder"
echo "  $RUN"

step "preflight"
if ! $PY -c "import sys; assert sys.version_info[:2]==(3,12)" 2>/dev/null; then
  red "  python 3.12 not found via 'py -3.12'"; exit 1
fi
$PY -c "import sys; print('  python', '.'.join(map(str, sys.version_info[:3])))"
if ! $PY -c "import fastapi, uvicorn" 2>/dev/null; then
  red "  dependencies missing (fastapi / uvicorn)"
  echo "  fix: py -3.12 -m pip install fastapi uvicorn"
  exit 1
fi
grn "  dependencies present"

HARD_RED_FILE="${TMPDIR:-/tmp}/loom-hard-red.$$"
: > "$HARD_RED_FILE"
hard_red() { printf "%s" "$1" >> "$HARD_RED_FILE"; printf "
" >> "$HARD_RED_FILE"; }

clear_orphan_llama

step "loom-server (the writing app, the queue, and the model runner)"
if port_up "$LOOM_PORT"; then
  grn "  already running on $LOOM_PORT"
else
  LOOM_TRACE_FLAG=""
  [ "${LOOM_TRACE:-}" = "1" ] && LOOM_TRACE_FLAG="--trace"
  ( cd "$BASE" && $PY -u loom-server.py --port "$LOOM_PORT" \
      --data "$LOOM_DATA" $LOOM_TRACE_FLAG >"$RUN/loom-server.log" 2>&1 & )
  wait_for "$LOOM_PORT" "loom-server" 60 || { red "  see $RUN/loom-server.log"; exit 1; }
fi

step "extractor (chapter scanner)"
if curl -fsS --max-time 5 "http://127.0.0.1:$LOOM_PORT/extract/health" >/dev/null 2>&1; then
  grn "  in-process on $LOOM_PORT — no second service since 2026-09-19"
else
  ylw "  loom-server is up but /extract/health did not answer"
  ylw "  see $RUN/loom-server.log — the extractor now shares that log"
fi

MODELS=$($PY - <<PYEOF 2>/dev/null || echo "?"
import json, urllib.request
d = json.load(urllib.request.urlopen("http://127.0.0.1:$LOOM_PORT/api/status", timeout=5))
print(sum(1 for m in d["models"] if m["present"]), "of", len(d["models"]))
PYEOF
)

ORPHAN=$($PY - <<PYEOF 2>/dev/null || echo ""
import json, urllib.request
d = json.load(urllib.request.urlopen("http://127.0.0.1:$LOOM_PORT/api/status", timeout=5))
print("yes" if d.get("orphan") else "")
PYEOF
)
if [ -n "$ORPHAN" ]; then
  red "  WARNING: the runner reports an ORPHANED llama-server on 8901."
  red "  Generation will fail until it is cleared:  ./start.sh \"$WORKSPACE\" --restart"
fi

URL="http://127.0.0.1:$LOOM_PORT/index.html"

{
  echo "# Story Loom — run started $(date '+%Y-%m-%d %H:%M:%S')"
  echo
  echo "## Open this"
  echo
  echo "    $URL"
  echo
  echo "## What is running"
  echo
  echo "| door | port | what it does |"
  echo "|---|---|---|"
  echo "| **the app** | **$LOOM_PORT** | everything you open: the writing app, the queue, the model runner, and the Extract tab |"
  echo
  echo "ONE process listens on that door. One subprocess exists behind it and"
  echo "is not yours to type:"
  echo
  echo "| internal | port | why it exists |"
  echo "|---|---|---|"
  echo "| llama-server | 8901 | the model. llama.cpp has no socket-less mode, so it must have a port; only loom-server ever addresses it. |"
  echo
  echo "The extractor used to be a second process on 8789. Since 2026-09-19 it"
  echo "runs inside loom-server, so the Extract tab is same-origin by being the"
  echo "SAME SERVER rather than by being proxied to one."
  echo
  echo "Models present: $MODELS"
  echo
  echo "## Tabs"
  echo
  echo "- **Writing** — chapters, parts, paragraphs; write, react, rewrite."
  echo "- **Outline** — story settings: temperature and style."
  echo "- **Extract** — scan a chapter into plot, interactions, style and more."
  echo "- **Auto** — every model writes the whole story, start to finish."
  echo "- **Bench** — one prompt, one model, for quick tests."
  echo
  echo "## Stories"
  echo
  echo "Each story is a whole book and shares nothing with the others."
  echo "Prose lives in $WORKSPACE\\loom\\projects\\<Project>\\stories\\<Story>\\manuscript\\."
  echo "Use the picker at the top left to switch, or + story to start a new one."
  echo
  echo "## Stopping"
  echo
  echo "    ./start.sh --stop"
  echo
  echo "Logs for this run are beside this file."
} > "$RUN/run.md"

if [ -s "$HARD_RED_FILE" ]; then
  echo
  red "== NOT READY - a correctness suite is red"
  while IFS= read -r line; do [ -n "$line" ] && red "     $line"; done < "$HARD_RED_FILE"
  echo
  echo "  The servers are up at $URL - nothing was torn down - but this launch"
  echo "  will not call itself ready. Fix the suite, or if the red is a known and"
  echo "  OWNED finding, register it in tests/realstack/ALLOWED-RED.json with a"
  echo "  reason, an owner and the plan carrying its ledger row. Silence is the"
  echo "  one option that is not available."
  echo
  echo "  details:    $RUN/run.md"
  rm -f "$HARD_RED_FILE"
  exit 1
fi
rm -f "$HARD_RED_FILE"

_pp="$(printf '%s' "$LOOM_DATA" | tr '\\' '/')/projects"
if [ -d "$_pp" ]; then
  _debris=$(ls -1 "$_pp" 2>/dev/null | grep -iE "probe|^Test[0-9]+$" || true)
  if [ -n "$_debris" ]; then
    ylw "  test debris in your workspace (a GPU suite was interrupted):"
    printf "%s
" "$_debris" | sed "s/^/       /"
    echo "       delete them yourself if they are not yours - see tests/README-the-ladder.md"
  fi
fi

_tmpdir="${TEMP:-${TMPDIR:-/tmp}}"
_tmpdir="$(printf %s "$_tmpdir" | tr '\\' '/')"
if [ -d "$_tmpdir" ]; then
  _swept=$(find "$_tmpdir" -maxdepth 1 -type d -name "loom-*" -mtime +1 2>/dev/null | wc -l | tr -d " ")
  if [ "${_swept:-0}" -gt 0 ]; then
    find "$_tmpdir" -maxdepth 1 -type d -name "loom-*" -mtime +1 -exec rm -rf {} + 2>/dev/null || true
    echo "  swept $_swept stale test workspace(s) from %TEMP%"
  fi
fi

step "ready"
grn "  $URL"
echo "  details:    $RUN/run.md"
echo
echo "  stop everything with:  ./start.sh --stop"
