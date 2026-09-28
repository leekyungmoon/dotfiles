#!/usr/bin/env bash
# Acceptance tests for bin/pbcopy and bin/pbpaste.
#
# Backends are faked on PATH so backend selection, failure handling and exit
# codes can be tested without a graphical session and without ever touching the
# real clipboard. A separate opt-in section (CLIPBOARD_LIVE_TESTS=1) performs a
# real round trip; it restores the existing clipboard (clearing it again when it
# was empty), skips when the clipboard holds non-text types it could not put
# back, and never prints clipboard contents.

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

# The fakes reproduce wl-clipboard 2.2.1 and xclip 0.13 diagnostics: both
# exit 1 for "no server" and for "empty selection", and only the message tells
# them apart.
cat >"${fakebin}/wl-paste" <<'FAKE'
#!/usr/bin/env bash
if [[ ${FAKE_WL_ALIVE:-0} != 1 ]]; then
  if [[ ${FAKE_WL_HANG:-0} == 1 ]]; then sleep 30; fi
  printf 'Failed to connect to a Wayland server: No such file or directory\n' >&2
  exit 1
fi
if [[ ${FAKE_WL_EMPTY:-0} == 1 && ${1:-} != --help ]]; then
  printf 'Nothing is copied\n' >&2
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
if [[ ${FAKE_X11_ALIVE:-0} != 1 ]]; then
  printf "Error: Can't open display: %s\n" "${DISPLAY:-}" >&2
  exit 1
fi
args=" $* "
if [[ ${args} == *" -in "* ]]; then
  cat >"${FAKE_X11_SINK}"
  printf 'x11\n' >"${FAKE_BACKEND_USED}"
  exit 0
fi
if [[ ${args} == *" -target TARGETS "* ]]; then
  if [[ ${FAKE_X11_EMPTY:-0} == 1 ]]; then
    printf 'Error: target TARGETS not available\n' >&2
    exit 1
  fi
  printf 'TARGETS\nUTF8_STRING\ntext/plain\n'
  exit 0
fi
if [[ ${FAKE_X11_EMPTY:-0} == 1 ]]; then
  printf 'Error: target STRING not available\n' >&2
  exit 1
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

# --- an empty clipboard is a live backend, not a missing one ----------------

check "pbcopy picks wayland when the live selection is empty" wayland \
  "$(env PATH="${fakepath}" "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 \
     FAKE_WL_ALIVE=1 FAKE_WL_EMPTY=1 bash "${pbcopy}" --backend)"

rm -f "${FAKE_WL_SINK}" "${FAKE_BACKEND_USED}"
out=$(printf 'hello' | env PATH="${fakepath}" "${base_env[@]}" \
  WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=1 FAKE_WL_EMPTY=1 bash "${pbcopy}")
check "pbcopy into an empty wayland clipboard uses wl-copy" wayland \
  "$(cat "${FAKE_BACKEND_USED}" 2>/dev/null)"
check "pbcopy into an empty wayland clipboard writes the payload" hello \
  "$(cat "${FAKE_WL_SINK}" 2>/dev/null)"
check_not_contains "pbcopy into an empty wayland clipboard emits no OSC52" \
  $'\033]52' "${out}"

check "pbpaste picks wayland when the live selection is empty" wayland \
  "$(run_pbpaste "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=1 \
     FAKE_WL_EMPTY=1 --backend)"
out=$(run_pbpaste "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=1 \
  FAKE_WL_EMPTY=1 2>"${fakestate}/stderr")
status=$?
check "pbpaste on an empty wayland clipboard succeeds" 0 "${status}"
check "pbpaste on an empty wayland clipboard prints nothing" "" "${out}"
check "pbpaste on an empty wayland clipboard reports nothing" "" \
  "$(cat "${fakestate}/stderr")"

check "pbcopy picks x11 when the X clipboard has no owner" x11 \
  "$(env PATH="${fakepath}" "${base_env[@]}" DISPLAY=:0 FAKE_X11_ALIVE=1 \
     FAKE_X11_EMPTY=1 bash "${pbcopy}" --backend)"
out=$(run_pbpaste "${base_env[@]}" DISPLAY=:0 FAKE_X11_ALIVE=1 \
  FAKE_X11_EMPTY=1 2>"${fakestate}/stderr")
status=$?
check "pbpaste on an ownerless X clipboard succeeds" 0 "${status}"
check "pbpaste on an ownerless X clipboard prints nothing" "" "${out}"
check "pbpaste on an ownerless X clipboard reports nothing" "" \
  "$(cat "${fakestate}/stderr")"

# A compositor or X server that cannot be reached is still "no backend".
check "pbcopy still falls back to osc52 when nothing answers" osc52 \
  "$(env PATH="${fakepath}" "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 \
     DISPLAY=:0 bash "${pbcopy}" --backend)"
