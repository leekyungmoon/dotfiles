"""input-remapper serialization, reconciliation and target-discovery adapters.

Ubuntu 22.04 ships input-remapper 1.4.0 and Ubuntu 24.04 ships 2.0.1. The two
releases store presets differently and handle a held modifier differently, so
every format decision lives here, keyed by a *family* ("1.4" or "2.0").

Facts taken from the packages' own source (python3-inputremapper 1.4.0-1 and
2.0.1-1) and checked against their own loaders:

* The config root is ``<passwd home>/.config/input-remapper`` (1.4) or
  ``<passwd home>/.config/input-remapper-2`` (2.0). input-remapper ignores
  ``XDG_CONFIG_HOME``, so callers pass the owner's home, never a config home.
* ``config.json`` holds ``{"version": ..., "autoload": {group_key: preset}}``:
  exactly one autoload preset per device group key.
* Presets live in ``presets/<group name>/<preset>.json``. The group *name* is
  the shortest device name of the group (2.0 replaces reserved filename
  characters); autoload is keyed by the group *key*, which is the name plus
  " 2", " 3" ... for identical devices.
* 1.4 presets are ``{"mapping": {"1,29,1+1,125,1+1,105,1": [symbol, target]},
  ...extra config}``; 2.0 presets are a JSON list of mapping objects whose
  ``input_combination`` is a list of ``{"type", "code", "origin_hash"?}``.
* A combination matches regardless of the order of all but its last key.
* input-remapper's own virtual devices are named ``input-remapper ...``.

Everything here is pure except the small read-only helpers at the bottom,
which take explicit roots, and :func:`run_control`, which is the only place a
subprocess is started and which tests never call. Nothing in this module
writes a file: :func:`apply_group` and :func:`restore_group` return
:class:`FileOp` lists so the caller can back the targets up first.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

FAMILY_V1 = "1.4"
FAMILY_V2 = "2.0"
FAMILIES = (FAMILY_V1, FAMILY_V2)

# Exact upstream versions whose formats were inspected. Anything else is
# refused rather than assumed to be compatible.
SUPPORTED_VERSIONS = {"1.4.0": FAMILY_V1, "2.0.1": FAMILY_V2}

CONFIG_DIRNAMES = {FAMILY_V1: "input-remapper", FAMILY_V2: "input-remapper-2"}

LIBRARY_PACKAGE = "python3-inputremapper"
CONTROL_PACKAGE = "input-remapper-daemon"
CONTROL_EXECUTABLE = "input-remapper-control"

EV_KEY = 1
VIRTUAL_NAME_PREFIX = "input-remapper"

# Whether the family can deliver the intent using only the managed chords.
# 1.4 has no "release_combination_keys": the already-held Ctrl and Super keep
# being forwarded, so the focused application would see Ctrl+Super+Page_Up.
# Its documented workaround needs the Super keys themselves remapped, which is
# outside the two managed chords.
INTENT_SUPPORT = {
    FAMILY_V1: (
        False,
        "input-remapper 1.4 keeps forwarding the held Super key while a "
        "combination triggers (no release_combination_keys), so the focused "
        "application would receive Ctrl+Super+Page_Up instead of Ctrl+Page_Up",
    ),
    FAMILY_V2: (True, ""),
}

MANAGED_NAME_PREFIX = "personal-dotfiles"

RESTORE_SCHEMA = 1


class AdapterError(Exception):
    """input-remapper data cannot be handled safely."""


class UnsupportedIntent(AdapterError):
    """This input-remapper family cannot express the intent safely."""


class MigrationPending(AdapterError):
    """input-remapper 2 has not migrated the 1.4 configuration yet."""


# ---------------------------------------------------------------------------
# Version detection


def parse_dpkg_status(text: str, packages: Iterable[str]) -> dict[str, str]:
    """Return ``{package: version}`` for installed packages in dpkg status text."""

    wanted = set(packages)
    found: dict[str, str] = {}
    for stanza in re.split(r"\n\s*\n", text):
        fields: dict[str, str] = {}
        for line in stanza.splitlines():
            if not line or line[0].isspace() or ":" not in line:
                continue
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
        name = fields.get("Package")
        if name not in wanted:
            continue
        if fields.get("Status", "").split()[-1:] != ["installed"]:
            continue
        if "Version" in fields:
            found[name] = fields["Version"]
    return found


def upstream_version(debian_version: str) -> str:
    """``"1:2.0.1-1ubuntu1"`` -> ``"2.0.1"``."""

    version = debian_version.strip()
    if ":" in version:
        version = version.split(":", 1)[1]
    if "-" in version:
        version = version.rsplit("-", 1)[0]
    return version


def family_for_version(version: str) -> str:
    upstream = upstream_version(version)
    try:
        return SUPPORTED_VERSIONS[upstream]
    except KeyError:
        raise AdapterError(
            f"input-remapper {upstream!r} is not a verified version; "
            f"verified: {', '.join(sorted(SUPPORTED_VERSIONS))}"
        ) from None


def detect_family(dpkg_status_text: str) -> tuple[str, str]:
    """Return ``(family, upstream_version)`` of the installed library package."""

    versions = parse_dpkg_status(dpkg_status_text, [LIBRARY_PACKAGE, CONTROL_PACKAGE])
    library = versions.get(LIBRARY_PACKAGE)
    if library is None:
        raise AdapterError(f"{LIBRARY_PACKAGE} is not installed")
    control = versions.get(CONTROL_PACKAGE)
    if control is None:
        raise AdapterError(f"{CONTROL_PACKAGE} is not installed")
    if upstream_version(control) != upstream_version(library):
        raise AdapterError(
            f"{CONTROL_PACKAGE} {control} does not match {LIBRARY_PACKAGE} {library}"
        )
    return family_for_version(library), upstream_version(library)


def require_family(family: str) -> None:
    if family not in FAMILIES:
        raise AdapterError(f"unknown input-remapper family {family!r}")


# ---------------------------------------------------------------------------
# Intent


@dataclasses.dataclass(frozen=True)
class Chord:
    id: str
    combos: tuple[tuple[int, ...], ...]  # EV_KEY codes, trigger key last
    emit_hold: tuple[str, ...]
    emit_press: str


@dataclasses.dataclass(frozen=True)
class Intent:
    owned_preset_name: str
    target_uinput: str
    chords: tuple[Chord, ...]

    @property
    def combos(self) -> tuple[tuple[int, ...], ...]:
        return tuple(combo for chord in self.chords for combo in chord.combos)

    @property
    def identities(self) -> frozenset:
        return frozenset(
            combo_identity((EV_KEY, code) for code in combo) for combo in self.combos
        )


def _expand(
    alternatives: list[list[str]], press: str, codes: dict[str, int]
) -> list[tuple[int, ...]]:
    combos: list[tuple[int, ...]] = [()]
    for options in alternatives:
        combos = [combo + (codes[name],) for combo in combos for name in options]
    return [combo + (codes[press],) for combo in combos]


def parse_intent(document: dict) -> Intent:
    if document.get("schema") != 1:
        raise AdapterError("unsupported input-remapper intent schema")
    codes = {str(k): int(v) for k, v in document["codes"].items()}
    chords = []
    for raw in document["chords"]:
        emit = raw["emit"]
        names = [n for group in raw["hold"] for n in group] + [raw["press"]]
        names += list(emit["hold"]) + [emit["press"]]
        missing = [n for n in names if n not in codes]
        if missing:
            raise AdapterError(f"intent chord {raw['id']!r} uses undeclared keys {missing}")
        chords.append(
            Chord(
                id=str(raw["id"]),
                combos=tuple(_expand(raw["hold"], raw["press"], codes)),
                emit_hold=tuple(emit["hold"]),
                emit_press=str(emit["press"]),
            )
        )
    owned = str(document["owned_preset_name"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]*", owned):
        raise AdapterError(f"owned preset name {owned!r} is not a safe file name")
    intent = Intent(owned, str(document.get("target_uinput", "keyboard")), tuple(chords))
    if len(intent.identities) != len(intent.combos):
        raise AdapterError("intent declares the same combination twice")
    return intent


def load_intent(path: Path) -> Intent:
    return parse_intent(json.loads(path.read_text(encoding="utf-8")))


def owned_preset_for(intent: Intent, group_key: str, group_name: str) -> str:
    """Installer-owned preset name for one autoload group key.

    Identical keyboards share a group *name* (and so one presets directory)
    but get distinct keys ("name", "name 2"). Each key gets its own owned
    preset so one device's prior entries never seed another's.
    """

    base = intent.owned_preset_name
    if group_key == group_name:
        return base
    suffix = group_key[len(group_name):].strip() if group_key.startswith(group_name) else ""
    if not re.fullmatch(r"[0-9]{1,4}", suffix):
        suffix = hashlib.sha256(group_key.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{suffix}"


# ---------------------------------------------------------------------------
# Combination identity


def combo_identity(inputs: Iterable[tuple]) -> tuple:
    """Order-insensitive identity: every key but the last is unordered.

    Elements are ``(type, code)`` for keys and ``(type, code, value)`` for
    other event types, where the value/threshold changes the meaning.
    """

    items = tuple(inputs)
    if not items:
        raise AdapterError("empty input combination")
    return (frozenset(items[:-1]), items[-1])


def _v1_split_key(key: str) -> tuple[tuple[int, ...], ...]:
    parts = []
    for chunk in key.split("+"):
        chunk = chunk.strip()
        if not chunk:
            continue
        fields = chunk.split(",")
        if len(fields) != 3:
            raise AdapterError(f"invalid 1.4 mapping key {key!r}")
        try:
            parts.append(tuple(int(x) for x in fields))
        except ValueError:
            raise AdapterError(f"invalid 1.4 mapping key {key!r}") from None
    return tuple(parts)


def v1_key_identity(key: str) -> tuple:
    if not isinstance(key, str):
        raise AdapterError(f"invalid 1.4 mapping key {key!r}")
    items = []
    for ev_type, code, value in _v1_split_key(key):
        items.append((ev_type, code) if ev_type == EV_KEY else (ev_type, code, value))
    return combo_identity(items)


def v1_format_key(combo: Iterable[int]) -> str:
    return "+".join(f"{EV_KEY},{code},1" for code in combo)


def v2_entry_identity(entry: Any) -> Optional[tuple]:
    if not isinstance(entry, dict):
        return None
    combination = entry.get("input_combination")
    if not isinstance(combination, list) or not combination:
        return None
    items = []
    for config in combination:
        if not isinstance(config, dict) or "type" not in config or "code" not in config:
            return None
        try:
            ev_type, code = int(config["type"]), int(config["code"])
        except (TypeError, ValueError):
            raise AdapterError(
                f"invalid 2.0 input configuration {config!r}"
            ) from None
        if ev_type == EV_KEY:
            items.append((ev_type, code))
        else:
            items.append((ev_type, code, config.get("analog_threshold")))
    return combo_identity(items)


# ---------------------------------------------------------------------------
# Managed entries


def v2_output_symbol(chord: Chord) -> str:
    return f"hold_keys({','.join(chord.emit_hold + (chord.emit_press,))})"


def _v2_managed_entry(chord: Chord, combo: tuple[int, ...], intent: Intent,
                      hashes: dict[int, str]) -> dict:
    combination = []
    for code in combo:
        config: dict[str, Any] = {"type": EV_KEY, "code": code}
        if code in hashes:
            config["origin_hash"] = hashes[code]
        combination.append(config)
    return {
        "input_combination": combination,
        "target_uinput": intent.target_uinput,
        "output_symbol": v2_output_symbol(chord),
        "name": f"{MANAGED_NAME_PREFIX} {chord.id}",
        "mapping_type": "key_macro",
        "release_combination_keys": True,
    }


def _v1_managed_value(chord: Chord, intent: Intent) -> list[str]:
    # 1.4 forwards the held Ctrl itself, so only the trigger key is injected.
    return [chord.emit_press, intent.target_uinput]


def _v2_hash_hints(replaced: list, identity: tuple) -> dict[int, str]:
    """Reuse the target-local origin hashes of the entry being replaced.

    Hashes are never borrowed from unrelated entries; without one, 2.0 picks
    the group's keyboard device itself (``Injector._update_preset``).
    """

    for entry in replaced:
        if v2_entry_identity(entry) != identity:
            continue
        hints = {}
        for config in entry["input_combination"]:
            if config.get("origin_hash") and int(config["type"]) == EV_KEY:
                hints[int(config["code"])] = str(config["origin_hash"])
        if hints:
            return hints
    return {}


@dataclasses.dataclass(frozen=True)
class BuildResult:
    document: Any
    replaced: list  # prior entries that bound a managed combination
    managed: list  # entries the installer wrote


def build_owned_preset(family: str, prior: Any, intent: Intent) -> BuildResult:
    """Seed the installer-owned preset from the prior effective preset.

    Every prior entry is kept verbatim and in order unless it binds one of the
    managed combinations (in any modifier order); those are dropped and the
    declared bindings are appended. Building from the result again yields the
    same document.
    """

    require_family(family)
    managed_ids = intent.identities
    if family == FAMILY_V2:
        if prior is None:
            prior = []
        if not isinstance(prior, list):
            raise AdapterError("2.0 preset must be a JSON list")
        kept, replaced = [], []
        for entry in prior:
            (replaced if v2_entry_identity(entry) in managed_ids else kept).append(entry)
        managed = []
        for chord in intent.chords:
            for combo in chord.combos:
                identity = combo_identity((EV_KEY, c) for c in combo)
                hints = _v2_hash_hints(replaced, identity)
                managed.append(_v2_managed_entry(chord, combo, intent, hints))
        document = copy.deepcopy(kept) + managed
        return BuildResult(document, copy.deepcopy(replaced), copy.deepcopy(managed))

    if prior is None:
        prior = {}
    if not isinstance(prior, dict):
        raise AdapterError("1.4 preset must be a JSON object")
    mapping = prior.get("mapping", {})
    if not isinstance(mapping, dict):
        raise AdapterError("1.4 preset 'mapping' must be an object")
    document = {k: copy.deepcopy(v) for k, v in prior.items() if k != "mapping"}
    new_mapping: dict[str, Any] = {}
    replaced = []
    for key, value in mapping.items():
        if v1_key_identity(key) in managed_ids:
            replaced.append([key, copy.deepcopy(value)])
        else:
            new_mapping[key] = copy.deepcopy(value)
    managed = []
    for chord in intent.chords:
        for combo in chord.combos:
            key = v1_format_key(combo)
            value = _v1_managed_value(chord, intent)
            new_mapping[key] = value
            managed.append([key, list(value)])
    document["mapping"] = new_mapping
    return BuildResult(document, replaced, managed)


# ---------------------------------------------------------------------------
# Autoload selection


def autoload_selection(config: Optional[dict], group_key: str) -> Optional[str]:
    if not config:
        return None
    autoload = config.get("autoload")
    if not isinstance(autoload, dict):
        return None
    value = autoload.get(group_key)
    return value if isinstance(value, str) else None


def set_autoload(config: Optional[dict], group_key: str, preset: Optional[str],
                 package_version: str) -> dict:
    """Return a copy of ``config.json`` with only ``group_key`` changed.

    A new file records the package version: without it input-remapper would
    treat the directory as a pre-0.4 layout and rerun every migration.
    """

    if config is None:
        document: dict[str, Any] = {"version": package_version, "autoload": {}}
    else:
        if not isinstance(config, dict):
            raise AdapterError("config.json must be a JSON object")
        document = copy.deepcopy(config)
    autoload = document.get("autoload")
    if autoload is None:
        autoload = document["autoload"] = {}
    if not isinstance(autoload, dict):
        raise AdapterError("config.json 'autoload' must be an object")
    if preset is None:
        autoload.pop(group_key, None)
    else:
        autoload[group_key] = preset
    return document


# ---------------------------------------------------------------------------
# Reconciliation and restore


def _normalize(family: str, entry: Any) -> Any:
    """Comparable form of one managed-combination entry.

    Ignores what input-remapper itself may add or drop on a resave (origin
    hashes recorded by the GUI, the display name, defaulted fields).
    """

    if family == FAMILY_V1:
        key, value = entry
        return (v1_key_identity(key), tuple(value) if isinstance(value, list) else value)
    return (
        v2_entry_identity(entry),
        entry.get("output_symbol"),
        entry.get("target_uinput"),
        entry.get("output_type"),
        entry.get("output_code"),
        entry.get("release_combination_keys", True),
    )


def _entries(family: str, document: Any) -> list:
    if document is None:
        return []
    if family == FAMILY_V1:
        return [[k, v] for k, v in document.get("mapping", {}).items()]
    return list(document)


def _identity(family: str, entry: Any) -> Optional[tuple]:
    return v1_key_identity(entry[0]) if family == FAMILY_V1 else v2_entry_identity(entry)


@dataclasses.dataclass(frozen=True)
class ApplyPlan:
    family: str
    group_key: str
    config: dict
    owned_preset: Any
    record: dict
    seed_selection: Optional[str]
    changed: bool


def new_restore_record(family: str, group_key: str, group_name: str, intent: Intent,
                       prior_selection: Optional[str], prior_known: bool,
                       build: BuildResult, owned_preset: Optional[str] = None) -> dict:
    return {
        "schema": RESTORE_SCHEMA,
        "family": family,
        "group_key": group_key,
        "group_name": group_name,
        "owned_preset": owned_preset or owned_preset_for(intent, group_key, group_name),
        "prior_selection": prior_selection,
        "prior_selection_known": prior_known,
        "prior_managed_entries": build.replaced,
        "installed_managed_entries": build.managed,
        "history": [],
    }


_MALFORMED = (TypeError, ValueError, KeyError, AttributeError, IndexError)


def plan_apply(family: str, package_version: str, config: Optional[dict],
               load_preset: Callable[[str], Any], group_key: str, group_name: str,
               intent: Intent, existing_record: Optional[dict] = None) -> ApplyPlan:
    """See :func:`_plan_apply`; malformed stored data raises AdapterError."""

    try:
        return _plan_apply(family, package_version, config, load_preset, group_key,
                           group_name, intent, existing_record)
    except AdapterError:
        raise
    except _MALFORMED as exc:
        raise AdapterError(f"malformed input-remapper data: {type(exc).__name__}: {exc}") from None


def _plan_apply(family: str, package_version: str, config: Optional[dict],
                load_preset: Callable[[str], Any], group_key: str, group_name: str,
                intent: Intent, existing_record: Optional[dict] = None) -> ApplyPlan:
    """Plan the owned preset, the autoload change and the restore record.

    ``load_preset(name)`` returns the parsed preset of this group or ``None``.
    On a rerun (the group already selects the owned preset) the owned preset is
    itself the prior effective preset, so target-local edits to unrelated
    entries survive and the original restore record is kept. If the selection
    moved elsewhere since, that preset becomes the new prior and the old record
    is kept in ``history``.
    """

    require_family(family)
    supported, reason = INTENT_SUPPORT[family]
    if not supported:
        raise UnsupportedIntent(reason)
    if SUPPORTED_VERSIONS.get(upstream_version(package_version)) != family:
        raise AdapterError(f"package version {package_version!r} is not family {family}")

    if config is not None and not isinstance(config, dict):
        raise AdapterError("config.json must be a JSON object")
    current = autoload_selection(config, group_key)
    owned = owned_preset_for(intent, group_key, group_name)
    seed = load_preset(current) if current else None
    build = build_owned_preset(family, seed, intent)

    usable_record = (
        existing_record is not None
        and existing_record.get("schema") == RESTORE_SCHEMA
        and existing_record.get("family") == family
        and existing_record.get("group_key") == group_key
    )
    if current == owned:
        if usable_record:
            record = copy.deepcopy(existing_record)
            record["installed_managed_entries"] = build.managed
        else:
            # Lineage was lost: never invent a prior selection.
            record = new_restore_record(family, group_key, group_name, intent, None, False,
                                        dataclasses.replace(build, replaced=[]), owned)
    else:
        record = new_restore_record(
            family, group_key, group_name, intent, current, True, build, owned
        )
        if usable_record:
            previous = copy.deepcopy(existing_record)
            record["history"] = previous.pop("history", []) + [previous]

    new_config = set_autoload(config, group_key, owned, package_version)
    changed = new_config != config or (current != owned) or build.document != seed
    return ApplyPlan(
        family, group_key, new_config, build.document, record, current, changed
    )


@dataclasses.dataclass(frozen=True)
class RestorePlan:
    config: Optional[dict]
    owned_preset: Any
    owned_redundant: bool
    notes: tuple[str, ...]


def plan_restore(family: str, config: Optional[dict], owned_preset: Any, record: dict,
                 prior_preset: Any = None) -> RestorePlan:
    """See :func:`_plan_restore`; malformed stored data raises AdapterError."""

    try:
        return _plan_restore(family, config, owned_preset, record, prior_preset)
    except AdapterError:
        raise
    except _MALFORMED as exc:
        raise AdapterError(f"malformed input-remapper data: {type(exc).__name__}: {exc}") from None


def _plan_restore(family: str, config: Optional[dict], owned_preset: Any, record: dict,
                  prior_preset: Any = None) -> RestorePlan:
    """Return the prior selection and managed entries without erasing edits.

    * The autoload entry goes back to the recorded prior selection only while
      it still selects the owned preset; a later user choice is left alone.
    * In the owned preset, managed entries still equal to what was installed
      are replaced by the recorded prior entries; managed entries the user
      edited afterwards and every unrelated entry are kept. The owned preset
      file is kept unless it has become identical to the prior preset.
    * Original preset files are never modified.
    """

    require_family(family)
    notes: list[str] = []
    group_key = record["group_key"]
    owned = record["owned_preset"]

    new_config = copy.deepcopy(config)
    if autoload_selection(config, group_key) == owned:
        if record.get("prior_selection_known", True):
            new_config = set_autoload(config, group_key, record.get("prior_selection"), "")
        else:
            notes.append("prior selection unknown; autoload left on the owned preset")
    else:
        notes.append("autoload selection changed after install; left unchanged")

    installed = {_normalize(family, e) for e in record.get("installed_managed_entries", [])}
    kept: list = []
    present_ids = set()
    for entry in _entries(family, owned_preset):
        identity = _identity(family, entry)
        if identity is not None and _normalize(family, entry) in installed:
            continue
        if identity is not None and identity in _record_ids(family, record):
            notes.append("managed combination edited after install; kept")
        kept.append(entry)
        present_ids.add(identity)
    for entry in record.get("prior_managed_entries", []):
        if _identity(family, entry) in present_ids:
            continue
        kept.append(copy.deepcopy(entry))

    if owned_preset is None:
        new_owned = None
    elif family == FAMILY_V1:
        new_owned = {k: copy.deepcopy(v) for k, v in owned_preset.items() if k != "mapping"}
        new_owned["mapping"] = {k: v for k, v in kept}
    else:
        new_owned = kept

    redundant = new_owned is not None and (
        (prior_preset is not None and _same_preset(family, new_owned, prior_preset))
        or (record.get("prior_selection") is None and not _entries(family, new_owned))
    )
    return RestorePlan(new_config, new_owned, redundant, tuple(notes))


def _record_ids(family: str, record: dict) -> set:
    ids = set()
    for key in ("installed_managed_entries", "prior_managed_entries"):
        for entry in record.get(key, []):
            ids.add(_identity(family, entry))
    return ids


def _same_preset(family: str, left: Any, right: Any) -> bool:
    if family == FAMILY_V1:
        return (
            {k: v for k, v in left.items() if k != "mapping"}
            == {k: v for k, v in right.items() if k != "mapping"}
            and sorted(json.dumps(e, sort_keys=True) for e in _entries(family, left))
            == sorted(json.dumps(e, sort_keys=True) for e in _entries(family, right))
        )
    return sorted(json.dumps(e, sort_keys=True) for e in left) == sorted(
        json.dumps(e, sort_keys=True) for e in right
    )


# ---------------------------------------------------------------------------
# Paths


def config_dir(family: str, home: Path) -> Path:
    require_family(family)
    return home / ".config" / CONFIG_DIRNAMES[family]


def preset_dirname(family: str, group_name: str) -> str:
    require_family(family)
    if family == FAMILY_V2:
        for character in '/\\?%*:|"<>':
            group_name = group_name.replace(character, "_")
        return group_name
    if "/" in group_name or group_name in ("", ".", ".."):
        raise AdapterError(f"1.4 cannot store presets for device name {group_name!r}")
    return group_name


def preset_path(family: str, home: Path, group_name: str, preset: str) -> Path:
    directory = config_dir(family, home) / "presets" / preset_dirname(family, group_name)
    return directory / f"{preset}.json"


def v2_migration_pending(home: Path) -> bool:
    """2.0 copies a 1.4 config only while ``input-remapper-2`` does not exist.

    Creating that directory first would silently skip the user's migration.
    """

    return (
        not config_dir(FAMILY_V2, home).exists()
        and (config_dir(FAMILY_V1, home) / "config.json").exists()
    )


# ---------------------------------------------------------------------------
# Device discovery (mirrors inputremapper.groups, from /proc/bus/input/devices)

_KEY_A, _KEY_CAMERA, _BTN_LEFT, _BTN_STYLUS = 30, 212, 272, 331
_REL_X, _REL_Y, _REL_WHEEL = 0, 1, 8
_ABS_X, _ABS_Y, _ABS_MT_POSITION_X = 0, 1, 53
_GAMEPAD_BUTTONS = {294, 304, 289, 291, 545}  # BASE, A/GAMEPAD, THUMB, TOP, DPAD_DOWN
_EV_REL, _EV_ABS = 2, 3
_DENYLIST = (r".*Yubico.*YubiKey.*", r"Eee PC WMI hotkeys")


@dataclasses.dataclass(frozen=True)
class InputDevice:
    name: str
    phys: str
    sysfs: str
    bustype: int
    vendor: int
    product: int
    event: Optional[str]
    ev: frozenset
    key: frozenset
    rel: frozenset
    abs: frozenset

    @property
    def is_virtual(self) -> bool:
        # uinput devices (input-remapper's own outputs, other injectors) live
        # under /devices/virtual/input. BLE keyboards arrive through uhid
        # under /devices/virtual/misc/uhid and are real hardware.
        return self.sysfs.startswith("/devices/virtual/input/")


def _bitmap(text: str, word_bits: int) -> frozenset:
    bits = set()
    for index, word in enumerate(reversed(text.split())):
        value = int(word, 16)
        offset = index * word_bits
        while value:
            low = value & -value
            bits.add(offset + low.bit_length() - 1)
            value ^= low
    return frozenset(bits)


def parse_proc_input_devices(text: str, word_bits: int = 64) -> list[InputDevice]:
    """Parse ``/proc/bus/input/devices`` (64-bit kernels: amd64 and arm64)."""

    devices = []
    for block in re.split(r"\n\s*\n", text.strip()):
        info: dict[str, str] = {}
        bitmaps: dict[str, str] = {}
        for line in block.splitlines():
            kind, _, rest = line.partition(": ")
            if kind == "I":
                for field in rest.split():
                    k, _, v = field.partition("=")
                    info[k] = v
            elif kind in ("N", "P", "S", "H"):
                k, _, v = rest.partition("=")
                info[k] = v.strip().strip('"') if kind == "N" else v.strip()
            elif kind == "B":
                k, _, v = rest.partition("=")
                bitmaps[k] = v
        if "Name" not in info:
            continue
        handlers = info.get("Handlers", "").split()
        event = next((h for h in handlers if re.fullmatch(r"event\d+", h)), None)
        devices.append(
            InputDevice(
                name=info["Name"],
                phys=info.get("Phys", ""),
                sysfs=info.get("Sysfs", ""),
                bustype=int(info.get("Bus", "0"), 16),
                vendor=int(info.get("Vendor", "0"), 16),
                product=int(info.get("Product", "0"), 16),
                event=event,
                ev=_bitmap(bitmaps.get("EV", "0"), word_bits),
                key=_bitmap(bitmaps.get("KEY", "0"), word_bits),
                rel=_bitmap(bitmaps.get("REL", "0"), word_bits),
                abs=_bitmap(bitmaps.get("ABS", "0"), word_bits),
            )
        )
    return devices


def classify(device: InputDevice) -> str:
    if _BTN_STYLUS in device.key:
        return "graphics-tablet"
    if _ABS_MT_POSITION_X in device.abs:
        return "touchpad"
    if _GAMEPAD_BUTTONS & device.key and {_ABS_X, _ABS_Y} <= device.abs:
        return "gamepad"
    if {_REL_X, _REL_Y, _REL_WHEEL} <= device.rel and _BTN_LEFT in device.key:
        return "mouse"
    if device.key == {_KEY_CAMERA}:
        return "camera"
    if _KEY_A in device.key:
        return "keyboard"
    return "unknown"


@dataclasses.dataclass(frozen=True)
class DeviceGroup:
    key: str
    name: str
    names: tuple[str, ...]
    types: tuple[str, ...]
    paths: tuple[str, ...]
    physical: Optional[bool]  # None when the source cannot tell
    keyboard_codes: Optional[frozenset]  # EV_KEY codes of keyboard members
    key_ambiguous: bool = False  # identical names: " 2" order is not stable


def group_devices(devices: Iterable[InputDevice]) -> list[DeviceGroup]:
    """Group devices the way input-remapper does (``_FindGroups.run``)."""

    grouped: dict[str, list[tuple[InputDevice, str]]] = {}
    for device in devices:
        if device.event is None or device.name == "Power Button":
            continue
        device_type = classify(device)
        if device_type == "camera":
            continue
        if not device.key and device_type != "gamepad":
            continue
        if any(re.match(p, device.name, re.IGNORECASE) for p in _DENYLIST):
            continue
        unique = (
            f"{device.bustype}_{device.vendor}_{device.product}_"
            f"{device.phys.split('/')[0] or '-'}"
        )
        grouped.setdefault(unique, []).append((device, device_type))

    shortest_names = [sorted((d.name for d, _ in m), key=len)[0] for m in grouped.values()]
    result = []
    used: set[str] = set()
    for members, shortest in zip(grouped.values(), shortest_names):
        key, index = shortest, 2
        while key in used:
            key, index = f"{shortest} {index}", index + 1
        used.add(key)
        keyboard_codes: set[int] = set()
        for device, device_type in members:
            if device_type == "keyboard":
                keyboard_codes |= device.key
        result.append(
            DeviceGroup(
                key=key,
                name=shortest,
                names=tuple(d.name for d, _ in members),
                types=tuple(sorted({t for _, t in members if t != "unknown"})),
                paths=tuple(f"/dev/input/{d.event}" for d, _ in members),
                physical=not all(d.is_virtual for d, _ in members),
                keyboard_codes=frozenset(keyboard_codes),
                key_ambiguous=shortest_names.count(shortest) > 1,
            )
        )
    return result


def groups_from_remapper_dump(text: str) -> list[DeviceGroup]:
    """Parse input-remapper's own ``groups.dumps()`` serialization."""

    result = []
    for item in json.loads(text):
        data = json.loads(item) if isinstance(item, str) else item
        names = tuple(data["names"])
        result.append(
            DeviceGroup(
                key=data["key"],
                name=sorted(names, key=len)[0],
                names=names,
                types=tuple(data["types"]),
                paths=tuple(data["paths"]),
                physical=None,
                keyboard_codes=None,
            )
        )
    return result


