#!/usr/bin/env bash
# Acceptance tests for bin/pbcopy and bin/pbpaste.
#
# Backends are faked on PATH so backend selection, failure handling and exit
# codes can be tested without a graphical session and without ever touching the
# real clipboard. A separate opt-in section (CLIPBOARD_LIVE_TESTS=1) performs a
# real round trip; it restores the existing clipboard (clearing it again when it
# was empty), skips when the clipboard holds non-text types it could not put
# back, and never prints clipboard contents.
#
# On macOS the helpers hand calls to /usr/bin/pbcopy and /usr/bin/pbpaste,
# which fakes on PATH cannot mask. Every case here therefore names the native
# binaries through PBCOPY_NATIVE/PBPASTE_NATIVE: a path that does not exist,
# or fakes that log what they get. The macOS cases set OSTYPE (bash keeps it
# from the environment), so they run on Linux too. The helpers run under the
# bash that runs this file: run it with /bin/bash 3.2 to test them there.

set -uo pipefail

# An inherited $TMUX names the caller's real server: never reach it.
unset TMUX TMUX_PANE

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
pbcopy=${repo_root}/bin/pbcopy
pbpaste=${repo_root}/bin/pbpaste
helper_bash=${BASH}

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

# The macOS natives, unless a case names its fakes: nothing at all.
export PBCOPY_NATIVE="${fakestate}/no-native/pbcopy"
export PBPASTE_NATIVE="${fakestate}/no-native/pbpaste"

# The helpers bound every Wayland/X11 probe and read with GNU timeout, and so
# does the live test. macOS has no timeout(1): a minimal stand-in (perl, which
# macOS ships) takes its place there, so the fake Wayland/X11 cases still run.
# CLIPBOARD_TEST_TIMEOUT_SHIM=1 forces it, to test it where coreutils exists.
if [[ ${CLIPBOARD_TEST_TIMEOUT_SHIM:-0} == 1 ]] \
    || ! timeout 5 true >/dev/null 2>&1; then
  mkdir -p "${fakestate}/shim"
  cat >"${fakestate}/shim/timeout" <<'SHIM'
#!/usr/bin/env perl
# timeout [--kill-after=DURATION] DURATION COMMAND [ARG]...: the part of GNU
# timeout the helpers use. COMMAND runs in a process group of its own, which
# gets TERM at the limit and KILL after the grace. Exit status: 124 when it
# timed out, 137 when it had to be killed, 127 when it was not found, else
# its own.
use strict;
use warnings;
use POSIX ();
use Time::HiRes ();

my $grace;
while (@ARGV && $ARGV[0] =~ /^--kill-after=(.+)$/) { $grace = $1; shift @ARGV; }
die "usage: timeout [--kill-after=DURATION] DURATION COMMAND...\n" if @ARGV < 2;
my $limit = shift @ARGV;
sub seconds { my ($d) = @_; $d =~ s/s$//; return $d + 0; }

my $pid = fork();
exit 125 unless defined $pid;
if ($pid == 0) {
    setpgrp(0, 0);
    exec { $ARGV[0] } @ARGV;
    exit($! == POSIX::ENOENT ? 127 : 126);
}
setpgrp($pid, $pid);
my ($timed_out, $killed) = (0, 0);
$SIG{ALRM} = sub {
    if (!$timed_out) {
        $timed_out = 1;
        kill '-TERM', $pid;
        Time::HiRes::alarm(seconds($grace)) if defined $grace;
    } else {
        $killed = 1;
        kill '-KILL', $pid;
    }
};
Time::HiRes::alarm(seconds($limit));
waitpid($pid, 0);
my $status = $?;
Time::HiRes::alarm(0);
exit 137 if $killed;
exit 124 if $timed_out;
exit(($status & 127) ? 128 + ($status & 127) : $status >> 8);
SHIM
  chmod 755 "${fakestate}/shim/timeout"
  export PATH="${fakestate}/shim:${PATH}"
fi

# SHA-256 of stdin: sha256sum (coreutils) or shasum (macOS, perl).
if printf '' | sha256sum >/dev/null 2>&1; then
  digest() { sha256sum | cut -c1-64; }
else
  digest() { shasum -a 256 | cut -c1-64; }
fi
check "sha-256 digests work" \
  e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 \
  "$(printf '' | digest)"

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
  env PATH="${fakepath}" ${envs[@]+"${envs[@]}"} "${helper_bash}" "${pbpaste}" "$@"
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
     FAKE_WL_ALIVE=1 "${helper_bash}" "${pbcopy}" --backend)"

