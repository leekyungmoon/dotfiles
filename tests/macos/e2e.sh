#!/bin/bash
#
# macOS end to end: the one-liner on a real macOS account, then the two
# LaunchAgents through a whole save -> kill -> restore cycle, a repair,
# `install.py services` and the removal by `restore --baseline`. CI runs it
# on GitHub's macOS runners (job e2e-macos in .github/workflows/ci.yml): a VM
# whose user is logged in to the desktop, so its launchd GUI domain
# (gui/<uid>) exists and the agents really load.
#
# It changes the account it runs in: it installs into ~/.dotfiles, loads,
# unloads, disables and enables LaunchAgents, and kills the tmux server it
# started. So it refuses to run anywhere but a GitHub-hosted runner, unless
# E2E_DISPOSABLE_ACCOUNT=1 says this is a throwaway account: a fresh macOS
# user with a desktop login, no ~/.dotfiles and no tmux server.
#
# Every check looks for positive evidence (a file and its mode, a launchctl
# answer, an exact message, a status in status.json) and never trusts an
# exit status alone: bash 3.2 and launchd both fail quietly in places. A
# failed check prints a ::error:: annotation and stops; what the run saw so
# far (install output, status JSON, launchctl answers, agent logs) is copied
# to $E2E_ARTIFACTS for the workflow to upload.
#
# Steps, in order (each one a ::group:: in the log):
#   1. preconditions: macOS 15/26/27, /bin/bash 3.2.57, the gui/<uid>
#      domain, a fresh account, Homebrew;
#   2. the one-liner, piped into /bin/bash as `curl ... | bash -s --` runs it,
#      from this checkout's commit, with the AI CLIs so that their darwin
#      artifacts are downloaded, verified and run;
#   3. what it installed: status, both plists (plutil, mode, keys), launchd's
#      view of both agents, the tmux server the tmux agent started;
#   4. the autosave agent saving with no client attached (snapshot, modes,
#      "last", the pane contents);
#   5. a config reload (the status bar is rebuilt) and a second save;
#   6. kill-server, then `launchctl kickstart -k` of the tmux agent: the
#      sessions, working directories and scrollback come back;
#   7. kickstart while a server runs: nothing new, the same server;
#   8. bootout + bootstrap of the tmux agent: the same server;
#   9. a repair that changes nothing: the same plist bytes, no reload;
#  10. `services disable`, a repair that keeps them off, `services enable`;
#  11. `restore --baseline`: both plists gone, both jobs unloaded.
#
# Inputs: E2E_REPO, the checkout to install (default $GITHUB_WORKSPACE, else
# the one this script is in); its HEAD is installed, and must equal
# $GITHUB_SHA when that is set. E2E_ARTIFACTS, where to put the evidence
# (default $RUNNER_TEMP/e2e-artifacts; unset and no RUNNER_TEMP: none).
#
# Runs under macOS's /bin/bash 3.2 and BSD tools.

set -u

LABEL_TMUX=com.github.leekyungmoon.dotfiles.tmux
LABEL_AUTOSAVE=com.github.leekyungmoon.dotfiles.tmux-resurrect-autosave
# The two agents' own messages (tmux/tmux-launchd-start).
RESTORED="started tmux and restored the last snapshot"
NO_SNAPSHOT="started tmux; there is no snapshot to restore yet"
LEFT_ALONE="a tmux server is already running; left untouched"
# tmux.conf's status-right until tmux/statusbar.tmux replaces it.
STATUS_PLACEHOLDER="Loading; if stuck"
# A save comes every 60 s (StartInterval); two intervals and a margin.
SAVE_WAIT=150
# tmux-launchd-start waits for the restore up to @continuum-restore-max-delay
# + 15 s to begin and 180 s to finish.
RESTORE_WAIT=240

passed=0
in_group=0
work=""
artifacts=""

# --- reporting ----------------------------------------------------------------

step() {
  end_group
  printf '::group::%s\n' "$*"
  in_group=1
}

end_group() {
  if [ "$in_group" = 1 ]; then
    printf '::endgroup::\n'
    in_group=0
  fi
}

say() {
  printf '%s\n' "$*"
}

ok() {
  passed=$((passed + 1))
  printf 'ok: %s\n' "$*"
}

# One line for the annotation (GitHub shows it on the run); details are
# printed before it.
fail() {
  end_group
  printf '::error title=macOS e2e::%s\n' "$*"
  exit 1
}

# --- small helpers --------------------------------------------------------------

# wait_for SECONDS WHAT COMMAND...: run COMMAND every 2 s until it succeeds,
# for at most SECONDS; WHAT names it when time runs out.
wait_for() {
  local limit=$1 what=$2 start
  shift 2
  start=$(date +%s)
  while :; do
    if "$@"; then
      return 0
    fi
    if [ $(($(date +%s) - start)) -ge "$limit" ]; then
      say "timed out after ${limit} s waiting for $what"
      return 1
    fi
    sleep 2
  done
}

file_size() {
  if [ -f "$1" ]; then
    wc -c < "$1" | tr -d ' \t'
  else
    echo 0
  fi
}

# What FILE gained after its first OFFSET bytes.
log_since() {
  tail -c +$(($2 + 1)) "$1" 2>/dev/null
}

log_has_since() {
  log_since "$1" "$2" | grep -qF -- "$3"
}