def cross_check_dump_groups(dump_groups: Iterable[DeviceGroup],
                            proc_groups: Iterable[DeviceGroup]) -> list[DeviceGroup]:
    """Fill physical/keyboard facts of dump-derived groups from /proc.

    input-remapper's dump carries no sysfs path or key bitmap. A dump group is
    confirmed only by a /proc group with the same key and the same member
    names; anything else keeps ``physical=None`` and is rejected by
    :func:`eligible_keyboards`.
    """

    by_key = {g.key: g for g in proc_groups}
    result = []
    for group in dump_groups:
        match = by_key.get(group.key)
        if match is not None and sorted(match.names) == sorted(group.names):
            result.append(dataclasses.replace(
                group, physical=match.physical, keyboard_codes=match.keyboard_codes,
                key_ambiguous=match.key_ambiguous or group.key_ambiguous,
            ))
        else:
            result.append(dataclasses.replace(group, physical=None, keyboard_codes=None))
    return result


def eligible_keyboards(groups: Iterable[DeviceGroup], intent: Intent
                       ) -> tuple[list[DeviceGroup], list[tuple[str, str]]]:
    """Return ``(eligible, rejected)``; ``rejected`` holds ``(key, reason)``."""

    eligible: list[DeviceGroup] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for group in groups:
        if group.key in seen:
            rejected.append((group.key, "duplicate group"))
            continue
        seen.add(group.key)
        labels = (group.key, group.name) + group.names
        if any(label.startswith(VIRTUAL_NAME_PREFIX) for label in labels):
            rejected.append((group.key, "input-remapper virtual device"))
        elif group.physical is False:
            rejected.append((group.key, "virtual (non-physical) device"))
        elif group.physical is None:
            rejected.append((group.key, "not confirmed by /proc/bus/input/devices"))
        elif "keyboard" not in group.types:
            rejected.append((group.key, "not a keyboard"))
        elif group.keyboard_codes is not None and not any(
            set(combo) <= group.keyboard_codes for combo in intent.combos
        ):
            rejected.append((group.key, "keyboard lacks the chord keys"))
        else:
            eligible.append(group)
    return eligible, rejected


