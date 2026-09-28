#!/usr/bin/env bash
# Acceptance test: fzf pickers work on tmux without `fzf --tmux` support.
#
# fzf --tmux needs tmux >= 3.3; tmux 3.2a (Ubuntu 22.04) rejects the popup
# options fzf passes, so a global FZF_DEFAULT_OPTS='--tmux=...' made every fzf
# call inside tmux fail. For each tmux binary under test this checks:
#   - tmux.conf loads without errors and binds prefix-S/@/? to the variant
#     that matches the server version (run-shell + fzf --tmux on >= 3.3,
#     display-popup -E + plain fzf below);
#   - FZF_DEFAULT_OPTS from zsh/zsh.d/envs.zsh, computed in a tmux pane,
#     carries --tmux only on >= 3.3, and fzf works through it (--filter and an
#     interactive --select-1 run on the pane's terminal);
#   - the prefix-S, prefix-@ and prefix-? pickers, driven through a real client
#     on a pseudo terminal, open fzf and act on the selection.
#
# Usage: TMUX_BINARIES="/path/to/tmux-3.2a /usr/bin/tmux" tests/test-fzf-tmux-fallback.sh
# (default: /usr/bin/tmux). FZF_BIN selects fzf (default: fzf on $PATH).
#
# Isolation, all load-bearing: $TMUX/$TMUX_PANE are unset first (an inherited
# $TMUX silently targets the caller's server); every harness call passes -S
# with a socket inside this test's temp dir, kill-server included; HOME and
# XDG_* are temp dirs; ~/.dotfiles is a copy of the repository's bin/ and
# tmux/, never the checkout itself. Commands started by the test server (key
# bindings, popups) reach it through the $TMUX that server sets for them.

set -uo pipefail

unset TMUX TMUX_PANE TMUX_TMPDIR

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

failures=0
checks=0
fail() {
  printf 'FAIL: %s\n' "$*" >&2
  failures=$((failures + 1))
}
ok() { checks=$((checks + 1)); }
check() {
  local label=$1 expected=$2 actual=$3
  ok
  [[ ${expected} == "${actual}" ]] && return 0
  fail "$(printf '%s\n  expected: %q\n  actual:   %q' "${label}" "${expected}" "${actual}")"
}
check_contains() {
  local label=$1 needle=$2 haystack=$3
  ok
  [[ ${haystack} == *"${needle}"* ]] && return 0
  fail "$(printf '%s\n  missing:  %q\n  in:       %q' "${label}" "${needle}" "${haystack}")"
}
check_absent() {
  local label=$1 needle=$2 haystack=$3
  ok
  [[ ${haystack} != *"${needle}"* ]] && return 0
  fail "$(printf '%s\n  unexpected: %q\n  in:         %q' "${label}" "${needle}" "${haystack}")"
}

fzf_bin=${FZF_BIN:-$(command -v fzf || true)}
if [[ -z ${fzf_bin} || ! -x ${fzf_bin} ]]; then
  printf 'SKIP: fzf is not installed (set FZF_BIN)\n' >&2
  exit 0
fi
for tool in zsh script; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    printf 'SKIP: %s is not installed\n' "${tool}" >&2
    exit 0
  fi
done
read -r -a tmux_binaries <<<"${TMUX_BINARIES:-/usr/bin/tmux}"

workdir=$(mktemp -d)
# Sockets get their own short directory: unix socket paths are limited to
# about 100 bytes, which a deep $TMPDIR exceeds.
sockdir=$(mktemp -d /tmp/fzft.XXXXXX)
socket=""

