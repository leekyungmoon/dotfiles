"""Packages phase: apt profile, pinned tool artifacts and the nvim venv.

apt work is a *system* phase: it goes through sudo, is not recorded by the
dotfiles transaction and is not rolled back by ``restore``. Before any package
is installed, the exact install is simulated with ``apt-get --simulate`` and
the whole resolved closure is checked against the manifest's forbidden
patterns; a match aborts before any package changes.

Tool artifacts from ``manifests/tools.json`` are downloaded to a private
temporary directory, verified (digest, checksum file, and signature where the
publisher signs), extracted into a versioned prefix under the installer-owned
tools root and exposed through links in the installer-owned bin directory.
Links that must also work outside zsh (``~/.local/bin``) are *returned* in
``details["links"]`` for the transaction to own; this module never writes
into ``~/.local/bin`` itself, except for the Claude Code launcher, which the
verified vendor binary creates.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Callable, Iterable

from installer import downloads
from installer.downloads import DownloadError, VerificationError
from installer.platform import PlatformError, require_supported_platform

PHASE = "packages"
PASS = "PASS"
FAIL = "FAIL"
SKIPPED = "SKIPPED"

MANIFEST_DIR = Path(__file__).resolve().parents[1] / "manifests"
MARKER_NAME = ".personal-dotfiles-verified.json"
MARKER_SCHEMA = 1

APT_UPDATE_TIMEOUT = 900.0
APT_INSTALL_TIMEOUT = 3600.0
APT_SIMULATE_TIMEOUT = 300.0
QUERY_TIMEOUT = 60.0
SUDO_PROMPT_TIMEOUT = 300.0
DOWNLOAD_TIMEOUT = 300.0
VERIFY_TIMEOUT = 120.0
NPM_TIMEOUT = 1200.0
VENDOR_INSTALL_TIMEOUT = 900.0
PIP_TIMEOUT = 900.0

DEFAULT_GROUPS = ("shell-core", "gui")
SYSTEM_PATH = "/usr/bin:/bin"

FetchFn = Callable[..., object]


class PackagesError(Exception):
    """A step failed; the message is the actionable reason."""


# --- manifests and placeholders -----------------------------------------------

def load_manifests(manifest_dir: Path | None = None) -> tuple[dict, dict]:
    root = Path(manifest_dir) if manifest_dir is not None else MANIFEST_DIR
    packages = json.loads((root / "packages.json").read_text(encoding="utf-8"))
    tools = json.loads((root / "tools.json").read_text(encoding="utf-8"))
    return packages, tools


_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def expand(value: str, mapping: dict[str, str]) -> str:
    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in mapping:
            raise PackagesError(f"unresolved placeholder {{{key}}} in {value!r}")
        return str(mapping[key])

    return _PLACEHOLDER.sub(replace, value)


class _Ctx:
    def __init__(self, target, platform, runner, fetch, dry_run, tools_manifest, log):
        self.target = target
        self.platform = platform
        self.runner = runner
        self.fetch = fetch
        self.dry_run = dry_run
        self.log = log
        self.base = {
            "home": str(target.home),
            "data_home": str(target.data_home),
            "codex_home": os.environ.get("CODEX_HOME") or str(target.home / ".codex"),
        }
        self.prefix_root = Path(expand(tools_manifest["prefix_root"], self.base))
        self.bin_dir = Path(expand(tools_manifest["bin_dir"], self.base))
        self.base.update(prefix_root=str(self.prefix_root), bin_dir=str(self.bin_dir))
        self.work_root = target.cache_home / "personal-dotfiles" / "downloads"

    def env(self, path: str | None = None, **extra: str) -> dict[str, str]:
        t = self.target
        env = {
            "HOME": str(t.home),
            "USER": t.username,
            "LOGNAME": t.username,
            "PATH": path or f"{self.bin_dir}:{SYSTEM_PATH}",
            "LANG": "C.UTF-8",
            "XDG_DATA_HOME": str(t.data_home),
            "XDG_STATE_HOME": str(t.state_home),
            "XDG_CONFIG_HOME": str(t.config_home),
            "XDG_CACHE_HOME": str(t.cache_home),
        }
        if os.environ.get("TMPDIR"):
            env["TMPDIR"] = os.environ["TMPDIR"]
        env.update(extra)
        return env

    def workdir(self) -> tempfile.TemporaryDirectory:
        self.work_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.work_root, 0o700)
        return tempfile.TemporaryDirectory(prefix="dl-", dir=self.work_root)

    def download(self, url: str, dest: Path) -> Path:
        self.log(f"packages: downloading {url}")
        self.fetch(url, dest, timeout=DOWNLOAD_TIMEOUT)
        if not Path(dest).is_file():
            raise DownloadError(f"{url}: download produced no file")
        return Path(dest)


def _out(completed) -> str:
    data = completed.stdout or b""
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)


def _err_tail(completed, limit: int = 400) -> str:
    data = completed.stderr or b""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
    return text.strip()[-limit:]


def _run(ctx: _Ctx, argv, *, timeout: float, read_only: bool = False, env=None, cwd=None):
    """Run with check=False; runner exceptions become PackagesError."""

    try:
        return ctx.runner.run([str(a) for a in argv], timeout=timeout, check=False,
                              env=env, cwd=cwd, read_only=read_only)
    except Exception as exc:  # RunnerError: timeout or cannot execute
        raise PackagesError(f"{Path(str(argv[0])).name}: {exc}") from None


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- apt ---------------------------------------------------------------------

def query_installed(ctx: _Ctx, names: Iterable[str]) -> dict[str, str]:
    names = list(names)
    if not names:
        return {}
    completed = _run(ctx, ["dpkg-query", "-W", "-f=${Package}\t${db:Status-Abbrev}\t${Version}\n",
                           *names], timeout=QUERY_TIMEOUT, read_only=True)
    installed: dict[str, str] = {}
    for line in _out(completed).splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[1].startswith("ii"):
            installed[parts[0].split(":")[0]] = parts[2]
    return installed


def parse_simulation(text: str) -> tuple[list[str], list[str]]:
    installs: list[str] = []
    removals: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^(Inst|Remv|Purg) (\S+)", line)
        if not match:
            continue
        name = match.group(2).split(":")[0]
        (installs if match.group(1) == "Inst" else removals).append(name)
    return installs, removals


def forbidden_matches(names: Iterable[str], patterns: Iterable[str]) -> list[str]:
    compiled = [re.compile(p) for p in patterns]
    return sorted({n for n in names for c in compiled if c.search(n)})


def _sudo_ready(ctx: _Ctx) -> tuple[bool, str]:
    if ctx.runner.which("sudo") is None:
        return False, "sudo is not installed"
    try:
        probe = _run(ctx, ["sudo", "-n", "true"], timeout=30, read_only=True)
    except PackagesError as exc:
        return False, str(exc)
    if probe.returncode == 0:
        return True, "cached"
    if ctx.dry_run:
        return False, "sudo needs a password (it will prompt during a real run)"
    ctx.log("packages: sudo is needed for apt; enter your password if prompted")
    try:
        prompt = _run(ctx, ["sudo", "-v"], timeout=SUDO_PROMPT_TIMEOUT)
    except PackagesError as exc:
        return False, str(exc)
    if prompt.returncode == 0:
        return True, "prompted"
    return False, f"sudo was refused: {_err_tail(prompt) or prompt.returncode}"


def _sudo_hint(missing: list[str]) -> str:
    return ("run 'sudo -v' in this terminal and rerun, or have an administrator run: "
            f"sudo apt-get install --yes --no-install-recommends {' '.join(missing)}")


def _deb822_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith((" ", "#")):
            key, _, value = line.partition(":")
            fields[key.strip().lower()] = value.strip()
    return fields


def render_sources(repo: dict, architecture: str) -> bytes:
    lines = [f"{k}: {v.replace('{architecture}', architecture)}"
             for k, v in repo["sources_deb822"].items()]
    return ("\n".join(lines) + "\n").encode("utf-8")


def chrome_repo_configured(repo: dict) -> tuple[bool, str]:
    keyring = Path(repo["keyring_path"])
    sources = Path(repo["sources_path"])
    fpr = downloads.normalize_fingerprint(repo["key_primary_fingerprint"])
    try:
        data = keyring.read_bytes()
        if downloads.is_armored(data):
            data = downloads.dearmor(data)
        if fpr not in downloads.primary_fingerprints(data):
            return False, f"{keyring} does not hold the pinned key"
    except (OSError, VerificationError) as exc:
        return False, f"keyring unusable: {exc}"
    try:
        fields = _deb822_fields(sources.read_text(encoding="utf-8"))
    except OSError:
        return False, f"{sources} missing"
    want_uri = repo["sources_deb822"]["URIs"].rstrip("/")
    uris = [u.rstrip("/") for u in fields.get("uris", "").split()]
    if want_uri not in uris or fields.get("signed-by") != str(keyring):
        return False, f"{sources} does not match the pinned source"
    return True, "configured"


def setup_chrome_repo(ctx: _Ctx, repo: dict) -> None:
    with ctx.workdir() as tmp:
        tmp = Path(tmp)
        key = ctx.download(repo["key_url"], tmp / "key.asc")
        keyring = downloads.write_keyring(tmp / "google-chrome.gpg", key.read_bytes(),
                                          [repo["key_primary_fingerprint"]])
        sources = tmp / "google-chrome.sources"
        sources.write_bytes(render_sources(repo, ctx.platform.architecture))
        os.chmod(sources, 0o644)
        for src, dest in ((keyring, repo["keyring_path"]), (sources, repo["sources_path"])):
            done = _run(ctx, ["sudo", "install", "-m", "0644", "-o", "root", "-g", "root",
                              str(src), dest], timeout=60)
            if done.returncode != 0:
                raise PackagesError(f"cannot write {dest}: {_err_tail(done)}")


def _locale_present(ctx: _Ctx, step: dict) -> bool:
    try:
        completed = _run(ctx, step["check"], timeout=QUERY_TIMEOUT, read_only=True)
    except PackagesError:
        return False
    pattern = re.compile(step["present_if_output_matches"], re.M)
    return completed.returncode == 0 and bool(pattern.search(_out(completed)))


def apt_step(ctx: _Ctx, manifest: dict, groups: list[str]) -> dict:
    """Return {"status", "reasons", ...details} for the apt part."""

    policy = manifest["apt_policy"]
    install_cmd = list(policy["install_command"])
    simulate_cmd = list(policy["simulate_command"])
    if policy.get("install_recommends") is not False or \
            "--no-install-recommends" not in install_cmd or \
            "--no-install-recommends" not in simulate_cmd:
        raise PackagesError("packages.json apt_policy must use --no-install-recommends")

    wanted: list[str] = []
    repo_of: dict[str, str] = {}
    for group in manifest["groups"]:
        if group["id"] in groups:
            for pkg in group["packages"]:
                if pkg["name"] not in wanted:
                    wanted.append(pkg["name"])
                if pkg.get("repository"):
                    repo_of[pkg["name"]] = pkg["repository"]
    info: dict = {
        "transactional": False,
        "note": "System phase through sudo; not recorded by the dotfiles transaction "
                "and not undone by restore.",
        "groups": groups,
        "requested": wanted,
    }
    reasons: list[str] = []
    installed = query_installed(ctx, wanted)
    missing = [p for p in wanted if p not in installed]
    info["already_installed"] = {p: installed[p] for p in wanted if p in installed}
    info["missing"] = missing

    locale_steps = [s for s in manifest.get("system_steps", [])
                    if s.get("id") == "locale-en-us-utf8"] if "shell-core" in groups else []
    locale_needed = [s for s in locale_steps if not _locale_present(ctx, s)]

    repos_needed: list[tuple[str, dict]] = []
    for name in missing:
        repo_id = repo_of.get(name)
        if repo_id:
            repo = manifest["repositories"][repo_id]
            if ctx.platform.architecture not in repo.get("package_architectures", []):
                raise PackagesError(f"{name} is not published for {ctx.platform.architecture}")
            ok, why = chrome_repo_configured(repo)
            info.setdefault("repositories", {})[repo_id] = why
            if not ok:
                repos_needed.append((repo_id, repo))

    if missing or locale_needed:
        ok, why = _sudo_ready(ctx)
        info["sudo"] = why
        if not ok and not ctx.dry_run:
            reasons.append(f"apt: {why}; {_sudo_hint(missing or ['locales'])}")
            info["status"] = FAIL
            info["reasons"] = reasons
            return info
        if not ok:
            reasons.append(f"apt: {why}")

    if ctx.dry_run:
        unsimulated = [n for n in missing if repo_of.get(n) in {r for r, _ in repos_needed}]
        sim_list = [n for n in missing if n not in unsimulated]
        info["plan"] = []
        for repo_id, repo in repos_needed:
            info["plan"].append(f"write {repo['keyring_path']} (key {repo['key_primary_fingerprint']}) "
                                f"and {repo['sources_path']}")
        if missing:
            info["plan"].append("sudo apt-get update")
            info["plan"].append(" ".join(["sudo", *install_cmd, *missing]))
        if unsimulated:
            info["unsimulated"] = unsimulated
            reasons.append(f"apt: closure of {', '.join(unsimulated)} not simulated "
                           "(its repository is not configured yet)")
        if sim_list:
            sim = _run(ctx, [*simulate_cmd, *sim_list], timeout=APT_SIMULATE_TIMEOUT,
                       read_only=True)
            if sim.returncode != 0:
                reasons.append(f"apt simulation failed: {_err_tail(sim)}")
                info.update(status=FAIL, reasons=reasons)
                return info
            installs, removals = parse_simulation(_out(sim))
            hits = forbidden_matches(installs, manifest["forbidden"]["patterns"])
            info["closure"] = {"count": len(installs), "packages": installs,
                               "removals": removals, "forbidden": hits}
            if hits or removals:
                reasons.append(_closure_reason(hits, removals))
                info.update(status=FAIL, reasons=reasons)
                return info
        for step in locale_needed:
            info["plan"].append("sudo " + " ".join(step["command"]))
        info.update(status=SKIPPED, reasons=reasons or ["dry-run: plan only"])
        return info

    if missing:
        for repo_id, repo in repos_needed:
            ctx.log(f"packages: configuring the {repo_id} apt repository")
            setup_chrome_repo(ctx, repo)
            info.setdefault("repositories", {})[repo_id] = "written"
        ctx.log("packages: apt-get update")
        upd = _run(ctx, ["sudo", "apt-get", "update"], timeout=APT_UPDATE_TIMEOUT)
        if upd.returncode != 0:
            raise PackagesError(f"apt-get update failed: {_err_tail(upd)}")
        sim = _run(ctx, [*simulate_cmd, *missing], timeout=APT_SIMULATE_TIMEOUT, read_only=True)
        if sim.returncode != 0:
            raise PackagesError(f"apt simulation failed: {_err_tail(sim)}")
        installs, removals = parse_simulation(_out(sim))
        hits = forbidden_matches(installs, manifest["forbidden"]["patterns"])
        info["closure"] = {"count": len(installs), "packages": installs,
                           "removals": removals, "forbidden": hits}
        if hits or removals:
            reasons.append(_closure_reason(hits, removals) + "; nothing was installed")
            info.update(status=FAIL, reasons=reasons)
            return info
        ctx.log(f"packages: installing {len(missing)} apt packages ({len(installs)} with dependencies)")
        done = _run(ctx, ["sudo", "env", "DEBIAN_FRONTEND=noninteractive", *install_cmd,
                          "-o", "Dpkg::Options::=--force-confdef",
                          "-o", "Dpkg::Options::=--force-confold", *missing],
                    timeout=APT_INSTALL_TIMEOUT)
        if done.returncode != 0:
            raise PackagesError(f"apt-get install failed: {_err_tail(done)}")

    for step in locale_needed:
        done = _run(ctx, ["sudo", *step["command"]], timeout=300)
        if done.returncode != 0 or not _locale_present(ctx, step):
            reasons.append(f"locale step {step['id']} failed: {_err_tail(done)}")
    info["locale"] = "generated" if locale_needed else "present"

    versions = query_installed(ctx, wanted)
    info["versions"] = versions
    still = [p for p in wanted if p not in versions]
    if still:
        reasons.append(f"apt: still not installed after install: {', '.join(still)}")
    info.update(status=FAIL if reasons else PASS, reasons=reasons)
    return info


def _closure_reason(hits: list[str], removals: list[str]) -> str:
    parts = []
    if hits:
        parts.append(f"forbidden packages in the apt closure: {', '.join(hits)}")
    if removals:
        parts.append(f"apt would remove packages: {', '.join(removals)}")
    return "apt: " + "; ".join(parts)


def shim_step(ctx: _Ctx, manifest: dict, groups: list[str]) -> dict:
    results: dict[str, str] = {}
    if "shell-core" not in groups:
        return results
    other_path = os.pathsep.join(p for p in os.environ.get("PATH", SYSTEM_PATH).split(os.pathsep)
                                 if p and Path(p) != ctx.bin_dir)
    for shim in manifest.get("shims", []):
        name, target = shim["name"], shim["target"]
        link = ctx.bin_dir / name
        if shim.get("only_if_missing") and shutil.which(name, path=other_path):
            results[name] = "not needed (already on PATH)"
            continue
        if not os.path.exists(target):
            results[name] = f"skipped ({target} not installed)"
            continue
        if ctx.dry_run:
            results[name] = f"plan: {link} -> {target}"
            continue
        switch_link(link, Path(target))
        results[name] = f"{link} -> {target}"
    return results


# --- tool installation -------------------------------------------------------

def switch_link(link: Path, target: Path) -> bool:
    """Point ``link`` at ``target`` atomically; refuse to replace a non-link."""

    link.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(link):
        if not link.is_symlink():
            raise PackagesError(f"{link} exists and is not a symlink; refusing to replace it")
        if os.readlink(link) == str(target):
            return False
    tmp = link.parent / f".{link.name}.tmp-{secrets.token_hex(4)}"
    os.symlink(str(target), tmp)
    os.replace(tmp, link)
    return True


def safe_extract(archive: Path, dest: Path, *, kind: str, strip: int) -> None:
    modes = {"tar.gz": "r:gz", "tar.xz": "r:xz", "tgz": "r:gz"}
    if kind not in modes:
        raise PackagesError(f"unsupported archive format {kind!r}")
    dest.mkdir(parents=True)
    root = dest.resolve()
    with tarfile.open(archive, modes[kind]) as tar:
        for member in tar:
            raw = member.name
            parts = [p for p in raw.split("/") if p not in ("", ".")]
            if raw.startswith("/") or ".." in parts:
                raise VerificationError(f"unsafe archive member {raw!r}")
            parts = parts[strip:]
            if not parts:
                continue
            out = dest.joinpath(*parts)
            if not _inside(out.parent, root):
                raise VerificationError(f"archive member {raw!r} escapes the prefix")
            if os.path.lexists(out) and not (member.isdir() and out.is_dir() and not out.is_symlink()):
                raise VerificationError(f"archive member {raw!r} overwrites an existing path")
            if member.isdir():
                out.mkdir(parents=True, exist_ok=True)
                os.chmod(out, 0o755)
            elif member.isreg():
                out.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                assert source is not None
                with source, open(out, "xb") as handle:
                    shutil.copyfileobj(source, handle, 1024 * 1024)
                os.chmod(out, (member.mode & 0o755) | 0o600)
            elif member.issym():
                link = member.linkname
                rel = os.path.normpath(os.path.join(os.path.dirname("/".join(parts)), link))
                if os.path.isabs(link) or rel == ".." or rel.startswith("../"):
                    raise VerificationError(f"archive symlink {raw!r} -> {link!r} escapes")
                out.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(link, out)
            elif member.islnk():
                lparts = [p for p in member.linkname.split("/") if p not in ("", ".")]
                if member.linkname.startswith("/") or ".." in lparts or len(lparts) <= strip:
                    raise VerificationError(f"unsafe archive hardlink {raw!r}")
                source_path = dest.joinpath(*lparts[strip:])
                if not source_path.is_file() or source_path.is_symlink() \
                        or not _inside(source_path, root):
                    raise VerificationError(f"archive hardlink {raw!r} has no target")
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, out)
            else:
                raise VerificationError(f"unsupported archive member type {raw!r}")


def _inside(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    return resolved == root or root in resolved.parents


def _read_marker(prefix: Path) -> dict | None:
    try:
        data = json.loads((prefix / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _marker_valid(prefix: Path, tool_id: str, version: str, sha256: str) -> bool:
    marker = _read_marker(prefix) if prefix.is_dir() and not prefix.is_symlink() else None
    return bool(marker) and marker.get("schema") == MARKER_SCHEMA and \
        marker.get("tool") == tool_id and marker.get("version") == version and \
        marker.get("sha256") == sha256


def _write_marker(prefix: Path, tool_id: str, version: str, artifact: dict, extra: dict) -> None:
    payload = {"schema": MARKER_SCHEMA, "tool": tool_id, "version": version,
               "url": artifact["url"], "sha256": artifact["sha256"], **extra}
    _atomic_write(prefix / MARKER_NAME, (json.dumps(payload, indent=2, sort_keys=True) + "\n")
                  .encode("utf-8"), 0o644)


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        for dirpath, dirnames, _ in os.walk(path):
            for name in dirnames:
                full = os.path.join(dirpath, name)
                if not os.path.islink(full):
                    os.chmod(full, 0o755)
        shutil.rmtree(path)


def check_integrity(ctx: _Ctx, spec: dict, artifact: dict, work: Path) -> str:
    """Cross-check the pinned artifact digest against the publisher's source."""

    source = spec.get("integrity", {}).get("checksum_source", {})
    kind = source.get("type")
    if kind in ("signed-checksum-file", "checksum-file"):
        sums = ctx.download(source["url"], work / ("sums-" + Path(source["url"]).name))
        if source.get("sha256"):
            downloads.verify_sha256(sums, source["sha256"])
        if kind == "signed-checksum-file":
            _verify_signature(ctx, spec["integrity"], source["signature_url"], sums, work)
        entries = downloads.parse_checksum_file(sums.read_text(encoding="utf-8", errors="replace"))
        name = Path(artifact["url"]).name
        if entries.get(name) != artifact["sha256"].lower():
            raise VerificationError(f"{name}: checksum file entry does not match the pinned sha256")
        return f"{kind} ok"
    if kind in ("github-release-asset-digest", "npm-registry-integrity"):
        return f"{kind} (pinned digest)"
    raise VerificationError(f"unknown integrity source {kind!r}")