# ---------------------------------------------------------------------------
# Read-only I/O helpers and planned file operations (explicit roots)

DEFAULT_FILE_MODE = 0o644


@dataclasses.dataclass(frozen=True)
class FileOp:
    """One planned change: write ``content`` (``None`` deletes) with ``mode``."""

    path: Path
    content: Optional[bytes]
    mode: int


def dumps_json(document: Any) -> bytes:
    """Serialize the way input-remapper does (indent 4, trailing newline)."""

    return (json.dumps(document, indent=4) + "\n").encode("utf-8")


def check_safe_path(home: Path, path: Path) -> None:
    """Refuse ``path`` when it or an existing parent below ``home`` is a
    symlink or a non-directory; a write would otherwise land elsewhere."""

    home = Path(home)
    path = Path(path)
    try:
        relative = path.relative_to(home)
    except ValueError:
        raise AdapterError(f"{path} is outside {home}") from None
    current = home
    parts = relative.parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode):
            raise AdapterError(f"{current} is a symlink; refusing to follow it")
        last = index == len(parts) - 1
        if not last and not stat.S_ISDIR(st.st_mode):
            raise AdapterError(f"{current} is not a directory")
        if last and not stat.S_ISREG(st.st_mode):
            raise AdapterError(f"{current} is not a regular file")