check "pbpaste falls back to x11 when the wayland probe fails" x11 \
  "$(run_pbpaste "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=0 \
     DISPLAY=:0 FAKE_X11_ALIVE=1 --backend)"

check "pbcopy falls back to x11 when the wayland probe fails" x11 \
  "$(env PATH="${fakepath}" "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 \
     FAKE_WL_ALIVE=0 DISPLAY=:0 FAKE_X11_ALIVE=1 "${helper_bash}" "${pbcopy}" --backend)"

check "pbpaste ignores wayland tools without WAYLAND_DISPLAY" x11 \
  "$(run_pbpaste "${base_env[@]}" FAKE_WL_ALIVE=1 DISPLAY=:0 \
     FAKE_X11_ALIVE=1 --backend)"

check "pbcopy uses osc52 over ssh even with local tools present" osc52 \
  "$(env PATH="${fakepath}" "${base_env[@]}" SSH_TTY=/dev/pts/9 \
     WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=1 "${helper_bash}" "${pbcopy}" --backend)"

# --- an empty clipboard is a live backend, not a missing one ----------------

check "pbcopy picks wayland when the live selection is empty" wayland \
  "$(env PATH="${fakepath}" "${base_env[@]}" WAYLAND_DISPLAY=wayland-0 \
     FAKE_WL_ALIVE=1 FAKE_WL_EMPTY=1 "${helper_bash}" "${pbcopy}" --backend)"

rm -f "${FAKE_WL_SINK}" "${FAKE_BACKEND_USED}"
out=$(printf 'hello' | env PATH="${fakepath}" "${base_env[@]}" \
  WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=1 FAKE_WL_EMPTY=1 "${helper_bash}" "${pbcopy}")
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
     FAKE_X11_EMPTY=1 "${helper_bash}" "${pbcopy}" --backend)"
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
     DISPLAY=:0 "${helper_bash}" "${pbcopy}" --backend)"
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
  WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=1 "${helper_bash}" "${pbcopy}"
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
  "${helper_bash}" "${pbcopy}" 2>&1)
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
  WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=1 "${helper_bash}" "${pbcopy}" 2>&1 >/dev/null)
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

# --- macOS: the system pasteboard, through fakes -----------------------------

# The native fakes log their arguments and stdin; pbpaste's prints a payload.
nativebin="${fakestate}/native"
mkdir -p "${nativebin}"
cat >"${nativebin}/pbcopy" <<'FAKE'
#!/usr/bin/env bash
printf '[%s]\n' "$*" >"${FAKE_NATIVE_ARGS}"
cat >"${FAKE_NATIVE_SINK}"
FAKE
cat >"${nativebin}/pbpaste" <<'FAKE'
#!/usr/bin/env bash
printf '[%s]\n' "$*" >"${FAKE_NATIVE_ARGS}"
cat "${FAKE_NATIVE_DATA}"
FAKE
chmod 755 "${nativebin}/pbcopy" "${nativebin}/pbpaste"
export FAKE_NATIVE_ARGS="${fakestate}/native.args"
export FAKE_NATIVE_SINK="${fakestate}/native.sink"
export FAKE_NATIVE_DATA="${fakestate}/native.data"
printf '%s' "${fixture}" >"${FAKE_NATIVE_DATA}"

mac_env=(
  "${base_env[@]}"
  OSTYPE=darwin24
  SSH_CONNECTION=
  PBCOPY_NATIVE="${nativebin}/pbcopy"
  PBPASTE_NATIVE="${nativebin}/pbpaste"
)
native_reset() { rm -f "${FAKE_NATIVE_ARGS}" "${FAKE_NATIVE_SINK}"; }
native_args() { cat "${FAKE_NATIVE_ARGS}" 2>/dev/null || printf 'not called\n'; }

# Outside SSH the call goes to the native one unchanged: its own options and
# the payload byte for byte, nothing printed of our own.
native_reset
out=$(printf '%s' "${fixture}" | env PATH="${fakepath}" "${mac_env[@]}" \
  "${helper_bash}" "${pbcopy}" -pboard find)
status=$?
check "macos: pbcopy hands the call to the native pbcopy" 0 "${status}"
check "macos: pbcopy passes its arguments through" "[-pboard find]" "$(native_args)"
check "macos: pbcopy passes the payload byte for byte" \
  "$(printf '%s' "${fixture}" | od -An -c | tr -s ' ')" \
  "$(od -An -c <"${FAKE_NATIVE_SINK}" | tr -s ' ')"
