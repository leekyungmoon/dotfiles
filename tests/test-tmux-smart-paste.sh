#!/usr/bin/env bash

set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
script_path=${script_dir}/../bin/tmux-smart-paste
test_root=$(mktemp -d)
trap 'rm -rf -- "${test_root}"' EXIT

mock_bin=${test_root}/bin
mkdir -p -- "${mock_bin}"

cat >"${mock_bin}/wl-paste" <<'EOF'
#!/usr/bin/env bash
if [[ ${1:-} == --list-types ]]; then
  mode=${MOCK_WL_TYPES_MODE:-ok}
  payload=${MOCK_CLIPBOARD_TYPES:-}
else
  mode=${MOCK_WL_PAYLOAD_MODE:-ok}
  payload=${MOCK_CLIPBOARD_TEXT:-}
fi
printf 'wl-paste:%s\n' "$*" >>"${MOCK_BACKEND_LOG}"
case ${mode} in
  fail)
    exit 1
    ;;
  slow)
    sleep "${MOCK_WL_DELAY:-0.45}"
    ;;
  hang)
    sleep 5
    ;;
  ignore-term|partial-ignore-term)
    if [[ ${mode} == partial-ignore-term ]]; then
      printf '%s' "${payload}"
    fi
    if [[ -n ${MOCK_WL_PID_FILE:-} ]]; then
      printf '%s\n' "$$" >"${MOCK_WL_PID_FILE}"
    fi
    trap '' TERM
    while :; do
      sleep 5
    done
    ;;
esac
if [[ ${1:-} == --list-types ]]; then
  printf '%s\n' "${payload}"
else
  printf '%s' "${payload}"
fi
EOF

cat >"${mock_bin}/xclip" <<'EOF'
#!/usr/bin/env bash
case " $* " in
  *" -t TARGETS "*|*" -target TARGETS "*)
    mode=${MOCK_XCLIP_TYPES_MODE:-ok}
    payload=${MOCK_X11_CLIPBOARD_TYPES:-}
    ;;
  *)
    mode=${MOCK_XCLIP_PAYLOAD_MODE:-ok}
    payload=${MOCK_X11_CLIPBOARD_TEXT:-}
    ;;
esac
printf 'xclip:%s\n' "$*" >>"${MOCK_BACKEND_LOG}"
# Xwayland authorizes the X11 bridge through this variable.
printf 'xclip:XAUTHORITY=%s\n' "${XAUTHORITY-<unset>}" >>"${MOCK_ENV_LOG}"
case ${mode} in
  fail)
    exit 1
    ;;
  slow)
    sleep "${MOCK_XCLIP_DELAY:-0.45}"
    ;;
  hang)
    sleep 5
    ;;
  ignore-term|partial-ignore-term)
    if [[ ${mode} == partial-ignore-term ]]; then
      printf '%s' "${payload}"
    fi
    if [[ -n ${MOCK_XCLIP_PID_FILE:-} ]]; then
      printf '%s\n' "$$" >"${MOCK_XCLIP_PID_FILE}"
    fi
    trap '' TERM
    while :; do
      sleep 5
    done
    ;;
esac
case " $* " in
  *" -t TARGETS "*|*" -target TARGETS "*)
    printf '%s\n' "${payload}"
    ;;
  *)
    printf '%s' "${payload}"
    ;;
esac
EOF

