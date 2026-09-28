#!/usr/bin/env zsh
# Regression test: fasd's preexec hook must never execute any part of the
# command line it records (a multi-line paste used to run lines 2..N twice).
emulate -L zsh
setopt err_return

repo=${0:A:h:h}
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
cd "$work"

# A fake fasd that records what --proc received, and the vulnerable upstream
# hook it replaces.
fasd() { [[ $1 == --proc ]] && { shift; print -r -- "$*" >> "$work/fasd.log" }; }
_fasd_preexec() { { eval "fasd --proc $(print -r -- $1)"; } >> /dev/null 2>&1 }

source "$repo/zsh/zsh.d/fasd.zsh"

_fasd_preexec $'ls some-file\ntouch PWNED-1\n$(touch PWNED-2)'

fail=0
for f in PWNED-1 PWNED-2; do
  if [[ -e $f ]]; then print -u2 "FAIL: hook executed part of the command ($f)"; fail=1; fi
done
if ! grep -q 'some-file' "$work/fasd.log" 2>/dev/null; then
  print -u2 "FAIL: fasd did not receive the command's arguments"; fail=1
fi
(( fail )) && exit 1
print "ok: fasd preexec records without executing"