check "macos: pbcopy prints nothing of its own" "" "${out}"

native_reset
actual=$(env PATH="${fakepath}" "${mac_env[@]}" "${helper_bash}" "${pbpaste}" \
  -Prefer txt | od -An -c | tr -s ' ')
check "macos: pbpaste reads the native pasteboard byte for byte" \
  "${expected}" "${actual}"
check "macos: pbpaste passes its arguments through" "[-Prefer txt]" "$(native_args)"

native_reset
check "macos: pbcopy --backend stays ours" macos \
  "$(env PATH="${fakepath}" "${mac_env[@]}" "${helper_bash}" "${pbcopy}" --backend)"
check "macos: pbpaste --backend stays ours" macos \
  "$(run_pbpaste "${mac_env[@]}" --backend)"
check "macos: --backend never calls the native one" "not called" "$(native_args)"
check_contains "macos: pbcopy --help stays ours" "OSC52" \
  "$(env PATH="${fakepath}" "${mac_env[@]}" "${helper_bash}" "${pbcopy}" --help)"

# The pasteboard pbcopy writes comes first, before an X11 or Wayland one
# that answers (XQuartz exports DISPLAY to every process); Linux keeps its
# order even where such a native exists.
check "macos: pbpaste prefers the native pasteboard to live X11/Wayland" macos \
  "$(run_pbpaste "${mac_env[@]}" WAYLAND_DISPLAY=wayland-0 FAKE_WL_ALIVE=1 \
     DISPLAY=:0 FAKE_X11_ALIVE=1 --backend)"
check "linux: pbpaste keeps wayland first" wayland \
  "$(run_pbpaste "${mac_env[@]}" OSTYPE=linux-gnu WAYLAND_DISPLAY=wayland-0 \
     FAKE_WL_ALIVE=1 DISPLAY=:0 FAKE_X11_ALIVE=1 --backend)"

# No pass-through anywhere else: on Linux, and over SSH (OSC52 then reaches
# the terminal the user sits at; pbpaste has nothing to read).
native_reset
out=$(printf 'x' | env PATH="${fakepath}" "${mac_env[@]}" OSTYPE=linux-gnu \
  "${helper_bash}" "${pbcopy}" -pboard find 2>&1)
status=$?
check "linux: pbcopy rejects native options" 2 "${status}"
check_contains "linux: pbcopy names the unknown option" "unknown option" "${out}"
check "linux: the native one is not called" "not called" "$(native_args)"

check "macos over ssh: pbcopy uses osc52" osc52 \
  "$(env PATH="${fakepath}" "${mac_env[@]}" SSH_TTY=/dev/ttys004 \
     SSH_CONNECTION='192.0.2.1 50000 192.0.2.2 22' "${helper_bash}" "${pbcopy}" --backend)"
native_reset
out=$(printf 'x' | env PATH="${fakepath}" "${mac_env[@]}" SSH_TTY=/dev/ttys004 \
  "${helper_bash}" "${pbcopy}")
check "macos over ssh: pbcopy writes OSC52" $'\033]52;c;eA==\a' "${out}"
check "macos over ssh: the native one is not called" "not called" "$(native_args)"
out=$(run_pbpaste "${mac_env[@]}" SSH_TTY=/dev/ttys004 2>&1)
status=$?
check "macos over ssh: pbpaste fails honestly" 1 "${status}"
check_contains "macos over ssh: pbpaste explains why" \
  "OSC52 does not provide clipboard reads" "${out}"

# A command run over SSH without a terminal (SSH_CONNECTION, no SSH_TTY)
# still copies to the Mac's pasteboard, as before, but gets no pass-through.
native_reset
out=$(printf 'y' | env PATH="${fakepath}" "${mac_env[@]}" \
  SSH_CONNECTION='192.0.2.1 50000 192.0.2.2 22' "${helper_bash}" "${pbcopy}")
check "macos over ssh without a tty: pbcopy writes the pasteboard" "y" \
  "$(cat "${FAKE_NATIVE_SINK}" 2>/dev/null)"
check "macos over ssh without a tty: no arguments for the native one" "[]" \
  "$(native_args)"
out=$(printf 'y' | env PATH="${fakepath}" "${mac_env[@]}" \
  SSH_CONNECTION='192.0.2.1 50000 192.0.2.2 22' "${helper_bash}" "${pbcopy}" \
  -pboard find 2>&1)
check "macos over ssh without a tty: pbcopy rejects native options" 2 "$?"