cat >"${mock_bin}/tmux" <<'EOF'
#!/usr/bin/env bash
case ${1:-} in
  show-environment)
    printf 'show-environment:%s\n' "$*" >>"${MOCK_LOG}"
    environment_name=${3:-}
    if [[ -z ${environment_name} ]]; then
      [[ -n ${MOCK_TMUX_GLOBAL_DISPLAY:-} ]] &&
        printf 'DISPLAY=%s\n' "${MOCK_TMUX_GLOBAL_DISPLAY}"
      [[ -n ${MOCK_TMUX_GLOBAL_WAYLAND_DISPLAY:-} ]] &&
        printf 'WAYLAND_DISPLAY=%s\n' \
          "${MOCK_TMUX_GLOBAL_WAYLAND_DISPLAY}"
      [[ -n ${MOCK_TMUX_GLOBAL_XAUTHORITY:-} ]] &&
        printf 'XAUTHORITY=%s\n' "${MOCK_TMUX_GLOBAL_XAUTHORITY}"
      [[ -n ${MOCK_TMUX_GLOBAL_XDG_RUNTIME_DIR:-} ]] &&
        printf 'XDG_RUNTIME_DIR=%s\n' \
          "${MOCK_TMUX_GLOBAL_XDG_RUNTIME_DIR}"
      [[ -n ${MOCK_TMUX_GLOBAL_XDG_SESSION_TYPE:-} ]] &&
        printf 'XDG_SESSION_TYPE=%s\n' \
          "${MOCK_TMUX_GLOBAL_XDG_SESSION_TYPE}"
      [[ -n ${MOCK_TMUX_GLOBAL_DBUS_SESSION_BUS_ADDRESS:-} ]] &&
        printf 'DBUS_SESSION_BUS_ADDRESS=%s\n' \
          "${MOCK_TMUX_GLOBAL_DBUS_SESSION_BUS_ADDRESS}"
      exit 0
    fi
    case ${environment_name} in
      DISPLAY)
        environment_value=${MOCK_TMUX_GLOBAL_DISPLAY:-}
        ;;
      WAYLAND_DISPLAY)
        environment_value=${MOCK_TMUX_GLOBAL_WAYLAND_DISPLAY:-}
        ;;
      XAUTHORITY)
        environment_value=${MOCK_TMUX_GLOBAL_XAUTHORITY:-}
        ;;
      XDG_RUNTIME_DIR)
        environment_value=${MOCK_TMUX_GLOBAL_XDG_RUNTIME_DIR:-}
        ;;
      XDG_SESSION_TYPE)
        environment_value=${MOCK_TMUX_GLOBAL_XDG_SESSION_TYPE:-}
        ;;
      DBUS_SESSION_BUS_ADDRESS)
        environment_value=${MOCK_TMUX_GLOBAL_DBUS_SESSION_BUS_ADDRESS:-}
        ;;
      *)
        exit 1
        ;;
    esac
    [[ -n ${environment_value} ]] || exit 1
    printf '%s=%s\n' "${environment_name}" "${environment_value}"
    ;;
  display-message)
    # The helper's first tmux call: what the caller (pane binding) had.
    printf 'display-message:XAUTHORITY=%s\n' "${XAUTHORITY-<unset>}" \
      >>"${MOCK_ENV_LOG}"
    printf '/dev/pts/77|||TSP|||%s\n' \
      "${MOCK_PANE_CURRENT_COMMAND:-zsh}"
    ;;
  load-buffer)
    cat >"${MOCK_PAYLOAD_FILE}"
    printf 'load-buffer:%s\n' "$*" >>"${MOCK_LOG}"
    ;;
  paste-buffer)
    printf 'paste-buffer:%s\n' "$*" >>"${MOCK_LOG}"
    if [[ ${MOCK_TMUX_PASTE_MODE:-ok} == fail ]]; then
      exit 1
    fi
    ;;
  send-keys)
    printf 'send-keys:%s\n' "$*" >>"${MOCK_LOG}"
    ;;
  delete-buffer)
    printf 'delete-buffer:%s\n' "$*" >>"${MOCK_LOG}"
    ;;
  *)
    exit 99
    ;;
esac
EOF

cat >"${mock_bin}/ps" <<'EOF'
#!/usr/bin/env bash
if [[ ${1:-} != -t || ${2:-} != pts/77 || ${3:-} != -o ||
  ${4:-} != comm=,pgid=,tpgid= ]]; then
  exit 98
