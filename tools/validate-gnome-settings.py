#!/usr/bin/env python3
"""Validate desktop/gnome-settings.json against a compiled GSettings schema dir.

Every probe runs the host's ``gsettings`` with ``--schemadir`` on the *memory*
backend inside a throwaway HOME/XDG tree, with no D-Bus address, so nothing
here can read or write a real dconf database. The schema dir is the only
schema source: the default search path is pointed at an empty directory and
that isolation is itself checked before any entry is judged.

Typical use, with schema XML extracted from a release's packages::

    validate-gnome-settings.py --release 24.04 --compile-from EXTRACTED_ROOT
    validate-gnome-settings.py --release 22.04 --schemadir COMPILED_DIR --json

Exit status: 0 every check passed, 1 at least one check failed, 2 the
validator could not run (bad arguments, unreadable manifest, missing tools).

Standard library only; Python 3.10 compatible.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "desktop" / "gnome-settings.json"
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_TIMEOUT = 10.0

# "enum" values are GVariant strings restricted to the schema's enum nicks.
SUPPORTED_TYPES = ("as", "s", "b", "enum")
ALLOWED_SCOPES = ("global", "setting", "app:gnome-terminal")
ALLOWED_GROUPS = (
    "window-navigation",
    "workspace-navigation",
    "monitor-navigation",
    "application-navigation",
    "terminal-launcher",
    "terminal-tabs",
    "terminal-clipboard",
    "window-tiling",
    "dock-behavior",
    "prerequisite",
)
# Only these groups may hold explicit unbindings (``@as []``).
UNBINDING_GROUPS = ("window-tiling",)
# The manifest is deliberately narrow. A schema outside this list is scope
# creep, not a navigation setting, and is rejected structurally.
ALLOWED_SCHEMAS = (
    "org.gnome.desktop.wm.keybindings",
    "org.gnome.shell.keybindings",
    "org.gnome.settings-daemon.plugins.media-keys",
    "org.gnome.mutter",
    "org.gnome.mutter.keybindings",
    "org.gnome.shell.extensions.dash-to-dock",
    "org.gnome.Terminal.Legacy.Settings",
    "org.gnome.Terminal.Legacy.Keybindings",
)
# Accelerator scopes: keys of these scopes are grabbed by the compositor or a
# desktop daemon. The rest are handled inside one application window.
GLOBAL_ACCEL_SCOPES = ("global",)

# GTK accelerator modifiers. ``Primary`` and ``Ctrl`` are spellings of Control.
_MODIFIER_ALIASES = {
    "shift": "Shift",
    "control": "Control",
    "ctrl": "Control",
    "primary": "Control",
    "alt": "Alt",
    "mod1": "Alt",
    "super": "Super",
    "meta": "Meta",
    "hyper": "Hyper",
    "mod2": "Mod2",
    "mod3": "Mod3",
    "mod4": "Mod4",
    "mod5": "Mod5",
}
_ACCEL_RE = re.compile(r"^((?:<[A-Za-z0-9]+>)*)([A-Za-z0-9_]+)$")
_MODIFIER_RE = re.compile(r"<([A-Za-z0-9]+)>")
_STRING_RE = re.compile(r"'([^'\\]*)'")
_LIST_RE = re.compile(r"^(?:@as )?\[(.*)\]$")
# Terminal-style single accelerators use this literal for "no binding".
DISABLED = "disabled"


class ValidatorError(Exception):
    """The validator cannot run; distinct from a failed check."""


# --------------------------------------------------------------------------
# GVariant text and accelerator parsing (no GLib needed)
# --------------------------------------------------------------------------


def parse_gvariant(text: str, gtype: str) -> object:
    """Parse the subset of GVariant text the manifest uses.

    Returns ``list[str]`` for ``as``, ``str`` for ``s`` and ``enum`` and
    ``bool`` for ``b``.
    Raises ``ValueError`` for anything else, including strings with escapes:
    no accelerator needs one, so an escape means the value is not what the
    manifest claims it is.
    """

    text = text.strip()
    if gtype == "b":
        if text in ("true", "false"):
            return text == "true"
        raise ValueError(f"not a GVariant boolean: {text!r}")
    if gtype in ("s", "enum"):
        match = _STRING_RE.fullmatch(text)
        if not match:
            raise ValueError(f"not a plain GVariant string: {text!r}")
        return match.group(1)
    if gtype == "as":
        match = _LIST_RE.match(text)
        if not match:
            raise ValueError(f"not a GVariant string list: {text!r}")
        body = match.group(1).strip()
        if not body:
            return []
        items: list[str] = []
        position = 0
        while True:
            string = _STRING_RE.match(body, position)
            if not string:
                raise ValueError(f"malformed string list element in {text!r}")
            items.append(string.group(1))
            position = string.end()
            if position == len(body):
                return items
            if body[position : position + 2] != ", ":
                raise ValueError(f"malformed string list separator in {text!r}")
            position += 2
    raise ValueError(f"unsupported GVariant type {gtype!r}")


def normalize_accelerator(accel: str) -> tuple[frozenset[str], str]:
    """Return a comparable ``(modifiers, key)`` form of a GTK accelerator.

    Modifier order and spelling (``<Primary>``/``<Ctrl>``/``<Control>``) do not
    matter, and a single letter key is case-insensitive, the same way GTK and
    Mutter treat them.
    """

    match = _ACCEL_RE.match(accel)
    if not match:
        raise ValueError(f"malformed accelerator {accel!r}")
    modifiers = set()
    for raw in _MODIFIER_RE.findall(match.group(1)):
        canonical = _MODIFIER_ALIASES.get(raw.lower())
        if canonical is None:
            raise ValueError(f"unknown modifier <{raw}> in {accel!r}")
        modifiers.add(canonical)
    key = match.group(2)
    if len(key) == 1:
        key = key.lower()
    return frozenset(modifiers), key


def accelerators_of(entry: dict) -> list[str]:
    """Accelerator strings an entry binds; empty for non-keybinding entries."""

    if entry.get("scope") == "setting" or entry.get("type") == "enum":
        return []
    value = parse_gvariant(entry["value"], entry["type"])
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        return [] if value == DISABLED else [value]
    return []


def target_of(entry: dict) -> str:
    """``SCHEMA`` or ``SCHEMA:PATH`` as gsettings expects it."""

    path = entry.get("path")
    return f"{entry['schema']}:{path}" if path else entry["schema"]


# --------------------------------------------------------------------------
# Structural checks (pure; also used by the unit tests)
# --------------------------------------------------------------------------


def load_manifest(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValidatorError(f"cannot read manifest {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValidatorError(f"manifest {path} is not valid JSON: {exc}") from exc


def structural_errors(manifest: dict) -> list[str]:
    """Return every structural problem; an empty list means well formed."""

    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest must be a JSON object"]
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {MANIFEST_SCHEMA_VERSION}, "
            f"got {manifest.get('schema_version')!r}"
        )
    if not isinstance(manifest.get("defaults_desktop"), str) or not manifest.get(
        "defaults_desktop"
    ):
        errors.append("defaults_desktop must name the XDG_CURRENT_DESKTOP of the defaults")
    releases = manifest.get("releases")
    if not isinstance(releases, dict) or not releases:
        errors.append("releases must be a non-empty object")
        releases = {}
    packages = manifest.get("schema_packages", {})
    if not isinstance(packages, dict):
        errors.append("schema_packages must be an object")
        packages = {}
    unverified = manifest.get("unverified", [])
    if not isinstance(unverified, list) or not all(
        isinstance(item, str) and item for item in unverified
    ):
        errors.append("unverified must be a list of non-empty strings")
    settings = manifest.get("settings")
    if not isinstance(settings, list) or not settings:
        return errors + ["settings must be a non-empty list"]
    if not all(isinstance(entry, dict) for entry in settings):
        return errors + ["every settings element must be an object"]

    seen_ids: set[str] = set()
    seen_targets: set[tuple[str, str]] = set()
    for index, entry in enumerate(settings):
        label = entry.get("id") or f"settings[{index}]"
        for field in ("id", "group", "scope", "schema", "key", "value", "type"):
            if not isinstance(entry.get(field), str) or not entry.get(field):
                errors.append(f"{label}: missing string field {field!r}")
        if any(
            not isinstance(entry.get(f), str) for f in ("id", "schema", "key", "value", "type")
        ):
            continue
        if entry["id"] in seen_ids:
            errors.append(f"{label}: duplicate id")
        seen_ids.add(entry["id"])
        pair = (target_of(entry), entry["key"])
        if pair in seen_targets:
            errors.append(f"{label}: {pair[0]} {pair[1]} is managed twice")
        seen_targets.add(pair)
        if entry.get("group") not in ALLOWED_GROUPS:
            errors.append(f"{label}: group {entry.get('group')!r} is out of scope")
        if entry.get("scope") not in ALLOWED_SCOPES:
            errors.append(f"{label}: unknown scope {entry.get('scope')!r}")
        if entry["schema"] not in ALLOWED_SCHEMAS:
            errors.append(f"{label}: schema {entry['schema']} is out of scope")
        if entry["schema"] not in packages:
            errors.append(f"{label}: schema {entry['schema']} has no schema_packages owner")
        path = entry.get("path")
        if path is not None and not (
            isinstance(path, str)
            and path.startswith("/")
            and path.endswith("/")
            and "//" not in path
        ):
            errors.append(f"{label}: relocatable path {path!r} must be /…/ form")
        if entry.get("type") not in SUPPORTED_TYPES:
            errors.append(f"{label}: unsupported type {entry.get('type')!r}")
            continue
        try:
            parsed = parse_gvariant(entry["value"], entry["type"])
            for accel in accelerators_of(entry):
                normalize_accelerator(accel)
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
            continue
        unbinds = entry.get("unbinds", False)
        if not isinstance(unbinds, bool):
            errors.append(f"{label}: unbinds must be a boolean")
        elif unbinds:
            if parsed != []:
                errors.append(f"{label}: unbinds=true needs the value @as []")
            if entry.get("group") not in UNBINDING_GROUPS:
                errors.append(f"{label}: explicit unbindings belong to {UNBINDING_GROUPS}")
            if not entry.get("note"):
                errors.append(f"{label}: an unbinding needs a note saying what it frees")
        elif parsed == [] and entry["type"] == "as":
            errors.append(f"{label}: an empty list unbinds the action; declare unbinds=true")
        if entry["type"] == "enum" and entry.get("scope") != "setting":
            errors.append(f"{label}: enum entries must use scope 'setting'")

        per_release = entry.get("releases")
        if not isinstance(per_release, dict) or set(per_release) != set(releases):
            errors.append(f"{label}: releases must cover exactly {sorted(releases)}")
            continue
        for release, record in per_release.items():
            where = f"{label} [{release}]"
            if not isinstance(record, dict) or not isinstance(record.get("available"), bool):
                errors.append(f"{where}: available must be a boolean")
                continue
            if not record["available"]:
                if set(record) != {"available"}:
                    errors.append(f"{where}: unavailable record must not claim a default")
                if not entry.get("note"):
                    errors.append(f"{where}: unavailable key needs a note explaining the gap")
                continue
            default = record.get("default")
            if not isinstance(default, str):
                errors.append(f"{where}: available record needs the release default")
                continue
            try:
                parse_gvariant(default, entry["type"])
            except ValueError as exc:
                errors.append(f"{where}: default {exc}")
            if record.get("equals_default") is not (default == entry["value"]):
                errors.append(f"{where}: equals_default disagrees with default/value")
        if not any(
            isinstance(r, dict) and r.get("available") for r in per_release.values()
        ):
            errors.append(f"{label}: not available on any release")
    errors.extend(_share_errors(settings))
    return errors


def _share_errors(settings: list) -> list[str]:
    """``shares_accelerator_with`` must name other, existing entries that
    really bind a common accelerator in the same scope."""

    errors: list[str] = []
    by_id = {e.get("id"): e for e in settings if isinstance(e.get("id"), str)}
    for entry in settings:
        if "shares_accelerator_with" not in entry:
            continue
        label = entry.get("id") or "(entry)"
        shares = entry["shares_accelerator_with"]
        if not isinstance(shares, list) or not all(isinstance(s, str) for s in shares):
            errors.append(f"{label}: shares_accelerator_with must be a list of ids")
            continue
        if len(shares) != len(set(shares)):
            errors.append(f"{label}: shares_accelerator_with lists an id twice")
        for other_id in shares:
            other = by_id.get(other_id)
            if other_id == label:
                errors.append(f"{label}: shares_accelerator_with names itself")
                continue
            if other is None:
                errors.append(f"{label}: shares_accelerator_with names unknown id {other_id!r}")
                continue
            if other.get("scope") != entry.get("scope"):
                errors.append(f"{label}: shares with {other_id} across scopes")
                continue
            try:
                mine = {normalize_accelerator(a) for a in accelerators_of(entry)}
                theirs = {normalize_accelerator(a) for a in accelerators_of(other)}
            except (ValueError, KeyError, TypeError):
                continue  # reported by the per-entry checks
            if not mine & theirs:
                errors.append(f"{label}: shares_accelerator_with {other_id} but no accelerator is common")
    return errors


def accelerator_conflicts(manifest: dict) -> list[str]:
    """Report accelerators bound twice within a scope, or shadowed by a global.

    Two entries may share an accelerator only when each lists the other in
    ``shares_accelerator_with``. An application-scope accelerator that equals
    a global one never reaches the application, so it is always a conflict.
    """

    owners: dict[tuple[str, tuple[frozenset[str], str]], list[str]] = {}
    shared: dict[str, set[str]] = {}
    for entry in manifest.get("settings", []):
        shared[entry["id"]] = set(entry.get("shares_accelerator_with", []))
        for accel in accelerators_of(entry):
            key = (entry["scope"], normalize_accelerator(accel))
            owners.setdefault(key, []).append(entry["id"])

    problems: list[str] = []
    global_accels = {
        accel: ids for (scope, accel), ids in owners.items() if scope in GLOBAL_ACCEL_SCOPES
    }
    for (scope, accel), ids in sorted(owners.items(), key=lambda item: str(item[0])):
        shown = _display_accel(accel)
        if len(ids) != len(set(ids)):
            problems.append(f"{ids[0]}: binds {shown} more than once")
        unique = sorted(set(ids))
        for i, first in enumerate(unique):
            for second in unique[i + 1 :]:
                if second not in shared[first] or first not in shared[second]:
                    problems.append(f"{first} and {second} both bind {shown} in scope {scope}")
        if scope not in GLOBAL_ACCEL_SCOPES and accel in global_accels:
            problems.append(
                f"{', '.join(unique)} ({scope}) binds {shown}, which the global "
                f"{', '.join(sorted(set(global_accels[accel])))} grabs first"
            )
    return problems


def _display_accel(accel: tuple[frozenset[str], str]) -> str:
    modifiers, key = accel
    return "".join(f"<{m}>" for m in sorted(modifiers)) + key


# --------------------------------------------------------------------------
# gsettings probes
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Result:
    id: str
    check: str
    status: str  # PASS / FAIL
    detail: str

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def isolated_env(
    sandbox: Path,
    desktop: str,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Environment for gsettings that cannot reach a real settings store.

    Built from scratch rather than by deleting variables, so an unexpected
    inherited variable (a D-Bus address, a schema dir, a dconf profile) cannot
    leak in. Only PATH is carried over.

    ``desktop`` becomes ``XDG_CURRENT_DESKTOP``. It is not cosmetic: GLib
    applies ``[schema:desktop]`` override sections by it, and Ubuntu ships
    such sections for window switching and workspaces, so the "release
    default" of a key depends on it.
    """

    base = dict(os.environ if base is None else base)
    empty = sandbox / "empty"
    for name in ("home", "config", "data", "cache", "runtime", "empty"):
        (sandbox / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    return {
        "PATH": base.get("PATH", "/usr/bin:/bin"),
        "LC_ALL": "C",
        "LANG": "C",
        "HOME": str(sandbox / "home"),
        "XDG_CONFIG_HOME": str(sandbox / "config"),
        "XDG_DATA_HOME": str(sandbox / "data"),
        "XDG_CACHE_HOME": str(sandbox / "cache"),
        "XDG_RUNTIME_DIR": str(sandbox / "runtime"),
        "XDG_DATA_DIRS": str(empty),
        "XDG_CURRENT_DESKTOP": desktop,
        "GSETTINGS_BACKEND": "memory",
        "DCONF_PROFILE": str(sandbox / "empty" / "no-such-profile"),
    }


class GSettings:
    def __init__(
        self,
        schemadir: Path,
        env: dict[str, str],
        *,
        binary: str = "gsettings",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.schemadir = schemadir
        self.env = env
        self.binary = binary
        self.timeout = timeout

    def run(self, *args: str, schemadir: bool = True) -> tuple[int, str, str]:
        argv = [self.binary]
        if schemadir:
            argv += ["--schemadir", str(self.schemadir)]
        argv += list(args)
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                env=self.env,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {self.timeout}s: {' '.join(argv[1:])}"
        except OSError as exc:
            raise ValidatorError(f"cannot run {self.binary}: {exc}") from exc
        return done.returncode, done.stdout.strip(), done.stderr.strip()


def check_isolation(gs: GSettings) -> list[Result]:
    """The default schema source must be empty, or a missing release schema
    would silently fall back to whatever the host has installed."""

    code, out, err = gs.run("list-schemas", schemadir=False)
    leaked = [line for line in out.splitlines() if line.strip()]
    if leaked:
        return [
            Result(
                "(environment)",
                "isolation",
                "FAIL",
                f"{len(leaked)} host schemas visible without --schemadir",
            )
        ]
    code, out, err = gs.run("list-schemas")
    if code != 0 or not out:
        return [
            Result(
                "(environment)",
                "isolation",
                "FAIL",
                f"no schemas readable from the schema dir ({err or 'empty'})",
            )
        ]
    return [Result("(environment)", "isolation", "PASS", "only the given schema dir is visible")]


def validate_release(manifest: dict, release: str, gs: GSettings, *, try_set: bool = True) -> list[Result]:
    results = check_isolation(gs)
    if results[0].status != "PASS":
        return results

    _, plain, _ = gs.run("list-schemas")
    _, relocatable, _ = gs.run("list-relocatable-schemas")
    fixed_schemas = set(plain.split())
    relocatable_schemas = set(relocatable.split())

    for entry in manifest["settings"]:
        ident = entry["id"]
        record = entry["releases"].get(release)
        if record is None:
            results.append(Result(ident, "release-record", "FAIL", f"no record for {release}"))
            continue
        target = target_of(entry)
        wanted_kind = relocatable_schemas if entry.get("path") else fixed_schemas
        schema_present = entry["schema"] in wanted_kind
        code, out, err = gs.run("range", target, entry["key"]) if schema_present else (1, "", "")

        if not record["available"]:
            if code == 0:
                results.append(
                    Result(ident, "absent", "FAIL", "manifest says unavailable but the key exists")
                )
            else:
                reason = "schema absent" if not schema_present else "key absent"
                results.append(Result(ident, "absent", "PASS", f"{reason}, as declared"))
            continue

        if not schema_present:
            kind = "relocatable" if entry.get("path") else "non-relocatable"
            results.append(
                Result(ident, "schema", "FAIL", f"{kind} schema {entry['schema']} not found")
            )
            continue
        if code != 0:
            results.append(Result(ident, "key", "FAIL", err or "key not found"))
            continue
        parts = out.split()
        if entry["type"] == "enum":
            nicks = parts[1:] if parts[:1] == ["enum"] else None
            if nicks is None:
                results.append(
                    Result(ident, "type", "FAIL", f"schema says {out!r}, manifest says enum")
                )
                continue
            if entry["value"] not in nicks:
                results.append(
                    Result(ident, "value", "FAIL", f"{entry['value']} is not one of {' '.join(nicks)}")
                )
                continue
        elif len(parts) < 2 or parts[0] != "type" or parts[1] != entry["type"]:
            results.append(
                Result(ident, "type", "FAIL", f"schema says {out!r}, manifest says {entry['type']}")
            )
            continue

        code, default, err = gs.run("get", target, entry["key"])
        if code != 0:
            results.append(Result(ident, "default", "FAIL", err or "get failed"))
            continue
        if default != record["default"]:
            results.append(
                Result(
                    ident,
                    "default",
                    "FAIL",
                    f"release default is {default}, manifest records {record['default']}",
                )
            )
            continue
        if not gs_writable(gs, target, entry["key"]):
            results.append(Result(ident, "writable", "FAIL", "key is not writable"))
            continue
        if try_set:
            # The memory backend forgets between processes, so this proves the
            # value parses against the key's type and range, not persistence.
            code, _, err = gs.run("set", target, entry["key"], entry["value"])
            if code != 0:
                results.append(Result(ident, "value", "FAIL", err or "set rejected the value"))
                continue
        results.append(
            Result(
                ident,
                "key",
                "PASS",
                f"type {entry['type']}, default {'==' if record['equals_default'] else '!='} value",
            )
        )
    return results


def default_collisions(manifest: dict, release: str, gs: GSettings) -> list[str]:
    """Report-only: unmanaged keys whose release default binds an accelerator
    that a managed global entry also binds on ``release``.

    GNOME does not arbitrate these statically (the Shell, Mutter and
    extensions grab keys at runtime), so a hit is a note for a live test,
    never a validation failure. Relocatable schemas have no path to list and
    are not searched.
    """

    managed_pairs = set()
    managed_accels: dict[tuple[frozenset[str], str], list[str]] = {}
    for entry in manifest.get("settings", []):
        managed_pairs.add((entry["schema"], entry["key"]))
        record = entry.get("releases", {}).get(release, {})
        if entry.get("scope") not in GLOBAL_ACCEL_SCOPES or not record.get("available"):
            continue
        for accel in accelerators_of(entry):
            managed_accels.setdefault(normalize_accelerator(accel), []).append(entry["id"])
    # ``list-recursively`` without a schema ignores --schemadir, so walk the
    # schemas of the given dir one by one.
    code, out, err = gs.run("list-schemas")
    if code != 0:
        return [f"(could not list schemas: {err or code})"]
    lines: list[str] = []
    for schema in sorted(out.split()):
        code, listing, err = gs.run("list-recursively", schema)
        if code == 0:
            lines.extend(listing.splitlines())
    notes: list[str] = []
    for line in lines:
        parts = line.split(" ", 2)
        if len(parts) != 3 or (parts[0], parts[1]) in managed_pairs:
            continue
        schema, key, value = parts
        try:
            accels = parse_gvariant(value, "as")
            normalized = [normalize_accelerator(a) for a in accels]
        except ValueError:
            continue
        for accel in normalized:
            if accel[0] and accel in managed_accels:
                notes.append(
                    f"{schema} {key} (unmanaged default) binds {_display_accel(accel)}, "
                    f"also bound by {', '.join(managed_accels[accel])}"
                )
    return notes


def gs_writable(gs: GSettings, target: str, key: str) -> bool:
    code, out, _ = gs.run("writable", target, key)
    return code == 0 and out == "true"


# --------------------------------------------------------------------------
# Schema compilation
# --------------------------------------------------------------------------

_SCHEMA_SUFFIXES = (".gschema.xml", ".enums.xml", ".gschema.override")


def compile_schemas(source: Path, destination: Path, *, timeout: float = 60.0) -> Path:
    """Copy schema sources from ``source`` and compile them into ``destination``.

    ``source`` may be a schema directory or the root of extracted packages
    (``usr/share/glib-2.0/schemas`` underneath it is used when present).
    ``--strict`` makes a broken or conflicting override fail instead of
    being dropped quietly.
    """

    nested = source / "usr" / "share" / "glib-2.0" / "schemas"
    if nested.is_dir():
        source = nested
    if not source.is_dir():
        raise ValidatorError(f"schema source {source} is not a directory")
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    for item in sorted(source.iterdir()):
        if item.is_file() and item.name.endswith(_SCHEMA_SUFFIXES):
            shutil.copyfile(item, destination / item.name)
            copied += 1
    if not copied:
        raise ValidatorError(f"no schema sources in {source}")
    compiler = shutil.which("glib-compile-schemas")
    if compiler is None:
        raise ValidatorError("glib-compile-schemas is not installed")
    try:
        done = subprocess.run(
            [compiler, "--strict", str(destination)],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValidatorError(f"glib-compile-schemas timed out after {timeout}s") from exc
    if done.returncode != 0:
        raise ValidatorError(f"glib-compile-schemas failed: {done.stderr.strip()}")
    return destination


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--release", required=True, help="Ubuntu release, e.g. 24.04")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--schemadir", type=Path, help="directory with gschemas.compiled")
    source.add_argument(
        "--compile-from",
        type=Path,
        help="schema XML directory or extracted package root to compile first",
    )
    parser.add_argument(
        "--desktop",
        help="XDG_CURRENT_DESKTOP for resolving defaults (default: manifest defaults_desktop)",
    )
    parser.add_argument("--gsettings", default="gsettings", help="gsettings binary to run")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--no-set", action="store_true", help="skip the memory-backend set probe")
    parser.add_argument("--json", action="store_true", help="print results as JSON")
    parser.add_argument(
        "--report-default-collisions",
        action="store_true",
        help="also list unmanaged keys whose release default shares a managed global "
        "accelerator (report only; never changes the exit status)",
    )
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(args.manifest)
        problems = structural_errors(manifest)
        if not problems:
            problems = accelerator_conflicts(manifest)
        if problems:
            for problem in problems:
                print(f"FAIL manifest: {problem}", file=sys.stderr)
            return 1
        if args.release not in manifest["releases"]:
            raise ValidatorError(
                f"release {args.release} is not in the manifest "
                f"({', '.join(sorted(manifest['releases']))})"
            )
        with tempfile.TemporaryDirectory(prefix="gnome-settings-validate.") as scratch:
            sandbox = Path(scratch)
            if args.compile_from is not None:
                schemadir = compile_schemas(args.compile_from, sandbox / "schemas")
            else:
                schemadir = args.schemadir
                if not (schemadir / "gschemas.compiled").is_file():
                    raise ValidatorError(f"{schemadir} has no gschemas.compiled")
            gs = GSettings(
                schemadir.resolve(),
                isolated_env(sandbox, args.desktop or manifest["defaults_desktop"]),
                binary=args.gsettings,
                timeout=args.timeout,
            )
            results = validate_release(manifest, args.release, gs, try_set=not args.no_set)
            collisions = (
                default_collisions(manifest, args.release, gs)
                if args.report_default_collisions and results and results[0].status == "PASS"
                else []
            )
    except ValidatorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    failed = [r for r in results if r.status != "PASS"]
    if args.json:
        payload = {
            "release": args.release,
            "passed": len(results) - len(failed),
            "failed": len(failed),
            "results": [r.to_dict() for r in results],
        }
        if args.report_default_collisions:
            payload["default_collisions"] = collisions
        print(json.dumps(payload, indent=2))
    else:
        for r in results:
            print(f"{r.status} {r.id} [{r.check}] {r.detail}")
        for note in collisions:
            print(f"NOTE collision: {note}")
        print(f"{args.release}: {len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
