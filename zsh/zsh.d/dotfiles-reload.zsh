# dotfiles-reload.zsh: keep long-running interactive shells on the installed setup
# vim: set sts=2 sw=2 ts=2
#
# install.py writes one line, "<generation>", to
#   ${XDG_STATE_HOME:-$HOME/.local/state}/personal-dotfiles/generation
# at the end of every successful install, repair or update. The generation is
# derived from the checkout's content, so a run that changes nothing leaves it
# as it was. Every interactive zsh records the generation it loaded at
# startup. Once it changed, the shell replaces itself with a fresh zsh. The
# exec keeps the pid, the terminal, the working directory and the environment.
# The new zsh loads the new setup and then gets this shell's session back
# (see "Hand-over"):
#
# 1. At Enter on the primary prompt (zle-line-finish with CONTEXT == start)
#    the generation is compared with the loaded one. When it changed, the
#    accepted line is kept for the new shell and emptied here, so this shell
#    runs nothing, and the reload follows at once from precmd. The new shell
#    runs the line at its first prompt. So the first command typed after an
#    update runs once, on the new setup, and enters the history once. An
#    aborted line (^C, ^G) never reaches zle-line-finish.
# 2. At every primary prompt (precmd) the same comparison catches a change
#    seen while a command ran, or during a continuation (PS2), heredoc,
#    select, vared or spelling-correction prompt, where nothing is done: the
#    command runs first, the reload happens before the next primary prompt.
# Both read the file with builtins only: no fork and no write per prompt.
#
# Hand-over. Before the exec this shell writes its session into private files
# (0600, O_EXCL). They go into $XDG_RUNTIME_DIR/personal-dotfiles when
# XDG_RUNTIME_DIR is a private directory of ours (in memory, not on disk),
# else into the state directory. It hands over:
#   - the kept line and the buffer stack (push-line);
#   - its own history list (fc -W). The new zsh loads it in place of
#     $HISTFILE, so 'r', !! and Up still mean this shell's commands. A
#     session that keeps no history (HISTFILE unset, SAVEHIST=0) hands it over
#     only through the in-memory place, never through the disk;
#   - its PATH. The new shell keeps that order. Each entry the new setup
#     brought goes in front of the first entry of the setup's own that
#     follows it in the new PATH, so never in front of a venv's or a
#     workspace's; a venv's saved PATH (_OLD_VIRTUAL_PATH) gets it too;
#   - what this session changed against the setup's own values (taken at the
#     end of startup, then at the first prompt for what plugins export
#     there), put back on top of the new setup: exported variables set,
#     changed or unset (a venv, a ROS overlay, conda, EDITOR=...), HISTFILE,
#     HISTSIZE and SAVEHIST (an unset HISTFILE stays unset), the umask;
#   - a venv's 'deactivate' and its _OLD_VIRTUAL_* variables, the directory
#     stack and OLDPWD.
# The records go through an unlinked file that is read back through a
# descriptor, never through the environment. zsh/zshenv sources this file
# first in the new process, so the hand-over is taken before the startup runs
# any program; zsh/zshrc calls _pd_reload_startup last, which records the
# setup's own values and puts the session back. The history file is removed
# once zsh has read it. When the startup did not get to that call
# (interrupted, or a zshrc without it), the first prompt does it: the history
# is then what zsh read from $HISTFILE (this shell's own list when it read
# nothing), HISTFILE stays the previous shell's when the setup set none, and
# a kept line comes back to the prompt, shown, not run. Not kept: other
# unexported shell state (functions, aliases, traps and variables set at the
# prompt, completions that a sourced setup.zsh added, hook functions) and $?.
# Export a variable to keep it.
#
# The reload waits, silently, while the shell has jobs (exec would orphan
# them) or stdin, stdout or stderr is not a terminal. A missing, unreadable,
# empty or non-regular generation file changes nothing. zsh ends when an exec
# fails, so the new zsh is test-run first (one fork, only when the generation
# changed). Without a working zsh, or without a private place for the
# hand-over, the shell stays as it is and that generation is not tried again;
# a typed line then runs in this shell. To keep one shell as it is:
# _pd_reload_disable (a reload already under way still completes).
#
# Hook mark. Every interactive zsh holds one file descriptor open, read-only
# and close-on-exec, on the static file
#   ${XDG_STATE_HOME:-$HOME/.local/state}/personal-dotfiles/shell-hook
# (created empty, 0600, in a 0700 directory when missing). installer/phases.py
# and bin/dotfiles tell such a shell, which reloads itself, from one without
# this hook by its /proc/<pid>/fd links; they never restart a hooked shell.
# Close-on-exec: the commands the shell runs do not get it, and 'exec bash'
# drops it. Across its own reload the shell passes a copy without
# close-on-exec (_PD_RELOAD_HOOKFD) that the new shell closes once it holds
# its own, so the process is never without the mark. The directory and the
# file are refused, without a message, when one is a symlink or not ours, or
# the directory is writable by others: then the shell has no mark (it still
# reloads itself when it has a private place for the hand-over).
#
# The reload functions run with the user's options on purpose: 'fc -AI' must
# write with this shell's history options (EXTENDED_HISTORY).

[[ -o interactive ]] || return 0

zmodload zsh/parameter 2>/dev/null   # $jobstates, $commands, $functions, $dirstack
autoload -Uz add-zsh-hook

