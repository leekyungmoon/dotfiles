#!/usr/bin/env zsh

set -eu
setopt pipefail

repo=${0:A:h:h}
test_root=$(mktemp -d "${TMPDIR:-/tmp}/ssh-zsh-profile.XXXXXX")
trap 'rm -rf -- "$test_root"' EXIT

fail() {
  print -u2 -- "FAIL: $*"
  exit 1
}

assert_eq() {
  [[ $1 == $2 ]] || fail "expected ${(qqq)2}, got ${(qqq)1}"
}

shared="$test_root/shared home"
profile="$test_root/profile home"
release="$test_root/release root"
source_root="$release/src"
mkdir -p -- "$shared/.local/bin" "$profile/config" \
  "$profile/data" "$profile/state/zsh" "$release/bin" \
  "$release/cache" "$source_root/bin" \
  "$source_root/zdot/.zsh/functions"

print 'export SHARED_LOCAL_WAS_SOURCED=1' > "$shared/.zshenv.local"
print 'export PROFILE_LOCAL_WAS_SOURCED=1' > "$profile/.zshenv.local"

HOME="$shared" SSH_ZSH_HOME="$profile" SSH_ZSH_ROOT="$source_root" \
ZDOTDIR="$source_root/zdot" XDG_CONFIG_HOME="$profile/config" \
XDG_CACHE_HOME="$release/cache" XDG_DATA_HOME="$profile/data" \
XDG_STATE_HOME="$profile/state" HISTFILE=inherited zsh -dfc '
  source "$1/zsh/zshenv"
  [[ -z ${SHARED_LOCAL_WAS_SOURCED:-} ]]
  [[ ${PROFILE_LOCAL_WAS_SOURCED:-} == 1 ]]
  [[ $HISTFILE == "$SSH_ZSH_HOME/state/zsh/history" ]]
  [[ ${(t)HISTFILE} != *export* ]]
  zstyle -s ":prezto:module:history" histfile styled_history
  [[ $styled_history == "$SSH_ZSH_HOME/state/zsh/history" ]]
  [[ $_FASD_DATA == "$SSH_ZSH_HOME/state/fasd" ]]
  [[ $FZF_MARKS_FILE == "$SSH_ZSH_HOME/state/fzf-marks" ]]
  [[ $GITSTATUS_AUTO_INSTALL == 0 ]]
  [[ $GOPATH == "$SSH_ZSH_HOME/data/go" && -d $GOPATH ]]
  [[ $WORKON_HOME == "$SSH_ZSH_HOME/data/virtualenvs" ]]
  [[ $PYTHONSTARTUP == "$SSH_ZSH_HOME/config/pythonrc.py" ]]
  [[ $MASON == "$XDG_DATA_HOME/nvim/mason/" ]]
  [[ $path[1] == "${SSH_ZSH_ROOT:h}/bin" ]]
  [[ $path[2] == "$SSH_ZSH_ROOT/bin" ]]
  (( $path[(Ie)$HOME/.local/bin] > 2 ))
  [[ $fpath[(Ie)$ZDOTDIR/.zsh/functions] -gt 0 ]]
  [[ ! -e "$HOME/.go" ]]
' zsh "$repo" || fail 'isolated ssh profile contract'

normal="$test_root/normal home"
mkdir -p -- "$normal/.dotfiles/bin" "$normal/.zsh/functions"
print 'export NORMAL_LOCAL_WAS_SOURCED=1' > "$normal/.zshenv.local"
normal_zdot="$test_root/normal custom zdot"
mkdir -p -- "$normal_zdot/.zsh/functions"
HOME="$normal" ZDOTDIR="$normal_zdot" \
SSH_ZSH_HOME='' SSH_ZSH_ROOT='' \
XDG_DATA_HOME="$test_root/normal custom data" zsh -dfc '
  source "$1/zsh/zshenv"
  [[ ${NORMAL_LOCAL_WAS_SOURCED:-} == 1 ]]
  [[ $GOPATH == "$HOME/.go" && -d $HOME/.go ]]
  [[ $WORKON_HOME == "$HOME/.virtualenvs" ]]
  [[ $PYTHONSTARTUP == "$HOME/.pythonrc.py" ]]
  [[ $MASON == "$HOME/.local/share/nvim/mason/" ]]
  [[ $path[(Ie)$HOME/.dotfiles/bin] -gt 0 ]]
  [[ $fpath[(Ie)$HOME/.zsh/functions] -gt 0 ]]
  [[ $fpath[(Ie)$ZDOTDIR/.zsh/functions] -eq 0 ]]