fi
printf '%s\n' "${MOCK_TTY_COMMANDS}"
EOF

chmod +x \
  "${mock_bin}/wl-paste" \
  "${mock_bin}/xclip" \
  "${mock_bin}/tmux" \
  "${mock_bin}/ps"

run_case() {
  : >"${MOCK_LOG}"
  : >"${MOCK_BACKEND_LOG}"
  : >"${MOCK_PAYLOAD_FILE}"
  : >"${MOCK_ENV_LOG}"
  rm -f -- "${MOCK_WL_PID_FILE}" "${MOCK_XCLIP_PID_FILE}"
  # The caller's XAUTHORITY is explicit: set, or truly absent (never the
  # value of whatever shell runs this test).
  if [[ -n ${MOCK_SESSION_XAUTHORITY} ]]; then
    caller_xauthority=("XAUTHORITY=${MOCK_SESSION_XAUTHORITY}")
  else
    caller_xauthority=(-u XAUTHORITY)
  fi
  command=(/usr/bin/env "${caller_xauthority[@]}" "${script_path}" %77)
  if [[ -n ${MOCK_OUTER_TIMEOUT:-} ]]; then
    command=(timeout --kill-after=0.20s \
      "${MOCK_OUTER_TIMEOUT}" "${command[@]}")
  fi
  PATH="${mock_bin}:/usr/bin:/bin" \
    DISPLAY="${MOCK_SESSION_DISPLAY}" \
    WAYLAND_DISPLAY="${MOCK_WAYLAND_DISPLAY}" \
    XDG_RUNTIME_DIR="${MOCK_SESSION_XDG_RUNTIME_DIR}" \
    XDG_SESSION_TYPE="${MOCK_SESSION_XDG_SESSION_TYPE}" \
    DBUS_SESSION_BUS_ADDRESS="${MOCK_SESSION_DBUS_SESSION_BUS_ADDRESS}" \
    MOCK_LOG="${MOCK_LOG}" \
    MOCK_BACKEND_LOG="${MOCK_BACKEND_LOG}" \
    MOCK_PAYLOAD_FILE="${MOCK_PAYLOAD_FILE}" \
    MOCK_ENV_LOG="${MOCK_ENV_LOG}" \
    MOCK_WL_TYPES_MODE="${MOCK_WL_TYPES_MODE}" \
    MOCK_WL_PAYLOAD_MODE="${MOCK_WL_PAYLOAD_MODE}" \
    MOCK_WL_DELAY="${MOCK_WL_DELAY}" \
    MOCK_WL_PID_FILE="${MOCK_WL_PID_FILE}" \
    MOCK_CLIPBOARD_TYPES="${MOCK_CLIPBOARD_TYPES}" \
    MOCK_CLIPBOARD_TEXT="${MOCK_CLIPBOARD_TEXT:-}" \
    MOCK_XCLIP_TYPES_MODE="${MOCK_XCLIP_TYPES_MODE}" \
    MOCK_XCLIP_PAYLOAD_MODE="${MOCK_XCLIP_PAYLOAD_MODE}" \
    MOCK_XCLIP_DELAY="${MOCK_XCLIP_DELAY}" \
    MOCK_XCLIP_PID_FILE="${MOCK_XCLIP_PID_FILE}" \
    MOCK_X11_CLIPBOARD_TYPES="${MOCK_X11_CLIPBOARD_TYPES:-}" \
    MOCK_X11_CLIPBOARD_TEXT="${MOCK_X11_CLIPBOARD_TEXT:-}" \
    MOCK_TTY_COMMANDS="${MOCK_TTY_COMMANDS}" \
    MOCK_PANE_CURRENT_COMMAND="${MOCK_PANE_CURRENT_COMMAND}" \
    MOCK_TMUX_PASTE_MODE="${MOCK_TMUX_PASTE_MODE}" \
    MOCK_TMUX_GLOBAL_DISPLAY="${MOCK_TMUX_GLOBAL_DISPLAY}" \
    MOCK_TMUX_GLOBAL_WAYLAND_DISPLAY="${MOCK_TMUX_GLOBAL_WAYLAND_DISPLAY}" \
    MOCK_TMUX_GLOBAL_XAUTHORITY="${MOCK_TMUX_GLOBAL_XAUTHORITY}" \
    MOCK_TMUX_GLOBAL_XDG_RUNTIME_DIR="${MOCK_TMUX_GLOBAL_XDG_RUNTIME_DIR}" \
    MOCK_TMUX_GLOBAL_XDG_SESSION_TYPE="${MOCK_TMUX_GLOBAL_XDG_SESSION_TYPE}" \
    MOCK_TMUX_GLOBAL_DBUS_SESSION_BUS_ADDRESS="${MOCK_TMUX_GLOBAL_DBUS_SESSION_BUS_ADDRESS}" \
    "${command[@]}"
}

