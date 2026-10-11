"""Managed-path manifest: load, validate and resolve against a Target.

Everything here is pure checking. A manifest that is malformed, overlaps
itself, points at missing sources, reaches out of the owner's home through a
symlinked parent or tries to take over a broad root (``~/.config`` itself,
``~/.local/share`` ...) is rejected with :class:`ManifestError` before the
transaction touches anything.

Kinds: ``symlink`` / ``link`` put a link in place (always replaced after a
backup), ``remove`` makes sure a path is absent, and ``copy`` installs a
regular file for configs that their programs rewrite. The transaction writes
a copy on its first install and whenever it still equals what was installed
last time; a copy the user or a program changed since is kept and reported
unless the install is forced (see ``installer.transaction``).

Conditions: an entry is installed only where its ``condition`` holds:
``always``, ``linux`` / ``darwin`` (the platform), or ``systemd-user`` (a
Linux machine with systemd user managers). The installer decides which hold
(``install.condition_facts``); this module only validates the names.

Destinations are also compared caselessly (case folding plus Unicode
normalization), as macOS compares names on its default case-insensitive
APFS: two entries that would name one file there, or an entry that would
take over a protected root under another spelling, are rejected on every
platform, so a manifest that passes on Linux also holds on macOS.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path

from .platform import APP_NAME, Target, _is_within, _resolve_existing_prefix

SCHEMA = 1
KINDS = ("symlink", "link", "copy", "remove")
CONDITIONS = ("always", "systemd-user", "linux", "darwin")
COMPAT_ID = "dotfiles-compat"
COMPAT_DEST = "{home}/.dotfiles"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TOKEN_RE = re.compile(r"^\{(home|config|data|state|cache)\}/(.+)$")
_MODE_RE = re.compile(r"^0?[0-7]{3}$")

_TOP_KEYS = {"schema", "entries", "description"}
_ENTRY_KEYS = {
    "id",
    "dest",
    "kind",
    "source",
    "link_to",
    "mode",
    "condition",
    "description",
}

# Lexical spellings that would hand a whole shared directory to one entry.
_BROAD_TOKENS = {
    "{home}/.config",
    "{home}/.local",
    "{home}/.local/share",
    "{home}/.local/state",
    "{home}/.local/bin",
    "{home}/.cache",
    "{home}/.ssh",
    "{home}/.gnupg",
    "{config}/systemd",
    "{config}/systemd/user",
    "{config}/autostart",
    "{data}/applications",
    # macOS: other applications' agents, data and preferences live here; a
    # LaunchAgent plist is managed file by file, never the whole directory.
    "{home}/Library",
    "{home}/Library/LaunchAgents",
    "{home}/Library/Application Support",
    "{home}/Library/Preferences",
}


def caseless_key(text: str) -> str:
    """``text`` as a case- and normalization-insensitive filesystem (macOS's
    default APFS) compares it: Unicode canonical caseless matching
    (NFD, then case folding), kept in NFC. Two names with the same key are
    one file there, although Linux keeps them apart."""

    return unicodedata.normalize("NFC", unicodedata.normalize("NFD", text).casefold())


def _caseless(path: Path) -> Path:
    return Path(caseless_key(str(path)))


_BROAD_KEYS = frozenset(caseless_key(token) for token in _BROAD_TOKENS)


class ManifestError(Exception):
    """The managed-path manifest is unusable; nothing was changed."""


@dataclasses.dataclass(frozen=True)
class ManifestEntry:
    id: str
    dest: str
    kind: str
    source: str | None
    link_to: str | None
    mode: int | None
    condition: str


@dataclasses.dataclass(frozen=True)
class Manifest:
    schema: int
    entries: tuple[ManifestEntry, ...]
    sha256: str


@dataclasses.dataclass(frozen=True)
class ResolvedEntry:
    id: str
    dest: Path
    kind: str
    source: Path | None
    link_text: str | None
    mode: int | None
    condition: str


def token_roots(target: Target) -> dict[str, Path]:
    return {
        "home": target.home,
        "config": target.config_home,
        "data": target.data_home,
        "state": target.state_home,
        "cache": target.cache_home,
    }


def _check_token_path(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise ManifestError(f"{where} must be a string")
    match = _TOKEN_RE.match(value)
    if not match:
        raise ManifestError(
            f"{where}={value!r} must start with {{home}}, {{config}}, {{data}}, "
            "{state} or {cache} followed by '/'"
        )
    segments = match.group(2).split("/")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise ManifestError(
                f"{where}={value!r} contains an empty, '.' or '..' segment"
            )
        if "\0" in segment:
            raise ManifestError(f"{where}={value!r} contains a NUL byte")
    return value


def _check_source(value: object, where: str, *, allow_dot: bool) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{where} must be a non-empty string")
    if value == ".":
        if allow_dot:
            return value
        raise ManifestError(f"{where}='.' is reserved for {COMPAT_ID}")
    if value.startswith("/") or "\0" in value:
        raise ManifestError(f"{where}={value!r} must be a relative repo path")
    for segment in value.split("/"):
        if segment in ("..", "."):
            raise ManifestError(f"{where}={value!r} contains traversal")
    if value.endswith("/") or "//" in value:
        raise ManifestError(f"{where}={value!r} is not a normalized path")
    return value


def _parse_entry(raw: object, index: int) -> ManifestEntry:
    where = f"entries[{index}]"
    if not isinstance(raw, dict):
        raise ManifestError(f"{where} must be an object")
    unknown = set(raw) - _ENTRY_KEYS
    if unknown:
        raise ManifestError(f"{where} has unknown keys: {sorted(unknown)}")
    entry_id = raw.get("id")
    if not isinstance(entry_id, str) or not _ID_RE.match(entry_id):
        raise ManifestError(f"{where}.id={entry_id!r} must match {_ID_RE.pattern}")
    where = f"entry {entry_id!r}"
    kind = raw.get("kind")
    if kind not in KINDS:
        raise ManifestError(f"{where}: unknown kind {kind!r}; expected {KINDS}")
    dest = _check_token_path(raw.get("dest"), f"{where}.dest")
    # Caselessly: '{home}/library' is ~/Library on macOS.
    if caseless_key(dest) in _BROAD_KEYS:
        raise ManifestError(f"{where}: dest {dest!r} is a broad shared root")
    condition = raw.get("condition", "always")
    if condition not in CONDITIONS:
        raise ManifestError(f"{where}: unknown condition {condition!r}")

    source = raw.get("source")
    link_to = raw.get("link_to")
    mode_raw = raw.get("mode")
    mode: int | None = None

    if kind in ("symlink", "copy"):
        source = _check_source(
            source, f"{where}.source", allow_dot=(entry_id == COMPAT_ID)
        )
    elif source is not None:
        raise ManifestError(f"{where}: kind {kind!r} takes no source")
    if kind == "link":
        link_to = _check_token_path(link_to, f"{where}.link_to")
    elif link_to is not None:
        raise ManifestError(f"{where}: only kind 'link' takes link_to")
    if kind == "copy":
        if mode_raw is None:
            mode = 0o644
        elif isinstance(mode_raw, str) and _MODE_RE.match(mode_raw):
            mode = int(mode_raw, 8)
        else:
            raise ManifestError(
                f"{where}: mode must be an octal string like '0644', got {mode_raw!r}"
            )
    elif mode_raw is not None:
        raise ManifestError(f"{where}: only kind 'copy' takes a mode")

    if entry_id == COMPAT_ID:
        if kind != "symlink" or dest != COMPAT_DEST or source != ".":
            raise ManifestError(
                f"{where} must be kind 'symlink', dest {COMPAT_DEST!r}, source '.'"
            )
    return ManifestEntry(entry_id, dest, kind, source, link_to, mode, condition)


def parse_manifest(data: bytes) -> Manifest:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ManifestError("manifest must be a JSON object")
    unknown = set(payload) - _TOP_KEYS
    if unknown:
        raise ManifestError(f"manifest has unknown keys: {sorted(unknown)}")
    if payload.get("schema") != SCHEMA or isinstance(payload.get("schema"), bool):
        raise ManifestError(f"manifest schema must be {SCHEMA}")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ManifestError("manifest entries must be a non-empty list")
    entries = tuple(_parse_entry(raw, i) for i, raw in enumerate(raw_entries))
    seen_ids: set[str] = set()
    seen_dests: dict[str, str] = {}  # caseless key -> dest as written
    for entry in entries:
        if entry.id in seen_ids:
            raise ManifestError(f"duplicate entry id {entry.id!r}")
        key = caseless_key(entry.dest)
        if key in seen_dests:
            if seen_dests[key] == entry.dest:
                raise ManifestError(f"duplicate dest {entry.dest!r}")
            raise ManifestError(
                f"dests {seen_dests[key]!r} and {entry.dest!r} differ only in case "
                "or Unicode normalization; a case-insensitive filesystem (macOS) "
                "treats them as one path"
            )
        seen_ids.add(entry.id)
        seen_dests[key] = entry.dest
    return Manifest(SCHEMA, entries, hashlib.sha256(data).hexdigest())


def load_manifest(path: Path) -> Manifest:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from None
    return parse_manifest(data)


def expand(value: str, target: Target) -> Path:
    match = _TOKEN_RE.match(value)
    if not match:
        raise ManifestError(f"{value!r} is not a token path")
    return token_roots(target)[match.group(1)] / match.group(2)


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or _is_within(a, b) or _is_within(b, a)


def protected_roots(target: Target) -> list[Path]:
    """Paths no managed entry may equal or contain."""

    home = target.home
    config = target.config_home
    data = target.data_home
    return [
        home,
        config,
        data,
        target.state_home,
        target.cache_home,
        home / ".config",
        home / ".local",
        home / ".local" / "share",
        home / ".local" / "state",
        home / ".local" / "bin",
        home / ".cache",
        home / ".ssh",
        home / ".gnupg",
        config / "systemd",
        config / "systemd" / "user",
        config / "autostart",
        data / "applications",
        home / "Library",
        home / "Library" / "LaunchAgents",
        home / "Library" / "Application Support",
        home / "Library" / "Preferences",
    ]


def installer_roots(target: Target) -> list[Path]:
    """Installer-owned trees no manifest entry may reach into or cover."""

    return [
        target.data_home / APP_NAME,
        target.repo_root,
        target.staging_root,
        target.state_root,
    ]


def check_dest_placement(
    dest: Path, target: Target, *, allow_repo_root: bool = False
) -> None:
    """Reject a destination whose placement would escape or clobber roots.

    Raises :class:`ValueError` with a human-readable reason; callers wrap it
    in their own error type. ``allow_repo_root`` admits exactly
    ``target.repo_root`` (the checkout promotion) and nothing else inside the
    installer-owned trees.

    The checks that forbid a place (protected roots, installer-owned trees)
    also match caselessly, because on macOS ``~/library`` *is* ``~/Library``;
    the checks that require a place (inside the home) stay exact, so case
    folding can only ever reject more.
    """

    if not dest.is_absolute():
        raise ValueError(f"{dest} is not absolute")
    if not _is_within(dest, target.home) or dest == target.home:
        raise ValueError(f"{dest} is not strictly inside {target.home}")
    if ".." in dest.parts:
        raise ValueError(f"{dest} contains traversal")
    folded = _caseless(dest)
    for root in protected_roots(target):
        folded_root = _caseless(root)
        if folded == folded_root or _is_within(folded_root, folded):
            raise ValueError(f"{dest} would replace the broad root {root}")
    promoting_repo = allow_repo_root and dest == target.repo_root
    if not promoting_repo:
        for root in installer_roots(target):
            folded_root = _caseless(root)
            if _is_within(folded, folded_root) or _is_within(folded_root, folded):
                raise ValueError(f"{dest} overlaps installer-owned {root}")
    resolved_home = _resolve_existing_prefix(target.home)
    resolved_parent = _resolve_existing_prefix(dest.parent)
    if not _is_within(resolved_parent, resolved_home):
        raise ValueError(
            f"{dest} escapes {target.home} through a symlinked parent "
            f"(parent resolves to {resolved_parent})"
        )
    if promoting_repo:
        return
    forbidden = [_resolve_existing_prefix(r) for r in installer_roots(target)]
    if os.path.lexists(target.compat_link):
        forbidden.append(_resolve_existing_prefix(target.compat_link))
    folded_parent = _caseless(resolved_parent)
    for root in forbidden:
        if _is_within(folded_parent, _caseless(root)):
            raise ValueError(
                f"{dest} is reached through a symlinked parent into {root}"
            )


def resolve(manifest: Manifest, target: Target, repo_root: Path) -> list[ResolvedEntry]:
    """Expand tokens and check every entry against the filesystem.

    ``repo_root`` is the checkout whose files are verified to exist (usually
    the staged checkout); link texts always point through the durable
    ``target.compat_link`` so they survive later promotions.
    """

    repo_root = Path(repo_root)
    try:
        resolved_repo = repo_root.resolve(strict=True)
    except OSError as exc:
        raise ManifestError(f"repository {repo_root} is not accessible: {exc}") from None

    resolved: list[ResolvedEntry] = []
    for entry in manifest.entries:
        dest = expand(entry.dest, target)
        try:
            check_dest_placement(dest, target)
        except ValueError as exc:
            raise ManifestError(f"entry {entry.id!r}: {exc}") from None

        source_path: Path | None = None
        link_text: str | None = None
        if entry.source is not None:
            source_path = repo_root if entry.source == "." else repo_root / entry.source
            if not os.path.lexists(source_path):
                raise ManifestError(
                    f"entry {entry.id!r}: source {entry.source!r} does not exist"
                )
            real = source_path.resolve()
            if real != resolved_repo and not _is_within(real, resolved_repo):
                raise ManifestError(
                    f"entry {entry.id!r}: source {entry.source!r} resolves "
                    "outside the repository"
                )
            if entry.kind == "copy" and (
                source_path.is_symlink() or not source_path.is_file()
            ):
                raise ManifestError(
                    f"entry {entry.id!r}: copy source {entry.source!r} must be a "
                    "regular file"
                )
        if entry.kind == "symlink":
            if entry.id == COMPAT_ID:
                link_text = str(target.repo_root)
            else:
                link_text = str(target.compat_link / entry.source)  # type: ignore[operator]
        elif entry.kind == "link":
            link_text = str(expand(entry.link_to, target))  # type: ignore[arg-type]

        resolved.append(
            ResolvedEntry(
                id=entry.id,
                dest=dest,
                kind=entry.kind,
                source=source_path,
                link_text=link_text,
                mode=entry.mode,
                condition=entry.condition,
            )
        )

    folded = [_caseless(entry.dest) for entry in resolved]
    for i, first in enumerate(resolved):
        for j in range(i + 1, len(resolved)):
            second = resolved[j]
            if _overlaps(first.dest, second.dest):
                raise ManifestError(
                    f"entries {first.id!r} and {second.id!r} have overlapping "
                    f"destinations {first.dest} and {second.dest}"
                )
            # Different tokens can spell one path ({config}/x, {home}/.config/x),
            # so the caseless comparison runs on the expanded paths too.
            if _overlaps(folded[i], folded[j]):
                raise ManifestError(
                    f"entries {first.id!r} and {second.id!r} have overlapping "
                    f"destinations {first.dest} and {second.dest} on a "
                    "case-insensitive filesystem (macOS)"
                )
    return resolved


__all__ = [
    "COMPAT_ID",
    "CONDITIONS",
    "Manifest",
    "ManifestEntry",
    "ManifestError",
    "ResolvedEntry",
    "caseless_key",
    "check_dest_placement",
    "expand",
    "installer_roots",
    "load_manifest",
    "parse_manifest",
    "protected_roots",
    "resolve",
    "token_roots",
]