out=$(run_pbpaste "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 DISPLAY=:0 2>&1)
status=$?
check "pbpaste still fails when nothing answers" 1 "${status}"
check_contains "pbpaste still names the backends it tried" "wl-paste" "${out}"

# A real read error is reported, not swallowed as "empty".
out=$(run_pbpaste "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=1 \
  FAKE_WL_DATA="${fakestate}/missing" 2>&1 >/dev/null)
status=$?
check "pbpaste propagates a failing read" 1 "${status}"
check_contains "pbpaste shows the backend's own error" "missing" "${out}"

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

# --- live round trip, and the restore logic it depends on -------------------

# The live test may only touch a clipboard it can put back exactly. It learns
# the current types first and sets live_state:
#   empty  nothing is copied; restored by clearing the selection again
#   text   plain text only; saved through pbpaste and restored through pbcopy
# Anything else (images, text/html, file lists...) cannot be restored from a
# single text stream, so the test is skipped unless CLIPBOARD_LIVE_FORCE=1.
live_text_types() {
  local t
  while IFS= read -r t; do
    case ${t} in
      ''|TARGETS|TIMESTAMP|MULTIPLE|SAVE_TARGETS|UTF8_STRING|STRING|TEXT) ;;
      COMPOUND_TEXT|text/plain|'text/plain;'*) ;;
      *) return 1 ;;
    esac
  done
}

live_probe_state() {
  local backend=$1 types status err_file="${fakestate}/live.err"
  case ${backend} in
    wayland)
      types=$(timeout 2 wl-paste --list-types 2>"${err_file}"); status=$? ;;
    x11)
      types=$(timeout 2 xclip -selection clipboard -out -target TARGETS \
        2>"${err_file}"); status=$? ;;
    *) live_state=text; return 0 ;;
  esac
  if (( status != 0 )); then
    if grep -q -e 'Nothing is copied' -e 'not available' "${err_file}"; then
      live_state=empty
      return 0
    fi
    return 1
  fi
  live_state=text
  live_text_types <<<"${types}" && return 0
  [[ ${CLIPBOARD_LIVE_FORCE:-0} == 1 ]] && return 0
  return 2
}

live_clear() {
  case $1 in
    wayland) wl-copy --clear >/dev/null 2>&1 ;;
    *) printf '' | "${pbcopy}" ;;
  esac
}

# Round trip through whatever backend pbpaste picks from the current
# environment; returns 3 when it skipped. Clipboard contents are compared by
# digest so a failure never prints them.
live_round_trip() {
  local backend saved="${fakestate}/clipboard.saved" status
  backend=$("${pbpaste}" --backend 2>/dev/null) || {
    printf 'SKIP: live clipboard: no local backend\n' >&2
    return 3
  }
  live_state=""
  live_probe_state "${backend}"
  status=$?
  if (( status == 2 )); then
    printf 'SKIP: live %s clipboard holds non-text types; not touching it ' \
      "${backend}" >&2
    printf '(CLIPBOARD_LIVE_FORCE=1 keeps only its text)\n' >&2
    return 3
  elif (( status != 0 )); then
    printf 'SKIP: live %s clipboard state could not be read\n' "${backend}" >&2
    return 3
  fi
  if [[ ${live_state} == text ]] && ! "${pbpaste}" >"${saved}" 2>/dev/null; then
    printf 'SKIP: live %s clipboard could not be saved\n' "${backend}" >&2
    rm -f "${saved}"
    return 3
  fi
  local live_fixture=$'live ✓ fixture\nwith trailing newline\n'
  printf '%s' "${live_fixture}" | "${pbcopy}"
  check "live ${backend} round trip preserves bytes" \
    "$(printf '%s' "${live_fixture}" | od -An -c | tr -s ' ')" \
    "$("${pbpaste}" | od -An -c | tr -s ' ')"
  if [[ ${live_state} == empty ]]; then
    live_clear "${backend}"
    check "live ${backend} clipboard is empty again" \
      "$(printf '' | sha256sum | cut -c1-64)" \
      "$("${pbpaste}" | sha256sum | cut -c1-64)"
  else
    "${pbcopy}" <"${saved}"
    check "live ${backend} clipboard restored" \
      "$(sha256sum <"${saved}" | cut -c1-64)" \
      "$("${pbpaste}" | sha256sum | cut -c1-64)"
  fi
  rm -f "${saved}"
}