reset_case() {
  MOCK_SESSION_DISPLAY=:0
  MOCK_WAYLAND_DISPLAY=wayland-test
  MOCK_SESSION_XDG_RUNTIME_DIR=/run/user/1000
  MOCK_SESSION_XDG_SESSION_TYPE=wayland
  MOCK_SESSION_DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
  MOCK_SESSION_XAUTHORITY=''
  MOCK_TMUX_GLOBAL_DISPLAY=:0
  MOCK_TMUX_GLOBAL_WAYLAND_DISPLAY=wayland-global
  MOCK_TMUX_GLOBAL_XAUTHORITY=''
  MOCK_TMUX_GLOBAL_XDG_RUNTIME_DIR=/run/user/1000
  MOCK_TMUX_GLOBAL_XDG_SESSION_TYPE=wayland
  MOCK_TMUX_GLOBAL_DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
  MOCK_WL_TYPES_MODE=ok
  MOCK_WL_PAYLOAD_MODE=ok
  MOCK_WL_DELAY=0.45
  MOCK_CLIPBOARD_TYPES=''
  MOCK_CLIPBOARD_TEXT=''
  MOCK_XCLIP_TYPES_MODE=ok
  MOCK_XCLIP_PAYLOAD_MODE=ok
  MOCK_XCLIP_DELAY=0.45
  MOCK_X11_CLIPBOARD_TYPES=''
  MOCK_X11_CLIPBOARD_TEXT=''
  MOCK_TTY_COMMANDS='zsh 77 77'
  MOCK_PANE_CURRENT_COMMAND=zsh
  MOCK_TMUX_PASTE_MODE=ok
  MOCK_OUTER_TIMEOUT=''
}

assert_buffer_cleaned() {
  buffer_name=$(sed -n \
    's/^load-buffer:load-buffer -b \([^ ]*\) -$/\1/p' \
    "${MOCK_LOG}")
  [[ ${buffer_name} =~ ^smart-paste-77-[0-9]+$ ]]
  grep -Fxq \
    "delete-buffer:delete-buffer -b ${buffer_name}" \
    "${MOCK_LOG}"
  [[ $(grep -Fc \
    "delete-buffer:delete-buffer -b ${buffer_name}" \
    "${MOCK_LOG}") -eq 1 ]]
}

MOCK_LOG=${test_root}/calls.log
MOCK_BACKEND_LOG=${test_root}/backend.log
MOCK_PAYLOAD_FILE=${test_root}/payload
MOCK_ENV_LOG=${test_root}/env.log
MOCK_WL_PID_FILE=${test_root}/wl.pid
MOCK_XCLIP_PID_FILE=${test_root}/xclip.pid

reset_case
MOCK_XCLIP_TYPES_MODE=fail
MOCK_CLIPBOARD_TYPES=image/png
MOCK_TTY_COMMANDS=$'zsh 77 77\ncodex 77 77'
run_case
grep -Fxq 'send-keys:send-keys -t %77 C-v' "${MOCK_LOG}"

