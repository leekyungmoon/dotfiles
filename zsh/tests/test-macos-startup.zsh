#!/usr/bin/env zsh
# What zsh/zshenv and zsh/zsh.d/alias.zsh set on macOS, and that Linux keeps
# what it had. Either platform is simulated on any system: a fake uname
# answers Darwin (arm64 or x86_64) or Linux, OSTYPE is set before the file is
# sourced, and for macOS a copy of zshenv finds Homebrew under a temporary
# directory instead of /opt/homebrew. Every case is a zsh -f with an empty
# environment and a temporary HOME.

emulate -LR zsh
setopt errexit nounset pipefail

typeset -gr repo=${0:A:h:h:h}
# The cases run with PATHs of fakes only: the zsh that runs them by its path.
typeset -gr zsh_bin=${commands[zsh]:-/bin/zsh}
typeset -g work
work=$(mktemp -d "${TMPDIR:-/tmp}/zsh-startup-test.XXXXXX")
trap 'rm -rf -- "$work"' EXIT

integer assertions=0 failures=0

check() {   # check NAME EXPECTED ACTUAL
    (( ++assertions ))
    [[ $2 == "$3" ]] && return 0
    print -u2 -r -- "FAIL: $1"
    print -u2 -r -- "  expected: ${(qqq)2}"
    print -u2 -r -- "  actual:   ${(qqq)3}"
    (( ++failures ))
}

# A fake uname per platform: uname, uname -s and uname -m.
fake_uname() {   # fake_uname DIR SYSTEM MACHINE
    mkdir -p $1
    print -r -- "#!/bin/sh
case \${1:-} in -m) echo $3 ;; *) echo $2 ;; esac" >| $1/uname
    chmod 755 $1/uname
}
fake_uname $work/linux Linux x86_64
fake_uname $work/darwin Darwin arm64
fake_uname $work/intel Darwin x86_64

# Homebrew's prefix, a python3 that is no Homebrew one, a venv with `python`.
typeset -gr brew=$work/opt/homebrew
mkdir -p $brew/bin $brew/sbin $work/pybin $work/venv/bin
for tool in $work/pybin/python3 $work/venv/bin/python; do
    print -r -- '#!/bin/sh' >| $tool
    chmod 755 $tool
done
# The copies run without the host's path_helper (macOS's /etc/paths.d would
# add the host's own entries), and the macOS one finds Homebrew under $brew.
sed "s|/usr/libexec/path_helper|$work/no-path-helper|g" $repo/zsh/zshenv >| $work/zshenv-linux
sed -e "s|/usr/libexec/path_helper|$work/no-path-helper|g" -e "s|/opt/homebrew|$brew|g" \
    $repo/zsh/zshenv >| $work/zshenv-darwin
[[ $(grep -c "$brew" $work/zshenv-darwin) -ge 2 \
    && $(grep -c "$work/no-path-helper" $work/zshenv-linux) -ge 2 ]] || {
    print -u2 -r -- "FAIL: zshenv no longer names /opt/homebrew or path_helper; adapt this test"
    exit 1
}