def _verify_signature(ctx: _Ctx, integrity: dict, signature_url: str, data: Path, work: Path) -> str:
    fingerprints = integrity["signer_fingerprints"]
    key = ctx.download(integrity["signer_key_url"], work / "signer-key.asc")
    keyring = downloads.write_keyring(work / "signer.gpg", key.read_bytes(), fingerprints)
    sig = ctx.download(signature_url, work / (data.name + ".sig"))
    gpgv = ctx.runner.which("gpgv")
    if gpgv is None:
        raise VerificationError("gpgv is not installed; cannot verify the signature")
    return downloads.verify_detached(ctx.runner, keyring, sig, data, fingerprints, gpgv=gpgv)


def _verify_commands(ctx: _Ctx, spec: dict, mapping: dict[str, str], *, substitute: dict[str, Path],
                     env: dict[str, str]) -> list[str]:
    outputs: list[str] = []
    for check in spec.get("verify", []):
        argv = [expand(a, mapping) for a in check["argv"]]
        if argv[0] in substitute:
            argv[0] = str(substitute[argv[0]])
        completed = _run(ctx, argv, timeout=VERIFY_TIMEOUT, read_only=True, env=env)
        text = _out(completed).strip()
        if completed.returncode != 0 or not re.search(check["stdout"], text, re.M):
            raise PackagesError(f"verify {' '.join(check['argv'])!s} failed "
                                f"(status {completed.returncode}): {text[:200] or _err_tail(completed)}")
        outputs.append(text.splitlines()[0] if text else "")
    return outputs