# Without the native binaries macOS falls back to the portable choice.
check "macos without native pbcopy: osc52 when nothing else answers" osc52 \
  "$(env PATH="${fakepath}" "${mac_env[@]}" PBCOPY_NATIVE="${fakestate}/missing" \
     "${helper_bash}" "${pbcopy}" --backend)"

# --tmux-buffer reads the tmux buffer without timeout(1), which macOS lacks;
# Linux keeps bounding it. The timeout here only logs that it was called.
macbin="${fakestate}/macbin"
mkdir -p "${macbin}"
cat >"${macbin}/tmux" <<'FAKE'
#!/usr/bin/env bash
[[ $* == 'save-buffer -' ]] || exit 99
cat "${FAKE_TMUX_BUFFER}"
FAKE
cat >"${macbin}/timeout" <<'FAKE'
#!/usr/bin/env bash
printf 'timeout %s\n' "$*" >>"${FAKE_TIMEOUT_LOG}"
shift 2
exec "$@"
FAKE
chmod 755 "${macbin}/tmux" "${macbin}/timeout"
export FAKE_TMUX_BUFFER="${fakestate}/tmux.buffer"
export FAKE_TIMEOUT_LOG="${fakestate}/timeout.log"
printf 'tmux ✓ buffer\n' >"${FAKE_TMUX_BUFFER}"
rm -f "${FAKE_TIMEOUT_LOG}"
out=$(env PATH="${macbin}:${fakepath}" "${mac_env[@]}" TMUX=/nonexistent/sock,1,0 \
  "${helper_bash}" "${pbpaste}" --tmux-buffer)
check "macos: --tmux-buffer reads the tmux buffer" "tmux ✓ buffer" "${out}"
check "macos: --tmux-buffer needs no timeout(1)" absent \
  "$([[ -e ${FAKE_TIMEOUT_LOG} ]] && echo present || echo absent)"
out=$(env PATH="${macbin}:${fakepath}" "${mac_env[@]}" OSTYPE=linux-gnu \
  TMUX=/nonexistent/sock,1,0 "${helper_bash}" "${pbpaste}" --tmux-buffer)
check "linux: --tmux-buffer reads the tmux buffer" "tmux ✓ buffer" "${out}"
check_contains "linux: --tmux-buffer stays bounded" "tmux save-buffer -" \
  "$(cat "${FAKE_TIMEOUT_LOG}" 2>/dev/null)"

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

# The same for the macOS pasteboard: `clipboard info` lists "type, size"
# pairs, e.g. «class utf8», 5, «class ut16», 12, string, 5, Unicode text, 10.
live_mac_text_types() {
  local -a items=()
  local i
  IFS=, read -r -a items || [[ ${#items[@]} -gt 0 ]] || return 0
  for (( i = 0; i < ${#items[@]}; i += 2 )); do
    case ${items[i]# } in
      '«class utf8»'|'«class ut16»'|'«class TEXT»'|string|'Unicode text') ;;
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
    macos)
      types=$(osascript -e 'clipboard info' 2>"${err_file}") || return 1
      if [[ -z ${types} ]]; then
        live_state=empty
        return 0
      fi
      live_state=text
      live_mac_text_types <<<"${types}" && return 0
      [[ ${CLIPBOARD_LIVE_FORCE:-0} == 1 ]] && return 0
      return 2
      ;;
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
      "$(printf '' | digest)" \
      "$("${pbpaste}" | digest)"
  else
    "${pbcopy}" <"${saved}"
    check "live ${backend} clipboard restored" \
      "$(digest <"${saved}")" \
      "$("${pbpaste}" | digest)"
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

# The macOS pasteboard's types decide the same way.
live_mac_text_types <<<'«class utf8», 5, «class ut16», 12, string, 5, Unicode text, 10'
check "live macOS text types are restorable" 0 "$?"
live_mac_text_types <<<'«class PNGf», 1234, «class utf8», 5'
check "live macOS image types are not" 1 "$?"
live_mac_text_types <<<'«class HTML», 40, «class utf8», 5'
check "live macOS rich text is not" 1 "$?"

if [[ ${CLIPBOARD_LIVE_TESTS:-0} == 1 ]]; then
  # The real clipboard, through the real native binaries on macOS.
  unset PBCOPY_NATIVE PBPASTE_NATIVE
  live_round_trip
fi

if (( failures )); then
  printf 'FAIL: %d of %d clipboard assertions failed\n' "${failures}" \
    "${checks}" >&2
  exit 1
fi
printf 'ok: %d clipboard helper assertions\n' "${checks}"