def existing_mode(path: Path) -> Optional[int]:
    try:
        return stat.S_IMODE(os.lstat(path).st_mode)
    except FileNotFoundError:
        return None


def read_json(path: Path, home: Optional[Path] = None) -> Any:
    if home is not None:
        check_safe_path(home, path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise AdapterError(f"{path} is not UTF-8: {exc}") from None
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"{path} is not valid JSON: {exc}") from exc


def _write_op(home: Path, path: Path, document: Any) -> FileOp:
    check_safe_path(home, path)
    mode = existing_mode(path)
    return FileOp(path, dumps_json(document), DEFAULT_FILE_MODE if mode is None else mode)


def _delete_op(home: Path, path: Path) -> FileOp:
    check_safe_path(home, path)
    mode = existing_mode(path)
    return FileOp(path, None, DEFAULT_FILE_MODE if mode is None else mode)


@dataclasses.dataclass(frozen=True)
class GroupChange:
    plan: Any  # ApplyPlan or RestorePlan
    ops: tuple[FileOp, ...]


def apply_group(family: str, package_version: str, home: Path, group: DeviceGroup,
                intent: Intent, existing_record: Optional[dict] = None) -> GroupChange:
    """Read the group's current state under ``home`` and plan the writes.

    Nothing is written; the caller backs up every ``FileOp.path`` and then
    performs the operations in order.
    """

    if family == FAMILY_V2 and v2_migration_pending(home):
        raise MigrationPending(
            "input-remapper 2 has not migrated the 1.4 configuration yet; run "
            f"`{CONTROL_EXECUTABLE} --command hello` once in the desktop session"
        )
    root = config_dir(family, home)
    config = read_json(root / "config.json", home)
    plan = plan_apply(
        family,
        package_version,
        config,
        lambda name: read_json(preset_path(family, home, group.name, name), home),
        group.key,
        group.name,
        intent,
        existing_record,
    )
    ops: list[FileOp] = []
    if plan.changed:
        owned_path = preset_path(family, home, group.name, plan.record["owned_preset"])
        ops.append(_write_op(home, owned_path, plan.owned_preset))
        ops.append(_write_op(home, root / "config.json", plan.config))
    return GroupChange(plan, tuple(ops))