def _select_artifact(spec: dict, architecture: str) -> dict:
    artifacts = spec["artifacts"]
    artifact = artifacts.get(architecture) or artifacts.get("any")
    if artifact is None:
        raise PackagesError(f"no artifact for architecture {architecture!r}")
    return artifact


def _mapping(ctx: _Ctx, spec: dict, artifact: dict) -> dict[str, str]:
    mapping = dict(ctx.base, version=spec["version"])
    if "target" in artifact:
        mapping["target"] = artifact["target"]
    mapping["prefix"] = expand(spec["prefix"], mapping) if "prefix" in spec else ""
    return mapping


def install_extract(ctx: _Ctx, tool_id: str, spec: dict) -> dict:
    artifact = _select_artifact(spec, ctx.platform.architecture)
    mapping = _mapping(ctx, spec, artifact)
    prefix = Path(mapping["prefix"])
    links = {name: prefix / rel for name, rel in spec.get("links", {}).items()}
    result = {"version": spec["version"], "prefix": str(prefix), "url": artifact["url"]}
    fresh = not _marker_valid(prefix, tool_id, spec["version"], artifact["sha256"])
    result["downloaded"] = fresh and not ctx.dry_run
    if ctx.dry_run:
        result.update(status=SKIPPED, plan="download+verify+extract" if fresh else "present (verified marker)")
        return result
    if fresh:
        with ctx.workdir() as work:
            work = Path(work)
            result["integrity"] = check_integrity(ctx, spec, artifact, work)
            archive = ctx.download(artifact["url"], work / Path(artifact["url"]).name)
            downloads.verify_sha256(archive, artifact["sha256"], size=artifact.get("size"))
            extras: list[tuple[Path, dict]] = []
            for name, extra in spec.get("extra_files", {}).items():
                path = ctx.download(extra["url"], work / f"extra-{name}")
                downloads.verify_sha256(path, extra["sha256"])
                extras.append((path, extra))
            prefix.parent.mkdir(parents=True, exist_ok=True)
            stage = prefix.parent / f".staging-{prefix.name}-{secrets.token_hex(4)}"
            try:
                safe_extract(archive, stage, kind=artifact["archive"],
                             strip=int(artifact.get("strip_components", 0)))
                for path, extra in extras:
                    rel = extra["dest"]
                    if rel.startswith("/") or ".." in rel.split("/"):
                        raise VerificationError(f"extra file {rel} escapes the prefix")
                    dest = stage / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if not _inside(dest.parent, stage.resolve()) or os.path.lexists(dest):
                        raise VerificationError(f"extra file {rel} collides or escapes")
                    shutil.copyfile(path, dest)
                    os.chmod(dest, int(extra.get("mode", "0644"), 8))
                if os.path.lexists(prefix):
                    _remove_tree(prefix)
                os.rename(stage, prefix)
            finally:
                if os.path.lexists(stage):
                    _remove_tree(stage)
    for name, path in links.items():
        if not os.path.lexists(path):
            raise PackagesError(f"{tool_id}: {path} missing after extraction")
    post = spec.get("install", {}).get("post")
    if post:
        argv = [expand(a, mapping) for a in post]
        if ctx.runner.which(argv[0]) is None:
            raise PackagesError(f"{tool_id}: {argv[0]} is not installed")
        done = _run(ctx, argv, timeout=VERIFY_TIMEOUT, env=ctx.env())
        if done.returncode != 0:
            raise PackagesError(f"{tool_id}: {' '.join(argv)} failed: {_err_tail(done)}")
    substitute = {str(ctx.bin_dir / name): path for name, path in links.items()}
    env = ctx.env(path=f"{prefix / 'bin'}:{ctx.bin_dir}:{SYSTEM_PATH}")
    try:
        result["verify"] = _verify_commands(ctx, spec, mapping, substitute=substitute, env=env)
    except PackagesError:
        if fresh:
            _remove_tree(prefix)
        raise
    if fresh:
        _write_marker(prefix, tool_id, spec["version"], artifact, {})
    for name, path in links.items():
        switch_link(ctx.bin_dir / name, path)
    result["links"] = sorted(links)
    result["status"] = PASS
    return result