# startup OSTYPE ZSHENV PATH CODE: CODE after sourcing ZSHENV in a zsh -f.
startup() {
    local home=$work/home-$RANDOM
    mkdir -p $home
    env -i HOME=$home PATH=$3 TMPDIR=$work TERM=dumb \
        $zsh_bin -f -c 'OSTYPE=$1; source $2; eval $3' zsh $1 $2 $4
}
report='print -r -- "SSD=${SHELL_SESSIONS_DISABLE-unset}|HB=${HOMEBREW_PREFIX-unset}|CELLAR=${HOMEBREW_CELLAR-unset}|REPO=${HOMEBREW_REPOSITORY-unset}|VWP=$VIRTUALENVWRAPPER_PYTHON"; print -r -- "PATH=$PATH"'
field() { print -r -- ${${(M)${(f)1}:#$2=*}#$2=} }   # field TEXT NAME
typeset -ga path_list matched

# --- zsh/zshenv on Linux: as before -------------------------------------------

out=$(startup linux-gnu $work/zshenv-linux $work/linux:$work/pybin:/usr/bin:/bin "$report")
check "linux: no Terminal.app switch" "SSD=unset|HB=unset|CELLAR=unset|REPO=unset|VWP=/usr/bin/python" \
    "${${(f)out}[1]}"
path_list=(${(s.:.)"$(field $out PATH)"})
matched=(${(M)path_list:#*homebrew*})
check "linux: no Homebrew directory on PATH" "" "${matched[*]}"

# --- zsh/zshenv on macOS (Apple silicon) -----------------------------------------

if [[ -x /usr/local/bin/python ]]; then
    want_vwp=/usr/local/bin/python   # upstream's Intel path, still used where it exists
else
    want_vwp=$work/pybin/python3
fi
out=$(startup darwin24.0 $work/zshenv-darwin $work/darwin:$work/pybin:/usr/bin:/bin "$report")
check "macos: Terminal.app sessions off, brew shellenv's variables, a python3 that exists" \
    "SSD=1|HB=$brew|CELLAR=$brew/Cellar|REPO=$brew|VWP=$want_vwp" "${${(f)out}[1]}"
path_list=(${(s.:.)"$(field $out PATH)"})
check "macos: Homebrew's bin, then right behind it its sbin" \
    "$brew/bin $brew/sbin" "${path_list[(r)$brew/bin]-} ${path_list[${path_list[(i)$brew/bin]}+1]-}"

# An inherited PATH (tmux, a LaunchAgent) with bin but not sbin: sbin goes in
# behind bin; with both, nothing moves and nothing doubles.
out=$(startup darwin24.0 $work/zshenv-darwin $brew/bin:$work/darwin:$work/pybin:/usr/bin:/bin 'print -r -- $PATH')
path_list=(${(s.:.)out})
check "macos: inherited bin, sbin added behind it" "$brew/sbin" \
    "${path_list[${path_list[(i)$brew/bin]}+1]-}"
out=$(startup darwin24.0 $work/zshenv-darwin \
    $brew/sbin:$work/darwin:$brew/bin:$work/pybin:/usr/bin:/bin 'print -r -- $PATH')
path_list=(${(s.:.)out})
integer bins=${#${(@M)path_list:#$brew/bin}} sbins=${#${(@M)path_list:#$brew/sbin}}
check "macos: inherited bin and sbin, once each" "1 1" "$bins $sbins"
check "macos: inherited sbin stays in front of bin" "yes" \
    "$( (( ${path_list[(i)$brew/sbin]} < ${path_list[(i)$brew/bin]} )) && print yes || print no )"

# No sbin directory: none added.
rmdir $brew/sbin
out=$(startup darwin24.0 $work/zshenv-darwin $work/darwin:$work/pybin:/usr/bin:/bin 'print -r -- $PATH')
path_list=(${(s.:.)out})
matched=(${(M)path_list:#$brew/sbin})
check "macos: no sbin directory, none on PATH" "" "${matched[*]}"
check "macos: (control) bin is still there" "$brew/bin" "${path_list[(r)$brew/bin]-}"
mkdir $brew/sbin

# Intel: Homebrew in /usr/local is no prefix of this block's (path_helper and
# the system entries at the end of zshenv bring /usr/local/bin).
out=$(startup darwin24.0 $work/zshenv-darwin $work/intel:$work/pybin:/usr/bin:/bin "$report")
check "macos intel: no arm64 prefix" "SSD=1|HB=unset" "${${${(f)out}[1]}%%|CELLAR=*}"

# --- zsh/zsh.d/alias.zsh: python tool aliases ------------------------------------

aliases_probe='compdef() { : }; source $ALIASES; for a in jupyter jupyter-lab python-config python3-config; do eval "print -r -- $a=${aliases[$a]}"; done'
alias_run() {   # alias_run OSTYPE PATH
    local home=$work/home-$RANDOM
    mkdir -p $home
    env -i HOME=$home PATH=$2 TERM=dumb ALIASES=$repo/zsh/zsh.d/alias.zsh \
        $zsh_bin -f -c 'OSTYPE=$1; eval $2' zsh $1 $aliases_probe 2>/dev/null
}

# macOS has no `python`: the aliases name python3's directory, not the text
# of `which python` ("python: aliased to python3"). Only the fakes are on
# PATH, so no python of the host's takes part (alias.zsh runs nothing else
# that matters at load).
out=$(alias_run darwin24.0 $work/darwin:$work/pybin)
check "macos: jupyter from python3's directory" "$work/pybin/jupyter" "$(field $out jupyter)"
check "macos: jupyter-lab too" "$work/pybin/jupyter-lab --no-browser" "$(field $out jupyter-lab)"
check "macos: python-config too" "$work/pybin/python3-config" "$(field $out python-config)"
check "macos: python3-config too" "$work/pybin/python3-config" "$(field $out python3-config)"
# A venv's python comes first.
out=$(alias_run darwin24.0 $work/venv/bin:$work/darwin:$work/pybin)
check "macos: a venv's jupyter" "$work/venv/bin/jupyter" "$(field $out jupyter)"

# Linux keeps its aliases as they were.
out=$(env -i HOME=$work PATH=$work/linux TERM=dumb ALIASES=$repo/zsh/zsh.d/alias.zsh \
    $zsh_bin -f -c 'OSTYPE=linux-gnu; compdef() { : }; source $ALIASES; print -r -- ${aliases[jupyter]}' 2>/dev/null)
check "linux: the jupyter alias as before" '${$(which python)%/*}/jupyter' "$out"

if (( failures )); then
    print -u2 -r -- "FAIL: $failures of $assertions startup assertions failed"
    exit 1
fi
print -r -- "ok: $assertions startup assertions"
