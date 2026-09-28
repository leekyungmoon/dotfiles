"""Backup, atomic replacement, rollback and restore of managed paths.

This is the part of the installer that overwrites a user's existing files, so
every rule here errs on the side of keeping data:

* The first time an id is managed, the object at its destination is copied
  into an immutable *baseline* (a verified temp directory renamed into place
  with a completeness marker). Baselines are never rewritten.
* Before the first replacement of a run, every destination that will change
  is copied into a per-run backup and verified, and the plan is journaled
  with fsync.
* Each replacement is atomic (temp sibling + rename) and journaled before it
  starts, so an exception rolls the run back in reverse order and a crash is
  rolled back by the next run that takes the lock.
* ``state.json`` is the commit point: once it names the run, the run stands.
* A copied file (``kind="file"``) that changed since the installer last wrote
  it belongs to the user or to the program that rewrote it: :meth:`apply`
  keeps it and reports it in ``ApplyResult.kept`` unless ``force`` is given.
* Crash recovery never discards an object it did not write: if a target is
  neither what the crashed run found nor what it wrote, it is copied into
  ``backups/recovery/`` before the old state is put back.

Snapshots use ``lstat`` and never follow a leaf symlink. Nothing here logs or
journals file contents; only hashes, modes and link texts.
"""

from __future__ import annotations

import dataclasses
import datetime
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from pathlib import Path
from typing import Callable, Iterable

from .manifest import ResolvedEntry, check_dest_placement
from .platform import Target, _is_within, is_generation_id

SCHEMA = 1
_CHUNK = 1 << 20
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DESIRED_KINDS = ("symlink", "file", "dir", "absent")
OBJECT_KINDS = ("absent", "file", "dir", "symlink")


class TransactionError(Exception):
    """A managed-path change could not be planned, applied or restored."""


class ConcurrentRunError(TransactionError):
    """Another installer run holds the install lock."""


# --------------------------------------------------------------------------
# Object snapshots