def install_npm(ctx: _Ctx, tool_id: str, spec: dict, tools: dict) -> dict:
    artifact = _select_artifact(spec, ctx.platform.architecture)
    mapping = _mapping(ctx, spec, artifact)
    prefix = Path(mapping["prefix"])
    links = {name: prefix / rel for name, rel in spec.get("links", {}).items()}
    result = {"version": spec["version"], "prefix": str(prefix), "url": artifact["url"]}
    fresh = not _marker_valid(prefix, tool_id, spec["version"], artifact["sha256"])
    result["downloaded"] = fresh and not ctx.dry_run
    if ctx.dry_run:
        result.update(status=SKIPPED, plan="download+verify+npm install" if fresh else "present (verified marker)")
        return result
    npm_link = ctx.bin_dir / "npm"
    if not os.path.exists(npm_link):
        raise PackagesError(f"{tool_id}: requires the pinned node/npm in {ctx.bin_dir}")
    npm_env = ctx.env(path=f"{ctx.bin_dir}:{SYSTEM_PATH}")
    for key, value in spec.get("install", {}).get("env", {}).items():
        npm_env[key] = value
    npm_env["npm_config_cache"] = str(ctx.target.cache_home / "personal-dotfiles" / "npm")
    if fresh:
        with ctx.workdir() as work:
            work = Path(work)
            tarball = ctx.download(artifact["url"], work / Path(artifact["url"]).name)
            downloads.verify_sha256(tarball, artifact["sha256"])
            if artifact.get("sha512"):
                downloads.verify_sri(tarball, artifact["sha512"])
            userconfig = work / "npmrc"
            userconfig.write_text("", encoding="utf-8")
            npm_env["npm_config_userconfig"] = str(userconfig)
            if os.path.lexists(prefix):
                _remove_tree(prefix)
            prefix.mkdir(parents=True)
            argv = [expand(a, dict(mapping, download=str(tarball))) for a in spec["install"]["argv"]]
            ctx.log(f"packages: npm install {tool_id} {spec['version']}")
            done = _run(ctx, argv, timeout=NPM_TIMEOUT, env=npm_env, cwd=work)
            if done.returncode != 0:
                _remove_tree(prefix)
                raise PackagesError(f"{tool_id}: npm install failed: {_err_tail(done)}")
    substitute = {str(ctx.bin_dir / name): path for name, path in links.items()}
    try:
        result["verify"] = _verify_commands(ctx, spec, mapping, substitute=substitute,
                                            env=ctx.env(path=f"{ctx.bin_dir}:{SYSTEM_PATH}"))
    except PackagesError:
        if fresh:
            _remove_tree(prefix)
        raise
    if fresh:
        _write_marker(prefix, tool_id, spec["version"], artifact, {})
    for name, path in links.items():
        switch_link(ctx.bin_dir / name, path)
    result["links"] = sorted(links)
    result["status"] = PASS
    return result