# Sourced again in the same shell (by zsh/zshenv first, then from zsh.d, or
# by 'source ~/.zshrc'): the functions are redefined, the state (loaded
# generation, hook mark, hand-over) is kept.
if (( ! ${+_pd_reload_started} )); then
  typeset -gi _pd_reload_started=0 _pd_reload_ready=0 _pd_reload_due=0
  typeset -gi _pd_reload_has_line=0 _pd_reload_has_pending=0 _pd_reload_off=0
  typeset -gi _pd_reload_late=0 _pd_reload_settled=0 _pd_reload_hist_count=0
  typeset -g _pd_reload_file= _pd_reload_hook_file= _pd_reload_dir=
  typeset -g _pd_reload_loaded= _pd_reload_tried= _pd_reload_hook_fd=
  typeset -g _pd_reload_due_generation= _pd_reload_no_handover= _pd_reload_line=
  typeset -g _pd_reload_inherited_hook= _pd_reload_pending=
  typeset -g _pd_reload_wfd= _pd_reload_wfile= _pd_reload_sent_hist=
  typeset -g _pd_reload_hist_file= _pd_reload_hist_set= _pd_reload_hist_value=
  typeset -ga _pd_reload_in=() _pd_reload_stack=()
  typeset -gA _pd_reload_base=() _pd_reload_restored=()