def restore_group(family: str, home: Path, record: dict) -> GroupChange:
    """Plan the restore of one group; nothing is written (see apply_group)."""

    if not isinstance(record, dict) or not all(
        isinstance(record.get(k), str) for k in ("group_key", "group_name", "owned_preset")
    ):
        raise AdapterError("malformed input-remapper restore record")
    root = config_dir(family, home)
    config = read_json(root / "config.json", home)
    owned_path = preset_path(family, home, record["group_name"], record["owned_preset"])
    prior = record.get("prior_selection")
    prior_doc = None
    if prior:
        prior_doc = read_json(preset_path(family, home, record["group_name"], prior), home)
    plan = plan_restore(family, config, read_json(owned_path, home), record, prior_doc)
    ops: list[FileOp] = []
    if plan.config is not None and plan.config != config:
        ops.append(_write_op(home, root / "config.json", plan.config))
    if plan.owned_preset is not None:
        still_selected = (
            autoload_selection(plan.config, record["group_key"]) == record["owned_preset"]
        )
        if plan.owned_redundant and not still_selected:
            ops.append(_delete_op(home, owned_path))
        else:
            ops.append(_write_op(home, owned_path, plan.owned_preset))
    return GroupChange(plan, tuple(ops))


# ---------------------------------------------------------------------------
# input-remapper-control (never called by tests)