reset_case
MOCK_XCLIP_TYPES_MODE=fail
MOCK_CLIPBOARD_TYPES='image/png;base64'
MOCK_TTY_COMMANDS=$'zsh 77 77\ncodex 77 77'
run_case
grep -Fxq 'send-keys:send-keys -t %77 C-v' "${MOCK_LOG}"

# Restored or scripted sessions can omit graphical variables even though tmux's
# global environment still has them.  The helper imports the fixed allowlist
# and keeps Wayland authoritative instead of selecting stale X11 metadata.
reset_case
MOCK_SESSION_DISPLAY=''
MOCK_WAYLAND_DISPLAY=''
MOCK_SESSION_XDG_RUNTIME_DIR=''
MOCK_SESSION_XDG_SESSION_TYPE=''
MOCK_SESSION_DBUS_SESSION_BUS_ADDRESS=''
MOCK_CLIPBOARD_TYPES=image/png
MOCK_X11_CLIPBOARD_TYPES='TARGETS text/plain;charset=utf-8'
MOCK_TTY_COMMANDS=$'zsh 77 77\ncodex 77 77'
run_case
grep -Fxq 'send-keys:send-keys -t %77 C-v' "${MOCK_LOG}"
[[ $(grep -Fc 'show-environment:show-environment -g' \
  "${MOCK_LOG}") -eq 1 ]]
first_backend_call=$(sed -n '1p' "${MOCK_BACKEND_LOG}")
[[ ${first_backend_call} == 'wl-paste:--list-types' ]]
if grep -q '^xclip:' "${MOCK_BACKEND_LOG}"; then
  exit 1
fi

# A successful but empty native metadata response falls back to the bridge.
reset_case
MOCK_CLIPBOARD_TYPES=''
MOCK_X11_CLIPBOARD_TYPES='TARGETS image/png'
MOCK_TTY_COMMANDS=$'zsh 77 77\ncodex 77 77'
run_case
grep -Fxq 'send-keys:send-keys -t %77 C-v' "${MOCK_LOG}"
grep -Fxq 'wl-paste:--list-types' "${MOCK_BACKEND_LOG}"
grep -Fxq 'xclip:-selection clipboard -out -target TARGETS' \
  "${MOCK_BACKEND_LOG}"

# Panes restored before the desktop exported XAUTHORITY lack it, while tmux's
# global environment carries the stable ~/.Xauthority.  The X11 bridge must
# run with the imported value, or Xwayland rejects the clipboard read.
reset_case
MOCK_TMUX_GLOBAL_XAUTHORITY=/home/fixture/.Xauthority
MOCK_CLIPBOARD_TYPES=''
MOCK_X11_CLIPBOARD_TYPES='TARGETS image/png'
MOCK_TTY_COMMANDS=$'zsh 77 77\ncodex 77 77'
run_case
grep -Fxq 'send-keys:send-keys -t %77 C-v' "${MOCK_LOG}"
grep -Fxq 'xclip:-selection clipboard -out -target TARGETS' \
  "${MOCK_BACKEND_LOG}"
grep -Fxq 'display-message:XAUTHORITY=<unset>' "${MOCK_ENV_LOG}"
[[ $(grep -c '^xclip:' "${MOCK_ENV_LOG}") -eq 1 ]]
grep -Fxq 'xclip:XAUTHORITY=/home/fixture/.Xauthority' "${MOCK_ENV_LOG}"

