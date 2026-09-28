#!/usr/bin/env bash
# Acceptance tests for bin/pbcopy and bin/pbpaste.
#
# Backends are faked on PATH so backend selection, failure handling and exit
# codes can be tested without a graphical session and without ever touching the
# real clipboard. A separate opt-in section (CLIPBOARD_LIVE_TESTS=1) performs a
# real round trip; it saves and restores the existing clipboard and never
# prints clipboard contents.

set -uo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
pbcopy=${repo_root}/bin/pbcopy
pbpaste=${repo_root}/bin/pbpaste

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

check_contains() {
  local label=$1 needle=$2 haystack=$3
  checks=$((checks + 1))
  if [[ ${haystack} == *"${needle}"* ]]; then
    return 0
  fi
  printf 'FAIL: %s\n  expected to contain: %q\n  actual: %q\n' \
    "${label}" "${needle}" "${haystack}" >&2
  failures=$((failures + 1))
}

check_not_contains() {
  local label=$1 needle=$2 haystack=$3
  checks=$((checks + 1))
  if [[ ${haystack} != *"${needle}"* ]]; then
    return 0
  fi
  printf 'FAIL: %s\n  must not contain: %q\n' "${label}" "${needle}" >&2
  failures=$((failures + 1))
}

fakebin=$(mktemp -d)
fakestate=$(mktemp -d)
cleanup() { rm -rf -- "${fakebin}" "${fakestate}"; }
trap cleanup EXIT

cat >"${fakebin}/wl-paste" <<'FAKE'
#!/usr/bin/env bash
if [[ ${FAKE_WL_ALIVE:-0} != 1 ]]; then
  if [[ ${FAKE_WL_HANG:-0} == 1 ]]; then sleep 30; fi
  exit 1
fi
case ${1:-} in
  --list-types) printf '%s\n' "${FAKE_WL_TYPES:-text/plain;charset=utf-8}" ;;
  --no-newline)
    if [[ ${FAKE_WL_READ_HANG:-0} == 1 ]]; then sleep 30; fi
    cat "${FAKE_WL_DATA}"
    ;;
  *) exit 2 ;;
esac
FAKE

cat >"${fakebin}/wl-copy" <<'FAKE'
#!/usr/bin/env bash
cat >"${FAKE_WL_SINK}"
printf 'wayland\n' >"${FAKE_BACKEND_USED}"
# Real wl-copy daemonizes to serve the selection and keeps every descriptor it
# inherited. Reproduce that so pbcopy is forced to detach them.
if [[ ${FAKE_WL_COPY_LINGER:-0} != 0 ]]; then
  sleep "${FAKE_WL_COPY_LINGER}" &
fi
FAKE

cat >"${fakebin}/xclip" <<'FAKE'
#!/usr/bin/env bash
[[ ${FAKE_X11_ALIVE:-0} == 1 ]] || exit 1
args=" $* "
if [[ ${args} == *" -target TARGETS "* ]]; then
  printf 'TARGETS\nUTF8_STRING\ntext/plain\n'
  exit 0
fi
if [[ ${args} == *" -in "* ]]; then
  cat >"${FAKE_X11_SINK}"
  printf 'x11\n' >"${FAKE_BACKEND_USED}"
  exit 0
fi
cat "${FAKE_X11_DATA}"
FAKE

chmod 755 "${fakebin}"/wl-paste "${fakebin}"/wl-copy "${fakebin}"/xclip

# Keep coreutils (timeout, cat, base64...) reachable; only clipboard tools fake.
fakepath="${fakebin}:${PATH}"

export FAKE_WL_DATA="${fakestate}/wl.data"
export FAKE_X11_DATA="${fakestate}/x11.data"
export FAKE_WL_SINK="${fakestate}/wl.sink"
export FAKE_X11_SINK="${fakestate}/x11.sink"
export FAKE_BACKEND_USED="${fakestate}/backend.used"

fixture=$'first line\nsecond ✓ línea\ttab\n'
printf '%s' "${fixture}" >"${FAKE_WL_DATA}"
printf '%s' "${fixture}" >"${FAKE_X11_DATA}"

