#!/usr/bin/env bash
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$(cd "$HERE/.." && pwd)"
FIND="$HERE/find_workspace.py"
TEMPLATE="$BASE/template-workspace"
PY="${PY:-py -3.12}"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }

usage() {
  printf '\n  %s\n' 'the one door:'
  printf '    %s\n' \
    './start.sh [folder]                 start here (no folder: the app'"'"'s own folder)' \
    './start.sh [folder] --models <dir>  read the .gguf model files from <dir>' \
    './start.sh [folder] --llama <exe>   use this llama-server program' \
    './start.sh <folder> --restart       stop whatever runs, then start here' \
    './start.sh --stop                   stop everything, including llama-server' \
    './start.sh <folder> --show          resolve and report. changes nothing.' \
    './start.sh <folder> --new "Title"   lay a new book from the template, then start' \
    './start.sh <folder> --run <label>   name this launch'"'"'s logs'
  printf '\n  %s\n\n' 'Name the folder with a drive letter: "D:\Books\Draft2".'
}

refuse() {
  red "  $*"
  ylw "  nothing was started, stopped or written."
  exit 2
}
refuse_usage() {
  red "  $*"
  usage
  ylw "  nothing was started, stopped or written."
  exit 2
}

unixify() { printf '%s' "$1" | tr '\\' '/' | sed 's|^/\([a-zA-Z]\)/|\1:/|'; }
winify()  { printf '%s' "$1" | tr '/' '\\'; }

FOLDER=""; TITLE=""; LABEL=""; MODELS=""; LLAMA=""
WANT_RESTART=0; WANT_SHOW=0; WANT_STOP=0; WANT_NEW=0; WANT_CHECK=0

while [ $# -gt 0 ]; do
  case "$1" in
    --restart)  WANT_RESTART=1 ;;
    --show)     WANT_SHOW=1 ;;
    --stop)     WANT_STOP=1 ;;
    --new)
      WANT_NEW=1
      case "${2:-}" in
        ''|-*) refuse_usage '--new needs the book'"'"'s name: --new "The Ferryman"' ;;
      esac
      TITLE="$2"; shift ;;
    --run)
      case "${2:-}" in
        ''|-*) refuse_usage '--run needs a label: --run "Tuesday night"' ;;
      esac
      LABEL="$2"; shift ;;
    --models)
      case "${2:-}" in
        ''|-*) refuse_usage '--models needs the folder holding the .gguf files' ;;
      esac
      MODELS="$2"; shift ;;
    --llama)
      case "${2:-}" in
        ''|-*) refuse_usage '--llama needs the path to llama-server' ;;
      esac
      LLAMA="$2"; shift ;;
    --help|-h)  usage; exit 0 ;;
    -*)         refuse_usage "unknown option: $1" ;;
    *)
      [ -z "$FOLDER" ] || refuse_usage "two folders were named: $FOLDER and $1"
      FOLDER="$1" ;;
  esac
  shift
done

RESULT=""
put() { RESULT="$RESULT$1	$2
"; }

if [ "$WANT_STOP" = "1" ]; then
  [ -z "$FOLDER" ] || refuse_usage "--stop stops everything; it does not take a folder."
  [ "$WANT_NEW" = "0" ] || refuse_usage "--stop and --new are opposite errands."
  put stop "$([ "$WANT_SHOW" = "1" ] && echo 0 || echo 1)"
  put start 0
  put workspace ""
  put wsunix ""
  put run ""
  [ -n "${LOOM_DOOR_OUT:-}" ] && printf '%s' "$RESULT" > "$LOOM_DOOR_OUT"
  if [ "$WANT_SHOW" = "1" ]; then
    printf '\n  %s\n' "would stop everything, including llama-server on 8901"
    grn "  nothing was started, stopped or written."
  else
    printf '\n  %s\n' "stopping everything, including llama-server on 8901"
  fi
  exit 0
fi

DEFAULTED=0
if [ -z "$FOLDER" ]; then FOLDER="$BASE"; DEFAULTED=1; fi

TGT="$(unixify "${FOLDER%/}")"

[ -f "$FIND" ] || refuse "the finder is missing: $FIND"
command -v py >/dev/null 2>&1 || refuse "python 3.12 is needed to resolve the workspace (py -3.12)."

_say="${TMPDIR:-/tmp}/.loom-door-find.$$"
_verdict="$($PY "$FIND" "$TGT" 2>"$_say")"
STATUS="$(printf '%s' "$_verdict" | cut -f1)"
FOUND="$(printf '%s' "$_verdict" | cut -f2)"
if [ -f "$_say" ]; then
  [ "$WANT_NEW" = "1" ] && [ "$STATUS" = "missing" ] || cat "$_say" >&2
  rm -f "$_say"
fi

[ "$DEFAULTED" = "1" ] && [ "$WANT_NEW" = "0" ] && STATUS="here"

MAKE=0
if [ "$WANT_NEW" = "1" ]; then
  case "$STATUS" in
    here|deeper)
      refuse "there is already a book in there — --new will not lay a second one on top of it." ;;
    rename)
      refuse "that is a book under another name, not an empty folder — --new refused." ;;
    many)
      refuse "more than one workspace is in there — name the one you want." ;;
    empty|none|missing)
      MAKE=1; TGT="$(unixify "${FOLDER%/}")" ;;
    *)
      refuse "the finder answered '$STATUS', which --new has no rule for." ;;
  esac