def _claude_file_ok(path: Path, artifact: dict) -> bool:
    try:
        downloads.verify_sha256(path, artifact["sha256"], size=artifact.get("size"))
    except (OSError, VerificationError):
        return False
    return True


def install_claude(ctx: _Ctx, tool_id: str, spec: dict) -> dict:
    artifact = _select_artifact(spec, ctx.platform.architecture)
    mapping = _mapping(ctx, spec, artifact)
    layout = spec["vendor_layout"]
    binary = Path(expand(layout["binary"], mapping))
    launcher = Path(expand(layout["launcher"], mapping))
    result = {"version": spec["version"], "binary": str(binary), "launcher": str(launcher),
              "url": artifact["url"]}

    def launcher_target() -> Path | None:
        return launcher.resolve() if os.path.lexists(launcher) else None

    present = _claude_file_ok(binary, artifact) and launcher_target() is not None and \
        launcher_target().parent == binary.parent.resolve()
    result["downloaded"] = not present and not ctx.dry_run
    if ctx.dry_run:
        result.update(status=SKIPPED, plan="present (sha256 verified)" if present
                      else "verify signed manifest, download, run vendor install")
        return result
    if not present:
        integrity = spec["integrity"]
        source = integrity["checksum_source"]
        with ctx.workdir() as work:
            work = Path(work)
            manifest = ctx.download(source["url"], work / "manifest.json")
            if source.get("sha256"):
                downloads.verify_sha256(manifest, source["sha256"])
            _verify_signature(ctx, integrity, source["signature_url"], manifest, work)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            entry = (data.get("platforms") or {}).get(artifact["platform"]) or {}
            if str(entry.get("checksum", "")).lower() != artifact["sha256"].lower():
                raise VerificationError("claude-code: signed manifest checksum != pinned sha256")
            if "size" in entry and artifact.get("size") is not None and \
                    int(entry["size"]) != int(artifact["size"]):
                raise VerificationError("claude-code: signed manifest size != pinned size")
            downloaded = ctx.download(artifact["url"], work / "claude")
            downloads.verify_sha256(downloaded, artifact["sha256"], size=artifact.get("size"))
            os.chmod(downloaded, 0o700)
            argv = [expand(a, dict(mapping, download=str(downloaded))) for a in spec["install"]["argv"]]
            ctx.log(f"packages: {Path(argv[0]).name} install {spec['version']}")
            done = _run(ctx, argv, timeout=VENDOR_INSTALL_TIMEOUT,
                        env=ctx.env(path=f"{launcher.parent}:{ctx.bin_dir}:{SYSTEM_PATH}"), cwd=work)
            if done.returncode != 0:
                raise PackagesError(f"claude-code: vendor install failed: {_err_tail(done)}")
        if not _claude_file_ok(binary, artifact):
            raise VerificationError(f"claude-code: {binary} does not match the pinned sha256/size")
    target = launcher_target()
    if target is None:
        raise PackagesError(f"claude-code: {launcher} was not created")
    if target != binary.resolve():
        if present and target.parent == binary.parent.resolve():
            # The vendor auto-updater (left at its default) moved the launcher on.
            result["launcher_version"] = target.name
        else:
            raise PackagesError(f"claude-code: {launcher} resolves to {target}, not {binary}")
    substitute = {} if target == binary.resolve() else {str(launcher): binary}
    result["verify"] = _verify_commands(ctx, spec, mapping, substitute=substitute,
                                        env=ctx.env(path=f"{launcher.parent}:{SYSTEM_PATH}"))
    result["status"] = PASS
    return result