def control_argv(command: str, device: Optional[str] = None, preset: Optional[str] = None,
                 executable: str = CONTROL_EXECUTABLE) -> list[str]:
    """Arguments for input-remapper-control; identical in 1.4.0 and 2.0.1.

    ``autoload`` without a device loads every selection, ``start`` replaces the
    group's running injection (``autoload`` skips a preset it already loaded).
    """

    if command not in ("autoload", "start", "stop", "stop-all", "hello"):
        raise AdapterError(f"unsupported control command {command!r}")
    argv = [executable, "--command", command]
    if command in ("start", "stop") and not device:
        raise AdapterError(f"{command} needs a device group key")
    if command == "start" and not preset:
        raise AdapterError("start needs a preset name")
    if device:
        argv += ["--device", device]
    if preset and command == "start":
        argv += ["--preset", preset]
    return argv


def list_devices_argv(executable: str = CONTROL_EXECUTABLE) -> list[str]:
    return [executable, "--list-devices"]


@dataclasses.dataclass(frozen=True)
class ControlResult:
    argv: tuple[str, ...]
    returncode: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool


def run_control(argv: list[str], timeout: float = 15.0) -> ControlResult:
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        return ControlResult(tuple(argv), None, _text(exc.stdout), _text(exc.stderr), True)
    except OSError as exc:
        return ControlResult(tuple(argv), None, "", str(exc), False)
    return ControlResult(
        tuple(argv), completed.returncode, completed.stdout, completed.stderr, False
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value)
