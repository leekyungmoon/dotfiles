"""Build the source-only payload used by ``ssh-dotfiles install``.

The remote host may have no network access.  This module therefore packages
the current checkout's zsh sources and the already-installed Antidote plugins,
but only files recorded by Git.  It never walks arbitrary parts of HOME and it
never includes Git metadata or generated plugin state.
"""

from __future__ import annotations

import json
import fnmatch
import os
import posixpath
import re
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


class BundleError(RuntimeError):
    """The local checkout cannot produce a complete, safe offline payload."""


_SOURCE_PATHS = ("zsh", "bin", "config/f-sy-h")
_REQUIRED_SUBMODULES = ("zsh/antidote", "zsh/fasd")
_PLUGIN = re.compile(r"^\s*plugin\s+(['\"])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\1(?:\s|$)")
_DARWIN_IF = re.compile(r"^\s*if\s+.*uname.*Darwin.*;?\s*then\s*$")
_FI = re.compile(r"^\s*fi\s*;?\s*$")
_MACH_O_MAGICS = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}


def _git_files(root: Path, paths: tuple[str, ...] = ()) -> list[str]:
    command = ["git", "-C", str(root), "ls-files", "--recurse-submodules", "-z"]
    if paths:
        command += ["--", *paths]
    try:
        result = subprocess.run(command, check=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace").strip()
        raise BundleError(f"cannot list tracked files in {root}: {detail or exc}") from exc
    return [item.decode("utf-8", "surrogateescape")
            for item in result.stdout.split(b"\0") if item]


def _is_excluded_path(path: PurePosixPath) -> bool:
    # ssh-dotfiles is the client-side transporter.  The server payload lacks
    # its installer modules and Git checkout, so shipping it would create a
    # broken onward-install command.
    if path == PurePosixPath("bin/ssh-dotfiles"):
        return True
    if path.parts[:2] == ("zsh", "tests"):
        return True
    name = path.name
    return (fnmatch.fnmatch(name, "*.local") or
            fnmatch.fnmatch(name, "*.local.zsh") or
            any(part.startswith(".git") or part == "__pycache__" or
                part.endswith(".zwc") for part in path.parts))


def _plugin_specs(path: Path) -> tuple[list[str], dict[str, list[str]]]:
    """Read the repository's deliberately simple ``plugin 'owner/repo'`` DSL."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BundleError(f"cannot read plugin specification {path}: {exc}") from exc

    common: list[str] = []
    platform = {"Darwin": []}
    darwin_depth = 0
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _DARWIN_IF.match(line):
            darwin_depth += 1
            continue
        if darwin_depth and _FI.match(line):
            darwin_depth -= 1
            continue
        match = _PLUGIN.match(line)
        if match:
            destination = platform["Darwin"] if darwin_depth else common
            destination.append(match.group(2))
            continue
        if re.match(r"^\s*plugin(?:\s|$)", line):
            raise BundleError(f"unsupported plugin declaration at {path}:{number}")
    if darwin_depth:
        raise BundleError(f"unterminated Darwin plugin block in {path}")
    if not common:
        raise BundleError(f"no plugins found in {path}")
    return (list(dict.fromkeys(common)),
            {name: list(dict.fromkeys(specs))
             for name, specs in platform.items() if specs})


def _validate_submodules(repo: Path) -> None:
    for relative in _REQUIRED_SUBMODULES:
        root = repo / relative
        if not (root / ".git").exists():
            raise BundleError(
                f"required submodule {relative} is not initialized; run "
                "git submodule update --init --recursive"
            )
        if not _git_files(root):
            raise BundleError(f"required submodule {relative} has no tracked files")


def _safe_link(arcname: PurePosixPath, target: str) -> None:
    target_path = PurePosixPath(target)
    if target_path.is_absolute():
        raise BundleError(f"refusing absolute symlink {arcname} -> {target}")
    resolved = posixpath.normpath(str(arcname.parent / target_path))
    if resolved == ".." or resolved.startswith("../"):
        raise BundleError(f"refusing escaping symlink {arcname} -> {target}")


def _native_binary_kind(source: Path) -> str | None:
    with source.open("rb") as stream:
        header = stream.read(64)
        if header.startswith(b"\x7fELF"):
            return "ELF"
        if header[:4] in _MACH_O_MAGICS:
            return "Mach-O"
        if header.startswith(b"MZ") and len(header) >= 64:
            pe_offset = int.from_bytes(header[60:64], "little")
            stream.seek(pe_offset)
            if stream.read(4) == b"PE\0\0":
                return "PE"
    return None


def _add_file(archive: tarfile.TarFile, source: Path,
              arcname: PurePosixPath) -> None:
    try:
        info = source.lstat()
    except OSError as exc:
        raise BundleError(f"tracked file is missing: {source}") from exc

    tarinfo = tarfile.TarInfo(str(arcname))
    tarinfo.uid = tarinfo.gid = 0
    tarinfo.uname = tarinfo.gname = ""
    tarinfo.mtime = 0
    tarinfo.mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(source)
        _safe_link(arcname, target)
        tarinfo.type = tarfile.SYMTYPE
        tarinfo.linkname = target
        archive.addfile(tarinfo)
    elif stat.S_ISREG(info.st_mode):
        native_kind = _native_binary_kind(source)
        if native_kind:
            raise BundleError(
                f"refusing tracked {native_kind} binary in portable payload: {source}"
            )
        tarinfo.size = info.st_size
        with source.open("rb") as stream:
            archive.addfile(tarinfo, stream)
    else:
        raise BundleError(f"refusing non-file tracked entry: {source}")


def _add_bytes(archive: tarfile.TarFile, data: bytes,
               arcname: str) -> None:
    import io

    info = tarfile.TarInfo(arcname)
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    info.mode = 0o644
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def build_bundle(repo: Path, plugin_root: Path, output: Path) -> None:
    """Write a complete offline ssh-zsh payload to ``output`` atomically.

    ``plugin_root`` contains Antidote checkouts as ``owner/repository``.
    Sources are read from the current worktree while inclusion is governed by
    the Git indexes, allowing a new feature to be tested before it is committed.
    """
    repo = Path(repo).resolve()
    plugin_root = Path(plugin_root).resolve()
    output = Path(output)
    _validate_submodules(repo)

    source_files = _git_files(repo, _SOURCE_PATHS)
    source_files = [path for path in source_files
                    if not _is_excluded_path(PurePosixPath(path))]
    common_specs, platform_specs = _plugin_specs(repo / "zsh" / "plugins.zsh")
    specs = list(common_specs)
    # Platform-specific plugins are optional in the local cache.  Their full
    # requirement remains in bundle.json, so the remote preflight can refuse
    # activation on that platform rather than silently losing functionality.
    for platform_group in platform_specs.values():
        for spec in platform_group:
            candidate = plugin_root.joinpath(*spec.split("/"))
            if (candidate / ".git").exists() and spec not in specs:
                specs.append(spec)
    plugin_files: dict[str, list[str]] = {}
    for spec in specs:
        root = plugin_root.joinpath(*spec.split("/"))
        if not (root / ".git").exists():
            raise BundleError(
                f"required offline plugin {spec} is missing from {plugin_root}"
            )
        files = [path for path in _git_files(root)
                 if not _is_excluded_path(PurePosixPath(path))]
        if not files:
            raise BundleError(f"required offline plugin {spec} has no tracked files")
        plugin_files[spec] = files

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.",
                                          suffix=".part", dir=output.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for relative in source_files:
                _add_file(archive, repo / relative,
                          PurePosixPath("src") / PurePosixPath(relative))
            for spec, files in plugin_files.items():
                root = plugin_root.joinpath(*spec.split("/"))
                for relative in files:
                    _add_file(archive, root / relative,
                              PurePosixPath("plugins") / spec /
                              PurePosixPath(relative))
            metadata = json.dumps({
                "schema": 1,
                "plugins": specs,
                "required_plugins": common_specs,
                "platform_plugins": platform_specs,
            }, sort_keys=True, separators=(",", ":"))
            _add_bytes(archive, (metadata + "\n").encode(), "bundle.json")
        os.replace(temporary, output)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