def install_tool(ctx: _Ctx, tool_id: str, spec: dict, tools: dict) -> dict:
    method = spec.get("install", {}).get("method")
    if method == "extract":
        return install_extract(ctx, tool_id, spec)
    if method == "npm-global-prefix":
        return install_npm(ctx, tool_id, spec, tools)
    if method == "vendor-self-install":
        return install_claude(ctx, tool_id, spec)
    raise PackagesError(f"{tool_id}: unknown install method {method!r}")


# --- python venv for neovim --------------------------------------------------

_PYNVIM_PROBE = "import importlib.metadata as m, pynvim; print(m.version('pynvim'))"


def venv_step(ctx: _Ctx, config: dict) -> dict:
    mapping = dict(ctx.base)
    venv = Path(expand(config["path"], mapping))
    python = venv / "bin" / "python"
    result: dict = {"venv": str(venv), "python3_host_prog": str(python)}
    env = ctx.env(path=f"{venv / 'bin'}:{SYSTEM_PATH}", PIP_CONFIG_FILE=os.devnull,
                  PIP_REQUIRE_VIRTUALENV="1", PIP_DISABLE_PIP_VERSION_CHECK="1")

    def probe() -> str | None:
        if not python.exists():
            return None
        try:
            done = _run(ctx, [python, "-c", _PYNVIM_PROBE], timeout=60, read_only=True, env=env)
        except PackagesError:
            return None
        return _out(done).strip() if done.returncode == 0 else None

    version = probe()
    if version:
        result.update(status=PASS, pynvim=version, created=False)
        return result
    if ctx.dry_run:
        result.update(status=SKIPPED, plan=f"python3 -m venv {venv}; pip install "
                      + " ".join(config["requirements"]))
        return result
    system_python = ctx.runner.which("python3") or "/usr/bin/python3"
    venv.parent.mkdir(parents=True, exist_ok=True)
    argv = [system_python, "-m", "venv"]
    if os.path.lexists(venv):
        argv.append("--clear")
    done = _run(ctx, [*argv, venv], timeout=300, env=env)
    if done.returncode != 0:
        raise PackagesError(f"python venv {venv} failed: {_err_tail(done)} "
                            "(is python3-venv installed?)")
    pip = [python, "-m", "pip", "install", "--no-input"]
    if config.get("only_binary"):
        pip += ["--only-binary", ",".join(config["only_binary"])]
    ctx.log("packages: pip install " + " ".join(config["requirements"]) + " into the nvim venv")
    done = _run(ctx, [*pip, *config["requirements"]], timeout=PIP_TIMEOUT, env=env)
    if done.returncode != 0:
        raise PackagesError(f"pip install into {venv} failed: {_err_tail(done)}")
    version = probe()
    if not version:
        raise PackagesError(f"pynvim is not importable from {python}")
    result.update(status=PASS, pynvim=version, created=True)
    return result