# Leading NAME=VALUE words are environment; the rest are script arguments.
run_pbpaste() {
  local -a envs=()
  while (( $# )) && [[ $1 == *=* ]]; do envs+=("$1"); shift; done
  env PATH="${fakepath}" "${envs[@]}" bash "${pbpaste}" "$@"
}

base_env=(
  SSH_TTY=
  WAYLAND_DISPLAY=
  DISPLAY=
  TMUX=
  FAKE_WL_ALIVE=0
  FAKE_X11_ALIVE=0
)

# --- backend selection -------------------------------------------------------

check "pbpaste picks wayland when the compositor answers" wayland \
  "$(run_pbpaste "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=1 \
     --backend)"

check "pbcopy picks wayland when the compositor answers" wayland \
  "$(env PATH="${fakepath}" "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 \
     FAKE_WL_ALIVE=1 bash "${pbcopy}" --backend)"

check "pbpaste falls back to x11 when the wayland probe fails" x11 \
  "$(run_pbpaste "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=0 \
     DISPLAY=:0 FAKE_X11_ALIVE=1 --backend)"

check "pbcopy falls back to x11 when the wayland probe fails" x11 \
  "$(env PATH="${fakepath}" "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 \
     FAKE_WL_ALIVE=0 DISPLAY=:0 FAKE_X11_ALIVE=1 bash "${pbcopy}" --backend)"

check "pbpaste ignores wayland tools without WAYLAND_DISPLAY" x11 \
  "$(run_pbpaste "${base_env[@]}" FAKE_WL_ALIVE=1 DISPLAY=:0 \
     FAKE_X11_ALIVE=1 --backend)"

check "pbcopy uses osc52 over ssh even with local tools present" osc52 \
  "$(env PATH="${fakepath}" "${base_env[@]}" SSH_TTY=/dev/pts/9 \
     WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=1 bash "${pbcopy}" --backend)"

# --- honest failure instead of a stale buffer --------------------------------

out=$(run_pbpaste "${base_env[@]}" SSH_TTY=/dev/pts/9 2>&1)
status=$?
check "pbpaste fails without a local backend" 1 "${status}"
check_contains "pbpaste explains that OSC52 cannot read" \
  "OSC52 does not provide clipboard reads" "${out}"
check_not_contains "pbpaste does not invent a backend" "wayland" "${out}"

out=$(run_pbpaste "${base_env[@]}" 2>&1)
status=$?
check "pbpaste fails locally with no usable backend" 1 "${status}"
check_contains "pbpaste names the backends it tried" "wl-paste" "${out}"

out=$(run_pbpaste "${base_env[@]}" --tmux-buffer 2>&1)
status=$?
check "--tmux-buffer without tmux fails" 1 "${status}"
check_contains "--tmux-buffer explains the requirement" \
  "requires a tmux client" "${out}"