# The X11 text payload read (second xclip call) uses the same import.
reset_case
MOCK_WAYLAND_DISPLAY=''
MOCK_TMUX_GLOBAL_WAYLAND_DISPLAY=''
MOCK_SESSION_XDG_SESSION_TYPE=x11
MOCK_TMUX_GLOBAL_XDG_SESSION_TYPE=x11
MOCK_TMUX_GLOBAL_XAUTHORITY=/home/fixture/.Xauthority
MOCK_X11_CLIPBOARD_TYPES='TARGETS text/plain;charset=utf-8'
MOCK_X11_CLIPBOARD_TEXT='x11 text via imported auth'
MOCK_TTY_COMMANDS='zsh 77 77'
run_case
[[ $(<"${MOCK_PAYLOAD_FILE}") == 'x11 text via imported auth' ]]
grep -Fxq 'display-message:XAUTHORITY=<unset>' "${MOCK_ENV_LOG}"
[[ $(grep -c '^xclip:' "${MOCK_ENV_LOG}") -eq 2 ]]
[[ $(grep -Fxc 'xclip:XAUTHORITY=/home/fixture/.Xauthority' \
  "${MOCK_ENV_LOG}") -eq 2 ]]
assert_buffer_cleaned

# A caller that already has XAUTHORITY keeps its own value.
reset_case
MOCK_SESSION_XAUTHORITY=/home/fixture/.Xauthority-caller
MOCK_TMUX_GLOBAL_XAUTHORITY=/home/fixture/.Xauthority
MOCK_CLIPBOARD_TYPES=''
MOCK_X11_CLIPBOARD_TYPES='TARGETS image/png'
MOCK_TTY_COMMANDS=$'zsh 77 77\ncodex 77 77'
run_case
grep -Fxq 'send-keys:send-keys -t %77 C-v' "${MOCK_LOG}"
grep -Fxq 'display-message:XAUTHORITY=/home/fixture/.Xauthority-caller' \
  "${MOCK_ENV_LOG}"
grep -Fxq 'xclip:XAUTHORITY=/home/fixture/.Xauthority-caller' "${MOCK_ENV_LOG}"
if grep -Fxq 'xclip:XAUTHORITY=/home/fixture/.Xauthority' "${MOCK_ENV_LOG}"; then
  exit 1
fi

# Control: with no global value nothing is invented.
reset_case
MOCK_CLIPBOARD_TYPES=''
MOCK_X11_CLIPBOARD_TYPES='TARGETS image/png'
MOCK_TTY_COMMANDS=$'zsh 77 77\ncodex 77 77'
run_case
grep -Fxq 'xclip:XAUTHORITY=<unset>' "${MOCK_ENV_LOG}"

reset_case
MOCK_XCLIP_TYPES_MODE=fail
MOCK_CLIPBOARD_TYPES=image/jpeg
MOCK_TTY_COMMANDS=$'zsh 77 77\nclaude 77 77'
run_case
grep -Fxq 'send-keys:send-keys -t %77 C-v' "${MOCK_LOG}"

reset_case
MOCK_XCLIP_TYPES_MODE=fail
MOCK_CLIPBOARD_TYPES=image/png
MOCK_TTY_COMMANDS='zsh 77 77'
run_case
grep -Fxq 'send-keys:send-keys -t %77 C-v' "${MOCK_LOG}"

reset_case
MOCK_XCLIP_TYPES_MODE=fail
MOCK_CLIPBOARD_TYPES=image/png
MOCK_TTY_COMMANDS=$'nvim 77 77\ncodex 88 77'
MOCK_PANE_CURRENT_COMMAND=nvim
run_case
grep -Fxq 'send-keys:send-keys -t %77 C-v' "${MOCK_LOG}"
[[ ! -s ${MOCK_BACKEND_LOG} ]]

# tmux's current-command value remains a compatibility signal when a packaged
# AI TUI does not expose codex/claude as its process comm.
reset_case
MOCK_XCLIP_TYPES_MODE=fail
MOCK_CLIPBOARD_TYPES=image/png
MOCK_TTY_COMMANDS='node 77 77'
MOCK_PANE_CURRENT_COMMAND=codex
run_case
grep -Fxq 'send-keys:send-keys -t %77 C-v' "${MOCK_LOG}"
grep -Fxq 'wl-paste:--list-types' "${MOCK_BACKEND_LOG}"