# Keep a copy of FILE among the artifacts (the name without a leading dot,
# which upload-artifact would leave out).
keep() {
  local name
  [ -n "$artifacts" ] && [ -e "$1" ] || return 0
  name=$(basename "$1")
  name=${name#.}
  cp -p "$1" "$artifacts/${2:-$name}" 2>/dev/null || :
}

# --- launchd ------------------------------------------------------------------------

# A top-level "name = value" line of a saved `launchctl print gui/<uid>/<label>`
# (one tab deep; nested blocks such as the environment are deeper).
service_field() {
  awk -v name="$2" '
    /^\t[^\t]/ {
      line = substr($0, 2)
      i = index(line, " = ")
      if (i > 0 && substr(line, 1, i - 1) == name) { print substr(line, i + 3); exit }
    }' "$1"
}

print_service() {
  launchctl print "gui/$uid/$1" > "$work/print-$2.txt" 2>&1
}

service_loaded() {
  launchctl print "gui/$uid/$1" > /dev/null 2>&1
}

service_gone() {
  ! service_loaded "$1"
}

# "disabled", "enabled" or nothing, from print-disabled (older releases
# print true/false).
disabled_state() {
  launchctl print-disabled "gui/$uid" 2>/dev/null | awk -v want="\"$1\"" '
    $1 == want && $2 == "=>" {
      v = $3
      if (v == "true") v = "disabled"
      if (v == "false") v = "enabled"
      print v
      exit
    }'
}

# How many services of the GUI domain carry LABEL (its services block lists
# "pid  status  label"; the disabled block's "label" => lines do not count).
registrations() {
  launchctl print "gui/$uid" 2>/dev/null | awk -v label="$1" '
    $NF == label && $0 !~ /=>/ { n++ }
    END { print n + 0 }'
}

# The tmux agent is loaded and its run is over.
tmux_agent_idle() {
  print_service "$LABEL_TMUX" tmux-agent || return 1
  [ "$(service_field "$work/print-tmux-agent.txt" state)" != running ]
}

tmux_agent_exit_code() {
  service_field "$work/print-tmux-agent.txt" "last exit code"
}

# The number of times launchd started LABEL ("runs"), or nothing.
service_runs() {
  local runs
  print_service "$1" runs || return 0
  runs=$(service_field "$work/print-runs.txt" runs)
  case $runs in
    '' | *[!0-9]*) ;;
    *) printf '%s\n' "$runs" ;;
  esac
}

# The tmux agent logged TEXT after OFFSET, or its run is over without it
# (it logged something else and is no longer running): no use waiting.
tmux_agent_said() {
  log_has_since "$tmux_log" "$1" "$2" && return 0
  log_has_since "$tmux_log" "$1" "tmux-launchd-start:" && tmux_agent_idle
}

# Wait for the tmux agent to log TEXT after OFFSET, finish, and exit 0.
expect_tmux_agent_run() {
  local offset=$1 text=$2 limit=$3 code
  wait_for "$limit" "the tmux agent to log: $text" tmux_agent_said "$offset" "$text"
  if ! log_has_since "$tmux_log" "$offset" "$text"; then
    say "--- the tmux agent's log since then:"
    log_since "$tmux_log" "$offset"
    fail "the tmux agent did not log '$text' (waited up to $limit s)"
  fi
  wait_for 60 "the tmux agent to finish" tmux_agent_idle \
    || fail "the tmux agent was still running 60 s after it logged '$text'"
  code=$(tmux_agent_exit_code)
  [ "$code" = 0 ] || fail "the tmux agent's last exit code is '$code', not 0"
  ok "the tmux agent logged '$text' and exited 0"
}

# --- tmux -----------------------------------------------------------------------------

server_running() {
  "$tmux_bin" has-session 2> /dev/null
}

server_gone() {
  ! server_running
}

server_pid() {
  "$tmux_bin" display-message -p '#{pid}' 2> /dev/null
}

sessions() {
  "$tmux_bin" list-sessions -F '#{session_name}' 2> /dev/null | sort
}

option() {
  "$tmux_bin" show-option -gqv "$1" 2> /dev/null
}

