#!/usr/bin/env bash
# Tests for bin/mem-usage with fakes on PATH: procps free(1) for Linux,
# sysctl(8) and vm_stat(1) for macOS. Python is told the platform each case
# stands for, so both branches run on either system. Runs under bash 3.2.

set -uo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
mem_usage=${repo_root}/bin/mem-usage

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

fakebin=$(mktemp -d)
trap 'rm -rf -- "${fakebin}"' EXIT

# procps-ng free -m (Ubuntu 24.04).
cat >"${fakebin}/free" <<'FAKE'
#!/bin/sh
[ "$1" = -m ] || exit 2
cat <<'OUT'
               total        used        free      shared  buff/cache   available
Mem:           15897        5000        2000         500        8397       10000
Swap:           2047         100        1947
OUT
FAKE

# macOS 15 on Apple silicon: 16 KiB pages, 16 GiB.
cat >"${fakebin}/sysctl" <<'FAKE'
#!/bin/sh
[ "$1" = -n ] || exit 2
case $2 in
  hw.memsize) echo 17179869184 ;;
  vm.swapusage) echo 'total = 2048.00M  used = 1024.25M  free = 1023.75M  (encrypted)' ;;
  *) echo "sysctl: unknown oid '$2'" >&2; exit 1 ;;
esac
FAKE
cat >"${fakebin}/vm_stat" <<'FAKE'
#!/bin/sh
[ -n "${FAKE_VM_STAT_FAIL:-}" ] && { echo 'vm_stat: failed' >&2; exit 1; }
cat <<'OUT'
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                5000.
Pages active:                            320000.
Pages inactive:                          310000.
Pages speculative:                         2000.
Pages throttled:                              0.
Pages wired down:                        100000.
Pages purgeable:                          10000.
"Translation faults":                 987654321.
Pages copy-on-write:                   12345678.
Pages zero filled:                    123456789.
Pages reactivated:                       123456.
Pages purged:                             23456.
File-backed pages:                       200000.
Anonymous pages:                         300000.
Pages stored in compressor:              150000.
Pages occupied by compressor:             50000.
Decompressions:                          345678.
Compressions:                            456789.
Pageins:                                 567890.
Pageouts:                                  1234.
Swapins:                                      0.
Swapouts:                                     0.
OUT
FAKE
chmod 755 "${fakebin}/free" "${fakebin}/sysctl" "${fakebin}/vm_stat"

# mem_usage PLATFORM [ARG...]: the script, as on PLATFORM ("linux"/"darwin").
mem_usage() {
  local platform=$1; shift
  PATH="${fakebin}:${PATH}" python3 -B -c '
import runpy, sys
sys.platform = sys.argv[1]
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name="__main__")
' "${platform}" "${mem_usage}" "$@"
}

check "the shebang names python3 (macOS has no python)" '#!/usr/bin/env python3' \
  "$(head -n 1 "${mem_usage}")"

# Linux: free -m, with shared memory counted as used (5000 + 500 MiB).
out=$(mem_usage linux)
check "linux: exit status" 0 "$?"
check_contains "linux: memory used of total" '  5.4 /  15.5 G' "${out}"
check_contains "linux: swap used of total" '  0.1 /   2.0 G' "${out}"

# macOS: total from hw.memsize; used as Activity Monitor counts it: app memory
# (anonymous - purgeable: 290000 pages), wired (100000) and compressed
# (50000), 440000 pages of 16 KiB = 6875 MiB; swap from vm.swapusage.
out=$(mem_usage darwin)
check "macos: exit status" 0 "$?"
check_contains "macos: memory used of total" '  6.7 /  16.0 G' "${out}"
check_contains "macos: swap used of total" '  1.0 /   2.0 G' "${out}"
check "macos: the bars, at --width 10" \
  'Mem : [||||      ]   6.7 /  16.0 G      Swap: [|||||     ]   1.0 /   2.0 G' \
  "$(mem_usage darwin --width 10 | tr -d '\033' | sed 's/\[0;3[23]m//g; s/\[0m//g')"

# macOS without its statistics: one line, no traceback, status 1.
err=$(FAKE_VM_STAT_FAIL=1 mem_usage darwin 2>&1 >/dev/null)
check "macos: a failing vm_stat exits 1" 1 "$(FAKE_VM_STAT_FAIL=1 mem_usage darwin >/dev/null 2>&1; echo $?)"
check_contains "macos: a failing vm_stat is reported" 'mem-usage: cannot read the memory statistics' "${err}"
check "macos: no traceback" "" "$(grep -c Traceback <<<"${err}" | grep -v '^0$')"

if (( failures )); then
  printf 'FAIL: %d of %d mem-usage assertions failed\n' "${failures}" "${checks}" >&2
  exit 1
fi
printf 'ok: %d mem-usage assertions\n' "${checks}"