# A wayland probe that hangs must not hang pbpaste.
start=${SECONDS}
out=$(run_pbpaste "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 FAKE_WL_HANG=1 \
  PBPASTE_PROBE_TIMEOUT=0.3 2>&1)
status=$?
elapsed=$((SECONDS - start))
check "a hanging compositor probe still fails" 1 "${status}"
checks=$((checks + 1))
if (( elapsed > 5 )); then
  printf 'FAIL: hanging probe was not bounded (%ss)\n' "${elapsed}" >&2
  failures=$((failures + 1))
fi

# --- payload fidelity --------------------------------------------------------

actual=$(run_pbpaste "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 \
  FAKE_WL_ALIVE=1 | od -An -c | tr -s ' ')
expected=$(printf '%s' "${fixture}" | od -An -c | tr -s ' ')
check "wayland read preserves bytes exactly" "${expected}" "${actual}"

actual=$(run_pbpaste "${base_env[@]}" DISPLAY=:0 FAKE_X11_ALIVE=1 \
  | od -An -c | tr -s ' ')
check "x11 read preserves bytes exactly" "${expected}" "${actual}"

rm -f "${FAKE_WL_SINK}" "${FAKE_BACKEND_USED}"
printf '%s' "${fixture}" | env PATH="${fakepath}" "${base_env[@]}" \
  WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=1 bash "${pbcopy}"
check "pbcopy writes to the wayland sink" "wayland" \
  "$(cat "${FAKE_BACKEND_USED}" 2>/dev/null)"
check "pbcopy preserves bytes exactly" \
  "$(printf '%s' "${fixture}" | od -An -c | tr -s ' ')" \
  "$(od -An -c <"${FAKE_WL_SINK}" | tr -s ' ')"

# The serving process must not keep the caller's stdout/stderr open, or every
# script that pipes into pbcopy would hang until the selection is replaced.
start=${SECONDS}
out=$(printf 'x' | env PATH="${fakepath}" "${base_env[@]}" \
  WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=1 FAKE_WL_COPY_LINGER=20 \
  bash "${pbcopy}" 2>&1)
elapsed=$((SECONDS - start))
checks=$((checks + 1))
if (( elapsed > 5 )); then
  printf 'FAIL: pbcopy held the caller open for %ss\n' "${elapsed}" >&2
  failures=$((failures + 1))
fi

# --- timeout propagation -----------------------------------------------------

out=$(run_pbpaste "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=1 \
  FAKE_WL_READ_HANG=1 PBPASTE_READ_TIMEOUT=0.3 2>&1 >/dev/null)
status=${PIPESTATUS[0]}
check_contains "a hanging read reports a timeout" "timed out" "${out}"

# --- no payload ever reaches the logs ---------------------------------------

secret='CLIPBOARD_CANARY_9f2b'
printf '%s' "${secret}" >"${FAKE_WL_DATA}"
out=$(run_pbpaste "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=1 \
  2>&1 >/dev/null)
check_not_contains "pbpaste never logs the payload" "${secret}" "${out}"
out=$(printf '%s' "${secret}" | env PATH="${fakepath}" "${base_env[@]}" \
  WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=1 bash "${pbcopy}" 2>&1 >/dev/null)
check_not_contains "pbcopy never logs the payload" "${secret}" "${out}"
printf '%s' "${fixture}" >"${FAKE_WL_DATA}"

# --- callable from a plain POSIX shell and from tmux run-shell ---------------

actual=$(env PATH="${fakepath}" "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 \
  FAKE_WL_ALIVE=1 sh -c "'${pbpaste}' --backend")
check "pbpaste runs under sh -c" wayland "${actual}"
actual=$(env PATH="${fakepath}" "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 \
  FAKE_WL_ALIVE=1 sh -c "'${pbcopy}' --backend")
check "pbcopy runs under sh -c" wayland "${actual}"

if command -v tmux >/dev/null 2>&1; then
  socket="${fakestate}/tmux.sock"
  out_file="${fakestate}/tmux.out"
  env PATH="${fakepath}" tmux -f /dev/null -S "${socket}" \
    new-session -d -s helpers 2>/dev/null
  if tmux -S "${socket}" has-session -t helpers 2>/dev/null; then
    tmux -S "${socket}" set-environment -g WAYLAND_DISPLAY wayland-0
    tmux -S "${socket}" set-environment -g FAKE_WL_ALIVE 1
    tmux -S "${socket}" set-environment -g PATH "${fakepath}"
    tmux -S "${socket}" run-shell \
      "'${pbpaste}' --backend > '${out_file}' 2>&1"
    check "pbpaste runs under tmux run-shell" wayland \
      "$(cat "${out_file}" 2>/dev/null)"
    tmux -S "${socket}" kill-server 2>/dev/null
  else
    printf 'SKIP: tmux session could not be created\n' >&2
  fi
else
  printf 'SKIP: tmux not installed; tmux run-shell case not exercised\n' >&2
fi

# --- optional live round trip ------------------------------------------------

if [[ ${CLIPBOARD_LIVE_TESTS:-0} == 1 ]]; then
  if backend=$("${pbpaste}" --backend 2>/dev/null); then
    saved="${fakestate}/clipboard.saved"
    have_saved=0
    if "${pbpaste}" >"${saved}" 2>/dev/null; then have_saved=1; fi
    live_fixture=$'live ✓ fixture\nwith trailing newline\n'
    printf '%s' "${live_fixture}" | "${pbcopy}"
    actual=$("${pbpaste}" | od -An -c | tr -s ' ')
    expected=$(printf '%s' "${live_fixture}" | od -An -c | tr -s ' ')
    check "live ${backend} round trip preserves bytes" \
      "${expected}" "${actual}"
    if (( have_saved )); then "${pbcopy}" <"${saved}"; fi
    rm -f "${saved}"
  else
    printf 'SKIP: CLIPBOARD_LIVE_TESTS set but no local backend\n' >&2
  fi
fi

if (( failures )); then
  printf 'FAIL: %d of %d clipboard assertions failed\n' "${failures}" \
    "${checks}" >&2
  exit 1
fi
printf 'ok: %d clipboard helper assertions\n' "${checks}"