require_disposable_socket() {
  if [[ -z ${sockdir} || -z ${socket} || ${socket} != "${sockdir}/"* ]]; then
    printf 'ABORT: refusing to address a tmux socket outside %s\n' "${sockdir}" >&2
    exit 99
  fi
}
t() {
  require_disposable_socket
  "${tmux_bin}" -S "${socket}" "$@"
}
stop_server() {
  if [[ -n ${client_pid:-} ]]; then
    exec 7>&-
    kill "${client_pid}" 2>/dev/null
    wait "${client_pid}" 2>/dev/null
    client_pid=""
  fi
  if [[ -n ${socket} && -S ${socket} ]]; then
    t kill-server 2>/dev/null
  fi
}
cleanup() {
  stop_server
  rm -rf -- "${workdir}" "${sockdir}"
}
trap cleanup EXIT

# wait_for <seconds> <command...>: poll until the command succeeds.
wait_for() {
  local deadline=$((SECONDS + $1)); shift
  until "$@"; do
    ((SECONDS >= deadline)) && return 1
    sleep 0.1
  done
}

# Disposable HOME laid out like an installed system: ~/.dotfiles, ~/.tmux,
# ~/.tmux.conf. Copies, so nothing can write into the checkout.
home=${workdir}/home
mkdir -p "${home}" "${workdir}/dotfiles" "${workdir}/bin" "${workdir}/xdg/"{data,config,cache,state}
cp -R "${repo_root}/bin" "${repo_root}/tmux" "${workdir}/dotfiles/"
mkdir -p "${workdir}/dotfiles/zsh"
cp -R "${repo_root}/zsh/zsh.d" "${workdir}/dotfiles/zsh/"
ln -s "${workdir}/dotfiles" "${home}/.dotfiles"
ln -s .dotfiles/tmux "${home}/.tmux"
ln -s .dotfiles/tmux/tmux.conf "${home}/.tmux.conf"
ln -s "${fzf_bin}" "${workdir}/bin/fzf"
# fd stand-in (Ubuntu names it fdfind): list files below the cwd like fd does.
cat >"${workdir}/bin/fd" <<'EOF'
#!/bin/sh
find . -type f ! -path './.git/*' | sed 's|^\./||' | sort
EOF
printf '#!/bin/sh\ncat -- "$1"\n' >"${workdir}/bin/fzf-preview.sh"
chmod +x "${workdir}/bin/fd" "${workdir}/bin/fzf-preview.sh"
mkdir -p "${workdir}/project/src"
printf 'x\n' >"${workdir}/project/src/needle-target.txt"
printf 'y\n' >"${workdir}/project/README"

export HOME=${home}
export XDG_DATA_HOME=${workdir}/xdg/data XDG_CONFIG_HOME=${workdir}/xdg/config
export XDG_CACHE_HOME=${workdir}/xdg/cache XDG_STATE_HOME=${workdir}/xdg/state
export PATH=${workdir}/bin:/usr/bin:/bin
export TERM=xterm-256color
unset FZF_DEFAULT_OPTS FZF_DEFAULT_COMMAND FZF_TMUX FZF_TMUX_OPTS TERM_PROGRAM TERM_PROGRAM_VERSION