reset_case
MOCK_XCLIP_TYPES_MODE=fail
MOCK_CLIPBOARD_TYPES='text/plain;charset=utf-8'
MOCK_CLIPBOARD_TEXT='plain text'
MOCK_TTY_COMMANDS='zsh 77 77'
run_case
[[ $(<"${MOCK_PAYLOAD_FILE}") == 'plain text' ]]
grep -Eq '^paste-buffer:paste-buffer .* -p$' "${MOCK_LOG}"
assert_buffer_cleaned

# In an X11-only session, X11 gets the full primary timeout and remains
# authoritative; the Wayland fallback is not consulted after valid text.
reset_case
MOCK_WAYLAND_DISPLAY=''
MOCK_TMUX_GLOBAL_WAYLAND_DISPLAY=''
MOCK_SESSION_XDG_SESSION_TYPE=x11
MOCK_TMUX_GLOBAL_XDG_SESSION_TYPE=x11
MOCK_XCLIP_TYPES_MODE=slow
MOCK_X11_CLIPBOARD_TYPES='TARGETS text/plain;charset=utf-8'
MOCK_X11_CLIPBOARD_TEXT='current x11 text'
MOCK_CLIPBOARD_TYPES=image/png
MOCK_TTY_COMMANDS='zsh 77 77'
MOCK_OUTER_TIMEOUT=2s
run_case
[[ $(<"${MOCK_PAYLOAD_FILE}") == 'current x11 text' ]]
first_backend_call=$(sed -n '1p' "${MOCK_BACKEND_LOG}")
[[ ${first_backend_call} == \
  'xclip:-selection clipboard -out -target TARGETS' ]]
if grep -q '^wl-paste:' "${MOCK_BACKEND_LOG}"; then
  exit 1
fi
assert_buffer_cleaned

# A Wayland clipboard owner may expose no selection and leave wl-paste
# waiting forever.  The global tmux key binding must remain bounded and use
# the X11 clipboard bridge when it has the offered image.
reset_case
MOCK_WL_TYPES_MODE=hang
MOCK_X11_CLIPBOARD_TYPES='TARGETS image/png'
MOCK_TTY_COMMANDS=$'zsh 77 77\ncodex 77 77'
MOCK_OUTER_TIMEOUT=2s
run_case
grep -Fxq 'send-keys:send-keys -t %77 C-v' "${MOCK_LOG}"

# The same fallback preserves normal text paste instead of forwarding the
# image shortcut to a plain shell.
reset_case
MOCK_WL_TYPES_MODE=hang
MOCK_X11_CLIPBOARD_TYPES='TARGETS text/plain;charset=utf-8 UTF8_STRING'
MOCK_X11_CLIPBOARD_TEXT='x11 fallback text'
MOCK_TTY_COMMANDS='zsh 77 77'
MOCK_OUTER_TIMEOUT=2s
run_case
[[ $(<"${MOCK_PAYLOAD_FILE}") == 'x11 fallback text' ]]
grep -Eq '^paste-buffer:paste-buffer .* -p$' "${MOCK_LOG}"
assert_buffer_cleaned

# If neither bridge can report MIME metadata within the bound, an AI TUI still
# receives its native image-paste shortcut instead of a silent no-op.
reset_case
MOCK_WL_TYPES_MODE=hang
MOCK_XCLIP_TYPES_MODE=fail
MOCK_TTY_COMMANDS=$'zsh 77 77\nclaude 77 77'
MOCK_OUTER_TIMEOUT=2s
run_case
grep -Fxq 'send-keys:send-keys -t %77 C-v' "${MOCK_LOG}"