# Exercise the restore logic against a stateful fake Wayland clipboard, so the
# live test's own safety is checked on every run without a real session.
livebin="${fakestate}/livebin"
livestate="${fakestate}/livestate"
mkdir -p "${livebin}" "${livestate}"
cat >"${livebin}/wl-copy" <<'FAKE'
#!/usr/bin/env bash
printf 'wl-copy %s\n' "$*" >>"${LIVE_STATE}/calls"
if [[ ${1:-} == --clear ]]; then
  rm -f "${LIVE_STATE}/types" "${LIVE_STATE}/data"
  exit 0
fi
cat >"${LIVE_STATE}/data"
printf 'text/plain;charset=utf-8\nUTF8_STRING\n' >"${LIVE_STATE}/types"
FAKE
cat >"${livebin}/wl-paste" <<'FAKE'
#!/usr/bin/env bash
if [[ ! -e ${LIVE_STATE}/types ]]; then
  printf 'Nothing is copied\n' >&2
  exit 1
fi
case ${1:-} in
  --list-types) cat "${LIVE_STATE}/types" ;;
  --no-newline) cat "${LIVE_STATE}/data" ;;
  *) exit 2 ;;
esac
FAKE
chmod 755 "${livebin}/wl-copy" "${livebin}/wl-paste"

run_fake_live() {
  local -x PATH="${livebin}:${PATH}" LIVE_STATE="${livestate}"
  local -x WAYLAND_DISPLAY=wayland-0 DISPLAY= SSH_TTY= TMUX=
  local -x CLIPBOARD_LIVE_FORCE=0
  live_round_trip
}

rm -f "${livestate}"/*
run_fake_live 2>/dev/null
check "live test ran on an empty clipboard" 0 "$?"
check "live test leaves an empty clipboard empty" absent \
  "$([[ -e ${livestate}/types ]] && echo present || echo absent)"

rm -f "${livestate}"/*
printf 'saved text\n' >"${livestate}/data"
printf 'text/plain;charset=utf-8\nUTF8_STRING\nTEXT\n' >"${livestate}/types"
run_fake_live 2>/dev/null
check "live test ran on a text clipboard" 0 "$?"
check "live test restores a text clipboard byte for byte" \
  "$(printf 'saved text\n' | od -An -c | tr -s ' ')" \
  "$(od -An -c <"${livestate}/data" | tr -s ' ')"

rm -f "${livestate}"/*
printf 'PNGDATA' >"${livestate}/data"
printf 'image/png\ntext/html\n' >"${livestate}/types"
run_fake_live 2>/dev/null
check "live test skips a clipboard with non-text types" 3 "$?"
check "live test leaves a non-text clipboard untouched" PNGDATA \
  "$(cat "${livestate}/data")"
check "live test never writes to a non-text clipboard" absent \
  "$([[ -e ${livestate}/calls ]] && echo present || echo absent)"

if [[ ${CLIPBOARD_LIVE_TESTS:-0} == 1 ]]; then
  live_round_trip
fi

if (( failures )); then
  printf 'FAIL: %d of %d clipboard assertions failed\n' "${failures}" \
    "${checks}" >&2
  exit 1
fi
printf 'ok: %d clipboard helper assertions\n' "${checks}"