fi
(( _pd_reload_started )) || () {
  local state
  if [[ ${XDG_STATE_HOME:-} == /* ]]; then
    state=$XDG_STATE_HOME
  else  # unset or not absolute: the XDG default
    state=$HOME/.local/state
  fi
  _pd_reload_dir=$state/personal-dotfiles
  _pd_reload_file=$_pd_reload_dir/generation
  _pd_reload_hook_file=$_pd_reload_dir/shell-hook
}

# Read the first line of the generation file into REPLY using builtins only.
# Fails when the file is missing, unreadable, not a regular file (a FIFO would
# block the prompt) or empty.
function _pd_reload_read() {
  REPLY=
  [[ -f $_pd_reload_file && -r $_pd_reload_file ]] || return 1
  { IFS= read -r REPLY || [[ -n $REPLY ]] } 2>/dev/null < $_pd_reload_file || return 1
  [[ -n $REPLY ]]
}

# Succeeds when the generation changed to one not tried yet (REPLY: it).
function _pd_reload_changed() {
  _pd_reload_read || return 1
  [[ $REPLY != "$_pd_reload_loaded" && $REPLY != "$_pd_reload_tried" ]]
}

# Succeeds when an exec of generation $1 now loses nothing (no jobs) and a
# zsh that runs is there (the only fork, and only when the generation
# changed). Without a working zsh that generation is not tried again.
function _pd_reload_possible() {
  (( ${#jobstates} == 0 )) || return 1
  local zsh_bin=${commands[zsh]:-}
  if [[ -n $zsh_bin && -x $zsh_bin ]] \
      && "$zsh_bin" -fc 'exit 0' </dev/null >/dev/null 2>&1; then
    return 0
  fi
  _pd_reload_tried=$1
  return 1
}

# --- private directories and the hook mark ----------------------------------

function _pd_reload_modules() {
  zmodload -F zsh/files b:zf_mkdir b:zf_rm 2>/dev/null \
    && zmodload -F zsh/stat b:zstat 2>/dev/null \
    && zmodload -F zsh/system b:sysopen b:sysread 2>/dev/null
}

# Create $1 (0700) and its missing parents (0700); succeed when $1 is a
# directory (not a symlink) of ours that others cannot write.
function _pd_reload_private_dir() {
  emulate -L zsh
  local -A st
  local dir=$1
  if ! zstat -L -H st -- $dir 2>/dev/null; then
    _pd_reload_private_parent ${dir:h} || return 1
    zf_mkdir -m 700 -- $dir 2>/dev/null
    zstat -L -H st -- $dir 2>/dev/null || return 1
  fi
  (( st[uid] == UID && (st[mode] & 8#170000) == 8#040000 && !(st[mode] & 8#022) ))
}

# Missing parents of the state directory (e.g. ~/.local/state), 0700 as the
# XDG spec asks; an existing one is used as it is.
function _pd_reload_private_parent() {
  emulate -L zsh
  local -A st
  local dir=$1
  zstat -L -H st -- $dir 2>/dev/null && return 0
  [[ ${dir:h} == $dir ]] && return 1
  _pd_reload_private_parent ${dir:h} || return 1
  zf_mkdir -m 700 -- $dir 2>/dev/null
  zstat -L -H st -- $dir 2>/dev/null && (( (st[mode] & 8#170000) == 8#040000 ))
}

# $XDG_RUNTIME_DIR when it is a directory of ours that only we can use.
function _pd_reload_runtime_ok() {
  emulate -L zsh
  local -A st
  [[ ${XDG_RUNTIME_DIR:-} == /* ]] && zstat -L -H st -- $XDG_RUNTIME_DIR 2>/dev/null \
    && (( st[uid] == UID && (st[mode] & 8#170000) == 8#040000 && !(st[mode] & 8#077) ))
}

# The private place for the hand-over (REPLY).
function _pd_reload_place() {
  emulate -L zsh
  REPLY=
  _pd_reload_modules || return 1
  if _pd_reload_runtime_ok && _pd_reload_private_dir $XDG_RUNTIME_DIR/personal-dotfiles; then
    REPLY=$XDG_RUNTIME_DIR/personal-dotfiles
  elif _pd_reload_private_dir $_pd_reload_dir; then
    REPLY=$_pd_reload_dir
  fi
  [[ -n $REPLY ]]
}

function _pd_reload_hook_open() {
  emulate -L zsh
  _pd_reload_modules || return 1
  local -A st
  local fd
  _pd_reload_private_dir $_pd_reload_dir || return 1
  if ! zstat -L -H st -- $_pd_reload_hook_file 2>/dev/null; then
    # O_EXCL: never opens what someone put there meanwhile.
    sysopen -w -o creat,excl,nofollow,cloexec -m 600 -u fd -- $_pd_reload_hook_file \
      2>/dev/null && exec {fd}>&-
  fi
  # O_NONBLOCK: a FIFO in its place does not block the shell.
  sysopen -r -o cloexec,nofollow,nonblock -u fd -- $_pd_reload_hook_file 2>/dev/null \
    || return 1
  if ! zstat -H st -f $fd 2>/dev/null \
      || (( st[uid] != UID || (st[mode] & 8#170000) != 8#100000 )); then
    exec {fd}<&-
    return 1
  fi
  _pd_reload_hook_fd=$fd
}

# Succeeds when fd $1 is open on a file whose name (deleted or not) ends in
# $2: only then is an inherited descriptor ours to read or close.
function _pd_reload_fd_is() {
  emulate -L zsh
  local -a target
  [[ $1 == <10-> ]] || return 1
  zstat -L -A target +link -- /proc/$$/fd/$1 2>/dev/null || return 1
  [[ ${target[1]%' (deleted)'} == *$2 ]]
}

# --- session state ------------------------------------------------------------

# The umask (REPLY), from /proc with builtins: no fork.
function _pd_reload_umask() {
  emulate -L zsh
  local line
  REPLY=
  { while IFS= read -r line; do
      if [[ $line == Umask:* ]]; then
        REPLY=${${line#Umask:}//[[:space:]]/}
        break
      fi
    done } 2>/dev/null </proc/$$/status
  [[ $REPLY == <-> ]]
}

# The variables whose session changes are handed over (reply): the exported
# ones and the history settings, without those zsh itself keeps changing.
function _pd_reload_tracked() {
  emulate -L zsh
  reply=(${(k)parameters[(R)*-export*]} HISTFILE HISTSIZE SAVEHIST)
  reply=(${(u)reply:#(PATH|_|PWD|OLDPWD|SHLVL|LINES|COLUMNS|_PD_RELOAD_*)})
}

# The setup's own values, at the end of startup: what a hand-over compares.
function _pd_reload_baseline() {
  emulate -L zsh
  local _pd_n REPLY
  local -a reply
  _pd_reload_base=()
  _pd_reload_tracked
  for _pd_n in $reply; do
    (( ${(P)+_pd_n} )) && _pd_reload_base[$_pd_n]=+${(P)_pd_n}
  done
  _pd_reload_umask && _pd_reload_base[.umask]=$REPLY
  _pd_reload_base[.path]=$PATH
}

# At the first prompt: what the plugins exported meanwhile (p10k's P9K_TTY,
# ...) is the setup's too, not a change of this session's. What the
# hand-over put back keeps the setup's value.
function _pd_reload_settle() {
  emulate -L zsh
  _pd_reload_settled=1
  (( _pd_reload_ready )) || return 0
  local _pd_n
  local -a reply
  _pd_reload_tracked
  for _pd_n in $reply; do
    (( ${+_pd_reload_restored[$_pd_n]} )) && continue
    (( ${(P)+_pd_n} )) && _pd_reload_base[$_pd_n]=+${(P)_pd_n}
  done
  for _pd_n in ${(k)_pd_reload_base}; do
    [[ $_pd_n == .* ]] && continue
    (( ${+_pd_reload_restored[$_pd_n]} || ${(P)+_pd_n} )) || unset "_pd_reload_base[$_pd_n]"
  done
}

function _pd_reload_name() {
  [[ -n $1 && $1 != [0-9]* && $1 != *[^A-Za-z0-9_]* ]]
}

# reply: the PATH value $1 in its order, with each entry of _pd_m_added put
# in front of the first entry of $1 that follows it in _pd_m_new and is one
# of the old setup's own (_pd_m_setup); at the end when there is none.
function _pd_reload_insert() {
  emulate -L zsh
  local -a list=(${(s.:.)1}) out=() tail=()
  local -A inlist before used
  local e a
  integer i
  for e in $list; do inlist[$e]=1; done
  for e in $_pd_m_added; do
    a=
    i=${_pd_m_new[(ie)$e]}
    for a in "${(@)_pd_m_new[i+1,-1]}"; do
      (( ${+inlist[$a]} && ${+_pd_m_setup[$a]} )) && break
      a=
    done
    if [[ -n $a ]]; then
      before[$a]+=${before[$a]:+$'\0'}$e
    else
      tail+=($e)
    fi
  done
  for e in $list; do
    if (( ${+before[$e]} && ! ${+used[$e]} )); then
      out+=("${(@0)before[$e]}")
    fi
    used[$e]=1
    out+=($e)
  done
  reply=($out $tail)
}

# PATH after a reload: $1 is the previous shell's PATH, $2 its setup's own
# (its baseline; empty: all of $1), $path what the new setup made of it.
# Empty entries are dropped. The baseline becomes the new setup's PATH
# without the session's own entries.
function _pd_reload_merge_path() {
  emulate -L zsh
  local -a old=(${(s.:.)1}) _pd_m_new=($path) _pd_m_added=() reply base=()
  local -A _pd_m_setup inold
  local e
  for e in $old; do inold[$e]=1; done
  for e in ${(s.:.)${2:-$1}}; do _pd_m_setup[$e]=1; done
  for e in $_pd_m_new; do
    (( ${+inold[$e]} )) || _pd_m_added+=($e)
  done
  _pd_reload_insert $1
  path=($reply)
  for e in $_pd_m_new; do
    (( ${+inold[$e]} && ! ${+_pd_m_setup[$e]} )) || base+=($e)
  done
  _pd_reload_base[.path]=${(j.:.)base}
  if [[ ${parameters[_OLD_VIRTUAL_PATH]-} == scalar ]]; then
    _pd_reload_insert $_OLD_VIRTUAL_PATH
    _OLD_VIRTUAL_PATH=${(j.:.)reply}
  fi
}

# Write this shell's history list to $1, every entry (SAVEHIST=0 too), with
# its timestamps.
function _pd_reload_save_history() {
  emulate -L zsh
  setopt extended_history
  local SAVEHIST=$HISTSIZE
  fc -W $1 2>/dev/null
}

# Push the saved buffer stack (_pd_reload_stack, top first) back, so that
# the next prompt finds the top entry, as it would have.
function _pd_reload_push_stack() {
  local i
  for (( i = ${#_pd_reload_stack}; i >= 1; i-- )); do
    print -rz -- "$_pd_reload_stack[i]"
  done
  _pd_reload_stack=()
}

# --- reload -------------------------------------------------------------------

# Create this shell's hand-over file in the private place, open for writing
# (_pd_reload_wfd). Fails without a private place or when it is not writable.
function _pd_reload_open_handover() {
  emulate -L zsh
  [[ -n $_pd_reload_wfd ]] && return 0
  local REPLY fd file
  _pd_reload_place || return 1
  file=$REPLY/.handover.$$
  zf_rm -f -- $file 2>/dev/null   # a leftover of a process that had this pid
  sysopen -w -o creat,excl,nofollow,cloexec -m 600 -u fd -- $file 2>/dev/null || return 1
  _pd_reload_wfd=$fd
  _pd_reload_wfile=$file
}

function _pd_reload_close_handover() {
  emulate -L zsh
  local fd=$_pd_reload_wfd
  [[ -n $fd ]] && exec {fd}>&-
  [[ -n $_pd_reload_wfile ]] && zf_rm -f -- $_pd_reload_wfile 2>/dev/null
  _pd_reload_wfd= _pd_reload_wfile=
}

# Write the hand-over (see the header) and export what the new shell reads:
# _PD_RELOAD_PID, _PD_RELOAD_FD (the records) and _PD_RELOAD_HOOKFD (a copy
# of the hook mark), all without close-on-exec.
function _pd_reload_hand_over() {
  emulate -L zsh
  _pd_reload_open_handover || return 1
  local -a _pd_rec=(v 1) _pd_stack=() reply
  local _pd_n _pd_e _pd_fd _pd_copy _pd_hist REPLY
  # The typed line; at a reloaded shell's first prompt, the one still to run.
  if (( _pd_reload_has_line )); then
    _pd_rec+=(line "$_pd_reload_line")
  elif (( _pd_reload_has_pending )); then
    _pd_rec+=(line "$_pd_reload_pending")
  fi
  # The buffer stack, top first; read -z takes the entries off it. Entries
  # handed over earlier and not pushed back yet are under them.
  while read -rz _pd_e 2>/dev/null; do
    _pd_stack+=("$_pd_e")
  done
  _pd_reload_stack=("${_pd_stack[@]}" "${_pd_reload_stack[@]}")
  for _pd_e in "${_pd_reload_stack[@]}"; do
    _pd_rec+=(stack "$_pd_e")
  done
  _pd_rec+=(path "$PATH")
  if (( _pd_reload_ready )); then
    [[ -n ${_pd_reload_base[.path]-} ]] && _pd_rec+=(basepath "$_pd_reload_base[.path]")
    _pd_reload_tracked
    for _pd_n in $reply; do
      (( ${(P)+_pd_n} )) || continue
      [[ ${_pd_reload_base[$_pd_n]-} == +${(P)_pd_n} ]] && continue
      if [[ ${parameters[$_pd_n]} == *-export* ]]; then
        _pd_rec+=(x $_pd_n "${(P)_pd_n}")
      else
        _pd_rec+=(s $_pd_n "${(P)_pd_n}")
      fi
    done
    for _pd_n in ${(k)_pd_reload_base}; do
      [[ $_pd_n == .* ]] || (( ${(P)+_pd_n} )) || _pd_rec+=(u $_pd_n)
    done
    if _pd_reload_umask && [[ $REPLY != ${_pd_reload_base[.umask]-$REPLY} ]]; then
      _pd_rec+=(umask $REPLY)
    fi
  fi
  for _pd_e in "${dirstack[@]}"; do
    _pd_rec+=(dir "$_pd_e")
  done
  (( ${+OLDPWD} )) && _pd_rec+=(oldpwd "$OLDPWD")
  [[ -n ${HISTFILE:-} ]] && _pd_rec+=(hf "$HISTFILE")
  if (( ${+VIRTUAL_ENV} && ${+functions[deactivate]} )); then
    _pd_rec+=(fn deactivate "$functions[deactivate]")
    for _pd_n in ${(k)parameters[(I)_OLD_VIRTUAL_*]}; do
      [[ ${parameters[$_pd_n]} == scalar ]] && _pd_rec+=(var $_pd_n "${(P)_pd_n}")
    done
  fi
  # The history list, by name: zsh loads it in the new shell. A session that
  # keeps no history hands it over only through memory. Leftovers of a
  # process that had this pid go first (a stale .LOCK would stall fc).
  _pd_hist=${_pd_reload_wfile:h}/.hist.$$
  if [[ -n ${HISTFILE:-} ]] && (( ${SAVEHIST:-0} > 0 )) \
      || { _pd_reload_runtime_ok && [[ $_pd_hist == $XDG_RUNTIME_DIR/* ]] }; then
    zf_rm -f -- $_pd_hist $_pd_hist.LOCK $_pd_hist.new 2>/dev/null
    if sysopen -w -o creat,excl,nofollow,cloexec -m 600 -u _pd_fd -- $_pd_hist 2>/dev/null; then
      exec {_pd_fd}>&-
      if _pd_reload_save_history $_pd_hist; then
        _pd_rec+=(hist $_pd_hist histn ${#history})
        _pd_reload_sent_hist=$_pd_hist
      else
        zf_rm -f -- $_pd_hist 2>/dev/null
      fi
    fi
  fi
  if ! { print -rn -u $_pd_reload_wfd -- ${(j: :)${(qq)_pd_rec}} } 2>/dev/null \
      || ! sysopen -r -o nofollow,cloexec -u _pd_fd -- $_pd_reload_wfile 2>/dev/null; then
    _pd_reload_close_handover
    return 1
  fi
  _pd_reload_close_handover   # the name goes; fd still reads the records
  if ! { exec {_pd_copy}<&$_pd_fd } 2>/dev/null; then
    exec {_pd_fd}<&-
    return 1
  fi
  exec {_pd_fd}<&-
  export _PD_RELOAD_PID=$$ _PD_RELOAD_FD=$_pd_copy
  _pd_copy=
  if [[ -n $_pd_reload_hook_fd ]] && { exec {_pd_copy}<&$_pd_reload_hook_fd } 2>/dev/null; then
    export _PD_RELOAD_HOOKFD=$_pd_copy
  fi
}

# Replace this shell with a fresh zsh; returns only when that did not happen.
function _pd_reload_exec() {
  # Write out the history lines not in $HISTFILE yet (-I: only the new ones,
  # so INC_APPEND_HISTORY/SHARE_HISTORY lines are not written twice). zsh
  # saves history on exec by itself only while the RCS option is set; this
  # does not depend on it, and zsh's own save then writes no line twice.
  if [[ -n ${HISTFILE:-} ]] && (( ${SAVEHIST:-0} > 0 )); then
    fc -AI 2>/dev/null
  fi
  _pd_reload_hand_over || return 1
  # zsh's exec lowers SHLVL, so the new zsh ends up at this level again.
  if [[ -o login ]]; then
    exec zsh -l
  else
    exec zsh
  fi
}

# No exec happened: undo the hand-over. The buffer stack and a kept line go
# back (the line is shown again, not run); a line still to run at this first
# prompt stays for zle-line-init, with the stack.
function _pd_reload_failed() {
  local fd
  for fd in ${_PD_RELOAD_FD:-} ${_PD_RELOAD_HOOKFD:-}; do
    [[ $fd == <10-> ]] && exec {fd}<&-
  done
  unset _PD_RELOAD_PID _PD_RELOAD_FD _PD_RELOAD_HOOKFD
  _pd_reload_close_handover
  if [[ -n $_pd_reload_sent_hist ]]; then
    zf_rm -f -- $_pd_reload_sent_hist 2>/dev/null
    _pd_reload_sent_hist=
  fi
  (( _pd_reload_has_pending )) || _pd_reload_push_stack
  (( _pd_reload_has_line )) && print -rz -- "$_pd_reload_line"
  _pd_reload_has_line=0 _pd_reload_line=
}

function _pd_reload_precmd() {
  local REPLY generation
  integer due=$_pd_reload_due
  _pd_reload_first_prompt
  _pd_reload_ensure_widgets
  _pd_reload_due=0
  if (( due )); then
    generation=$_pd_reload_due_generation
  else
    _pd_reload_changed || return 0
    generation=$REPLY
  fi
  # 0-2 are checked here, at the exec: in a zle widget stdin is /dev/null,
  # and powerlevel10k's instant prompt redirects them until its own precmd
  # hook (after this one) ran at a shell's first prompt.
  if [[ -t 0 && -t 1 && -t 2 ]] && _pd_reload_possible $generation; then
    _pd_reload_exec
    _pd_reload_tried=$generation   # no hand-over or no exec: not this one again
  elif (( due )); then
    # Enter saw a reload possible that this prompt does not: the line goes
    # back to the user, and this generation reloads from precmd only.
    _pd_reload_no_handover=$generation
  fi
  _pd_reload_failed
  return 0
}

# zle-line-finish: Enter on the primary prompt after the generation changed.
# The accepted line is kept for the new shell and emptied here, so this
# shell runs nothing and its precmd reloads at once. Every other context
# (PS2, heredoc, select, vared, ...) is left alone: precmd reloads after the
# command ran.
function _pd_reload_line_finish() {
  [[ $CONTEXT == start ]] || return 0
  # Only with the precmd hook that runs the reload: else the line runs here.
  (( ${precmd_functions[(Ie)_pd_reload_precmd]} )) || return 0
  local REPLY
  _pd_reload_changed || return 0
  [[ $REPLY != "$_pd_reload_no_handover" ]] || return 0
  [[ -t 1 && -t 2 ]] || return 0
  _pd_reload_possible $REPLY || return 0
  if ! _pd_reload_open_handover; then
    _pd_reload_tried=$REPLY   # no private place: the line runs here
    return 0
  fi
  if [[ -n $BUFFER ]]; then
    _pd_reload_line=$BUFFER
    _pd_reload_has_line=1
    BUFFER=
  fi
  _pd_reload_due_generation=$REPLY
  _pd_reload_due=1
  return 0
}

# zle-line-init: at the first prompt the setup's values settle, and a
# reloaded shell runs the handed-over line; the buffer stack goes back under
# it. When the startup emptied precmd_functions after _pd_reload_startup,
# this ends the hand-over instead and puts the reload hook back.
function _pd_reload_line_init() {
  (( _pd_reload_settled )) || _pd_reload_settle
  [[ $CONTEXT == start ]] || return 0   # a kept line waits for the primary prompt
  _pd_reload_first_prompt
  if (( ! _pd_reload_off && ! ${precmd_functions[(Ie)_pd_reload_precmd]} )); then
    add-zsh-hook precmd _pd_reload_first_prompt
    add-zsh-hook precmd _pd_reload_precmd
  fi
  (( _pd_reload_has_pending )) || return 0
  _pd_reload_has_pending=0
  _pd_reload_push_stack
  BUFFER=$_pd_reload_pending
  CURSOR=${#BUFFER}
  _pd_reload_pending=
  zle .accept-line
}

# The hooks are added through add-zle-hook-widget, so they compose with the
# plugins' own (zsh-vi-mode, F-Sy-H, powerlevel10k, prezto's editor). A
# plugin that later replaces zle-line-init/-finish outright is put back
# behind the dispatcher at the next prompt (builtins only).
function _pd_reload_ensure_widgets() {
  if (( ! ${+widgets} )); then
    zmodload zsh/zle 2>/dev/null && zmodload zsh/zleparameter 2>/dev/null || return 0
  fi
  (( ${+functions[add-zle-hook-widget]} )) || autoload -Uz add-zle-hook-widget
  local hook fn
  local -a hooked
  for hook fn in zle-line-finish _pd_reload_line_finish zle-line-init _pd_reload_line_init; do
    (( _pd_reload_off )) && [[ $fn == _pd_reload_line_finish ]] && continue
    hooked=()
    zstyle -a $hook widgets hooked
    if [[ ${widgets[$hook]-} != "user:azhw:$hook" || -z ${(M)hooked:#<->:$fn} ]]; then
      add-zle-hook-widget $hook $fn 2>/dev/null
    fi
  done
}

# Opt out for this shell: no reload (the hook mark stays, so this shell is
# still never restarted from outside). A reload under way still completes:
# the first prompt puts HISTFILE back and runs the handed-over line.
function _pd_reload_disable() {
  _pd_reload_off=1
  add-zsh-hook -d precmd _pd_reload_precmd
  (( ${+functions[add-zle-hook-widget]} )) || return 0
  add-zle-hook-widget -d line-finish _pd_reload_line_finish 2>/dev/null
  return 0
}

# --- startup ------------------------------------------------------------------

# Remove hand-over files of shells that died during a reload, with what
# zsh's history writing may have left of them (.LOCK, .new).
function _pd_reload_sweep() {
  emulate -L zsh
  local dir file pid
  local -A st
  for dir in ${XDG_RUNTIME_DIR:+$XDG_RUNTIME_DIR/personal-dotfiles} $_pd_reload_dir; do
    [[ $dir == $_pd_reload_dir ]] || _pd_reload_runtime_ok || continue
    zstat -L -H st -- $dir 2>/dev/null || continue
    (( st[uid] == UID && (st[mode] & 8#170000) == 8#040000 && !(st[mode] & 8#022) )) || continue
    for file in $dir/.(hist|handover).<->(|.LOCK|.new)(N); do
      pid=${${${file:t}#.(hist|handover).}%%.*}
      [[ $pid == $$ || -e /proc/$pid ]] || zf_rm -f -- $file 2>/dev/null
    done
  done
}

# Is $1 this shell's history hand-over file: .hist.<pid> in a private place,
# a regular file of ours?
function _pd_reload_own_hist() {
  emulate -L zsh
  local -A st
  [[ $1 == $_pd_reload_dir/.hist.$$ ]] \
    || { _pd_reload_runtime_ok && [[ $1 == $XDG_RUNTIME_DIR/personal-dotfiles/.hist.$$ ]] } \
    || return 1
  zstat -L -H st -- $1 2>/dev/null && (( st[uid] == UID && (st[mode] & 8#170000) == 8#100000 ))
}

function _pd_reload_apply_histfile() {
  case $_pd_reload_hist_set in
    (1) HISTFILE=$_pd_reload_hist_value ;;
    (0) unset HISTFILE ;;
  esac
  _pd_reload_hist_set= _pd_reload_hist_value=
}

# Put the previous shell's session back on top of the new setup: first what
# the next prompt needs (the line, the stack, the directories, PATH, the
# history), then the variables, each on its own (one that the new setup made
# read-only is skipped, and no failure stops the rest). With $1 ("late",
# from the first prompt) zsh has read $HISTFILE already: the handed-over
# history is then dropped.
function _pd_reload_restore() {
  emulate -L zsh
  local -a _pd_w=("${(@)_pd_reload_in}") _pd_dirs=() _pd_vars=() _pd_vfn=()
  local _pd_k _pd_n _pd_v _pd_path= _pd_basepath= _pd_hist= _pd_hset= _pd_hval=
  local _pd_umask= _pd_oldpwd= _pd_has_oldpwd= _pd_hf=
  local -i _pd_i=1 _pd_histn=0
  _pd_reload_in=()
  while (( _pd_i <= ${#_pd_w} )); do
    _pd_k=$_pd_w[_pd_i] _pd_n=$_pd_w[_pd_i+1] _pd_v=$_pd_w[_pd_i+2]
    case $_pd_k in
      (line) _pd_reload_pending=$_pd_n _pd_reload_has_pending=1 ;;
      (stack) _pd_reload_stack+=("$_pd_n") ;;
      (path) _pd_path=$_pd_n ;;
      (basepath) _pd_basepath=$_pd_n ;;
      (dir) _pd_dirs+=("$_pd_n") ;;
      (oldpwd) _pd_oldpwd=$_pd_n _pd_has_oldpwd=1 ;;
      (umask) [[ $_pd_n == <-> ]] && _pd_umask=$_pd_n ;;
      (hist) _pd_hist=$_pd_n ;;
      (histn) [[ $_pd_n == <-> ]] && _pd_histn=$_pd_n ;;
      (hf) _pd_hf=$_pd_n ;;
      (u)
        if [[ $_pd_n == HISTFILE ]]; then
          _pd_hset=0
        else
          _pd_vars+=(u $_pd_n '')
        fi ;;
      (x|s)
        (( _pd_i++ ))
        if [[ $_pd_n == HISTFILE ]]; then
          _pd_hset=1 _pd_hval=$_pd_v
        else
          _pd_vars+=($_pd_k $_pd_n "$_pd_v")
        fi ;;
      (fn|var)
        (( _pd_i++ ))
        _pd_vfn+=($_pd_k $_pd_n "$_pd_v") ;;
      (*) break ;;   # a newer format: the rest is not ours to read
    esac
    (( _pd_i += 2 ))
  done
  { (( ${#_pd_dirs} )) && dirstack=("${(@)_pd_dirs}") } always { TRY_BLOCK_ERROR=0 }
  { [[ -n $_pd_has_oldpwd ]] && OLDPWD=$_pd_oldpwd } always { TRY_BLOCK_ERROR=0 }
  { [[ -n $_pd_umask ]] && umask $_pd_umask } always { TRY_BLOCK_ERROR=0 }
  # A venv's deactivate and the PATH it saved, then PATH (which adds the new
  # setup's entries to that saved PATH too).
  for _pd_k _pd_n _pd_v in "${(@)_pd_vfn}"; do
    {
      if [[ $_pd_k == fn && $_pd_n == deactivate ]]; then
        (( ${+functions[$_pd_n]} )) || functions[$_pd_n]=$_pd_v
      elif [[ $_pd_k == var && $_pd_n == _OLD_VIRTUAL_* ]] && _pd_reload_name $_pd_n \
          && (( ! ${(P)+_pd_n} )); then
        typeset -g $_pd_n=$_pd_v
      fi
    } always { TRY_BLOCK_ERROR=0 }
  done 2>/dev/null
  { [[ -n $_pd_path ]] && _pd_reload_merge_path $_pd_path $_pd_basepath } \
    always { TRY_BLOCK_ERROR=0 }
  # HISTFILE: the session's own, else the new setup's, else (a startup that
  # set none, e.g. one interrupted before it) the previous shell's.
  if [[ -n $_pd_hset ]]; then
    _pd_reload_restored[HISTFILE]=1
  elif (( ${+HISTFILE} )); then
    _pd_hset=1 _pd_hval=$HISTFILE
  elif [[ -n $_pd_hf ]]; then
    _pd_hset=1 _pd_hval=$_pd_hf
  else
    _pd_hset=0
  fi
  _pd_reload_hist_set=$_pd_hset _pd_reload_hist_value=$_pd_hval
  if [[ -n $_pd_hist ]] && ! _pd_reload_own_hist $_pd_hist; then
    _pd_hist=
  fi
  if [[ -n $_pd_hist && $1 != late ]]; then
    # zsh reads the history file after the startup files: this one, here.
    _pd_reload_hist_file=$_pd_hist _pd_reload_hist_count=$_pd_histn
    HISTFILE=$_pd_hist
  else
    # Late, zsh has read the history already: this shell's own list only
    # when it read nothing.
    if [[ -n $_pd_hist ]] && (( ${#history} == 0 )); then
      fc -R $_pd_hist 2>/dev/null
    fi
    [[ -n $_pd_hist ]] && zf_rm -f -- $_pd_hist 2>/dev/null
    _pd_reload_apply_histfile
  fi
  # The variables, each on its own.
  for _pd_k _pd_n _pd_v in "${(@)_pd_vars}"; do
    _pd_reload_name $_pd_n || continue
    [[ ${parameters[$_pd_n]-} == *readonly* ]] && continue
    _pd_reload_restored[$_pd_n]=1
    {
      if [[ $_pd_k == u ]]; then
        unset $_pd_n
      else
        : ${(P)_pd_n::=$_pd_v}
        [[ $_pd_k == x ]] && export $_pd_n
      fi
    } always { TRY_BLOCK_ERROR=0 }
  done 2>/dev/null
  return 0
}

# Called last by zsh/zshrc, else by the first prompt ("late"): the setup's
# own values become the baseline, then the previous shell's session goes
# back on top of them. The hooks a reload needs are put back too, in case
# the startup removed them (a disabled shell gets only the one that ends the
# hand-over).
function _pd_reload_startup() {
  (( _pd_reload_ready )) && return 0
  _pd_reload_ready=1
  _pd_reload_baseline
  _pd_reload_modules && _pd_reload_sweep
  (( ${#_pd_reload_in} )) && _pd_reload_restore $1
  add-zsh-hook precmd _pd_reload_first_prompt
  (( _pd_reload_off )) || add-zsh-hook precmd _pd_reload_precmd
  return 0
}

# At the first prompt (cheap afterwards): zsh has read the handed-over
# history, so HISTFILE goes back to what it should be (and, should zsh have
# read nothing of a non-empty list, $HISTFILE is read instead). The buffer
# stack goes back now unless a kept line runs first (zle-line-init does it
# then). After an incomplete startup ("late") the kept line comes back to
# the prompt, shown, not run.
function _pd_reload_first_prompt() {
  if (( ! _pd_reload_ready )); then
    _pd_reload_late=1
    _pd_reload_startup late
  fi
  if [[ -n $_pd_reload_hist_file ]]; then
    zf_rm -f -- $_pd_reload_hist_file 2>/dev/null
    _pd_reload_hist_file=
    _pd_reload_apply_histfile
    if (( _pd_reload_hist_count > 0 && ${#history} == 0 )) && [[ -n ${HISTFILE:-} ]]; then
      fc -R 2>/dev/null
    fi
    _pd_reload_hist_count=0
  fi
  if (( _pd_reload_late && _pd_reload_has_pending )); then
    _pd_reload_has_pending=0
    _pd_reload_push_stack
    if [[ -n ${WIDGET-} ]]; then   # from zle-line-init: into this prompt's line
      BUFFER=$_pd_reload_pending
      CURSOR=${#BUFFER}
    else
      print -rz -- "$_pd_reload_pending"
    fi
    _pd_reload_pending=
  elif (( ${#_pd_reload_stack} && ! _pd_reload_has_pending )); then
    _pd_reload_push_stack
  fi
}

# What the shell this process was before the exec handed over; anything
# meant for another process (inherited through the environment) is dropped.
(( _pd_reload_started )) || () {
  emulate -L zsh
  local fd=${_PD_RELOAD_FD:-} chunk data
  local -a words
  if [[ ${_PD_RELOAD_PID:-} == $$ ]]; then
    _pd_reload_inherited_hook=${_PD_RELOAD_HOOKFD:-}
    if _pd_reload_modules && _pd_reload_fd_is "$fd" "/personal-dotfiles/.handover.$$"; then
      while sysread -i $fd chunk 2>/dev/null; do data+=$chunk; done
      exec {fd}<&-
      words=("${(@Q)${(z)data}}")
      if [[ $words[1] == v && $words[2] == 1 ]]; then
        _pd_reload_in=("${(@)words[3,-1]}")
      fi
    fi
  fi
  unset _PD_RELOAD_PID _PD_RELOAD_FD _PD_RELOAD_HOOKFD
}

(( _pd_reload_started )) || () {
  local REPLY fd
  _pd_reload_read && _pd_reload_loaded=$REPLY
  _pd_reload_hook_open || _pd_reload_hook_fd=
  # Only now, with this shell's own mark open, drop the inherited copy.
  fd=$_pd_reload_inherited_hook
  _pd_reload_inherited_hook=
  if _pd_reload_fd_is "$fd" /personal-dotfiles/shell-hook; then
    exec {fd}<&-
  fi
  _pd_reload_started=1
}

add-zsh-hook precmd _pd_reload_first_prompt
(( _pd_reload_off )) || add-zsh-hook precmd _pd_reload_precmd
# From zsh/zshenv (_pd_reload_early) the widgets wait for zsh.d or the first
# prompt, so the plugins' hooks keep their order.
(( ${_pd_reload_early:-0} )) || _pd_reload_ensure_widgets