# The snapshot "last" points at, when it is a non-empty file.
last_snapshot() {
  local target
  target=$(readlink "$resurrect_dir/last" 2> /dev/null) || return 1
  case $target in
    /*) ;;
    *) target=$resurrect_dir/$target ;;
  esac
  [ -f "$target" ] && [ -s "$target" ] || return 1
  printf '%s\n' "$target"
}

# "last" holds window WINDOW of session SESSION (tmux-resurrect's lines:
# window <TAB> session <TAB> index <TAB> :name ...).
saved_window() {
  local file
  file=$(last_snapshot) || return 1
  awk -F '\t' -v s="$1" -v w=":$2" '
    $1 == "window" && $2 == s && $4 == w { found = 1 }
    END { exit !found }' "$file"
}

# Saved at or after the time TS (seconds): last-successful-save, touched by
# tmux/resurrect-save after every verified save.
saved_since() {
  local mark=$resurrect_dir/last-successful-save
  [ -f "$mark" ] && [ "$(stat -f '%m' "$mark")" -ge "$1" ]
}

status_bar_rebuilt() {
  local right left
  right=$(option status-right)
  left=$(option status-left)
  case $right in
    '' | e2e-sentinel | *"$STATUS_PLACEHOLDER"*) return 1 ;;
  esac
  case $left in
    *'#S'*) return 0 ;;
  esac
  return 1
}

# The working directory of every pane of SESSION:WINDOW, one per line.
pane_dirs() {
  "$tmux_bin" list-panes -t "$1:$2" -F '#{pane_current_path}' 2> /dev/null
}

# --- the installer ----------------------------------------------------------------------

# Evaluate a Python expression on install.py's JSON; `d` is the whole
# document, `last` its last_run, phase(name), agent(name) (post-install's
# launchd details) and service(name) (the live launchd view) help.
JSON_EXPECT='
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
last = d.get("last_run") or {}
def phase(name):
    for entry in last.get("phases") or []:
        if entry.get("phase") == name:
            return entry
    return {}
def agent(name):
    details = phase("post-install").get("details") or {}
    return ((details.get("launchd") or {}).get("agents") or {}).get(name) or {}
def service(name):
    return ((d.get("services") or {}).get("agents") or {}).get(name) or {}
try:
    value = eval(sys.argv[2])
except Exception as exc:
    print("cannot evaluate %s: %r" % (sys.argv[2], exc))
    sys.exit(2)
if sys.argv[3:] == ["print"]:
    print(value)
elif not value:
    print("false: " + sys.argv[2])
    sys.exit(1)
'

json_expect() {
  local file=$1 what=$2 expression=$3 out
  if ! out=$("$python" -I -B -c "$JSON_EXPECT" "$file" "$expression" 2>&1); then
    say "$out"
    say "--- phases of the last run:"
    "$python" -I -B -c "$JSON_EXPECT" "$file" \
      '[(p["phase"], p["status"], p["reasons"]) for p in last.get("phases") or []]' print 2>&1
    fail "$what"
  fi
  ok "$what"
}

json_value() {
  "$python" -I -B -c "$JSON_EXPECT" "$1" "$2" print
}

# install.py status --json into $work/status-NAME.json.
status_json() {
  local file="$work/status-$1.json"
  "$python" -B "$dest/install.py" status --json > "$file" 2> "$work/status-$1.stderr" \
    || { cat "$work/status-$1.stderr"; fail "install.py status --json failed ($1)"; }
  keep "$file"
}

# Run install.py with ARGS (no terminal, as CI runs it), keeping its output
# as $work/NAME.log; its exit status.
installer() {
  local name=$1 rc
  shift
  "$python" -B "$dest/install.py" "$@" < /dev/null 2>&1 | tee "$work/$name.log"
  rc=${PIPESTATUS[0]}
  keep "$work/$name.log"
  return "$rc"
}

repair() {
  installer "$1" repair --no-shell-change --skip-vimplug \
    || fail "install.py repair exited with an error ($1; see $1.log)"
  grep -q 'overall' "$work/$1.log" || fail "install.py repair printed no summary ($1)"
  ok "install.py repair ($1) exited 0"
}

# --- evidence on exit ------------------------------------------------------------------------

collect() {
  local status=$? label name
  end_group
  if [ -n "$artifacts" ] && [ -d "$artifacts" ]; then
    printf '::group::%s\n' "Evidence for the artifacts ($artifacts)"
    if [ -n "$work" ]; then
      cp -p "$work"/*.txt "$work"/*.log "$work"/*.json "$work"/*.stderr "$artifacts/" 2>/dev/null
    fi
    if [ -n "${state_root:-}" ]; then
      for name in "$state_root"/launchd/*.log*; do
        keep "$name"
      done
      keep "$state_root/status.json" installer-status.json
    fi
    for label in "$LABEL_TMUX" "$LABEL_AUTOSAVE"; do
      keep "$HOME/Library/LaunchAgents/$label.plist"
      launchctl print "gui/$uid/$label" > "$artifacts/launchctl-print-$label.txt" 2>&1
    done
    launchctl print-disabled "gui/$uid" > "$artifacts/launchctl-print-disabled.txt" 2>&1
    if [ -n "${tmux_bin:-}" ] && [ -x "$tmux_bin" ]; then
      {
        "$tmux_bin" list-sessions
        "$tmux_bin" list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} #{window_name} #{pane_current_command} #{pane_current_path}'
        "$tmux_bin" show-options -g
      } > "$artifacts/tmux-server.txt" 2>&1
    fi
    if [ -n "${resurrect_dir:-}" ] && [ -d "$resurrect_dir" ]; then
      ls -la "$resurrect_dir" > "$artifacts/resurrect-dir.txt" 2>&1
      name=$(last_snapshot) && keep "$name" last-snapshot.txt
    fi
    ls -la "$artifacts"
    printf '::endgroup::\n'
  fi
  if [ "$status" -eq 0 ]; then
    say "macOS e2e: all $passed checks passed"
  else
    say "macOS e2e: failed after $passed passing checks (exit status $status)"
  fi
}

# --- the steps -------------------------------------------------------------------------------

check_preconditions() {
  local version arch bash_version label
  step "1. Preconditions"
  [ "$(uname -s)" = Darwin ] || fail "this is not macOS ($(uname -s))"
  [ "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}" = 3.2 ] \
    || fail "run this with macOS's /bin/bash 3.2, not bash $BASH_VERSION"
  bash_version=$(/bin/bash -c 'echo "$BASH_VERSION"')
  case $bash_version in
    3.2.57*) ok "/bin/bash is $bash_version" ;;
    *) fail "/bin/bash is '$bash_version', not 3.2.57" ;;
  esac
  version=$(SYSTEM_VERSION_COMPAT=0 sw_vers -productVersion)
  arch=$(uname -m)
  case "$arch:${version%%.*}" in
    arm64:15 | arm64:26 | arm64:27 | x86_64:15 | x86_64:26)
      ok "macOS $version on $arch is a supported target" ;;
    *) fail "macOS $version on $arch is not a supported target" ;;
  esac
  if [ "${GITHUB_ACTIONS:-}" = true ] && [ "${RUNNER_ENVIRONMENT:-}" = github-hosted ]; then
    ok "a GitHub-hosted runner (a disposable VM)"
  elif [ "${E2E_DISPOSABLE_ACCOUNT:-}" = 1 ]; then
    ok "a throwaway account (E2E_DISPOSABLE_ACCOUNT=1)"
  else
    fail "refusing to run: this installs into the real home, replaces LaunchAgents and kills the tmux server; run it on a GitHub-hosted runner, or set E2E_DISPOSABLE_ACCOUNT=1 in a throwaway account"
  fi
  if ! launchctl print "gui/$uid" > /dev/null 2>&1; then
    fail "there is no launchd GUI domain gui/$uid (no desktop login for this user): the agents cannot load, and post-install would rightly be RELOGIN_REQUIRED (launchd-gui-domain-unavailable)"
  fi
  ok "the launchd GUI domain gui/$uid exists"
  if [ -e "$HOME/.dotfiles" ] || [ -L "$HOME/.dotfiles" ]; then
    fail "$HOME/.dotfiles already exists: this needs a fresh account"
  fi
  for label in "$LABEL_TMUX" "$LABEL_AUTOSAVE"; do
    [ ! -e "$HOME/Library/LaunchAgents/$label.plist" ] \
      || fail "$HOME/Library/LaunchAgents/$label.plist already exists: this needs a fresh account"
    service_gone "$label" || fail "$label is already loaded: this needs a fresh account"
  done
  ok "a fresh account: no ~/.dotfiles, no plists, no agents"
  brew=${HOMEBREW_PREFIX:-}
  case $brew in
    /*) brew=${brew%/} ;;
    *) if [ "$arch" = arm64 ]; then brew=/opt/homebrew; else brew=/usr/local; fi ;;
  esac
  [ -x "$brew/bin/brew" ] || fail "no Homebrew at $brew (the bootstrap would offer to install it; this check needs it present)"
  ok "Homebrew at $brew"
  tmux_bin=$brew/bin/tmux
  if [ -x "$tmux_bin" ] && server_running; then
    fail "a tmux server is already running for this user: this needs a fresh account"
  fi
  ok "no tmux server is running"
  [ -f "$repo/etc/install" ] || fail "$repo has no etc/install"
  ref=$(git -C "$repo" rev-parse HEAD) || fail "$repo is not a git checkout"
  if [ -n "${GITHUB_SHA:-}" ] && [ "$ref" != "$GITHUB_SHA" ]; then
    fail "$repo is at $ref, not at GITHUB_SHA $GITHUB_SHA"
  fi
  if [ -n "$(git -C "$repo" status --porcelain --untracked-files=no)" ]; then
    say "::warning title=macOS e2e::$repo has uncommitted changes; the one-liner installs commit $ref without them"
  fi
  ok "installing commit $ref from $repo"
  end_group
}

install_one_liner() {
  local rc
  step "2. The one-liner (/bin/bash, piped like curl ... | bash -s --)"
  # As documented, with the clone source and commit pointed at this
  # checkout, no new login shell at the end, and both AI CLIs, so that their
  # darwin artifacts are downloaded, verified and installed.
  DOTFILES_REPO_URL="$repo" DOTFILES_REF="$ref" DOTFILES_EXEC_SHELL=0 \
    /bin/bash -s -- --no-shell-change --skip-vimplug --claude-code --codex \
    < "$repo/etc/install" 2>&1 | tee "$work/install.log"
  rc=${PIPESTATUS[0]}
  keep "$work/install.log"
  [ "$rc" -eq 0 ] || fail "the one-liner exited with status $rc (see install.log)"
  grep -q '^All Done!$' "$work/install.log" \
    || fail "the one-liner exited 0 but never reached 'All Done!'"
  ok "the one-liner exited 0 and said 'All Done!'"
  [ -d "$dest" ] && [ ! -L "$dest" ] || fail "$dest is not the new checkout"
  [ "$(git -C "$dest" rev-parse HEAD)" = "$ref" ] \
    || fail "$dest is at $(git -C "$dest" rev-parse HEAD), not at $ref"
  ok "$dest is a clone at $ref"
  python=$brew/bin/python3
  "$python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || fail "$python is missing or older than 3.10 after the bootstrap"
  ok "Homebrew's python3 is $("$python" -c 'import platform; print(platform.python_version())')"
  end_group
}

check_installed() {
  local name label program plist mode owner lint tool version
  step "3. What the install left: status, plists, launchd, the tmux server"
  status_json install
  st=$work/status-install.json
  json_expect "$st" "the last run was the install" 'last.get("command") == "install"'
  json_expect "$st" "no phase failed" 'all(p["status"] != "FAIL" for p in last["phases"])'
  for name in preflight packages transaction post-install smoke; do
    json_expect "$st" "phase $name passed" "phase(\"$name\").get(\"status\") == \"PASS\""
  done
  json_expect "$st" "no GNOME phase on macOS" 'phase("gui") == {}'
  json_expect "$st" "no managed path drifted" \
    'not [k for k, v in (d["state"].get("entries") or {}).items() if v.get("matches") is False]'
  json_expect "$st" "the tmux agent started tmux (exit 0)" \
    'agent("tmux").get("status") == "PASS" and agent("tmux").get("outcome") == "started"'
  json_expect "$st" "the autosave agent loaded, every 60 s" \
    'agent("autosave").get("status") == "PASS" and agent("autosave").get("run_interval") == 60'
  json_expect "$st" "the AI hook's self-test passed (macOS process inspection works)" \
    'phase("post-install")["details"].get("ai_hook_self_test") == "pass"'
  json_expect "$st" "tmux-256color is known (system or ~/.terminfo)" \
    'phase("post-install")["details"].get("terminfo") in ("present", "installed")'
  json_expect "$st" "launchd's live view: GUI domain, both agents loaded and enabled" \
    'd["services"]["gui_domain"] and all(service(n).get("loaded") is True and service(n).get("disabled") is False and service(n).get("plist_present") for n in ("tmux", "autosave"))'
  json_expect "$st" "launchd's live view: the autosave agent runs every 60 s" \
    'service("autosave").get("run_interval") == 60'
  tmux_log=$(json_value "$st" 'service("tmux")["log"]') || fail "no tmux agent log path in the status"
  autosave_log=$(json_value "$st" 'service("autosave")["log"]') || fail "no autosave log path in the status"
  state_root=$(dirname "$(dirname "$tmux_log")")
  [ "$(stat -f '%Lp' "$state_root/launchd")" = 700 ] || fail "$state_root/launchd is not 0700"
  ok "the agents' log directory $state_root/launchd is 0700"

  for name in tmux autosave; do
    if [ "$name" = tmux ]; then
      label=$LABEL_TMUX program=tmux-launchd-start
    else
      label=$LABEL_AUTOSAVE program=resurrect-autosave
    fi
    plist=$HOME/Library/LaunchAgents/$label.plist
    [ "$(json_value "$st" "service(\"$name\")[\"plist\"]")" = "$plist" ] \
      || fail "the status names another plist for $name"
    [ -f "$plist" ] && [ ! -L "$plist" ] || fail "$plist is not a regular file"
    mode=$(stat -f '%Lp' "$plist")
    owner=$(stat -f '%Su' "$plist")
    [ "$mode" = 644 ] && [ "$owner" = "$(id -un)" ] || fail "$plist is $mode owned by $owner, not 644 owned by $(id -un)"
    lint=$(plutil -lint "$plist" 2>&1) || fail "plutil -lint $plist: $lint"
    case $lint in
      *": OK") ;;
      *) fail "plutil -lint $plist: $lint" ;;
    esac
    [ "$(plutil -extract Label raw -o - "$plist")" = "$label" ] || fail "$plist: wrong Label"
    [ "$(plutil -extract ProgramArguments.0 raw -o - "$plist")" = "$dest/tmux/$program" ] \
      || fail "$plist: ProgramArguments does not run $dest/tmux/$program"
    [ "$(plutil -extract LimitLoadToSessionType raw -o - "$plist")" = Aqua ] \
      || fail "$plist: not limited to the Aqua session"
    case ":$(plutil -extract EnvironmentVariables.PATH raw -o - "$plist"):" in
      *":$brew/bin:"*) ;;
      *) fail "$plist: its PATH lacks $brew/bin" ;;
    esac
    ok "$plist: plutil OK, 0644, $owner, runs $program in Aqua with $brew/bin on PATH"
    print_service "$label" "$name" || fail "launchctl print gui/$uid/$label failed: $(cat "$work/print-$name.txt")"
    keep "$work/print-$name.txt" "launchctl-print-$name-after-install.txt"
    ok "launchctl print gui/$uid/$label: loaded"
  done
  [ "$(plutil -extract RunAtLoad raw -o - "$HOME/Library/LaunchAgents/$LABEL_TMUX.plist")" = true ] \
    || fail "the tmux agent is not RunAtLoad"
  [ "$(plutil -extract StartInterval raw -o - "$HOME/Library/LaunchAgents/$LABEL_AUTOSAVE.plist")" = 60 ] \
    || fail "the autosave agent's StartInterval is not 60"
  ok "the plists say RunAtLoad (tmux) and StartInterval 60 (autosave)"
  grep -q 'run interval = 60 seconds' "$work/print-autosave.txt" \
    || fail "launchctl print does not show 'run interval = 60 seconds' for the autosave agent"
  ok "launchctl print: autosave 'run interval = 60 seconds'"
  [ ! -e "$HOME/Library/LaunchAgents/Tmux.Start.plist" ] \
    || fail "tmux-continuum's own Tmux.Start.plist exists"
  ok "no tmux-continuum Tmux.Start.plist"

  [ -x "$tmux_bin" ] || fail "no Homebrew tmux at $tmux_bin"
  wait_for 30 "the tmux server" server_running || fail "the tmux agent left no tmux server"
  server=$(server_pid)
  ok "the tmux agent's server runs (pid $server, $("$tmux_bin" -V))"
  wait_for 30 "the tmux agent to finish" tmux_agent_idle || fail "the tmux agent is still running"
  [ "$(tmux_agent_exit_code)" = 0 ] || fail "the tmux agent's last exit code is '$(tmux_agent_exit_code)'"
  grep -qF "$NO_SNAPSHOT" "$tmux_log" || fail "the tmux agent's log lacks '$NO_SNAPSHOT'"
  [ "$(option @tmux-restore-status)" = "no snapshot" ] \
    || fail "@tmux-restore-status is '$(option @tmux-restore-status)', not 'no snapshot'"
  ok "a fresh account: the tmux agent exited 0, logged '$NO_SNAPSHOT', @tmux-restore-status 'no snapshot'"
  [ "$(option @continuum-boot)" = off ] || fail "@continuum-boot is not off on macOS"
  ok "@continuum-boot is off"
  /usr/bin/infocmp -x tmux-256color > /dev/null 2>&1 || fail "infocmp does not find tmux-256color"
  ok "infocmp finds tmux-256color"

  # The pinned tools, darwin builds: each one must run.
  json_expect "$st" "Homebrew ran from $brew" \
    "phase(\"packages\")[\"details\"][\"homebrew\"][\"prefix\"] == \"$brew\""
  json_expect "$st" "the nerd font is skipped on macOS with the cask hint" \
    'phase("packages")["details"]["tools"]["nerd-font"]["status"] == "SKIPPED" and "font-jetbrains-mono-nerd-font" in (phase("packages")["details"]["tools"]["nerd-font"].get("hint") or "")'
  for tool in node nvim fzf codex claude; do
    version=$("$HOME/.local/bin/$tool" --version 2>&1) \
      || fail "$HOME/.local/bin/$tool --version failed: $version"
    version=$(printf '%s\n' "$version" | head -1)
    [ -n "$version" ] || fail "$HOME/.local/bin/$tool --version printed nothing"
    ok "$tool runs: $version"
  done

  # The command users are told to run (bin/dotfiles passes it on to
  # install.py), the way a login shell finds it.
  /bin/zsh -lc 'dotfiles services status' > "$work/dotfiles-services-status.txt" 2>&1 \
    || { cat "$work/dotfiles-services-status.txt"; fail "'dotfiles services status' failed"; }
  keep "$work/dotfiles-services-status.txt"
  if ! grep -q "^services:  launchd gui/$uid\$" "$work/dotfiles-services-status.txt" \
      || ! grep -q '^  tmux  *loaded' "$work/dotfiles-services-status.txt" \
      || ! grep -q '^  autosave  *loaded, .*every 60 s' "$work/dotfiles-services-status.txt"; then
    cat "$work/dotfiles-services-status.txt"
    fail "'dotfiles services status' does not show both agents loaded"
  fi
  ok "'dotfiles services status' shows both agents loaded"
  end_group
}

check_autosave() {
  local runs_before runs_after started file archive mode
  step "4. The autosave agent saves with no client attached"
  resurrect_dir=$(option @resurrect-dir)
  case $resurrect_dir in
    /*) ;;
    *) fail "@resurrect-dir is not absolute: '$resurrect_dir'" ;;
  esac
  [ -z "$("$tmux_bin" list-clients 2> /dev/null)" ] || fail "a tmux client is attached"
  ok "no client is attached; snapshots go to $resurrect_dir"
  session=e2e-$(date +%s)-$$
  marker=e2e-scrollback-$session
  dir_a=$HOME/e2e-panes/a-$session
  dir_b=$HOME/e2e-panes/b-$session
  mkdir -p "$dir_a" "$dir_b" || fail "cannot create the pane directories"
  dir_a=$(cd "$dir_a" && pwd -P)
  dir_b=$(cd "$dir_b" && pwd -P)
  runs_before=$(service_runs "$LABEL_AUTOSAVE")
  started=$(date +%s)
  # One pane prints the marker and stays (its scrollback must come back);
  # a window of two login shells (the default command) in another directory.
  "$tmux_bin" new-session -d -s "$session" -n marker -c "$dir_a" \
    "printf '%s\\n' $marker; exec sleep 86400" || fail "tmux new-session failed"
  "$tmux_bin" new-window -d -t "$session:" -n shell -c "$dir_b" || fail "tmux new-window failed"
  "$tmux_bin" split-window -d -h -t "$session:shell" -c "$dir_b" || fail "tmux split-window failed"
  wait_for 30 "the marker in the pane" pane_shows_marker || fail "the marker never showed in $session:marker"
  ok "session $session: marker pane in $dir_a, two shells in $dir_b"
  wait_for "$SAVE_WAIT" "a snapshot with $session" saved_window "$session" shell \
    || fail "no snapshot with $session within $SAVE_WAIT s (see the autosave log)"
  file=$(last_snapshot)
  ok "the autosave agent saved $session: last -> $(basename "$file")"
  saved_since "$started" || fail "last-successful-save was not touched by that save"
  ok "last-successful-save is newer than the session"
  runs_after=$(service_runs "$LABEL_AUTOSAVE")
  if [ -n "$runs_before" ] && [ -n "$runs_after" ]; then
    [ "$runs_after" -gt "$runs_before" ] || fail "launchd started the autosave agent $runs_before -> $runs_after times"
    ok "launchd ran the autosave agent (runs $runs_before -> $runs_after)"
  else
    say "launchctl print shows no 'runs' count; the new snapshot is the evidence"
  fi
  print_service "$LABEL_AUTOSAVE" autosave || fail "the autosave agent is no longer loaded"
  [ "$(service_field "$work/print-autosave.txt" "last exit code")" = 0 ] \
    || fail "the autosave agent's last exit code is '$(service_field "$work/print-autosave.txt" "last exit code")'"
  ok "the autosave agent's last exit code is 0"
  # A save that failed, could not lock, or ran without process inspection
  # (the AI hook's one warning per save) would have said so in its log.
  if [ -f "$autosave_log" ] \
      && grep -E 'resurrect-save failed|cannot lock|nothing saved|cannot inspect processes' "$autosave_log"; then
    fail "the autosave agent's log reports a problem ($autosave_log)"
  fi
  ok "the autosave agent's log reports no failed save and no missing process inspection"

  mode=$(stat -f '%Lp' "$resurrect_dir")
  [ "$mode" = 700 ] || fail "$resurrect_dir is $mode, not 0700"
  archive=$resurrect_dir/pane_contents.tar.gz
  for file in "$file" "$archive" "$resurrect_dir/last-successful-save"; do
    mode=$(stat -f '%Lp' "$file") || fail "$file is missing"
    [ "$mode" = 600 ] || fail "$file is $mode, not 0600"
  done
  ok "$resurrect_dir is 0700; the snapshot, pane contents and marker files are 0600"
  tar -xzOf "$archive" 2> /dev/null | grep -qF "$marker" || fail "the saved pane contents lack the marker"
  ok "the saved pane contents hold the marker"
  if tar -tzf "$archive" | grep -qE '(^|/)\._'; then
    fail "the pane contents archive has AppleDouble ._ entries"
  fi
  ok "no AppleDouble ._ entries in the archive"
  end_group
}

pane_shows_marker() {
  "$tmux_bin" capture-pane -p -J -S - -t "$session:marker" 2> /dev/null | grep -qF "$marker"
}

check_reload() {
  local reloaded
  step "5. A config reload rebuilds the status bar; saves go on"
  "$tmux_bin" set-option -g status-right e2e-sentinel || fail "cannot set status-right"
  reloaded=$(date +%s)
  "$tmux_bin" source-file "$HOME/.tmux.conf" || fail "tmux source-file ~/.tmux.conf failed"
  wait_for 30 "tmux/statusbar.tmux to rebuild the status bar" status_bar_rebuilt \
    || fail "the status bar was not rebuilt (status-right: '$(option status-right)')"
  ok "the reload ran tmux/statusbar.tmux (status-left and status-right rebuilt)"
  "$tmux_bin" new-window -d -t "$session:" -n later -c "$dir_a" || fail "tmux new-window failed"
  wait_for "$SAVE_WAIT" "a snapshot with the window made after the reload" saved_window "$session" later \
    || fail "no snapshot after the reload within $SAVE_WAIT s"
  saved_since "$reloaded" || fail "last-successful-save was not touched after the reload"
  ok "the autosave agent saved again after the reload"
  end_group
}

check_restore() {
  local offset status_text dirs
  step "6. kill-server, then launchctl kickstart -k of the tmux agent: it all comes back"
  "$tmux_bin" kill-server || fail "tmux kill-server failed"
  wait_for 20 "the tmux server to exit" server_gone || fail "the tmux server did not exit"
  ok "the tmux server is gone"
  offset=$(file_size "$tmux_log")
  launchctl kickstart -k "gui/$uid/$LABEL_TMUX" || fail "launchctl kickstart -k of the tmux agent failed"
  expect_tmux_agent_run "$offset" "$RESTORED" "$RESTORE_WAIT"
  if log_has_since "$tmux_log" "$offset" "continuum skipped"; then
    say "tmux-continuum skipped the restore; tmux-launchd-start restored it itself"
  fi
  server=$(server_pid)
  status_text=$(option @tmux-restore-status)
  case $status_text in
    "restored "*) ok "@tmux-restore-status: $status_text" ;;
    *) fail "@tmux-restore-status is '$status_text', not 'restored ...'" ;;
  esac
  [ "$(option @tmux-restore-complete)" = on ] || fail "@tmux-restore-complete is not on"
  ok "@tmux-restore-complete is on (saves go on from here)"
  "$tmux_bin" has-session -t "$session" 2> /dev/null || fail "session $session was not restored"
  dirs=$(pane_dirs "$session" marker)
  [ "$dirs" = "$dir_a" ] || fail "the marker window came back in '$dirs', not $dir_a"
  dirs=$(pane_dirs "$session" shell)
  [ "$(printf '%s\n' "$dirs" | grep -c .)" = 2 ] || fail "the shell window came back with panes in '$dirs'"
  [ "$(printf '%s\n' "$dirs" | grep -cvxF "$dir_b")" = 0 ] \
    || fail "a shell pane came back elsewhere than $dir_b: $dirs"
  pane_dirs "$session" later | grep -qxF "$dir_a" || fail "the window made after the reload was not restored"
  ok "session $session: windows marker, shell (2 panes) and later, each in its directory"
  wait_for 30 "the marker in the restored pane" pane_shows_marker \
    || fail "the restored marker pane lacks its scrollback"
  ok "the restored pane shows its saved scrollback"
  end_group
}

check_kickstart_running() {
  local before offset
  step "7. kickstart while a server runs: nothing new, the same server"
  before=$(sessions)
  offset=$(file_size "$tmux_log")
  launchctl kickstart -k "gui/$uid/$LABEL_TMUX" || fail "launchctl kickstart -k failed"
  expect_tmux_agent_run "$offset" "$LEFT_ALONE" 60
  [ "$(server_pid)" = "$server" ] || fail "the tmux server changed: $server -> $(server_pid)"
  [ "$(sessions)" = "$before" ] || fail "the sessions changed: $(sessions | tr '\n' ' ')"
  ok "the same server (pid $server) and the same sessions"
  end_group
}

check_reload_agent() {
  local offset rc=0 plist=$HOME/Library/LaunchAgents/$LABEL_TMUX.plist
  step "8. bootout + bootstrap of the tmux agent: the same server"
  # launchd finishes a bootout asynchronously; 36 says it is under way.
  launchctl bootout "gui/$uid/$LABEL_TMUX" || rc=$?
  case $rc in
    0 | 36) ;;
    *) fail "launchctl bootout of the tmux agent failed (exit status $rc)" ;;
  esac
  wait_for 15 "launchd to let the tmux agent go" service_gone "$LABEL_TMUX" \
    || fail "the tmux agent was still loaded 15 s after bootout"
  [ "$(server_pid)" = "$server" ] || fail "bootout of the tmux agent stopped the server"
  ok "booted out; the server (pid $server) runs on"
  offset=$(file_size "$tmux_log")
  launchctl bootstrap "gui/$uid" "$plist" || fail "launchctl bootstrap of the tmux agent failed"
  expect_tmux_agent_run "$offset" "$LEFT_ALONE" 60
  [ "$(server_pid)" = "$server" ] || fail "bootstrap of the tmux agent replaced the server"
  ok "bootstrapped again; the same server (pid $server)"
  end_group
}

check_idempotent_repair() {
  local tmux_sum autosave_sum offset runs_before runs_after label count st
  step "9. A repair that changes nothing: same plists, no reload, same server"
  tmux_sum=$(shasum -a 256 < "$HOME/Library/LaunchAgents/$LABEL_TMUX.plist")
  autosave_sum=$(shasum -a 256 < "$HOME/Library/LaunchAgents/$LABEL_AUTOSAVE.plist")
  runs_before=$(service_runs "$LABEL_AUTOSAVE")
  offset=$(file_size "$tmux_log")
  repair repair
  [ "$(shasum -a 256 < "$HOME/Library/LaunchAgents/$LABEL_TMUX.plist")" = "$tmux_sum" ] \
    && [ "$(shasum -a 256 < "$HOME/Library/LaunchAgents/$LABEL_AUTOSAVE.plist")" = "$autosave_sum" ] \
    || fail "the repair rewrote a plist"
  ok "both plists are byte for byte the same"
  status_json repair
  st=$work/status-repair.json
  json_expect "$st" "the last run was the repair" 'last.get("command") == "repair"'
  json_expect "$st" "no phase failed" 'all(p["status"] != "FAIL" for p in last["phases"])'
  json_expect "$st" "post-install passed" 'phase("post-install").get("status") == "PASS"'
  json_expect "$st" "both agents were left as they were (no bootout, no bootstrap)" \
    'all(agent(n).get("action") == "unchanged" and agent(n).get("plist_state") == "unchanged" for n in ("tmux", "autosave"))'
  for label in "$LABEL_TMUX" "$LABEL_AUTOSAVE"; do
    count=$(registrations "$label")
    [ "$count" = 1 ] || fail "gui/$uid lists $label $count times"
  done
  ok "gui/$uid lists each agent once"
  if log_since "$tmux_log" "$offset" | grep -q 'tmux-launchd-start:'; then
    log_since "$tmux_log" "$offset"
    fail "the tmux agent ran during the repair (it was reloaded)"
  fi
  ok "the tmux agent did not run again"
  runs_after=$(service_runs "$LABEL_AUTOSAVE")
  if [ -n "$runs_before" ] && [ -n "$runs_after" ]; then
    [ "$runs_after" -ge "$runs_before" ] || fail "the autosave agent's run count went back ($runs_before -> $runs_after): it was reloaded"
    ok "the autosave agent kept counting (runs $runs_before -> $runs_after)"
  fi
  [ "$(server_pid)" = "$server" ] || fail "the repair replaced the tmux server"
  "$tmux_bin" has-session -t "$session" 2> /dev/null || fail "the repair lost session $session"
  ok "the same server (pid $server) with $session"
  end_group
}

check_services() {
  local label name st offset
  step "10. services disable; a repair keeps them off; services enable"
  installer services-disable services disable || fail "install.py services disable failed"
  [ "$(grep -c 'disabled (' "$work/services-disable.log")" -ge 2 ] \
    || fail "install.py services disable did not report both agents"
  for label in "$LABEL_TMUX" "$LABEL_AUTOSAVE"; do
    wait_for 15 "$label to be unloaded" service_gone "$label" || fail "$label is still loaded"
    [ "$(disabled_state "$label")" = disabled ] || fail "print-disabled does not list $label as disabled"
  done
  ok "both agents unloaded and listed disabled in print-disabled"
  [ "$(server_pid)" = "$server" ] || fail "services disable stopped the tmux server"
  ok "the tmux server runs on"

  repair repair-disabled
  status_json repair-disabled
  st=$work/status-repair-disabled.json
  json_expect "$st" "post-install passed" 'phase("post-install").get("status") == "PASS"'
  for name in tmux autosave; do
    json_expect "$st" "the repair left $name off: SKIPPED, with the reason" \
      "agent(\"$name\").get(\"status\") == \"SKIPPED\" and agent(\"$name\").get(\"action\") == \"disabled\" and \"$name: disabled (enable: dotfiles services enable $name)\" in phase(\"post-install\")[\"reasons\"]"
  done
  for label in "$LABEL_TMUX" "$LABEL_AUTOSAVE"; do
    service_gone "$label" || fail "the repair loaded the disabled $label"
    [ "$(disabled_state "$label")" = disabled ] || fail "the repair enabled $label"
  done
  ok "both agents are still unloaded and disabled"

  offset=$(file_size "$tmux_log")
  installer services-enable services enable || fail "install.py services enable failed"
  [ "$(grep -c 'enabled and loaded' "$work/services-enable.log")" -ge 2 ] \
    || fail "install.py services enable did not report both agents loaded"
  for label in "$LABEL_TMUX" "$LABEL_AUTOSAVE"; do
    service_loaded "$label" || fail "$label is not loaded after services enable"
    [ "$(disabled_state "$label")" != disabled ] || fail "$label is still disabled"
  done
  ok "both agents loaded and enabled again"
  # Loading the tmux agent runs it (RunAtLoad); it must leave the server be.
  expect_tmux_agent_run "$offset" "$LEFT_ALONE" 60
  [ "$(server_pid)" = "$server" ] || fail "services enable replaced the tmux server"
  ok "the tmux agent left the running server (pid $server) alone"
  status_json enabled
  json_expect "$work/status-enabled.json" "status: both agents loaded, none disabled" \
    'all(service(n).get("loaded") is True and service(n).get("disabled") is False for n in ("tmux", "autosave"))'
  end_group
}

check_removal() {
  local out label
  step "11. restore --baseline removes both agents"
  out=$work/restore-baseline.log
  "$python" -B "$dest/install.py" restore --baseline < /dev/null > "$out" 2> "$work/restore-baseline.stderr" \
    || { cat "$out" "$work/restore-baseline.stderr"; fail "install.py restore --baseline failed"; }
  keep "$out"
  # Its JSON report is the last thing it prints, from a line "{" on.
  sed -n '/^{$/,$p' "$out" > "$work/restore-baseline.json"
  keep "$work/restore-baseline.json"
  "$python" -I -B -c '
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
sys.exit(data.get("launchd") != {"tmux": "unloaded", "autosave": "unloaded"})
' "$work/restore-baseline.json" || { cat "$out"; fail "restore did not report both agents unloaded"; }
  ok "restore --baseline reported launchd: tmux and autosave unloaded"
  for label in "$LABEL_TMUX" "$LABEL_AUTOSAVE"; do
    [ ! -e "$HOME/Library/LaunchAgents/$label.plist" ] || fail "$label.plist is still there"
    wait_for 15 "$label to be gone" service_gone "$label" || fail "$label is still loaded"
  done
  ok "both plists removed and both labels gone from gui/$uid"
  end_group
}

main() {
  uid=$(id -u)
  repo=${E2E_REPO:-${GITHUB_WORKSPACE:-}}
  if [ -z "$repo" ]; then
    repo=$(cd "$(dirname "$0")/../.." && pwd -P)
  fi
  dest=$HOME/.dotfiles
  artifacts=${E2E_ARTIFACTS:-${RUNNER_TEMP:+$RUNNER_TEMP/e2e-artifacts}}
  if [ -n "$artifacts" ]; then
    mkdir -p "$artifacts" || artifacts=""
  fi
  work=$(mktemp -d "${TMPDIR:-/tmp}/macos-e2e.XXXXXX") || fail "cannot create a work directory"
  trap collect EXIT
  # The agents' server is the default one; nothing here may point elsewhere.
  unset TMUX TMUX_PANE TMUX_TMPDIR

  check_preconditions
  install_one_liner
  check_installed
  check_autosave
  check_reload
  check_restore
  check_kickstart_running
  check_reload_agent
  check_idempotent_repair
  check_services
  check_removal
  step "Clean up"
  "$tmux_bin" kill-server 2> /dev/null || :
  rm -rf "$HOME/e2e-panes"
  end_group
}

main "$@"