else
  case "$STATUS" in
    here)   ;;
    deeper) TGT="$(unixify "$FOUND")"
            grn "  using the workspace inside it" ;;
    empty)  ;;
    *)
      refuse "that folder cannot be opened as a workspace ($STATUS)." ;;
  esac
fi

WS_UNIX="${TGT%/}"
WS_WIN="$(winify "$WS_UNIX")"
RUN_LABEL="${LABEL:-Run 1}"

if [ "$MAKE" = "1" ]; then
  [ -d "$TEMPLATE" ] || refuse "the template is not there: $TEMPLATE"
  _clash=""
  while IFS= read -r f; do
    rel="${f#"$TEMPLATE"/}"
    if [ -e "$WS_UNIX/$rel" ]; then _clash="$rel"; break; fi
  done < <(find "$TEMPLATE" -type f ! -name .keep 2>/dev/null)
  [ -z "$_clash" ] || refuse "there is already a $_clash in that folder — --new will not overwrite it."

  if [ "$WANT_SHOW" = "1" ]; then
    ylw "  would lay a new book here from $TEMPLATE"
  else
    mkdir -p "$WS_UNIX" || refuse "could not create $WS_WIN"
    cp -r "$TEMPLATE/." "$WS_UNIX/" || refuse "could not copy the template into $WS_WIN"
    while IFS= read -r k; do
      rm -f "$WS_UNIX/${k#"$TEMPLATE"/}"
    done < <(find "$TEMPLATE" -name ".keep" 2>/dev/null)
    if [ -f "$WS_UNIX/README.txt" ]; then
      case "$(head -1 "$WS_UNIX/README.txt")" in
        "This folder is ONE BOOK"*)
          _rest="$(tail -n +2 "$WS_UNIX/README.txt")"
          { printf 'This folder is ONE BOOK: %s\n' "$TITLE"
            printf '%s\n' "$_rest"; } > "$WS_UNIX/README.txt" ;;
        *)
          printf '\nThis folder is ONE BOOK: %s\n' "$TITLE" >> "$WS_UNIX/README.txt" ;;
      esac
    fi
    grn "  new book: $TITLE"
  fi
fi

if [ "$WANT_SHOW" = "0" ]; then
  for d in loom loom/projects loom/trace extractor logs; do
    mkdir -p "$WS_UNIX/$d" 2>/dev/null || refuse "could not lay out $WS_WIN\\$d"
  done
fi

if [ "$WANT_SHOW" = "1" ]; then
  put stop 0
  put start 0
else
  put stop "$WANT_RESTART"
  put start 1
fi
put workspace "$WS_WIN"
put wsunix "$WS_UNIX"
put run "$RUN_LABEL"
put models "$MODELS"
put llama "$LLAMA"
[ -n "${LOOM_DOOR_OUT:-}" ] && printf '%s' "$RESULT" > "$LOOM_DOOR_OUT"

printf '\n  %-11s %s\n' workspace "$WS_WIN"
printf '  %-11s %s\n' loom "$WS_WIN\\loom"
printf '  %-11s %s   %s\n' extractor "$WS_WIN\\extractor" "(not passed to the server yet — row 42)"
printf '  %-11s %s\n' logs "$WS_WIN\\logs\\$RUN_LABEL"

if [ -d "$WS_UNIX/loom/projects" ]; then
  _n=$(find "$WS_UNIX/loom/projects" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
  if [ "${_n:-0}" -gt 0 ]; then grn "  $_n project(s) in this workspace"
  else ylw "  no projects here yet — it will start empty"; fi
fi

if [ "$WANT_SHOW" = "1" ]; then
  echo
  if [ "$WANT_RESTART" = "1" ]; then ylw "  would stop everything, then start here"
  elif [ "$MAKE" = "1" ]; then       ylw "  would lay that book, then start here"
  else                                ylw "  would start here"; fi
  grn "  nothing was started, stopped or written."
fi
exit 0