# --- phase -------------------------------------------------------------------

def _select(pkg_manifest: dict, tools_manifest: dict, groups: Iterable[str],
            only: Iterable[str] | None) -> tuple[list[str], list[str]]:
    group_ids = [g["id"] for g in pkg_manifest["groups"]]
    tools = tools_manifest["tools"]
    if only:
        sel_groups: list[str] = []
        sel_tools: list[str] = []
        for name in only:
            if name in group_ids:
                sel_groups.append(name)
            elif name in tools:
                sel_tools.append(name)
            else:
                raise PackagesError(f"{name!r} is not a package group or tool")
    else:
        sel_groups = [g for g in groups if g in group_ids]
        unknown = [g for g in groups if g not in group_ids]
        if unknown:
            raise PackagesError(f"unknown package group(s): {', '.join(unknown)}")
        sel_tools = []
    chosen = [t for t, spec in tools.items()
              if spec.get("group", "shell-core") in sel_groups or t in sel_tools]
    for tool_id in list(chosen):
        dep = tools[tool_id].get("install", {}).get("requires_tool")
        if dep and dep not in chosen:
            chosen.append(dep)
    ordered = [t for t in tools if t in chosen]
    return sel_groups, ordered


def _default_log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def link_requests(details: dict) -> list:
    """Turn ``details["links"]`` into transaction DesiredEntry objects."""

    from installer.transaction import DesiredEntry

    return [DesiredEntry(e["id"], Path(e["dest"]), "symlink", link_text=e["link_text"])
            for e in details.get("links", [])]


