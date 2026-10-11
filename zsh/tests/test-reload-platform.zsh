#!/usr/bin/env zsh
# The platform-dependent pieces of zsh/zsh.d/dotfiles-reload.zsh, natively on
# this system: how the module tells an inherited descriptor (Linux: its
# /proc/<pid>/fd link; macOS: fstat), reads the umask, chooses the hand-over
# place (macOS: a private $TMPDIR, never XDG_RUNTIME_DIR) and tells live pids
# when it sweeps. On Linux it also runs against a copy of the module without
# /proc whose $OSTYPE tests answer darwin (as the noproc tier of
# tests/test-zsh-dotfiles-reload.zsh, which needs /proc for its own checks
# and so cannot run on macOS; this one can). Each round is an interactive
# zsh -f with the module sourced and a temporary HOME, XDG_STATE_HOME and
# TMPDIR; nothing else of the system is touched.

emulate -LR zsh
setopt errexit nounset pipefail

typeset -gr repo=${0:A:h:h:h}
typeset -g work
work=$(mktemp -d "${TMPDIR:-/tmp}/zsh-reload-platform.XXXXXX")
trap 'rm -rf -- "$work"' EXIT

# Run inside the interactive zsh, after the module. PROBE_DARWIN says which
# platform's answers to expect; PROBE_SYSTEM_TMPDIR is the $TMPDIR this test
# itself got (on macOS the system's per-user /var/folders/.../T/).
read -r -d '' probe <<'PROBE' || :
emulate -L zsh
zmodload zsh/system zsh/stat zsh/files
local -i darwin=$PROBE_DARWIN
local got w r c l h p dead f
ok() { print -r -- "ok $1" }
bad() { print -r -- "FAIL $1" }
expect() {   # expect NAME WANT(0: succeeds, 1: fails) COMMAND...
    local name=$1 want=$2
    shift 2
    if "$@"; then got=0; else got=1; fi
    if [[ $got == $want ]]; then ok $name; else bad "$name (status $got, want $want)"; fi
}
[[ -n $_pd_reload_hook_fd ]] && ok "the module holds its mark" || bad "the module holds its mark"

# The records, made as _pd_reload_hand_over makes them: created 0600, written,
# reopened for reading, unlinked, the descriptor copied without close-on-exec.
f=$_pd_reload_dir/.handover.$$
sysopen -w -o creat,excl,nofollow,cloexec -m 600 -u w -- $f
print -rn -u $w -- "'v' '1'"
sysopen -r -o nofollow,cloexec -u r -- $f
exec {w}>&-
zf_rm -f -- $f
exec {c}<&$r
exec {r}<&-
expect "the records, unlinked" 0 _pd_reload_fd_is $c /personal-dotfiles/.handover.$$
expect "the records offered as the mark" 1 _pd_reload_fd_is $c /personal-dotfiles/shell-hook
expect "the records of another pid" 1 _pd_reload_fd_is $c /personal-dotfiles/.handover.1
exec {c}<&-

# A file that keeps its name, a copy of the mark, a pipe, a low descriptor.
: >| $PROBE_WORK/linked
chmod 600 $PROBE_WORK/linked
exec {l}<$PROBE_WORK/linked
expect "a named file offered as the records" 1 _pd_reload_fd_is $l /personal-dotfiles/.handover.$$
expect "a named file offered as the mark" 1 _pd_reload_fd_is $l /personal-dotfiles/shell-hook
exec {l}<&-
exec {h}<&$_pd_reload_hook_fd
expect "a copy of the mark" 0 _pd_reload_fd_is $h /personal-dotfiles/shell-hook
expect "the mark offered as the records" 1 _pd_reload_fd_is $h /personal-dotfiles/.handover.$$
exec {h}<&-
exec {p}< <(print -r -- x)
expect "a pipe" 1 _pd_reload_fd_is $p /personal-dotfiles/shell-hook
exec {p}<&-
expect "a descriptor below 10" 1 _pd_reload_fd_is 2 /personal-dotfiles/shell-hook

# The umask, as a login shell's hand-over reads it.
umask 027
_pd_reload_umask && [[ $REPLY == (0|)027 ]] && ok "the umask" || bad "the umask (${REPLY-})"
umask 077

# The hand-over place: macOS takes a private $TMPDIR, never XDG_RUNTIME_DIR.
expect "a private XDG_RUNTIME_DIR counts" $darwin _pd_reload_runtime_ok
expect "a private \$TMPDIR counts" $(( ! darwin )) _pd_reload_tmpdir_ok
_pd_reload_place
if (( darwin )); then
    [[ $REPLY == ${TMPDIR%/}/personal-dotfiles && -d $REPLY ]] \
        && ok "the place is \$TMPDIR/personal-dotfiles" || bad "the place ($REPLY)"
else
    [[ $REPLY == $XDG_RUNTIME_DIR/personal-dotfiles ]] \
        && ok "the place is XDG_RUNTIME_DIR/personal-dotfiles" || bad "the place ($REPLY)"
