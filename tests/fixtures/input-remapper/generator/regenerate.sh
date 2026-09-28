#!/bin/bash
# Regenerate and loader-verify the input-remapper fixtures for one family.
#
#   regenerate.sh FAMILY DIST_PACKAGES OUT_DIR [PYTHON]
#
# FAMILY        1.4 or 2.0
# DIST_PACKAGES <root>/usr/lib/python3/dist-packages of python3-inputremapper
#               extracted with "dpkg-deb -x" (1.4.0-1 from jammy, 2.0.1-1 from
#               noble). Never point it at an installed copy.
# PYTHON        interpreter with python3-evdev (plus pydantic for 2.0). For an
#               extracted interpreter export PYTHONHOME/PYTHONPATH first.
#
# Everything runs with a throwaway HOME under /tmp. The scripts patch
# input-remapper's pwd-derived config root into it and deny evdev
# device/uinput access and subprocesses; no daemon, reader, injector, GUI or
# input-remapper-control command runs. OUT_DIR/fixtures and OUT_DIR/owned are
# what tests/fixtures/input-remapper/v<FAMILY>/ holds.
set -euo pipefail

family=$1
dist=$2
out=$3
py=${4:-python3}

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../../.." && pwd)
home=$(mktemp -d /tmp/ir-fixture-home.XXXXXX)
trap 'rm -rf "$home"' EXIT

run() {
  local extra=()
  [[ -n ${PYTHONHOME:-} ]] && extra+=("PYTHONHOME=$PYTHONHOME")
  [[ -n ${PYTHONPATH:-} ]] && extra+=("PYTHONPATH=$PYTHONPATH")
  env -i PATH=/usr/bin:/bin HOME="$home" XDG_CONFIG_HOME="$home/.config" \
    USER="$(id -un)" LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 "${extra[@]}" \
    "$py" -W ignore "$@"
}

mkdir -p "$out/fixtures" "$out/owned"
run "$here/gen_proc.py" > "$out/proc-bus-input-devices.txt"
run "$here/gen_fixtures.py" "$family" "$dist" "$out/fixtures" \
  > "$out/generate-report.json"
run "$here/verify_adapters.py" "$family" "$dist" "$out/fixtures" \
  "$repo/desktop/input-remapper/adapters.py" \
  "$out/proc-bus-input-devices.txt" "$out/owned" > "$out/verify-report.json"
echo "verified: $out/verify-report.json"