def run_packages_phase(
    target,
    platform,
    runner,
    *,
    dry_run: bool = False,
    groups: Iterable[str] = DEFAULT_GROUPS,
    fetch: FetchFn | None = None,
    only: Iterable[str] | None = None,
    manifest_dir: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> dict:
    log = log or _default_log
    reasons: list[str] = []
    details: dict = {"dry_run": dry_run}
    try:
        require_supported_platform(platform)
    except PlatformError as exc:
        return {"phase": PHASE, "status": FAIL, "reasons": [str(exc)], "details": details}
    try:
        pkg_manifest, tools_manifest = load_manifests(manifest_dir)
        sel_groups, sel_tools = _select(pkg_manifest, tools_manifest, groups, only)
    except (OSError, ValueError, KeyError, PackagesError) as exc:
        return {"phase": PHASE, "status": FAIL, "reasons": [f"manifests: {exc}"], "details": details}
    ctx = _Ctx(target, platform, runner, fetch or downloads.fetch, dry_run, tools_manifest, log)
    details.update(groups=sel_groups, tools_selected=sel_tools, bin_dir=str(ctx.bin_dir))
    statuses: list[str] = []

    # 1. apt (system, non-transactional)
    if sel_groups:
        try:
            apt = apt_step(ctx, pkg_manifest, sel_groups)
        except (PackagesError, DownloadError, VerificationError, OSError) as exc:
            apt = {"status": FAIL, "reasons": [f"apt: {exc}"], "transactional": False}
        details["apt"] = apt
        statuses.append(apt["status"])
        reasons.extend(apt.get("reasons", []) if apt["status"] == FAIL else
                       [r for r in apt.get("reasons", []) if r != "dry-run: plan only"])
        try:
            details["shims"] = shim_step(ctx, pkg_manifest, sel_groups)
        except (PackagesError, OSError) as exc:
            statuses.append(FAIL)
            reasons.append(f"shims: {exc}")

    # 2-3. pinned tools
    tool_results: dict[str, dict] = {}
    for tool_id in sel_tools:
        spec = tools_manifest["tools"][tool_id]
        dep = spec.get("install", {}).get("requires_tool")
        if dep and tool_results.get(dep, {}).get("status") == FAIL:
            tool_results[tool_id] = {"status": FAIL, "reason": f"requires {dep}, which failed"}
        else:
            try:
                tool_results[tool_id] = install_tool(ctx, tool_id, spec, tools_manifest)
            except (PackagesError, DownloadError, VerificationError, OSError,
                    tarfile.TarError, ValueError, KeyError) as exc:
                tool_results[tool_id] = {"status": FAIL, "version": spec.get("version"),
                                         "reason": f"{type(exc).__name__}: {exc}"}
        if tool_results[tool_id]["status"] == FAIL:
            reasons.append(f"{tool_id}: {tool_results[tool_id]['reason']}")
        statuses.append(tool_results[tool_id]["status"])
    details["tools"] = tool_results

    # Links that must work outside zsh; the transaction owns ~/.local/bin.
    links = []
    for name in tools_manifest.get("home_links", []):
        source = ctx.bin_dir / name
        owner = next((t for t in sel_tools if name in tools_manifest["tools"][t].get("links", {})), None)
        if owner is None or tool_results.get(owner, {}).get("status") == FAIL:
            continue
        links.append({"id": f"tool-link-{name}", "dest": str(target.home / ".local" / "bin" / name),
                      "kind": "symlink", "link_text": str(source)})
    details["links"] = links

    # 4. python venv for neovim
    venv_cfg = tools_manifest.get("python_venvs", {}).get("nvim")
    if venv_cfg and "neovim" in sel_tools:
        try:
            details["python"] = venv_step(ctx, venv_cfg)
        except (PackagesError, OSError) as exc:
            details["python"] = {"status": FAIL, "reason": str(exc)}
            reasons.append(f"nvim venv: {exc}")
        statuses.append(details["python"]["status"])

    if FAIL in statuses:
        status = FAIL
    elif dry_run:
        status = SKIPPED
        reasons.insert(0, "dry-run: plan only")
    else:
        status = PASS

    if dry_run:
        _print_plan(log, details)
    else:
        _save_state(target, status, details)
    return {"phase": PHASE, "status": status, "reasons": reasons, "details": details}


def _print_plan(log, details: dict) -> None:
    apt = details.get("apt", {})
    if apt:
        closure = apt.get("closure")
        log(f"packages (dry-run): apt missing {len(apt.get('missing', []))}: "
            f"{' '.join(apt.get('missing', [])) or '-'}")
        if closure:
            log(f"packages (dry-run): simulated closure {closure['count']} packages, "
                f"forbidden: {', '.join(closure['forbidden']) or 'none'}, "
                f"removals: {', '.join(closure['removals']) or 'none'}")
        for step in apt.get("plan", []):
            log(f"packages (dry-run): {step}")
    for tool_id, result in details.get("tools", {}).items():
        log(f"packages (dry-run): {tool_id} {result.get('version', '')}: "
            f"{result.get('plan') or result.get('reason') or result.get('status')}")
    python = details.get("python")
    if python:
        log(f"packages (dry-run): nvim venv: {python.get('plan') or python.get('status')}")


def _save_state(target, status: str, details: dict) -> None:
    root = target.state_root / "packages"
    try:
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        summary = {
            "status": status,
            "apt_versions": details.get("apt", {}).get("versions", {}),
            "tools": {k: {"status": v.get("status"), "version": v.get("version"),
                          "prefix": v.get("prefix")} for k, v in details.get("tools", {}).items()},
            "python3_host_prog": details.get("python", {}).get("python3_host_prog"),
        }
        _atomic_write(root / "last-run.json",
                      (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    except OSError:
        pass


__all__ = ["PackagesError", "apt_step", "forbidden_matches", "link_requests", "load_manifests",
           "parse_simulation", "run_packages_phase", "safe_extract", "switch_link"]
