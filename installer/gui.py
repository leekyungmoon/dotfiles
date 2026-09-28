"""GNOME settings and input-remapper: apply now, or defer to the next login.

Desktop settings need a user D-Bus session inside a GNOME login and the
schemas of the installed desktop. :func:`apply_or_defer` applies whatever is
ready, records per-key and per-component state under
``target.state_root/gui/`` and reports ``PENDING_GUI`` with precise reasons
for everything else. An XDG autostart entry (:func:`autostart_desired_entry`)
runs ``install.py gui-apply --autostart`` at each GNOME login;
:func:`run_autostart` retries only readiness gaps (session bus, schemas,
remapper service, keyboards) on a fixed schedule inside a hard 65 second
budget and then exits. Nothing here is a daemon.

Every managed GSettings key is backed up before it is written, keeping the
difference between an explicit user value and an unset key (``dconf read``
prints nothing for an unset key). :func:`restore_gui` returns explicit keys
with ``gsettings set`` and unset keys with ``gsettings reset``.

State layout (dirs 0700, files 0600, atomic writes)::

    gui/state.json                           generation, components, keys
    gui/remapper-records.json                adapter restore records per group
    gui/backups/baseline/gsettings.json      first value ever seen per key
    gui/backups/<generation>/gsettings.json  values before that generation
    gui/backups/<generation>/remapper/       files before the adapter wrote them
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import importlib.util
import json
import os
import re
import secrets
import stat
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .platform import Platform, PlatformError, Target, detect_platform
from .runner import RunnerError
from .transaction import DesiredEntry

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "desktop" / "gnome-settings.json"
REMAPPER_DIR = REPO_ROOT / "desktop" / "input-remapper"
ADAPTERS_PATH = REMAPPER_DIR / "adapters.py"
INTENT_PATH = REMAPPER_DIR / "intent.json"
AUTOSTART_TEMPLATE = REPO_ROOT / "desktop" / "autostart" / "personal-dotfiles-gui-apply.desktop.in"
AUTOSTART_NAME = "personal-dotfiles-gui-apply.desktop"
AUTOSTART_ENTRY_ID = "gui-autostart"
AUTOSTART_PYTHON = "/usr/bin/python3"
PROC_INPUT_DEVICES = Path("/proc/bus/input/devices")

STATE_SCHEMA = 1
COMPONENT_GNOME = "gnome-settings"
COMPONENT_REMAPPER = "input-remapper"
COMPONENTS = (COMPONENT_GNOME, COMPONENT_REMAPPER)
AUTOSTART_ENV = "PERSONAL_DOTFILES_GUI_AUTOSTART"
COMPONENTS_ENV = "PERSONAL_DOTFILES_GUI_COMPONENTS"

# Autostart schedule: attempts at these elapsed seconds, all work (including
# subprocess timeouts) inside DEADLINE seconds.
RETRY_OFFSETS = (0, 2, 5, 10, 20, 40, 60)
DEADLINE = 65.0
MIN_CALL_TIMEOUT = 0.5
TIMEOUTS = {"probe": 5.0, "gsettings": 10.0, "dconf": 5.0, "dpkg": 10.0, "control": 15.0}

# Key / component statuses.
APPLIED = "applied"
PENDING = "pending"
FAILED = "failed"
NOT_APPLICABLE = "not-applicable"
UNSUPPORTED = "unsupported"  # permanent: reported as pending, never retried
SKIPPED_COMPONENT = "skipped"
RESTORED = "restored"
FILES_WRITTEN = "files-written"

REASON_V1_REMAPPER = "input-remapper-1.4-cannot-express-intent"

# Phase statuses (installer/phases.py contract).
PASS = "PASS"
FAIL = "FAIL"
SKIPPED = "SKIPPED"
PENDING_GUI = "PENDING_GUI"


class GuiError(Exception):
    """GUI state or inputs cannot be used safely."""


class _DeadlineReached(Exception):
    pass


# --------------------------------------------------------------------------
# Small helpers


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_dir(path: Path) -> None:
    missing = []
    probe = path
    while not os.path.lexists(probe) and probe.parent != probe:
        missing.append(probe)
        probe = probe.parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        os.chmod(directory, 0o700)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise GuiError(f"{path} is not a real directory")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(tmp, flags, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _write_json(path: Path, payload) -> None:
    _ensure_dir(path.parent)
    _atomic_write(path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _read_json(path: Path, default):
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GuiError(f"{path} is corrupted: {exc}") from None


def _tail(data, limit: int = 300) -> str:
    if not data:
        return ""
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    return " ".join(str(data).split())[-limit:]


_ADAPTERS = None


def load_adapters():
    """Import desktop/input-remapper/adapters.py (hyphenated dir) by path."""

    global _ADAPTERS
    if _ADAPTERS is None:
        name = "personal_dotfiles_input_remapper_adapters"
        spec = importlib.util.spec_from_file_location(name, ADAPTERS_PATH)
        if spec is None or spec.loader is None:
            raise GuiError(f"cannot load {ADAPTERS_PATH}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        _ADAPTERS = module
    return _ADAPTERS


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("settings"), list):
        raise GuiError(f"{path} is not a GNOME settings manifest")
    return data


def generation_id(release: str) -> str:
    """Content-derived id of the desired GUI configuration for a release."""

    digest = hashlib.sha256()
    digest.update(f"gui-state-{STATE_SCHEMA}\0{release}\0".encode())
    for path in (MANIFEST_PATH, INTENT_PATH, ADAPTERS_PATH):
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "g" + digest.hexdigest()[:16]


def gsettings_target(entry: dict) -> str:
    path = entry.get("path")
    return f"{entry['schema']}:{path}" if path else entry["schema"]


# --------------------------------------------------------------------------
# Execution context


@dataclasses.dataclass
class _Ctx:
    target: Target
    runner: object
    env: dict
    platform: Platform
    components: tuple
    now: Callable[[], float]
    deadline_at: float | None
    proc_devices: Path

    def timeout(self, kind: str) -> float:
        wanted = TIMEOUTS[kind]
        if self.deadline_at is None:
            return wanted
        remaining = self.deadline_at - self.now()
        if remaining < MIN_CALL_TIMEOUT:
            raise _DeadlineReached()
        return min(wanted, remaining)

    def run(self, argv: list, kind: str, *, read_only: bool):
        return self.runner.run(argv, timeout=self.timeout(kind), check=False,
                               env=self.env, read_only=read_only)

    @property
    def gui_root(self) -> Path:
        return self.target.state_root / "gui"


def _child_env(env: Mapping[str, str]) -> dict:
    child = {k: v for k, v in env.items() if k not in (AUTOSTART_ENV, COMPONENTS_ENV)}
    child["LC_ALL"] = "C"
    if not child.get("DBUS_SESSION_BUS_ADDRESS"):
        runtime = child.get("XDG_RUNTIME_DIR", "")
        if runtime:
            bus = Path(runtime) / "bus"
            try:
                if stat.S_ISSOCK(os.stat(bus).st_mode):
                    child["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
            except OSError:
                pass
    return child


def _select_components(env: Mapping[str, str], components: Iterable[str] | None) -> tuple:
    if components is None:
        raw = env.get(COMPONENTS_ENV, "").strip()
        components = [c.strip() for c in raw.split(",") if c.strip()] if raw else COMPONENTS
    components = list(components)
    unknown = set(components) - set(COMPONENTS)
    if unknown:
        raise GuiError(f"unknown GUI components {sorted(unknown)}")
    return tuple(c for c in COMPONENTS if c in components)


def _make_ctx(target, runner, env, *, platform, components, now, deadline_at,
              proc_devices) -> _Ctx:
    if platform is None:
        platform = detect_platform()
    return _Ctx(
        target=target,
        runner=runner,
        env=_child_env(env),
        platform=platform,
        components=_select_components(env, components),
        now=now,
        deadline_at=deadline_at,
        proc_devices=Path(proc_devices),
    )


# --------------------------------------------------------------------------
# State


def _state_path(ctx: _Ctx) -> Path:
    return ctx.gui_root / "state.json"


def _fresh_state(generation: str, release: str) -> dict:
    return {
        "schema": STATE_SCHEMA,
        "generation": generation,
        "release": release,
        "components": {
            name: {"status": PENDING, "reasons": ["not-attempted"], "retryable": True}
            for name in COMPONENTS
        },
        "keys": {},
        "remapper_groups": {},
        "attempts": 0,
        "created": _utc_now(),
    }


def _load_state(ctx: _Ctx, generation: str) -> tuple[dict, bool]:
    """Return ``(state, same_generation)``; a new generation starts fresh."""

    data = _read_json(_state_path(ctx), None)
    if data is None:
        return _fresh_state(generation, ctx.platform.release), False
    if (
        not isinstance(data, dict)
        or data.get("schema") != STATE_SCHEMA
        or not isinstance(data.get("components", {}), dict)
        or not isinstance(data.get("keys", {}), dict)
    ):
        raise GuiError(f"{_state_path(ctx)} has an unknown format")
    if data.get("generation") != generation:
        fresh = _fresh_state(generation, ctx.platform.release)
        fresh["previous_generation"] = data.get("generation")
        return fresh, False
    for name in COMPONENTS:
        data.setdefault("components", {}).setdefault(
            name, {"status": PENDING, "reasons": ["not-attempted"], "retryable": True})
    data.setdefault("keys", {})
    data.setdefault("remapper_groups", {})
    return data, True


def _save_state(ctx: _Ctx, state: dict) -> None:
    state["updated"] = _utc_now()
    _write_json(_state_path(ctx), state)


def _set_component(state: dict, name: str, status: str, reasons: list,
                   retryable: bool = False, **details) -> None:
    record = {"status": status, "reasons": list(dict.fromkeys(reasons)), "retryable": retryable}
    record.update(details)
    state["components"][name] = record


def _retryable_pending(state: dict, components: Iterable[str]) -> bool:
    return any(
        state["components"][c]["status"] == PENDING and state["components"][c].get("retryable")
        for c in components
    )


def _needs_attempt(state: dict, components: Iterable[str], retry_failed: bool) -> bool:
    for name in components:
        status = state["components"][name]["status"]
        if status == PENDING or (status == FAILED and retry_failed):
            return True
    return False


# --------------------------------------------------------------------------
# Readiness


def _session_gaps(ctx: _Ctx) -> list:
    """Missing session readiness as ``(reason, retryable_within_this_run)``."""

    desktops = [p.strip().upper() for p in ctx.env.get("XDG_CURRENT_DESKTOP", "").split(":")]
    if "GNOME" not in desktops:
        return [("no-gnome-session", False)]
    missing = [name for name in ("gdbus", "gsettings", "dconf") if ctx.runner.which(name) is None]
    if missing:
        return [(f"{name}-missing", False) for name in missing]
    if not ctx.env.get("DBUS_SESSION_BUS_ADDRESS"):
        return [("session-bus-unreachable", True)]
    argv = ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
            "--object-path", "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.GetId"]
    try:
        done = ctx.run(argv, "probe", read_only=True)
    except RunnerError:
        return [("session-bus-unreachable", True)]
    if done.returncode != 0:
        return [("session-bus-unreachable", True)]
    return []


def _list_schemas(ctx: _Ctx):
    """``({fixed schema: path or None}, {relocatable})`` or None on failure."""

    try:
        fixed = ctx.run(["gsettings", "list-schemas", "--print-paths"], "gsettings",
                        read_only=True)
        reloc = ctx.run(["gsettings", "list-relocatable-schemas"], "gsettings", read_only=True)
    except RunnerError:
        return None
    if fixed.returncode != 0 or reloc.returncode != 0:
        return None
    paths: dict = {}
    for line in fixed.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        if parts:
            paths[parts[0]] = parts[1] if len(parts) > 1 else None
    relocatable = set(reloc.stdout.decode("utf-8", errors="replace").split())
    return paths, relocatable


# --------------------------------------------------------------------------
# GNOME settings


def _backup_file(ctx: _Ctx, name: str) -> Path:
    return ctx.gui_root / "backups" / name / "gsettings.json"


def _load_key_backup(path: Path) -> dict:
    data = _read_json(path, {"schema": STATE_SCHEMA, "keys": {}})
    if not isinstance(data, dict) or not isinstance(data.get("keys"), dict):
        raise GuiError(f"{path} has an unknown format")
    return data


def _dconf_read(ctx: _Ctx, dconf_key: str):
    """Current explicit value text, ``""`` when unset, None when unreadable."""

    try:
        done = ctx.run(["dconf", "read", dconf_key], "dconf", read_only=True)
    except RunnerError:
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", errors="replace").strip()


def _classify_set_failure(stderr: str) -> tuple:
    text = stderr.lower()
    if "not writable" in text or "permission" in text or "denied" in text:
        return FAILED, "not-writable"
    if "no such key" in text:
        return FAILED, "key-missing"
    if "no such schema" in text:
        return PENDING, "schema-missing"
    return FAILED, "invalid-value"


def _pending_key(value: str, reason: str) -> dict:
    return {"status": PENDING, "value": value, "reason": reason, "retryable": True}


def _apply_gnome(ctx: _Ctx, state: dict, generation: str, retry_failed: bool) -> None:
    manifest = load_manifest()
    release = ctx.platform.release
    keys = state["keys"]
    todo = []
    for entry in manifest["settings"]:
        ident = entry["id"]
        current = keys.get(ident, {})
        done_before = current.get("status") in (APPLIED, NOT_APPLICABLE)
        if done_before and current.get("value") == entry["value"]:
            continue
        if current.get("status") == FAILED and not retry_failed:
            continue
        record = entry.get("releases", {}).get(release)
        if not isinstance(record, dict) or not record.get("available"):
            keys[ident] = {"status": NOT_APPLICABLE, "value": entry["value"],
                           "reason": f"not available on {release}"}
            continue
        todo.append(entry)

    fixed: dict = {}
    relocatable: set = set()
    if todo:
        listing = _list_schemas(ctx)
        if listing is None:
            for entry in todo:
                keys[entry["id"]] = _pending_key(entry["value"], "gsettings-unavailable")
            todo = []
        else:
            fixed, relocatable = listing

    # Resolve dconf paths, then back up every key before the first write.
    ready = []
    for entry in todo:
        ident, schema = entry["id"], entry["schema"]
        if entry.get("path"):
            if schema not in relocatable:
                keys[ident] = _pending_key(entry["value"], f"schema-missing:{schema}")
                continue
            base = entry["path"]
        else:
            if schema not in fixed:
                keys[ident] = _pending_key(entry["value"], f"schema-missing:{schema}")
                continue
            base = fixed[schema]
            if not base:
                keys[ident] = {"status": FAILED, "value": entry["value"],
                               "reason": "schema-has-no-path"}
                continue
        ready.append((entry, base + entry["key"]))

    baseline_path = _backup_file(ctx, "baseline")
    generation_path = _backup_file(ctx, generation)
    baseline = _load_key_backup(baseline_path)
    gen_backup = _load_key_backup(generation_path)
    writable = []
    for entry, dconf_key in ready:
        ident = entry["id"]
        if ident in gen_backup["keys"] and ident in baseline["keys"]:
            writable.append((entry, dconf_key))
            continue
        previous = _dconf_read(ctx, dconf_key)
        if previous is None:
            keys[ident] = _pending_key(entry["value"], "dconf-read-failed")
            continue
        snapshot = {
            "target": gsettings_target(entry),
            "key": entry["key"],
            "dconf_key": dconf_key,
            "explicit": previous != "",
            "value": previous if previous != "" else None,
            "recorded": _utc_now(),
        }
        baseline["keys"].setdefault(ident, snapshot)
        gen_backup["keys"].setdefault(ident, snapshot)
        writable.append((entry, dconf_key))
    if writable:
        _write_json(baseline_path, baseline)
        _write_json(generation_path, gen_backup)

    for entry, dconf_key in writable:
        ident = entry["id"]
        argv = ["gsettings", "set", gsettings_target(entry), entry["key"], entry["value"]]
        try:
            done = ctx.run(argv, "gsettings", read_only=False)
        except RunnerError as exc:
            reason = "gsettings-timeout" if exc.returncode is None else "gsettings-missing"
            keys[ident] = _pending_key(entry["value"], reason)
            continue
        if done.returncode != 0:
            detail = _tail(done.stderr)
            status, reason = _classify_set_failure(detail)
            keys[ident] = {"status": status, "value": entry["value"], "reason": reason,
                           "detail": detail, "retryable": status == PENDING}
            continue
        stored = _dconf_read(ctx, dconf_key)
        if stored != entry["value"]:
            # gsettings "succeeded" but dconf does not hold the value (e.g. a
            # non-persistent memory backend). Never report that as applied.
            keys[ident] = {"status": FAILED, "value": entry["value"], "reason": "not-persisted"}
            continue
        keys[ident] = {"status": APPLIED, "value": entry["value"], "applied": _utc_now()}

    _summarize_gnome(state)


def _summarize_gnome(state: dict) -> None:
    keys = state["keys"]
    pending = sorted({v["reason"] for v in keys.values() if v.get("status") == PENDING})
    failed = sorted(k for k, v in keys.items() if v.get("status") == FAILED)
    counts: dict = {}
    for value in keys.values():
        status = value.get("status", "?")
        counts[status] = counts.get(status, 0) + 1
    if pending:
        retryable = any(v.get("retryable") for v in keys.values() if v.get("status") == PENDING)
        _set_component(state, COMPONENT_GNOME, PENDING, pending, retryable, counts=counts,
                       failed_keys=failed)
    elif failed:
        _set_component(state, COMPONENT_GNOME, FAILED,
                       [f"{k}: {keys[k]['reason']}" for k in failed], False,
                       counts=counts, failed_keys=failed)
    else:
        _set_component(state, COMPONENT_GNOME, APPLIED, [], False, counts=counts)


# --------------------------------------------------------------------------
# input-remapper


def _records_path(ctx: _Ctx) -> Path:
    return ctx.gui_root / "remapper-records.json"


def _load_records(ctx: _Ctx) -> dict:
    data = _read_json(_records_path(ctx), {"schema": STATE_SCHEMA, "records": {}})
    if not isinstance(data, dict) or not isinstance(data.get("records"), dict):
        raise GuiError(f"{_records_path(ctx)} has an unknown format")
    return data


def _remapper_version(ctx: _Ctx, ad) -> tuple:
    """``(debian version, reason)``; the version is None when not usable."""

    fmt = "${Package}\\t${db:Status-Abbrev}\\t${Version}\\n"
    argv = ["dpkg-query", "-W", f"-f={fmt}", ad.LIBRARY_PACKAGE, ad.CONTROL_PACKAGE]
    try:
        done = ctx.run(argv, "dpkg", read_only=True)
    except RunnerError:
        return None, "input-remapper-not-installed"
    found = {}
    for line in done.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[1].strip().startswith("ii"):
            found[parts[0]] = parts[2].strip()
    library, control = found.get(ad.LIBRARY_PACKAGE), found.get(ad.CONTROL_PACKAGE)
    if library is None or control is None:
        return None, "input-remapper-not-installed"
    if ad.upstream_version(library) != ad.upstream_version(control):
        return None, "input-remapper-version-mismatch"
    return library, ""


def _backup_remapper_ops(ctx: _Ctx, name: str, ops) -> None:
    """Copy every file an op will replace or delete, once per backup name."""

    root = ctx.gui_root / "backups" / name / "remapper"
    manifest_path = root / "manifest.json"
    manifest = _read_json(manifest_path, {"schema": STATE_SCHEMA, "files": {}})
    changed = False
    for op in ops:
        key = str(op.path)
        if key in manifest["files"]:
            continue
        record: dict = {"existed": False, "recorded": _utc_now()}
        try:
            fd = os.open(op.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            fd = None
        if fd is not None:
            with os.fdopen(fd, "rb") as handle:
                data = handle.read()
                mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
            blob = f"{len(manifest['files']):04d}.bin"
            _ensure_dir(root)
            _atomic_write(root / blob, data)
            record.update(existed=True, mode=mode, blob=blob,
                          sha256=hashlib.sha256(data).hexdigest())
        manifest["files"][key] = record
        changed = True
    if changed:
        _write_json(manifest_path, manifest)


def _makedirs_under(ad, home: Path, directory: Path) -> None:
    current = home
    for part in directory.relative_to(home).parts:
        current = current / part
        try:
            os.mkdir(current, 0o755)
        except FileExistsError:
            pass
        st = os.lstat(current)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ad.AdapterError(f"{current} is not a real directory")


def _perform_ops(ad, home: Path, ops) -> None:
    for op in ops:
        ad.check_safe_path(home, op.path)
        if op.content is None:
            if os.path.lexists(op.path):
                os.unlink(op.path)
            continue
        _makedirs_under(ad, home, op.path.parent)
        _atomic_write(op.path, op.content, op.mode)


def _control(ctx: _Ctx, argv: list) -> str:
    """Run input-remapper-control; return "" on success or a reason."""

    try:
        done = ctx.run(argv, "control", read_only=False)
    except RunnerError as exc:
        return "autoload-timeout" if exc.returncode is None else "autoload-failed"
    if done.returncode != 0:
        tail = _tail(done.stderr, 160)
        return f"autoload-failed: {tail}" if tail else "autoload-failed"
    return ""


def _apply_remapper(ctx: _Ctx, state: dict, generation: str, retry_failed: bool) -> None:
    if ctx.platform.release == "22.04":
        _set_component(state, COMPONENT_REMAPPER, UNSUPPORTED, [REASON_V1_REMAPPER], False)
        return
    ad = load_adapters()
    if ctx.runner.which(ad.CONTROL_EXECUTABLE) is None:
        _set_component(state, COMPONENT_REMAPPER, PENDING, ["input-remapper-control-missing"],
                       True)
        return
    version, reason = _remapper_version(ctx, ad)
    if version is None:
        _set_component(state, COMPONENT_REMAPPER, PENDING, [reason], True)
        return
    try:
        family = ad.family_for_version(version)
    except ad.AdapterError:
        _set_component(state, COMPONENT_REMAPPER, UNSUPPORTED,
                       [f"input-remapper-unverified-version:{ad.upstream_version(version)}"])
        return
    if not ad.INTENT_SUPPORT[family][0]:
        _set_component(state, COMPONENT_REMAPPER, UNSUPPORTED, [REASON_V1_REMAPPER], False)
        return
    try:
        text = ctx.proc_devices.read_text(encoding="utf-8", errors="replace")
    except OSError:
        _set_component(state, COMPONENT_REMAPPER, PENDING, ["input-devices-unreadable"], True)
        return
    intent = ad.load_intent(INTENT_PATH)
    groups = ad.group_devices(ad.parse_proc_input_devices(text))
    eligible, rejected = ad.eligible_keyboards(groups, intent)
    if not eligible:
        _set_component(state, COMPONENT_REMAPPER, PENDING, ["no-eligible-keyboard"], True,
                       rejected=[f"{k}: {r}" for k, r in rejected])
        return

    records = _load_records(ctx)
    group_states = state["remapper_groups"]
    restart = []
    for group in sorted(eligible, key=lambda g: g.key):
        current = group_states.get(group.key, {})
        if current.get("status") == APPLIED:
            continue
        if current.get("status") == FAILED and not retry_failed:
            continue
        record_key = f"{family}:{group.key}"
        try:
            change = ad.apply_group(family, version, ctx.target.home, group, intent,
                                    records["records"].get(record_key))
            if change.ops:
                _backup_remapper_ops(ctx, generation, change.ops)
                _perform_ops(ad, ctx.target.home, change.ops)
        except ad.MigrationPending as exc:
            group_states[group.key] = {"status": PENDING, "reason": str(exc)}
            continue
        except (ad.AdapterError, OSError) as exc:
            group_states[group.key] = {"status": FAILED,
                                       "reason": f"{type(exc).__name__}: {exc}"}
            continue
        records["records"][record_key] = change.plan.record
        _write_json(_records_path(ctx), records)
        owned = change.plan.record["owned_preset"]
        if change.ops and change.plan.seed_selection == owned:
            # autoload skips a preset it already loaded; reload changed ones.
            restart.append((group.key, owned))
        group_states[group.key] = {"status": FILES_WRITTEN, "owned_preset": owned,
                                   "key_ambiguous": bool(group.key_ambiguous)}

    written = [k for k, v in group_states.items() if v.get("status") == FILES_WRITTEN]
    if written:
        failure = _control(ctx, ad.control_argv("autoload"))
        for key, preset in restart:
            if not failure:
                failure = _control(ctx, ad.control_argv("start", key, preset))
        if failure:
            _set_component(state, COMPONENT_REMAPPER, PENDING, [failure], True,
                           groups_with_files=sorted(written))
            return
        for key in written:
            group_states[key]["status"] = APPLIED

    pending = sorted({v["reason"] for v in group_states.values() if v.get("status") == PENDING})
    failed = sorted(f"{k}: {v['reason']}" for k, v in group_states.items()
                    if v.get("status") == FAILED)
    if pending:
        _set_component(state, COMPONENT_REMAPPER, PENDING, pending, False)
    elif failed:
        _set_component(state, COMPONENT_REMAPPER, FAILED, failed, False)
    else:
        _set_component(state, COMPONENT_REMAPPER, APPLIED, [], False,
                       groups=sorted(group_states))


# --------------------------------------------------------------------------
# One attempt


def _attempt(ctx: _Ctx, state: dict, generation: str, *, retry_failed: bool) -> None:
    state["attempts"] = int(state.get("attempts", 0)) + 1
    for name in COMPONENTS:
        if name not in ctx.components:
            _set_component(state, name, SKIPPED_COMPONENT, ["not selected"], False)
    work = [c for c in ctx.components
            if state["components"][c]["status"] == PENDING
            or (retry_failed and state["components"][c]["status"] == FAILED)]
    if COMPONENT_REMAPPER in work and ctx.platform.release == "22.04":
        _set_component(state, COMPONENT_REMAPPER, UNSUPPORTED, [REASON_V1_REMAPPER], False)
        work.remove(COMPONENT_REMAPPER)
    if not work:
        return
    gaps = _session_gaps(ctx)
    if gaps:
        reasons = [reason for reason, _ in gaps]
        retryable = any(flag for _, flag in gaps)
        for name in work:
            _set_component(state, name, PENDING, reasons, retryable)
        return
    finished: set = set()
    try:
        if COMPONENT_GNOME in work:
            _apply_gnome(ctx, state, generation, retry_failed)
            finished.add(COMPONENT_GNOME)
        if COMPONENT_REMAPPER in work:
            _apply_remapper(ctx, state, generation, retry_failed)
            finished.add(COMPONENT_REMAPPER)
    except _DeadlineReached:
        # An interrupted component is never complete, whatever it wrote.
        for name in work:
            if name in finished:
                continue
            comp = state["components"][name]
            reasons = [r for r in comp.get("reasons", []) if r != "not-attempted"]
            _set_component(state, name, PENDING, reasons + ["deadline-reached"], True)
        raise


def _mark_deadline(state: dict, components: Iterable[str]) -> None:
    for name in components:
        comp = state["components"][name]
        if comp["status"] == PENDING and "deadline-reached" not in comp["reasons"]:
            comp["reasons"] = comp["reasons"] + ["deadline-reached"]


# --------------------------------------------------------------------------
# Results


def _result(state: dict, components: Iterable[str], *, phase: str = "gui",
            extra: dict | None = None) -> dict:
    components = tuple(components)
    reasons: list = []
    any_failed = any_pending = False
    for name in components:
        comp = state["components"][name]
        if comp["status"] in (PENDING, UNSUPPORTED):
            any_pending = True
            reasons += [f"{name}: {r}" for r in comp["reasons"]]
        elif comp["status"] == FAILED:
            any_failed = True
            reasons += [f"{name}: {r}" for r in comp["reasons"]]
    if COMPONENT_GNOME in components:
        for key, value in sorted(state.get("keys", {}).items()):
            if value.get("status") == FAILED:
                any_failed = True
                line = f"{COMPONENT_GNOME}: {key}: {value['reason']}"
                if line not in reasons:
                    reasons.append(line)
    if any_failed:
        status = FAIL
    elif any_pending:
        status = PENDING_GUI
    elif not components:
        status = SKIPPED
        reasons.append("no GUI component selected")
    else:
        status = PASS
    details = {
        "generation": state.get("generation"),
        "attempts": state.get("attempts", 0),
        "components": {n: state["components"][n] for n in COMPONENTS},
        "keys": {k: v.get("status") for k, v in sorted(state.get("keys", {}).items())},
    }
    if extra:
        details.update(extra)
    return {"phase": phase, "status": status, "reasons": reasons, "details": details}


def _error(phase: str, exc: BaseException, **details) -> dict:
    return {"phase": phase, "status": FAIL, "reasons": [f"{type(exc).__name__}: {exc}"],
            "details": details}


# --------------------------------------------------------------------------
# Public entry points


def apply_or_defer(target: Target, runner, env: Mapping[str, str], *,
                   now: Callable[[], float] = time.monotonic,
                   sleep: Callable[[float], None] = time.sleep,
                   platform: Platform | None = None,
                   components: Iterable[str] | None = None,
                   proc_devices: Path = PROC_INPUT_DEVICES) -> dict:
    """Apply ready GUI components once; record the rest as pending.

    With ``PERSONAL_DOTFILES_GUI_AUTOSTART=1`` in ``env`` this is the login
    autostart mode (:func:`run_autostart`). Interactive runs also retry keys
    that failed before; autostart runs never do.
    """

    env = dict(env)
    if env.get(AUTOSTART_ENV) == "1":
        return run_autostart(target, runner, env, now=now, sleep=sleep, platform=platform,
                             components=components, proc_devices=proc_devices)
    if hasattr(runner, "recorded"):  # DryRunRunner
        return {"phase": "gui", "status": SKIPPED,
                "reasons": ["dry-run: desktop settings are not applied"], "details": {}}
    try:
        ctx = _make_ctx(target, runner, env, platform=platform, components=components,
                        now=now, deadline_at=None, proc_devices=proc_devices)
        generation = generation_id(ctx.platform.release)
        state, same = _load_state(ctx, generation)
    except (GuiError, PlatformError, OSError, ValueError) as exc:
        return _error("gui", exc, mode="interactive")
    if same and state.get("restored"):
        state = _fresh_state(generation, ctx.platform.release)
    try:
        if _needs_attempt(state, ctx.components, retry_failed=True):
            _attempt(ctx, state, generation, retry_failed=True)
        _save_state(ctx, state)
    except (GuiError, OSError) as exc:
        return _error("gui", exc, mode="interactive")
    return _result(state, ctx.components,
                   extra={"mode": "interactive", "state_path": str(_state_path(ctx))})


def run_autostart(target: Target, runner, env: Mapping[str, str], *,
                  now: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], None] = time.sleep,
                  platform: Platform | None = None,
                  components: Iterable[str] | None = None,
                  proc_devices: Path = PROC_INPUT_DEVICES) -> dict:
    """Login mode: retry readiness gaps at RETRY_OFFSETS within DEADLINE."""

    start = now()
    deadline_at = start + DEADLINE
    try:
        ctx = _make_ctx(target, runner, env, platform=platform, components=components,
                        now=now, deadline_at=deadline_at, proc_devices=proc_devices)
        generation = generation_id(ctx.platform.release)
        state, same = _load_state(ctx, generation)
    except (GuiError, PlatformError, OSError, ValueError) as exc:
        return _error("gui", exc, mode="autostart")
    if same and (state.get("restored") or not _needs_attempt(state, ctx.components, False)):
        return _result(state, ctx.components,
                       extra={"mode": "autostart", "tries": 0,
                              "skipped": "generation already handled"})

    tries = 0
    try:
        for offset in RETRY_OFFSETS:
            at = start + offset
            if at >= deadline_at:
                break
            wait = at - now()
            if wait > 0:
                sleep(wait)
            if now() >= deadline_at:
                break
            tries += 1
            try:
                _attempt(ctx, state, generation, retry_failed=False)
            except _DeadlineReached:
                break
            _save_state(ctx, state)
            if not _retryable_pending(state, ctx.components):
                break
        if _retryable_pending(state, ctx.components):
            _mark_deadline(state, ctx.components)
        state["last_autostart"] = {"at": _utc_now(), "tries": tries,
                                   "elapsed": round(now() - start, 3)}
        _save_state(ctx, state)
    except (GuiError, OSError) as exc:
        return _error("gui", exc, mode="autostart", tries=tries)
    return _result(state, ctx.components,
                   extra={"mode": "autostart", "tries": tries,
                          "elapsed": round(now() - start, 3)})


def restore_gui(target: Target, runner, env: Mapping[str, str],
                generation: str | None = None, *,
                platform: Platform | None = None,
                components: Iterable[str] | None = None) -> dict:
    """Restore managed keys from the baseline (or ``generation``) backup and
    the input-remapper groups from their restore records."""

    phase = "gui-restore"
    env = dict(env)
    name = "baseline" if generation is None else generation
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise GuiError(f"invalid generation {name!r}")
        ctx = _make_ctx(target, runner, env, platform=platform, components=components,
                        now=time.monotonic, deadline_at=None, proc_devices=PROC_INPUT_DEVICES)
        backup = _load_key_backup(_backup_file(ctx, name))
        records = _load_records(ctx)
    except (GuiError, PlatformError, OSError, ValueError) as exc:
        return _error(phase, exc)
    if not backup["keys"] and not records["records"]:
        return {"phase": phase, "status": SKIPPED, "reasons": [f"no GUI backup for {name}"],
                "details": {"backup": name}}
    gaps = _session_gaps(ctx)
    if gaps:
        return {"phase": phase, "status": PENDING_GUI,
                "reasons": [r for r, _ in gaps] + ["restore needs the GNOME desktop session"],
                "details": {"backup": name}}

    outcomes: dict = {}
    reasons: list = []
    if COMPONENT_GNOME in ctx.components:
        for ident, snap in sorted(backup["keys"].items()):
            if snap.get("explicit"):
                argv = ["gsettings", "set", snap["target"], snap["key"], snap["value"]]
                expected = snap["value"]
            else:
                argv = ["gsettings", "reset", snap["target"], snap["key"]]
                expected = ""
            try:
                done = ctx.run(argv, "gsettings", read_only=False)
                ok, detail = done.returncode == 0, _tail(done.stderr)
            except RunnerError as exc:
                ok, detail = False, str(exc)
            if ok and _dconf_read(ctx, snap["dconf_key"]) != expected:
                ok, detail = False, "value after restore differs from the backup"
            outcomes[ident] = RESTORED if ok else FAILED
            if not ok:
                reasons.append(f"{COMPONENT_GNOME}: {ident}: restore failed: {detail}")

    remapper_outcome = None
    if COMPONENT_REMAPPER in ctx.components and records["records"]:
        ad = load_adapters()
        stamp = "restore-" + datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ")
        remapper_outcome = RESTORED
        for record_key, record in sorted(records["records"].items()):
            family = record_key.split(":", 1)[0]
            try:
                change = ad.restore_group(family, ctx.target.home, record)
                if change.ops:
                    _backup_remapper_ops(ctx, stamp, change.ops)
                    _perform_ops(ad, ctx.target.home, change.ops)
            except (ad.AdapterError, OSError) as exc:
                remapper_outcome = FAILED
                reasons.append(f"{COMPONENT_REMAPPER}: {record_key}: {exc}")
        if remapper_outcome == RESTORED and ctx.runner.which(ad.CONTROL_EXECUTABLE):
            failure = _control(ctx, ad.control_argv("autoload"))
            if failure:
                reasons.append(f"{COMPONENT_REMAPPER}: {failure} (files restored)")

    try:
        state, _ = _load_state(ctx, generation_id(ctx.platform.release))
        state["restored"] = {"backup": name, "at": _utc_now()}
        for ident, outcome in outcomes.items():
            if outcome == RESTORED:
                state["keys"].pop(ident, None)
        _save_state(ctx, state)
    except (GuiError, OSError) as exc:
        reasons.append(f"state not updated: {exc}")
    failed = any(v == FAILED for v in outcomes.values()) or remapper_outcome == FAILED
    return {
        "phase": phase,
        "status": FAIL if failed else PASS,
        "reasons": reasons,
        "details": {"backup": name, "keys": outcomes, "input-remapper": remapper_outcome},
    }


# --------------------------------------------------------------------------
# Autostart entry

_EXEC_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def desktop_exec_quote(arg: str) -> str:
    """Quote one Exec argument per the Desktop Entry specification."""

    if "\n" in arg or "\r" in arg:
        raise GuiError("an Exec argument cannot contain a line break")
    arg = arg.replace("%", "%%")
    if arg == "" or any(ch in _EXEC_RESERVED for ch in arg):
        arg = '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'
    return arg


def desktop_string_escape(value: str) -> str:
    """Escape a string-typed value (the spec's backslash sequences)."""

    return value.replace("\\", "\\\\").replace("\t", "\\t")


def autostart_exec(target: Target) -> str:
    argv = [AUTOSTART_PYTHON, str(target.compat_link / "install.py"), "gui-apply", "--autostart"]
    return desktop_string_escape(" ".join(desktop_exec_quote(a) for a in argv))


def render_autostart(target: Target) -> bytes:
    template = AUTOSTART_TEMPLATE.read_text(encoding="utf-8")
    if template.count("@EXEC@") != 1:
        raise GuiError(f"{AUTOSTART_TEMPLATE} must contain exactly one @EXEC@")
    return template.replace("@EXEC@", autostart_exec(target)).encode("utf-8")


def autostart_desired_entry(target: Target) -> DesiredEntry:
    return DesiredEntry(
        id=AUTOSTART_ENTRY_ID,
        dest=target.config_home / "autostart" / AUTOSTART_NAME,
        kind="file",
        content=render_autostart(target),
        mode=0o644,
    )


__all__ = [
    "apply_or_defer",
    "autostart_desired_entry",
    "generation_id",
    "restore_gui",
    "run_autostart",
]
