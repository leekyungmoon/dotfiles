#!/usr/bin/env bash
# Acceptance tests for the trn alias and the tmux alias family around it.
#
# Everything runs against a disposable tmux server on a socket inside a fresh
# temporary directory, so the caller's real sessions are never touched.
#
# Isolation rules, all load-bearing:
# - An inherited $TMUX names the caller's real server and silently wins over
#   TMUX_TMPDIR, so it is removed before anything else runs.
# - Harness commands always pass -S with the disposable socket.
# - Aliases, which cannot take -S, run with $TMUX pointing at the disposable
#   socket, which is how a tmux client picks its server when -S is absent.
# - kill-server is only ever sent with -S, and only after checking that the
#   socket is inside this test's own directory.

set -uo pipefail

unset TMUX TMUX_PANE TMUX_TMPDIR

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
settings=${repo_root}/zsh/zsh.d/zsh_custom_settings.zsh

failures=0
checks=0

check() {
  local label=$1 expected=$2 actual=$3
  checks=$((checks + 1))
  if [[ ${expected} == "${actual}" ]]; then
    return 0
  fi
  printf 'FAIL: %s\n  expected: %q\n  actual:   %q\n' \
    "${label}" "${expected}" "${actual}" >&2
  failures=$((failures + 1))
}

for tool in tmux zsh tr; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    printf 'SKIP: %s is not installed\n' "${tool}" >&2
    exit 0
  fi
done

workdir=$(mktemp -d)
socket="${workdir}/tmux.sock"

require_disposable_socket() {
  if [[ -z ${workdir} || ${socket} != "${workdir}/"* ]]; then
    printf 'ABORT: refusing to address a tmux socket outside %s\n' \
      "${workdir}" >&2
    exit 99
  fi
}

# Harness access to the disposable server. Never call plain `tmux`.
t() {
  require_disposable_socket
  command tmux -S "${socket}" "$@"
}

cleanup() {
  if [[ -S ${socket} ]]; then
    t kill-server 2>/dev/null
  fi
  rm -rf -- "${workdir}"
}
trap cleanup EXIT

# Run an alias as a user would type it, bound to the disposable server.
# zsh expands aliases when it parses a line, so an alias defined by `source`
# on the same command line is not yet visible; eval reparses afterwards.
run_zsh_alias() {
  require_disposable_socket
  env TMUX="${socket},0,0" zsh -f -c "source '${settings}'; eval \"\$1\"" \
    -- "$1" 2>&1
}

alias_body() {
  zsh -f -c "source '${settings}'; print -r -- \${aliases[$1]:-MISSING}"
}

session_names() {
  t list-sessions -F '#{session_name}' 2>/dev/null | sort | tr '\n' ' '
}

# --- the alias exists and is the intended command ---------------------------

check "trn is defined" "tmux rename-session" "$(alias_body trn)"

# --- the surrounding family is unchanged ------------------------------------

check "tl unchanged" "tmux ls" "$(alias_body tl)"
check "tn unchanged" "tmux new -s" "$(alias_body tn)"
check "ta unchanged" "tmux attach -t" "$(alias_body ta)"
check "tk unchanged" "tmux kill-session -t" "$(alias_body tk)"
check "td unchanged" "tmux detach" "$(alias_body td)"

# --- coreutils tr must keep working -----------------------------------------

check "tr is not aliased" "MISSING" "$(alias_body tr)"
check "tr still transforms input" "HELLO" "$(printf 'hello' | tr 'a-z' 'A-Z')"
check "tr survives sourcing the file" "HELLO" \
  "$(zsh -f -c "source '${settings}'; printf 'hello' | tr 'a-z' 'A-Z'")"

# --- functional rename against the disposable server ------------------------

# `zsh -f` keeps the pane from loading the caller's real shell configuration.
t -f /dev/null new-session -d -s original 'zsh -f' 2>/dev/null
if ! t has-session -t original 2>/dev/null; then
  printf 'SKIP: could not create a disposable tmux session\n' >&2
  exit 0
fi
check "harness is on the disposable socket" "${socket}" \
  "$(t display-message -p -t original '#{socket_path}')"
check "disposable server starts with only its own session" "original " \
  "$(session_names)"

# `trn NEW` renames the session the caller is in. Type it into a pane of the
# disposable server, where tmux itself sets $TMUX to that server.
t send-keys -t original \
  "source '${settings}'; eval 'trn renamed-current'" Enter
for _ in $(seq 1 20); do
  t has-session -t renamed-current 2>/dev/null && break
  sleep 0.25
done
check "trn renames the current session" "renamed-current " "$(session_names)"

t new-session -d -s second 'zsh -f' 2>/dev/null
out=$(run_zsh_alias "trn -t second renamed-target")
check "trn -t renames a targeted session quietly" "" "${out}"
check "both sessions carry their new names" \
  "renamed-current renamed-target " "$(session_names)"

if (( failures )); then
  printf 'FAIL: %d of %d rename assertions failed\n' "${failures}" \
    "${checks}" >&2
  exit 1
fi
printf 'ok: %d tmux rename assertions\n' "${checks}"