tested=()
for tmux_bin in "${tmux_binaries[@]}"; do
  if ! version=$("${tmux_bin}" -V 2>/dev/null); then
    fail "tmux binary does not run: ${tmux_bin}"
    continue
  fi
  version=${version##* }
  tested+=("${version}")
  printf -- '--- tmux %s (%s)\n' "${version}" "${tmux_bin}"
  if [[ ${version} =~ ^([0-9]+)\.([0-9]+) ]] &&
    ((BASH_REMATCH[1] > 3 || (BASH_REMATCH[1] == 3 && BASH_REMATCH[2] >= 3))); then
    popup=1
  else
    popup=0
  fi
  run=${workdir}/run-${#tested[@]}
  mkdir -p "${run}"
  socket=${sockdir}/s${#tested[@]}
  ln -sfn "${tmux_bin}" "${workdir}/bin/tmux"
  hash -r

  # 1. The configuration loads without errors.
  if ! t -f /dev/null new-session -d -s main -x 160 -y 40 -c "${workdir}/project" "cat"; then
    fail "tmux ${version}: cannot start the disposable server"
    continue
  fi
  out=$(t source-file "${home}/.tmux.conf" 2>&1)
  rc=$?
  out=$(grep -v 'tmux plugins are not installed' <<<"${out}")
  check "tmux ${version}: source-file ~/.tmux.conf exit status" 0 "${rc}"
  check "tmux ${version}: source-file ~/.tmux.conf output" "" "${out}"
  # `source-file -q ~/.tmux/resurrect.conf` expands ~ to $HOME: an option
  # only that file sets is present.
  check "tmux ${version}: ~/.tmux/resurrect.conf is sourced" \
    "on" "$(t show-option -gqv @resurrect-capture-pane-contents)"

  # 2. Bindings match the server version.
  for key in S @ '?'; do
    binding=$(t list-keys -T prefix "${key}" 2>&1)
    if ((popup)); then
      check_contains "tmux ${version}: prefix ${key} uses run-shell" "run-shell" "${binding}"
    else
      check_contains "tmux ${version}: prefix ${key} uses display-popup -E" "display-popup -E" "${binding}"
      check_absent "tmux ${version}: prefix ${key} avoids fzf --tmux" "--tmux" "${binding}"
    fi
  done

  # 3. FZF_DEFAULT_OPTS from envs.zsh inside a pane, and fzf through it.
  cat >"${run}/fzf-in-pane.zsh" <<'EOF'
source "$HOME/.dotfiles/zsh/zsh.d/envs.zsh"
out=$1
print -r -- "$FZF_DEFAULT_OPTS" >"$out.opts"
printf 'alpha\nbeta\ngamma\n' | fzf --filter=bet >"$out.filter"; print -r -- $? >>"$out.filter"
printf 'only-line\n' | fzf --select-1 >"$out.select"; print -r -- $? >>"$out.select"
# control: the unconditional upstream option
printf 'only-line\n' | FZF_DEFAULT_OPTS="$FZF_DEFAULT_OPTS --tmux=center,80%" \
  fzf --select-1 >"$out.forced" 2>&1; print -r -- $? >>"$out.forced"
: >"$out.done"
EOF
  t new-window -d -t main: "zsh -f ${run}/fzf-in-pane.zsh ${run}/pane"
  if ! wait_for 15 test -e "${run}/pane.done"; then
    fail "tmux ${version}: fzf pane script did not finish"
  else
    opts=$(<"${run}/pane.opts")
    if ((popup)); then
      check_contains "tmux ${version}: FZF_DEFAULT_OPTS has --tmux" "--tmux=center,80%" "${opts}"
    else
      check_absent "tmux ${version}: FZF_DEFAULT_OPTS has no --tmux" "--tmux" "${opts}"
      check "tmux ${version}: control, forced --tmux fails on this server" \
        "yes" "$([[ $(tail -n1 "${run}/pane.forced") != 0 ]] && echo yes || echo no)"
    fi
    check "tmux ${version}: fzf --filter with FZF_DEFAULT_OPTS" $'beta\n0' "$(<"${run}/pane.filter")"
    if ((popup)); then
      # needs a client for the popup; covered by the client pickers below
      :
    else
      check "tmux ${version}: interactive fzf --select-1 in a pane" $'only-line\n0' "$(<"${run}/pane.select")"
    fi
  fi

  # 4. Pickers through a real client on a pseudo terminal. Server messages
  # logged from here on must not contain errors (the control above logs one).
  t new-session -d -s other -x 160 -y 40 "cat"
  mkfifo "${run}/keys"
  script -qfec "stty rows 40 cols 160; exec ${tmux_bin} -S ${socket} attach -t main" \
    "${run}/typescript" <"${run}/keys" >/dev/null 2>&1 &
  client_pid=$!
  exec 7>"${run}/keys"
  client_attached() { [[ -n $(t list-clients -F '#{client_tty}' 2>/dev/null) ]]; }
  if ! wait_for 10 client_attached; then
    fail "tmux ${version}: client did not attach"
    stop_server
    continue
  fi
  # (show-messages needs a client on 3.2a and its order differs between
  # versions: snapshot now, compare as sets)
  t show-messages >"${run}/messages.before" 2>&1
  client_session() { t list-clients -F '#{client_session}' 2>/dev/null | head -n1; }
  popup_open() { grep -aq -- "$1" "${run}/typescript"; }
  # Rows of the attached client's screen are not captured; the typescript is.

  # prefix-S: pick "other".
  printf '\001S' >&7
  if wait_for 10 popup_open 'tmux attach:'; then
    ok
    printf 'other' >&7
    sleep 0.5
    printf '\r' >&7
    session_is_other() { [[ $(client_session) == other ]]; }
    if wait_for 10 session_is_other; then ok; else
      fail "tmux ${version}: prefix-S did not switch to session 'other' (now: $(client_session))"
    fi
  else
    fail "tmux ${version}: prefix-S picker did not open"
  fi

  # tmux < 3.3, a binding that still calls tmux-attach through run-shell (no
  # terminal): tmux-attach reopens itself in display-popup -E.
  session_is_main() { [[ $(client_session) == main ]]; }
  if ((!popup)); then
    t switch-client -t main 2>/dev/null
    wait_for 5 session_is_main
    t bind-key -T prefix Y run-shell '$HOME/.dotfiles/bin/tmux-attach || true'
    : >"${run}/typescript"
    printf '\001Y' >&7
    if wait_for 10 popup_open 'tmux attach:'; then
      ok
      printf 'other' >&7
      sleep 0.5
      printf '\r' >&7
      if wait_for 10 session_is_other; then ok; else
        fail "tmux ${version}: run-shell tmux-attach did not switch to 'other' (now: $(client_session))"
      fi
    else
      fail "tmux ${version}: run-shell tmux-attach did not reopen in a popup"
    fi
    t unbind-key -T prefix Y
  fi

  # prefix-@: pick a file below the pane's cwd; it is pasted into the pane.
  t switch-client -t main 2>/dev/null
  wait_for 5 session_is_main
  printf '\001@' >&7
  if wait_for 10 popup_open 'needle-target.txt'; then
    ok
    printf 'needle' >&7
    sleep 0.5
    printf '\r' >&7
    pasted() { t capture-pane -p -t main:0 | grep -q '@src/needle-target.txt'; }
    if wait_for 10 pasted; then ok; else
      fail "tmux ${version}: prefix-@ did not paste the picked file: $(t capture-pane -p -t main:0 | tr -s '\n')"
    fi
  else
    fail "tmux ${version}: prefix-@ picker did not open"
  fi

  # prefix-?: the key help opens in fzf and closes on Escape.
  printf '\001?' >&7
  if wait_for 10 popup_open 'tmux keys>'; then
    ok
    printf '\033' >&7
  else
    fail "tmux ${version}: prefix-? key help did not open"
  fi
  sleep 0.5
  msgs=$(t show-messages 2>&1 | grep -vxFf "${run}/messages.before" |
    grep -iE 'usage|unknown|invalid|error|not found' || true)
  check "tmux ${version}: no error messages on the server" "" "${msgs}"

  stop_server
done

if [[ -n $(cd "${workdir}/dotfiles" && find . -newer "${workdir}/project/README" -type f \
  ! -path './zsh/*' -print -quit) ]]; then
  fail "a picker wrote into the ~/.dotfiles copy"
fi
ok

if ((${#tested[@]} == 0)); then
  fail "no tmux binary was tested"
fi
printf 'tested tmux: %s; fzf: %s\n' "${tested[*]}" "$("${fzf_bin}" --version)"
if ((failures)); then
  printf '%d of %d checks failed\n' "${failures}" "${checks}" >&2
  exit 1
fi
printf 'PASS: %d checks\n' "${checks}"