@dataclasses.dataclass(frozen=True)
class ObjectState:
    kind: str
    mode: int | None = None
    sha256: str | None = None
    link_text: str | None = None
    tree_sha256: str | None = None

    def to_json(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "ObjectState":
        if not isinstance(data, dict) or data.get("kind") not in OBJECT_KINDS:
            raise TransactionError(f"malformed object state {data!r}")
        return cls(
            kind=data["kind"],
            mode=data.get("mode"),
            sha256=data.get("sha256"),
            link_text=data.get("link_text"),
            tree_sha256=data.get("tree_sha256"),
        )

    def describe(self) -> str:
        if self.kind == "symlink":
            return f"symlink -> {self.link_text}"
        if self.kind == "absent":
            return "absent"
        return f"{self.kind} {oct(self.mode or 0)}"


ABSENT = ObjectState("absent")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _special(path: Path, mode: int) -> TransactionError:
    return TransactionError(
        f"{path} is a special file (mode {oct(mode)}); refusing to manage it"
    )


def _tree_records(root: Path, rel: bytes, digest) -> None:
    with os.scandir(root) as it:
        entries = sorted(it, key=lambda e: os.fsencode(e.name))
    for entry in entries:
        path = Path(entry.path)
        st = entry.stat(follow_symlinks=False)
        name = rel + os.fsencode(entry.name)
        mode = stat.S_IMODE(st.st_mode)
        if stat.S_ISLNK(st.st_mode):
            kind, payload = b"l", os.fsencode(os.readlink(path))
            mode = 0
        elif stat.S_ISREG(st.st_mode):
            kind, payload = b"f", _hash_file(path).encode()
        elif stat.S_ISDIR(st.st_mode):
            kind, payload = b"d", b""
        else:
            raise _special(path, st.st_mode)
        digest.update(
            kind + b"\0" + name + b"\0" + b"%o" % mode + b"\0" + payload + b"\n"
        )
        if kind == b"d":
            _tree_records(path, name + b"/", digest)


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    _tree_records(root, b"", digest)
    return digest.hexdigest()


def snapshot(path: Path) -> ObjectState:
    """Describe the object at ``path`` without following a leaf symlink."""

    path = Path(path)
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return ABSENT
    mode = stat.S_IMODE(st.st_mode)
    if stat.S_ISLNK(st.st_mode):
        return ObjectState("symlink", link_text=os.readlink(path))
    if stat.S_ISREG(st.st_mode):
        return ObjectState("file", mode=mode, sha256=_hash_file(path))
    if stat.S_ISDIR(st.st_mode):
        return ObjectState("dir", mode=mode, tree_sha256=_tree_hash(path))
    raise _special(path, st.st_mode)


# --------------------------------------------------------------------------
# Filesystem primitives


# Tests turn this off to keep large failure matrices fast; production never
# changes it.
FSYNC = True


def _fsync(fd: int) -> None:
    if FSYNC:
        os.fsync(fd)


def _fsync_dir(path: Path) -> None:
    if not FSYNC:
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        _fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            _fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _write_json(path: Path, payload: dict) -> None:
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _atomic_write(path, data, 0o600)


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TransactionError(f"cannot read {path}: {exc}") from None
    if not isinstance(payload, dict):
        raise TransactionError(f"{path} is not a JSON object")
    return payload


def _copy_file(src: Path, dst: Path, mode: int) -> None:
    in_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(in_fd, "rb") as source:
        out_fd = os.open(
            dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(out_fd, "wb") as target:
            shutil.copyfileobj(source, target, _CHUNK)
            os.fchmod(target.fileno(), mode)


def _copy_object(src: Path, dst: Path) -> None:
    """Copy ``src`` (file, symlink or tree) to the absent ``dst`` exactly."""

    st = os.lstat(src)
    if stat.S_ISLNK(st.st_mode):
        os.symlink(os.readlink(src), dst)
    elif stat.S_ISREG(st.st_mode):
        _copy_file(src, dst, stat.S_IMODE(st.st_mode))
    elif stat.S_ISDIR(st.st_mode):
        os.mkdir(dst, 0o700)
        with os.scandir(src) as it:
            names = [entry.name for entry in it]
        for name in names:
            _copy_object(src / name, dst / name)
        os.chmod(dst, stat.S_IMODE(st.st_mode))
    else:
        raise _special(src, st.st_mode)


def _make_tree_writable(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        return
    os.chmod(path, 0o700)
    with os.scandir(path) as it:
        subdirs = [Path(e.path) for e in it if e.is_dir(follow_symlinks=False)]
    for sub in subdirs:
        _make_tree_writable(sub)


def _remove_object(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        _make_tree_writable(path)
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _same_device(a: Path, b: Path) -> bool:
    """Whether a rename from ``a`` into directory ``b`` stays on one device."""

    return os.stat(a).st_dev == os.stat(b).st_dev


def _sibling_prefix(dest: Path) -> str:
    return f".{dest.name[:80]}.pd-"


def _sibling(dest: Path, tag: str, run_id: str) -> Path:
    name = f"{_sibling_prefix(dest)}{tag}-{run_id}-{secrets.token_hex(4)}"
    return dest.parent / name


def _cleanup_siblings(dest: Path, run_id: str) -> None:
    prefix = _sibling_prefix(dest)
    marker = f"-{run_id}-"
    try:
        names = os.listdir(dest.parent)
    except OSError:
        return
    for name in names:
        if name.startswith(prefix) and marker in name:
            _remove_object(dest.parent / name)


def _swap_in(new: Path, dest: Path, run_id: str) -> None:
    """Atomically put ``new`` at ``dest``, whatever currently sits there."""

    try:
        current = os.lstat(dest)
    except FileNotFoundError:
        current = None
    new_is_dir = stat.S_ISDIR(os.lstat(new).st_mode)
    if current is not None and (stat.S_ISDIR(current.st_mode) or new_is_dir):
        # rename(2) cannot swap a directory with a non-directory, and cannot
        # replace a non-empty directory; move the old object aside first. Its
        # verified backup already exists.
        aside = _sibling(dest, "old", run_id)
        os.rename(dest, aside)
        os.rename(new, dest)
        _remove_object(aside)
    else:
        os.replace(new, dest)
    _fsync_dir(dest.parent)


def _remove_atomically(dest: Path, run_id: str) -> None:
    try:
        st = os.lstat(dest)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        aside = _sibling(dest, "old", run_id)
        os.rename(dest, aside)
        _remove_object(aside)
    else:
        os.unlink(dest)
    _fsync_dir(dest.parent)


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def new_run_id() -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


# --------------------------------------------------------------------------
# Lock


class InstallLock:
    """Exclusive, non-blocking ``flock`` on ``state_root/install.lock``."""

    def __init__(self, state_root: Path) -> None:
        self.path = Path(state_root) / "install.lock"
        self._fd: int | None = None

    def __enter__(self) -> "InstallLock":
        fd = os.open(
            self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ConcurrentRunError(
                f"another installer run holds {self.path}; wait for it to finish"
            ) from None
        self._fd = fd
        return self

    def __exit__(self, *exc_info) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


# --------------------------------------------------------------------------
# Desired state


@dataclasses.dataclass(frozen=True)
class DesiredEntry:
    id: str
    dest: Path
    kind: str
    link_text: str | None = None
    content: bytes | None = None
    mode: int | None = None
    staged_dir: Path | None = None


def entries_from_manifest(
    resolved: Iterable[ResolvedEntry],
    target: Target,
    *,
    systemd_user: bool = True,
) -> list[DesiredEntry]:
    """Translate resolved manifest entries; ``systemd-user`` ones are dropped
    when ``systemd_user`` is false (see :func:`skipped_by_condition`)."""

    desired: list[DesiredEntry] = []
    for entry in resolved:
        if entry.condition == "systemd-user" and not systemd_user:
            continue
        if entry.kind in ("symlink", "link"):
            desired.append(
                DesiredEntry(entry.id, entry.dest, "symlink", link_text=entry.link_text)
            )
        elif entry.kind == "copy":
            assert entry.source is not None
            desired.append(
                DesiredEntry(
                    entry.id,
                    entry.dest,
                    "file",
                    content=Path(entry.source).read_bytes(),
                    mode=entry.mode if entry.mode is not None else 0o644,
                )
            )
        elif entry.kind == "remove":
            desired.append(DesiredEntry(entry.id, entry.dest, "absent"))
        else:
            raise TransactionError(f"entry {entry.id!r}: unknown kind {entry.kind!r}")
    return desired


def skipped_by_condition(
    resolved: Iterable[ResolvedEntry], *, systemd_user: bool
) -> list[str]:
    return [
        e.id for e in resolved if e.condition == "systemd-user" and not systemd_user
    ]


@dataclasses.dataclass
class _Step:
    record: dict
    desired: ObjectState
    content: bytes | None = None
    payload: Path | None = None
    staged_dir: Path | None = None


@dataclasses.dataclass
class ApplyResult:
    """What :meth:`Transaction.apply` did.

    ``drifted`` lists *retired* entries left alone because they changed after
    the installer wrote them. ``kept`` lists still-managed copied files
    (``kind="file"``) that changed since the last install and were therefore
    kept as they are; ``forced`` lists those that were backed up and
    overwritten anyway because ``force`` was given (they are also in
    ``changed``). ``recovery_saved`` describes objects that crash recovery
    found in an unexpected state and copied aside before rolling back.
    """

    run_id: str
    changed: list[str]
    unchanged: list[str]
    retired: list[str]
    drifted: list[str]
    baseline_added: list[str]
    backup_dir: Path | None
    kept: list[str] = dataclasses.field(default_factory=list)
    forced: list[str] = dataclasses.field(default_factory=list)
    recovery_saved: list[dict] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict:
        payload = dataclasses.asdict(self)
        payload["backup_dir"] = str(self.backup_dir) if self.backup_dir else None
        return payload


@dataclasses.dataclass
class RestoreResult:
    run_id: str
    which: str
    restored: list[str]
    unchanged: list[str]
    forced: list[str]
    backup_dir: Path | None
    recovery_saved: list[dict] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict:
        payload = dataclasses.asdict(self)
        payload["backup_dir"] = str(self.backup_dir) if self.backup_dir else None
        return payload


# --------------------------------------------------------------------------
# Layout helpers


def _layout(state_root: Path) -> dict[str, Path]:
    return {
        "root": state_root,
        "journal": state_root / "journal",
        "backups": state_root / "backups",
        "baseline": state_root / "backups" / "baseline",
        "runs": state_root / "backups" / "runs",
        "recovery": state_root / "backups" / "recovery",
    }


def _read_state(target: Target) -> dict | None:
    path = target.state_root / "state.json"
    if not os.path.lexists(path):
        return None
    state = _read_json(path)
    if state.get("schema") != SCHEMA:
        raise TransactionError(f"{path} has unsupported schema {state.get('schema')!r}")
    if state.get("owner_uid") != target.uid:
        raise TransactionError(
            f"{path} belongs to uid {state.get('owner_uid')!r}, not {target.uid}"
        )
    if not isinstance(state.get("entries"), dict):
        raise TransactionError(f"{path} has no entries map")
    return state


def _baseline_meta(baseline_root: Path, entry_id: str) -> dict | None:
    folder = baseline_root / entry_id
    if not (folder / "COMPLETE").is_file():
        return None
    return _read_json(folder / "meta.json")


# --------------------------------------------------------------------------
# Transaction


class Transaction:
    def __init__(
        self,
        target: Target,
        run_id: str,
        *,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        if not is_generation_id(run_id):
            raise TransactionError(f"invalid run id {run_id!r}")
        self.target = target
        self.run_id = run_id
        self._fault_hook = fault
        self._paths = _layout(target.state_root)
        self._lock: InstallLock | None = None
        self._state: dict | None = None
        self._journal: dict | None = None
        self._rolling_back = False
        self.recovered: list[str] = []
        # Objects an undo found in neither the recorded "before" nor the
        # recorded "desired" state, copied aside before being replaced.
        self.recovery_saved: list[dict] = []

    # -- context -----------------------------------------------------------

    def __enter__(self) -> "Transaction":
        if os.geteuid() != self.target.uid:
            raise TransactionError(
                f"running as uid {os.geteuid()}, but the target owner is "
                f"{self.target.uid}"
            )
        self._prepare_state_dirs()
        lock = InstallLock(self.target.state_root)
        lock.__enter__()
        self._lock = lock
        try:
            self._state = _read_state(self.target)
            self._recover()
            self._state = _read_state(self.target)
        except BaseException:
            lock.__exit__(None, None, None)
            self._lock = None
            raise
        return self

    def __exit__(self, *exc_info) -> None:
        if self._lock is not None:
            self._lock.__exit__(*exc_info)
            self._lock = None

    def _prepare_state_dirs(self) -> None:
        root = self.target.state_root
        if not _is_within(root, self.target.home):
            raise TransactionError(f"state root {root} is outside {self.target.home}")
        if os.path.islink(root):
            raise TransactionError(f"state root {root} is a symlink; refusing")
        root.parent.mkdir(parents=True, exist_ok=True)
        for key in ("root", "journal", "backups", "baseline", "runs"):
            path = self._paths[key]
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                pass
            st = os.lstat(path)
            if not stat.S_ISDIR(st.st_mode):
                raise TransactionError(f"{path} is not a directory")
            if st.st_uid != self.target.uid:
                raise TransactionError(f"{path} is not owned by uid {self.target.uid}")
            os.chmod(path, 0o700)

    def _require_entered(self) -> None:
        if self._lock is None:
            raise TransactionError("Transaction must be used as a context manager")

    def _fault(self, step: str) -> None:
        if self._fault_hook is not None and not self._rolling_back:
            self._fault_hook(step)

    # -- journal -------------------------------------------------------------

    def _journal_path(self, run_id: str) -> Path:
        return self._paths["journal"] / f"{run_id}.json"

    def _begin_journal(self, op: str, extra: dict) -> None:
        if os.path.lexists(self._journal_path(self.run_id)):
            raise TransactionError(f"run id {self.run_id} was already used")
        self._journal = {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "op": op,
            "status": "running",
            "started": _utc_now(),
            "baseline_created": [],
            "steps": [],
            **extra,
        }
        self._write_journal()

    def _write_journal(self) -> None:
        assert self._journal is not None
        self._fault("journal")
        _write_json(self._journal_path(self.run_id), self._journal)

    def _write_journal_best_effort(self, journal: dict) -> None:
        try:
            _write_json(self._journal_path(journal["run_id"]), journal)
        except OSError:
            pass

    # -- recovery ----------------------------------------------------------

    def _recover(self) -> None:
        committed_run = (self._state or {}).get("run_id")
        for path in sorted(self._paths["journal"].glob("*.json")):
            journal = _read_json(path)
            if journal.get("status") != "running":
                continue
            run_id = journal.get("run_id")
            if not isinstance(run_id, str) or not is_generation_id(run_id):
                raise TransactionError(f"malformed journal {path}")
            if run_id == committed_run:
                # state.json is the commit point; only the final journal
                # write was lost.
                journal["status"] = "committed"
                self._write_journal_best_effort(journal)
                continue
            errors = self._rollback_journal(journal)
            if errors:
                self._write_journal_best_effort(journal)
                raise TransactionError(
                    f"could not roll back interrupted run {run_id}: "
                    + "; ".join(errors)
                )
            journal["status"] = "rolled-back"
            journal["recovered"] = True
            self._write_journal_best_effort(journal)
            self.recovered.append(run_id)
        for leftover in self._paths["baseline"].glob(".tmp-*"):
            _remove_object(leftover)

    def _rollback_journal(self, journal: dict) -> list[str]:
        was = self._rolling_back
        self._rolling_back = True
        errors: list[str] = []
        try:
            run_id = journal["run_id"]
            for record in reversed(journal.get("steps", [])):
                if record.get("status") not in ("started", "done"):
                    continue
                try:
                    self._undo(record, run_id)
                    record["status"] = "undone"
                except Exception as exc:  # keep undoing the other steps
                    errors.append(f"{record.get('id')}: {exc}")
            if not errors:
                for entry_id in reversed(journal.get("baseline_created", [])):
                    folder = self._paths["baseline"] / entry_id
                    meta = _baseline_meta(self._paths["baseline"], entry_id)
                    if meta is not None and meta.get("created_run") == run_id:
                        _remove_object(folder)
                    for leftover in self._paths["baseline"].glob(
                        f".tmp-{entry_id}-{run_id}"
                    ):
                        _remove_object(leftover)
                    _fsync_dir(self._paths["baseline"])
        finally:
            self._rolling_back = was
        return errors

    def _undo(self, record: dict, run_id: str) -> None:
        dest = Path(record["dest"])
        before = ObjectState.from_json(record["before"])
        desired = ObjectState.from_json(record["desired"])
        staged = record.get("staged_dir")
        if (
            record.get("action") == "dir"
            and staged
            and not os.path.lexists(staged)
            and snapshot(dest) == desired
        ):
            # Hand the promoted checkout back to staging instead of
            # deleting it, so a caller can inspect or retry.
            os.rename(dest, staged)
        current = snapshot(dest)
        if current != before:
            if current != desired and current.kind != "absent":
                # Neither what the run found nor what it wrote: somebody
                # changed the target after the swap (typically after a
                # crash, before this recovery). Keep a verified copy.
                self._save_unexpected(record, run_id, dest, current)
            if before.kind == "absent":
                _remove_atomically(dest, run_id)
            else:
                backup = record.get("backup")
                if not backup or not os.path.lexists(backup):
                    raise TransactionError(f"backup for {dest} is missing")
                tmp = _sibling(dest, "new", run_id)
                _copy_object(Path(backup), tmp)
                if snapshot(tmp) != before:
                    _remove_object(tmp)
                    raise TransactionError(f"backup for {dest} failed verification")
                _swap_in(tmp, dest, run_id)
            if snapshot(dest) != before:
                raise TransactionError(f"{dest} did not return to its prior state")
        _cleanup_siblings(dest, run_id)
        for parent in reversed(record.get("created_parents", [])):
            try:
                os.rmdir(parent)
            except OSError:
                pass

    def _save_unexpected(
        self, record: dict, run_id: str, dest: Path, current: ObjectState
    ) -> None:
        recovery = self._paths["recovery"]
        root = recovery / self.run_id / run_id
        for directory in (recovery, recovery / self.run_id, root):
            try:
                os.mkdir(directory, 0o700)
            except FileExistsError:
                pass
        folder = root / record["id"]
        os.mkdir(folder, 0o700)
        saved = folder / "object"
        _copy_object(dest, saved)
        if snapshot(saved) != current:
            raise TransactionError(
                f"could not save the unexpected current state of {dest}; "
                "left it in place"
            )
        _write_json(
            folder / "meta.json",
            {
                "schema": SCHEMA,
                "id": record["id"],
                "dest": str(dest),
                "state": current.to_json(),
                "interrupted_run": run_id,
                "recovery_run": self.run_id,
                "saved": _utc_now(),
            },
        )
        _fsync_dir(folder)
        _fsync_dir(root)
        record["unexpected_saved"] = str(saved)
        self.recovery_saved.append(
            {"id": record["id"], "dest": str(dest), "saved_to": str(saved), "run_id": run_id}
        )

    # -- validation ----------------------------------------------------------

    def _validate_desired(self, desired: list[DesiredEntry]) -> None:
        ids: set[str] = set()
        for entry in desired:
            if not _ID_RE.match(entry.id):
                raise TransactionError(f"invalid entry id {entry.id!r}")
            if entry.id in ids:
                raise TransactionError(f"duplicate entry id {entry.id!r}")
            ids.add(entry.id)
            if entry.kind not in DESIRED_KINDS:
                raise TransactionError(f"entry {entry.id!r}: unknown kind {entry.kind!r}")
            dest = Path(entry.dest)
            try:
                check_dest_placement(
                    dest, self.target, allow_repo_root=(entry.kind == "dir")
                )
            except ValueError as exc:
                raise TransactionError(f"entry {entry.id!r}: {exc}") from None
            if entry.kind == "symlink" and not entry.link_text:
                raise TransactionError(f"entry {entry.id!r}: symlink needs link_text")
            if entry.kind == "file" and not isinstance(entry.content, bytes):
                raise TransactionError(f"entry {entry.id!r}: file needs bytes content")
            if entry.mode is not None and not 0 <= entry.mode <= 0o7777:
                raise TransactionError(f"entry {entry.id!r}: invalid mode")
            if entry.kind == "dir":
                staged = entry.staged_dir
                if staged is None or os.path.islink(staged) or not Path(staged).is_dir():
                    raise TransactionError(
                        f"entry {entry.id!r}: staged_dir must be a real directory"
                    )
        self._check_overlaps([(e.id, Path(e.dest)) for e in desired])

    @staticmethod
    def _check_overlaps(items: list[tuple[str, Path]]) -> None:
        for i, (first_id, first) in enumerate(items):
            for second_id, second in items[i + 1 :]:
                if first == second or _is_within(first, second) or _is_within(second, first):
                    raise TransactionError(
                        f"entries {first_id!r} and {second_id!r} have overlapping "
                        f"destinations {first} and {second}"
                    )

    # -- baseline ------------------------------------------------------------

    def _ensure_baseline(
        self, entry_id: str, dest: Path, state: ObjectState, adopt_from: str | None
    ) -> bool:
        assert self._journal is not None
        root = self._paths["baseline"]
        final = root / entry_id
        meta = _baseline_meta(root, entry_id)
        if meta is not None:
            if meta.get("dest") != str(dest):
                raise TransactionError(
                    f"entry {entry_id!r} was first managed at {meta.get('dest')}, "
                    f"not {dest}; give a moved destination a new id"
                )
            return False
        if os.path.lexists(final):
            raise TransactionError(
                f"baseline {final} exists without a completeness marker; "
                "inspect it before rerunning"
            )
        self._journal["baseline_created"].append(entry_id)
        self._write_journal()
        tmp = root / f".tmp-{entry_id}-{self.run_id}"
        os.mkdir(tmp, 0o700)
        source_meta = None
        if adopt_from is not None:
            source_meta = _baseline_meta(root, adopt_from)
        if source_meta is not None:
            recorded = ObjectState.from_json(source_meta["state"])
            if recorded.kind != "absent":
                _copy_object(root / adopt_from / "object", tmp / "object")
        else:
            recorded = state
            if state.kind != "absent":
                _copy_object(dest, tmp / "object")
        if recorded.kind != "absent" and snapshot(tmp / "object") != recorded:
            raise TransactionError(f"baseline copy of {dest} failed verification")
        _write_json(
            tmp / "meta.json",
            {
                "schema": SCHEMA,
                "id": entry_id,
                "dest": str(dest),
                "state": recorded.to_json(),
                "created_run": self.run_id,
                "created": _utc_now(),
                "adopted_from": adopt_from if source_meta is not None else None,
            },
        )
        self._fault(f"baseline:{entry_id}")
        _atomic_write(tmp / "COMPLETE", b"complete\n", 0o600)
        _fsync_dir(tmp)
        os.rename(tmp, final)
        _fsync_dir(root)
        self._fault(f"baseline-complete:{entry_id}")
        return True

    # -- steps -----------------------------------------------------------------

    def _new_step(
        self,
        op: str,
        entry_id: str,
        dest: Path,
        before: ObjectState,
        desired: ObjectState,
        action: str,
        **extra,
    ) -> _Step:
        record = {
            "id": entry_id,
            "op": op,
            "dest": str(dest),
            "action": action,
            "before": before.to_json(),
            "desired": desired.to_json(),
            "backup": None,
            "staged_dir": str(extra["staged_dir"]) if extra.get("staged_dir") else None,
            "status": "planned",
            "created_parents": [],
            "after": None,
        }
        return _Step(record=record, desired=desired, **extra)

    def _backup(self, step: _Step) -> None:
        record = step.record
        entry_id = record["id"]
        dest = Path(record["dest"])
        before = ObjectState.from_json(record["before"])
        folder = self._paths["runs"] / self.run_id / entry_id
        (self._paths["runs"] / self.run_id).mkdir(mode=0o700, exist_ok=True)
        os.mkdir(folder, 0o700)
        payload_ref = "none"
        if before.kind != "absent":
            meta = _baseline_meta(self._paths["baseline"], entry_id)
            if (
                meta is not None
                and meta.get("dest") == str(dest)
                and ObjectState.from_json(meta["state"]) == before
            ):
                payload_ref = "baseline"
                backup = self._paths["baseline"] / entry_id / "object"
            else:
                payload_ref = "object"
                backup = folder / "object"
                _copy_object(dest, backup)
            if snapshot(backup) != before or snapshot(dest) != before:
                raise TransactionError(f"backup of {dest} failed verification")
            record["backup"] = str(backup)
        _write_json(
            folder / "meta.json",
            {
                "schema": SCHEMA,
                "id": entry_id,
                "dest": str(dest),
                "state": before.to_json(),
                "payload": payload_ref,
                "run_id": self.run_id,
            },
        )
        self._fault(f"backup:{entry_id}")

    def _execute(self, step: _Step) -> None:
        record = step.record
        entry_id = record["id"]
        dest = Path(record["dest"])
        before = ObjectState.from_json(record["before"])
        action = record["action"]

        missing: list[Path] = []
        if action != "absent":
            parent = dest.parent
            while not os.path.lexists(parent):
                missing.append(parent)
                parent = parent.parent
            missing.reverse()
        record["status"] = "started"
        record["created_parents"] = [str(p) for p in missing]
        self._write_journal()
        self._fault(f"swap:{entry_id}")

        if snapshot(dest) != before:
            raise TransactionError(f"{dest} changed while the installer was running")
        for directory in missing:
            os.mkdir(directory)

        run_id = self.run_id
        if action == "absent":
            _remove_atomically(dest, run_id)
        elif action == "symlink":
            tmp = _sibling(dest, "new", run_id)
            os.symlink(step.desired.link_text, tmp)  # type: ignore[arg-type]
            _swap_in(tmp, dest, run_id)
        elif action == "file":
            tmp = _sibling(dest, "new", run_id)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(step.content or b"")
                handle.flush()
                os.fchmod(handle.fileno(), step.desired.mode or 0o644)
                _fsync(handle.fileno())
            _swap_in(tmp, dest, run_id)
        elif action == "dir":
            staged = step.staged_dir
            assert staged is not None
            if _same_device(staged, dest.parent):
                new = staged
            else:
                new = _sibling(dest, "new", run_id)
                _copy_object(staged, new)
                if snapshot(new) != step.desired:
                    raise TransactionError(f"cross-device copy of {staged} failed verification")
            _swap_in(new, dest, run_id)
        elif action == "payload":
            assert step.payload is not None
            tmp = _sibling(dest, "new", run_id)
            _copy_object(step.payload, tmp)
            if snapshot(tmp) != step.desired:
                raise TransactionError(f"stored copy for {dest} failed verification")
            _swap_in(tmp, dest, run_id)
        else:
            raise TransactionError(f"unknown action {action!r}")

        after = snapshot(dest)
        if after != step.desired:
            raise TransactionError(
                f"{dest} is {after.describe()} after replacement, expected "
                f"{step.desired.describe()}"
            )
        self._fault(f"swapped:{entry_id}")
        record["status"] = "done"
        record["after"] = after.to_json()
        self._write_journal()

    def _rollback_current(self) -> None:
        assert self._journal is not None
        errors = self._rollback_journal(self._journal)
        if errors:
            self._write_journal_best_effort(self._journal)
            raise TransactionError("rollback incomplete: " + "; ".join(errors))
        self._journal["status"] = "rolled-back"
        self._write_journal_best_effort(self._journal)

    def _run_steps(self, steps: list[_Step], new_state: Callable[[], dict]) -> None:
        """Back up, journal, replace and commit. Caller handles rollback."""

        assert self._journal is not None
        self._fault("before-backup")
        for step in steps:
            self._backup(step)
        if FSYNC:
            os.sync()
        self._fault("after-backup")
        self._journal["steps"] = [step.record for step in steps]
        self._journal["phase"] = "replace"
        self._write_journal()
        for step in steps:
            self._execute(step)
        self._fault("before-commit")
        state = new_state()
        _write_json(self.target.state_root / "state.json", state)
        self._state = state
        # Commit point passed: from here on nothing may roll back.
        self._journal["status"] = "committed"
        self._journal["finished"] = _utc_now()
        self._write_journal_best_effort(self._journal)

    def _state_payload(self, entries: dict, generation: dict | None, op: str) -> dict:
        target = self.target
        return {
            "schema": SCHEMA,
            "owner_uid": target.uid,
            "roots": {
                "home": str(target.home),
                "data": str(target.data_home),
                "state": str(target.state_home),
                "config": str(target.config_home),
                "cache": str(target.cache_home),
                "repo": str(target.repo_root),
                "compat_link": str(target.compat_link),
            },
            "generation": generation,
            "run_id": self.run_id,
            "op": op,
            "updated": _utc_now(),
            "entries": entries,
        }

    def _guarded(self, body: Callable[[], object]):
        try:
            return body()
        except BaseException as exc:
            if getattr(exc, "skip_rollback", False):
                raise  # simulated crash: leave the journal for recovery
            if self._journal is not None and self._journal.get("status") == "running":
                try:
                    self._rollback_current()
                except TransactionError as rollback_exc:
                    raise rollback_exc from exc
            raise

    # -- apply -----------------------------------------------------------------

    def apply(
        self, desired: list[DesiredEntry], *, generation: dict, force: bool = False
    ) -> ApplyResult:
        """Bring every desired entry into place.

        A ``kind="file"`` entry (a copied config) is written on its first
        install and whenever the live file still equals what the installer
        recorded last time. If it changed since then (``git config --global``,
        an application saving its settings, a hand edit) it is kept and
        listed in ``ApplyResult.kept``; ``force=True`` backs it up and
        overwrites it instead (listed in ``ApplyResult.forced``). A deleted
        copy is simply written again. Symlinks, directories and removals are
        always brought into place (after a backup).
        """

        self._require_entered()
        desired = list(desired)
        self._validate_desired(desired)
        prev_entries: dict = dict((self._state or {}).get("entries", {}))
        desired_ids = {d.id for d in desired}
        dest_to_desired = {Path(d.dest): d.id for d in desired}
        retired_ids = [i for i in prev_entries if i not in desired_ids]

        adopt: dict[str, str] = {}
        retire_dests: list[tuple[str, Path]] = []
        for rid in retired_ids:
            rdest = Path(prev_entries[rid]["dest"])
            if rdest in dest_to_desired:
                adopt[dest_to_desired[rdest]] = rid
            else:
                retire_dests.append((rid, rdest))
        for rid, rdest in retire_dests:
            for d in desired:
                dd = Path(d.dest)
                if _is_within(dd, rdest) or _is_within(rdest, dd):
                    raise TransactionError(
                        f"retired entry {rid!r} at {rdest} overlaps new entry "
                        f"{d.id!r} at {dd}; split this change into two releases"
                    )

        self._begin_journal("apply", {"generation": generation})

        def body() -> ApplyResult:
            befores = {d.id: snapshot(Path(d.dest)) for d in desired}
            wanted: dict[str, ObjectState] = {}
            for d in desired:
                if d.kind == "symlink":
                    wanted[d.id] = ObjectState("symlink", link_text=d.link_text)
                elif d.kind == "file":
                    wanted[d.id] = ObjectState(
                        "file",
                        mode=d.mode if d.mode is not None else 0o644,
                        sha256=hashlib.sha256(d.content or b"").hexdigest(),
                    )
                elif d.kind == "dir":
                    staged_state = snapshot(Path(d.staged_dir))  # type: ignore[arg-type]
                    wanted[d.id] = staged_state
                else:
                    wanted[d.id] = ABSENT

            retired: list[str] = []
            drifted: list[str] = []
            steps: list[_Step] = []
            for rid, rdest in retire_dests:
                installed = ObjectState.from_json(prev_entries[rid]["state"])
                current = snapshot(rdest)
                meta = _baseline_meta(self._paths["baseline"], rid)
                if current != installed or meta is None:
                    drifted.append(rid)
                    continue
                base_state = ObjectState.from_json(meta["state"])
                retired.append(rid)
                if current == base_state:
                    continue
                if base_state.kind == "absent":
                    steps.append(
                        self._new_step("retire", rid, rdest, current, ABSENT, "absent")
                    )
                else:
                    steps.append(
                        self._new_step(
                            "retire",
                            rid,
                            rdest,
                            current,
                            base_state,
                            "payload",
                            payload=self._paths["baseline"] / rid / "object",
                        )
                    )
            self._fault("plan")

            baseline_added = [
                d.id
                for d in desired
                if self._ensure_baseline(d.id, Path(d.dest), befores[d.id], adopt.get(d.id))
            ]

            changed: list[str] = []
            unchanged: list[str] = []
            kept: list[str] = []
            forced: list[str] = []
            kept_records: dict[str, dict] = {}
            for d in desired:
                before = befores[d.id]
                if before == wanted[d.id]:
                    unchanged.append(d.id)
                    continue
                if d.kind == "file" and before.kind != "absent":
                    record = prev_entries.get(d.id) or prev_entries.get(adopt.get(d.id, ""))
                    if record is not None and record.get("dest") == str(d.dest):
                        installed = ObjectState.from_json(record["state"])
                        if before != installed:
                            if not force:
                                kept.append(d.id)
                                kept_records[d.id] = {
                                    "dest": str(d.dest),
                                    "state": installed.to_json(),
                                }
                                continue
                            forced.append(d.id)
                changed.append(d.id)
                action = {"symlink": "symlink", "file": "file", "dir": "dir", "absent": "absent"}[d.kind]
                steps.append(
                    self._new_step(
                        "install",
                        d.id,
                        Path(d.dest),
                        befores[d.id],
                        wanted[d.id],
                        action,
                        content=d.content,
                        staged_dir=Path(d.staged_dir) if d.staged_dir else None,
                    )
                )

            def new_state() -> dict:
                entries = {k: v for k, v in prev_entries.items() if k not in retired_ids}
                for d in desired:
                    # A kept copy keeps the state the installer last wrote,
                    # so it stays "changed locally" until the user resolves it.
                    entries[d.id] = kept_records.get(d.id) or {
                        "dest": str(d.dest),
                        "state": wanted[d.id].to_json(),
                    }
                return self._state_payload(entries, generation, "apply")

            self._run_steps(steps, new_state)
            backup_dir = self._paths["runs"] / self.run_id
            return ApplyResult(
                run_id=self.run_id,
                changed=changed,
                unchanged=unchanged,
                retired=retired,
                drifted=drifted,
                baseline_added=baseline_added,
                backup_dir=backup_dir if steps else None,
                kept=kept,
                forced=forced,
                recovery_saved=list(self.recovery_saved),
            )

        return self._guarded(body)  # type: ignore[return-value]

    # -- restore ---------------------------------------------------------------

    def restore(
        self, which: str, *, force: bool = False, ids: list[str] | None = None
    ) -> RestoreResult:
        self._require_entered()
        entries: dict = dict((self._state or {}).get("entries", {}))
        baseline_root = self._paths["baseline"]

        sources: dict[str, tuple[Path, ObjectState, Path | None]] = {}
        if which == "baseline":
            candidates = list(ids) if ids is not None else sorted(entries)
            for entry_id in candidates:
                meta = _baseline_meta(baseline_root, entry_id)
                if meta is None:
                    raise TransactionError(f"no complete baseline for {entry_id!r}")
                state = ObjectState.from_json(meta["state"])
                payload = baseline_root / entry_id / "object"
                sources[entry_id] = (Path(meta["dest"]), state, payload)
        else:
            if not is_generation_id(which):
                raise TransactionError(f"{which!r} is neither 'baseline' nor a run id")
            journal_path = self._journal_path(which)
            if not journal_path.is_file() or _read_json(journal_path).get("status") != "committed":
                raise TransactionError(f"run {which} did not commit; nothing to restore")
            run_dir = self._paths["runs"] / which
            available = sorted(p.name for p in run_dir.iterdir()) if run_dir.is_dir() else []
            candidates = list(ids) if ids is not None else available
            for entry_id in candidates:
                meta_path = run_dir / entry_id / "meta.json"
                if not meta_path.is_file():
                    raise TransactionError(f"run {which} has no backup for {entry_id!r}")
                meta = _read_json(meta_path)
                state = ObjectState.from_json(meta["state"])
                ref = meta.get("payload")
                if ref == "baseline":
                    payload = baseline_root / entry_id / "object"
                elif ref == "object":
                    payload = run_dir / entry_id / "object"
                else:
                    payload = None
                sources[entry_id] = (Path(meta["dest"]), state, payload)

        for entry_id, (dest, _state, _payload) in sources.items():
            try:
                check_dest_placement(dest, self.target, allow_repo_root=True)
            except ValueError as exc:
                raise TransactionError(f"entry {entry_id!r}: {exc}") from None
        self._check_overlaps([(i, s[0]) for i, s in sources.items()])

        plan: list[tuple[str, Path, ObjectState, ObjectState, Path | None]] = []
        unchanged: list[str] = []
        drifted: list[str] = []
        for entry_id, (dest, state, payload) in sources.items():
            current = snapshot(dest)
            if current == state:
                unchanged.append(entry_id)
                continue
            installed = entries.get(entry_id)
            if installed is None or ObjectState.from_json(installed["state"]) != current:
                drifted.append(entry_id)
            if state.kind != "absent" and (payload is None or not os.path.lexists(payload)):
                raise TransactionError(f"stored copy for {entry_id!r} is missing")
            plan.append((entry_id, dest, current, state, payload))
        if drifted and not force:
            raise TransactionError(
                "these targets changed since the installer last wrote them: "
                + ", ".join(sorted(drifted))
                + "; rerun restore with --force to overwrite them (their current "
                "state is backed up first)"
            )

        self._begin_journal("restore", {"which": which})

        def body() -> RestoreResult:
            self._fault("plan")
            steps: list[_Step] = []
            for entry_id, dest, current, state, payload in plan:
                if state.kind == "absent":
                    steps.append(self._new_step("restore", entry_id, dest, current, ABSENT, "absent"))
                else:
                    steps.append(
                        self._new_step(
                            "restore", entry_id, dest, current, state, "payload", payload=payload
                        )
                    )

            def new_state() -> dict:
                new_entries = dict(entries)
                for entry_id, (dest, state, _payload) in sources.items():
                    if which == "baseline":
                        new_entries.pop(entry_id, None)
                    else:
                        new_entries[entry_id] = {"dest": str(dest), "state": state.to_json()}
                generation = (self._state or {}).get("generation")
                payload = self._state_payload(new_entries, generation, "restore")
                payload["restored_from"] = which
                return payload

            self._run_steps(steps, new_state)
            return RestoreResult(
                run_id=self.run_id,
                which=which,
                restored=[p[0] for p in plan],
                unchanged=unchanged,
                forced=sorted(drifted),
                backup_dir=(self._paths["runs"] / self.run_id) if steps else None,
                recovery_saved=list(self.recovery_saved),
            )

        return self._guarded(body)  # type: ignore[return-value]


def restore(
    target: Target,
    *,
    which: str,
    force: bool = False,
    ids: list[str] | None = None,
    fault: Callable[[str], None] | None = None,
) -> RestoreResult:
    """Restore managed targets to their baseline or to the state a run saved.

    ``which`` is ``"baseline"`` or the id of a committed run; that run's
    backups hold what each target looked like just before the run changed
    it. The current state is backed up as a new run first, so a restore can
    itself be restored.
    """

    with Transaction(target, new_run_id(), fault=fault) as tx:
        return tx.restore(which, force=force, ids=ids)


def load_status(target: Target) -> dict:
    """Read-only summary of the last run and whether each target still matches."""

    state = _read_state(target)
    journal_dir = _layout(target.state_root)["journal"]
    journals: list[dict] = []
    if journal_dir.is_dir():
        for path in sorted(journal_dir.glob("*.json")):
            try:
                journals.append(_read_json(path))
            except TransactionError:
                continue
    journals.sort(key=lambda j: str(j.get("started", "")))
    last = journals[-1] if journals else None

    entries: dict[str, dict] = {}
    drifted: list[str] = []
    for entry_id, entry in sorted(((state or {}).get("entries") or {}).items()):
        installed = ObjectState.from_json(entry["state"])
        try:
            current = snapshot(Path(entry["dest"]))
            matches = current == installed
            current_kind = current.kind
        except (OSError, TransactionError):
            matches, current_kind = False, "unreadable"
        if not matches:
            drifted.append(entry_id)
        entries[entry_id] = {
            "dest": entry["dest"],
            "installed": installed.to_json(),
            "current_kind": current_kind,
            "matches": matches,
        }
    return {
        "schema": SCHEMA,
        "installed": state is not None,
        "run_id": (state or {}).get("run_id"),
        "op": (state or {}).get("op"),
        "generation": (state or {}).get("generation"),
        "entries": entries,
        "drifted": drifted,
        "interrupted": [j.get("run_id") for j in journals if j.get("status") == "running"],
        "last_run": (
            {"run_id": last.get("run_id"), "op": last.get("op"), "status": last.get("status")}
            if last
            else None
        ),
        "backups": str(_layout(target.state_root)["backups"]),
    }


__all__ = [
    "ABSENT",
    "ApplyResult",
    "ConcurrentRunError",
    "DesiredEntry",
    "InstallLock",
    "ObjectState",
    "RestoreResult",
    "Transaction",
    "TransactionError",
    "entries_from_manifest",
    "load_status",
    "new_run_id",
    "restore",
    "skipped_by_condition",
    "snapshot",
]