' zsh "$repo" || fail 'ordinary profile fallback contract'

if HOME="$shared" SSH_ZSH_HOME=relative SSH_ZSH_ROOT="$source_root" \
    zsh -dfc 'source "$1/zsh/zshenv"' zsh "$repo" 2>/dev/null; then
  fail 'relative SSH_ZSH_HOME was accepted'
fi

# A shared profile never asks antidote to create (and potentially fetch) a
# missing bundle during connection. The client-side install owns this state.
fake_zdot="$test_root/fake zdot"
mkdir -p -- "$fake_zdot/.zsh/antidote" "$release/cache/zsh"
print 'antidote() { print called >> "$SSH_ZSH_HOME/antidote-called"; }' \
  > "$fake_zdot/.zsh/antidote/antidote.zsh"
offline_result=''
if offline_result=$(HOME="$shared" SSH_ZSH_HOME="$profile" SSH_ZSH_ROOT="$source_root" \
    ZDOTDIR="$fake_zdot" XDG_CONFIG_HOME="$profile/config" \
    XDG_CACHE_HOME="$release/cache" XDG_DATA_HOME="$profile/data" \
    XDG_STATE_HOME="$profile/state" TERM=dumb \
    zsh -dfc 'source "$1/zsh/zshrc"; rc=$?; print -r -- "$ANTIDOTE_HOME|$ANTIDOTE_BUNDLE|${_ssh_zsh_ready:-unset}"; exit $rc' \
    zsh "$repo" 2>/dev/null); then
  fail 'shared zshrc accepted a missing offline plugin bundle'
fi
assert_eq "$offline_result" \
  "$release/plugins|$release/antidote.bundled.zsh|0"
[[ ! -e "$profile/antidote-called" ]] || \
  fail 'shared zshrc invoked antidote for a missing bundle'

mkdir -p -- "$release/cache/prezto"
print dump > "$release/cache/prezto/zcompdump"
HOME="$shared" SSH_ZSH_HOME="$profile" \
XDG_CACHE_HOME="$release/cache" RESULT="$test_root/zcompiled" \
zsh -dfc '
  zcompile() { print -r -- "$1" >| "$RESULT"; }
  source "$1/zsh/zlogin"
  for attempt in {1..50}; do
    [[ -s $RESULT ]] && break
    sleep 0.01
  done
  [[ $(<$RESULT) == "$XDG_CACHE_HOME/prezto/zcompdump" ]]
' zsh "$repo" || fail 'shared zlogin compiled the wrong Prezto dump'

unsupported=$(SSH_ZSH_HOME="$profile" SSH_ZSH_ROOT="$source_root" \
  ZDOTDIR="$source_root/zdot" zsh -dfc '
    compdef() { :; }
    source "$1/zsh/zsh.d/alias.zsh"
    plugs
  ' zsh "$repo" 2>&1) && fail 'shared plugs unexpectedly succeeded'
[[ $unsupported == *'unavailable in the source-only ssh-dotfiles profile'* ]] || \
  fail 'shared plugs did not explain that editor assets are unavailable'

unsupported=$(SSH_ZSH_HOME="$profile" SSH_ZSH_ROOT="$source_root" \
  ZDOTDIR="$source_root/zdot" zsh -dfc '
    compdef() { :; }
    source "$1/zsh/zsh.d/alias.zsh"
    plenary-busted .
  ' zsh "$repo" 2>&1) && fail 'shared plenary-busted unexpectedly succeeded'
[[ $unsupported == *'unavailable in the source-only ssh-dotfiles profile'* ]] || \
  fail 'shared plenary-busted did not explain that editor assets are unavailable'

SSH_ZSH_HOME="$profile" zsh -dfc '
  source "$1/zsh/p10k.zsh"
  [[ $POWERLEVEL9K_DISABLE_GITSTATUS == true ]]
' zsh "$repo" || fail 'shared prompt did not disable gitstatus'

SSH_ZSH_HOME='' zsh -dfc '
  typeset -g POWERLEVEL9K_DISABLE_GITSTATUS=inherited
  source "$1/zsh/p10k.zsh"
  [[ -z ${POWERLEVEL9K_DISABLE_GITSTATUS:-} ]]
' zsh "$repo" || fail 'ordinary prompt unexpectedly disabled gitstatus'

print 'PASS: ssh zsh profile paths are isolated; ordinary paths are preserved'