fi
if (( darwin )) && [[ $PROBE_SYSTEM_TMPDIR == /var/folders/* ]]; then
    TMPDIR=$PROBE_SYSTEM_TMPDIR expect "the system's own \$TMPDIR counts" 0 _pd_reload_tmpdir_ok
fi
chmod 755 ${TMPDIR%/}
expect "a \$TMPDIR open to others does not" 1 _pd_reload_tmpdir_ok
chmod 700 ${TMPDIR%/}
ln -s ${TMPDIR%/} $PROBE_WORK/T-link
TMPDIR=$PROBE_WORK/T-link/ expect "a symlinked \$TMPDIR does not" 1 _pd_reload_tmpdir_ok

# The history hand-over in the $TMPDIR place is this shell's own on macOS
# only (elsewhere that directory is no place of the module's).
zf_mkdir -p -m 700 -- ${TMPDIR%/}/personal-dotfiles
: >| ${TMPDIR%/}/personal-dotfiles/.hist.$$
chmod 600 ${TMPDIR%/}/personal-dotfiles/.hist.$$
expect "a history hand-over in \$TMPDIR is own" $(( ! darwin )) \
    _pd_reload_own_hist ${TMPDIR%/}/personal-dotfiles/.hist.$$
zf_rm -f -- ${TMPDIR%/}/personal-dotfiles/.hist.$$

# The sweep: a dead shell's leftovers go (pid 0 counts as dead), a live
# process's stay; on macOS in the $TMPDIR place too, elsewhere that one is
# not touched.
( : ) &
dead=$!
wait $dead 2>/dev/null || :
for f in .hist.$dead .handover.$dead .handover.0 .hist.$PPID; do
    : >| $_pd_reload_dir/$f
done
: >| ${TMPDIR%/}/personal-dotfiles/.hist.$dead
_pd_reload_sweep
[[ ! -e $_pd_reload_dir/.hist.$dead && ! -e $_pd_reload_dir/.handover.$dead ]] \
    && ok "the sweep removes a dead pid's files" || bad "the sweep removes a dead pid's files"
[[ ! -e $_pd_reload_dir/.handover.0 ]] \
    && ok "the sweep removes pid 0's file" || bad "the sweep removes pid 0's file"
[[ -e $_pd_reload_dir/.hist.$PPID ]] \
    && ok "the sweep keeps a live pid's file" || bad "the sweep keeps a live pid's file"
if [[ -e ${TMPDIR%/}/personal-dotfiles/.hist.$dead ]]; then got=1; else got=0; fi
[[ $got == $(( ! darwin )) ]] && ok "the sweep and the \$TMPDIR place" \
    || bad "the sweep and the \$TMPDIR place (left: $got)"
PROBE

integer assertions=0 failures=0

round() {   # round NAME MODULE DARWIN(0|1)
    local name=$1 module=$2 root=$work/$1 line out
    integer before=$assertions
    mkdir -p $root/home $root/state $root/run $root/T
    chmod 700 $root/run $root/T
    out=$(env -i HOME=$root/home XDG_STATE_HOME=$root/state XDG_RUNTIME_DIR=$root/run \
        TMPDIR=$root/T/ PATH=/usr/bin:/bin TERM=dumb PROBE_DARWIN=$3 PROBE_WORK=$root \
        PROBE_SYSTEM_TMPDIR=${TMPDIR:-} zsh -f -i -c 'source $1; eval $2' zsh $module $probe \
        </dev/null 2>&1) || :
    for line in ${(f)out}; do
        case $line in
            (ok\ *) (( ++assertions )) ;;
            (FAIL\ *) (( ++assertions, ++failures )); print -u2 -r -- "FAIL: $name: ${line#FAIL }" ;;
            (*) (( ++failures )); print -u2 -r -- "FAIL: $name: unexpected output: $line" ;;
        esac
    done
    (( assertions - before >= 21 )) || {
        print -u2 -r -- "FAIL: $name: the probe did not run to its end"
        (( ++failures ))
    }
}

if [[ $OSTYPE == darwin* ]]; then
    round macos $repo/zsh/zsh.d/dotfiles-reload.zsh 1
else
    round native $repo/zsh/zsh.d/dotfiles-reload.zsh 0
    # Without /proc and with macOS's $OSTYPE: the module's macOS branches.
    sed -e 's|/proc/|/nonexistent-proc/|g' -e 's|\$OSTYPE|darwin24.0|g' \
        $repo/zsh/zsh.d/dotfiles-reload.zsh >| $work/dotfiles-reload-darwin.zsh
    if grep -v '^ *#' $work/dotfiles-reload-darwin.zsh | grep -q -e '/proc/' -e 'OSTYPE'; then
        print -u2 -r -- "FAIL: cannot take /proc and \$OSTYPE out of the module; adapt this test"
        exit 1
    fi
    round darwin-sim $work/dotfiles-reload-darwin.zsh 1
fi

if (( failures )); then
    print -u2 -r -- "FAIL: $failures of $assertions reload platform assertions failed"
    exit 1
fi
print -r -- "ok: $assertions reload platform assertions"