# A slow but successful Wayland selection is authoritative.  A stale X11 image
# must not override the current Wayland text clipboard.
reset_case
MOCK_WL_TYPES_MODE=slow
MOCK_CLIPBOARD_TYPES='text/plain;charset=utf-8'
MOCK_CLIPBOARD_TEXT='current wayland text'
MOCK_X11_CLIPBOARD_TYPES='TARGETS image/png'
MOCK_TTY_COMMANDS=$'zsh 77 77\ncodex 77 77'
MOCK_OUTER_TIMEOUT=2s
run_case
[[ $(<"${MOCK_PAYLOAD_FILE}") == 'current wayland text' ]]
if grep -q '^send-keys:' "${MOCK_LOG}"; then
  exit 1
fi
if grep -q '^xclip:' "${MOCK_BACKEND_LOG}"; then
  exit 1
fi
assert_buffer_cleaned

# A metadata owner that ignores TERM is forcibly killed before X11 fallback.
reset_case
MOCK_WL_TYPES_MODE=ignore-term
MOCK_X11_CLIPBOARD_TYPES='TARGETS image/png'
MOCK_TTY_COMMANDS=$'zsh 77 77\ncodex 77 77'
MOCK_OUTER_TIMEOUT=2s
run_case
grep -Fxq 'send-keys:send-keys -t %77 C-v' "${MOCK_LOG}"
[[ -s ${MOCK_WL_PID_FILE} ]]
if kill -0 "$(<"${MOCK_WL_PID_FILE}")" 2>/dev/null; then
  exit 1
fi

# A partial Wayland payload followed by a TERM-ignoring hang must never paste.
reset_case
MOCK_CLIPBOARD_TYPES='text/plain;charset=utf-8'
MOCK_CLIPBOARD_TEXT='partial payload'
MOCK_WL_PAYLOAD_MODE=partial-ignore-term
MOCK_OUTER_TIMEOUT=3s
if run_case; then
  exit 1
else
  status=$?
  [[ ${status} -eq 1 ]]
fi
if grep -q '^paste-buffer:' "${MOCK_LOG}"; then
  exit 1
fi
assert_buffer_cleaned
[[ -s ${MOCK_WL_PID_FILE} ]]
if kill -0 "$(<"${MOCK_WL_PID_FILE}")" 2>/dev/null; then
  exit 1
fi

# The same forced cleanup applies to the X11 payload fallback.
reset_case
MOCK_WL_TYPES_MODE=fail
MOCK_X11_CLIPBOARD_TYPES='TARGETS text/plain;charset=utf-8'
MOCK_X11_CLIPBOARD_TEXT='partial x11 payload'
MOCK_XCLIP_PAYLOAD_MODE=partial-ignore-term
MOCK_OUTER_TIMEOUT=3s
if run_case; then
  exit 1
else
  status=$?
  [[ ${status} -eq 1 ]]
fi
if grep -q '^paste-buffer:' "${MOCK_LOG}"; then
  exit 1
fi
assert_buffer_cleaned
[[ -s ${MOCK_XCLIP_PID_FILE} ]]
if kill -0 "$(<"${MOCK_XCLIP_PID_FILE}")" 2>/dev/null; then
  exit 1
fi

# A tmux paste failure also deletes the exact named buffer.
reset_case
MOCK_XCLIP_TYPES_MODE=fail
MOCK_CLIPBOARD_TYPES='text/plain;charset=utf-8'
MOCK_CLIPBOARD_TEXT='paste failure payload'
MOCK_TMUX_PASTE_MODE=fail
if run_case; then
  exit 1
else
  status=$?
  [[ ${status} -eq 1 ]]
fi
grep -Eq '^paste-buffer:paste-buffer .* -p$' "${MOCK_LOG}"
assert_buffer_cleaned

if PATH="${mock_bin}:/usr/bin:/bin" "${script_path}" bad-pane; then
  exit 1
else
  [[ $? -eq 2 ]]
fi

printf 'PASS: tmux smart paste\n'
