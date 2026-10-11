"""Platform, ownership and root resolution.

Everything the installer writes is anchored to one resolved owner and a set of
roots underneath that owner's home. This module is the only place that decides
who "the installing user" is; the rest of the installer takes the resolved
:class:`Target` and never re-reads ``HOME`` or ``SUDO_USER``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import plistlib
import pwd
import re
import subprocess
from pathlib import Path

SUPPORTED_RELEASES = ("22.04", "24.04")
SUPPORTED_ARCHITECTURES = ("amd64", "arm64")

# macOS: the majors Homebrew supports at Tier 1 on Apple silicon (bottles for
# every formula in manifests/packages-macos.json). Intel Macs stop at 26 and
# Homebrew has Intel at Tier 3 (no new bottles, formulae may build from
# source), so amd64 is accepted on its last releases as best effort only.
# Like the Ubuntu list, anything else is refused before any change; etc/install
# keeps the same lists (tests/unit/test_bootstrap_policy.py compares them).
SUPPORTED_MACOS_RELEASES = ("15", "26", "27")
SUPPORTED_MACOS_ARCHITECTURES = ("arm64", "amd64")
MACOS_INTEL_RELEASES = ("15", "26")
MACOS_RELEASE_NAMES = {"15": "Sequoia", "26": "Tahoe", "27": "27"}

# Homebrew's default prefix per architecture; HOMEBREW_PREFIX overrides it.
HOMEBREW_PREFIXES = {"arm64": "/opt/homebrew", "amd64": "/usr/local"}
MACOS_VERSION_PLIST = Path("/System/Library/CoreServices/SystemVersion.plist")

# dpkg architecture -> canonical name. Anything else is unsupported rather than
# guessed; a wrong guess would download the wrong binary artifacts.
_ARCH_ALIASES = {
    "amd64": "amd64",
    "x86_64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
}

APP_NAME = "personal-dotfiles"


class PlatformError(Exception):
    """The current machine cannot be used as an installation target."""


@dataclasses.dataclass(frozen=True)
class Platform:
    distribution: str
    release: str
    architecture: str
    # "linux" or "darwin". On macOS distribution is "macos" and release is the
    # major version ("15", "26", ...).
    system: str = "linux"

    @property
    def is_darwin(self) -> bool:
        return self.system == "darwin"

    @property
    def is_linux(self) -> bool:
        return self.system == "linux"

    @property
    def artifact_key(self) -> str:
        """Key of this platform's entry in manifests/tools.json artifacts:
        the bare dpkg architecture on Linux (unchanged), ``darwin-<arch>`` on
        macOS."""

        if self.is_linux:
            return self.architecture
        return f"{self.system}-{self.architecture}"

    @property
    def best_effort(self) -> bool:
        """Accepted, but outside what Homebrew still supports at Tier 1."""

        return self.is_darwin and self.architecture != "arm64"

    @property
    def is_supported(self) -> bool:
        if self.is_darwin:
            releases = (SUPPORTED_MACOS_RELEASES if self.architecture == "arm64"
                        else MACOS_INTEL_RELEASES)
            return (
                self.distribution == "macos"
                and self.architecture in SUPPORTED_MACOS_ARCHITECTURES
                and self.release in releases
            )
        return (
            self.distribution == "ubuntu"
            and self.release in SUPPORTED_RELEASES
            and self.architecture in SUPPORTED_ARCHITECTURES
        )

    def describe(self) -> str:
        if self.is_darwin:
            name = MACOS_RELEASE_NAMES.get(self.release, self.release)
            label = f"macos {self.release} {name}" if name != self.release else f"macos {self.release}"
            return f"{label} ({self.architecture})"
        return f"{self.distribution} {self.release} ({self.architecture})"


@dataclasses.dataclass(frozen=True)
class Target:
    """The resolved installation owner and the roots owned by them."""

    uid: int
    gid: int
    username: str
    home: Path
    data_home: Path
    state_home: Path
    config_home: Path
    cache_home: Path

    @property
    def repo_root(self) -> Path:
        """The git checkout itself: ``~/.dotfiles`` (as in upstream)."""

        return self.home / ".dotfiles"

    @property
    def staging_root(self) -> Path:
        """Scratch clones only; never the installed checkout."""

        return self.data_home / APP_NAME / "staging"

    @property
    def state_root(self) -> Path:
        return self.state_home / APP_NAME

    @property
    def compat_link(self) -> Path:
        """Alias of :attr:`repo_root`, kept for existing callers."""

        return self.repo_root

    def to_json(self) -> str:
        payload = {k: str(v) for k, v in dataclasses.asdict(self).items()}
        return json.dumps(payload, indent=2, sort_keys=True)


def parse_os_release(text: str) -> tuple[str, str]:
    """Return ``(distribution_id, version_id)`` from ``/etc/os-release`` text."""

    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        fields[key.strip()] = value

    distribution = fields.get("ID", "").strip().lower()
    release = fields.get("VERSION_ID", "").strip()
    if not distribution or not release:
        raise PlatformError("/etc/os-release does not declare ID and VERSION_ID")
    return distribution, release


def normalize_architecture(raw: str) -> str:
    key = raw.strip().lower()
    try:
        return _ARCH_ALIASES[key]
    except KeyError:
        raise PlatformError(
            f"unsupported CPU architecture {raw!r}; "
            f"supported: {', '.join(SUPPORTED_ARCHITECTURES)}"
        ) from None


def detect_architecture() -> str:
    """Prefer dpkg's opinion, since packages are what we install."""

    try:
        completed = subprocess.run(
            ["dpkg", "--print-architecture"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return normalize_architecture(os.uname().machine)
    return normalize_architecture(completed.stdout)


def current_system() -> str:
    """``darwin`` on macOS, ``linux`` on Linux (lower-cased uname otherwise)."""

    return os.uname().sysname.strip().lower()


def detect_platform(os_release_path: Path | None = None) -> Platform:
    """The running platform. An explicit ``os_release_path`` always means a
    Linux os-release file to parse (tests); without one, macOS is detected
    from the kernel name."""

    if os_release_path is None and current_system() == "darwin":
        return detect_macos()
    path = os_release_path or Path("/etc/os-release")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlatformError(f"cannot read {path}: {exc}") from exc
    distribution, release = parse_os_release(text)
    return Platform(distribution, release, detect_architecture())


# --- macOS --------------------------------------------------------------------

def parse_macos_version(version: str) -> str:
    """Major release from a ProductVersion such as ``15.5`` or ``26.0.1``."""

    match = re.match(r"^\s*([0-9]+)(?:\.[0-9]+)*\s*$", version or "")
    if match is None:
        raise PlatformError(f"cannot parse the macOS version {version!r}")
    return match.group(1)


def _sysctl_value(name: str) -> str | None:
    """``sysctl -n <name>``; None when the name does not exist (e.g.
    hw.optional.arm64 on Intel) or sysctl cannot run."""

    try:
        completed = subprocess.run(
            ["/usr/sbin/sysctl", "-n", name],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _macos_product_version(version_plist: Path) -> str:
    """ProductVersion from SystemVersion.plist (the real version even when
    SYSTEM_VERSION_COMPAT=1 makes sw_vers report 10.16), else sw_vers."""

    try:
        with open(version_plist, "rb") as handle:
            value = plistlib.load(handle).get("ProductVersion")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    try:
        completed = subprocess.run(
            ["/usr/bin/sw_vers", "-productVersion"],
            capture_output=True, text=True, timeout=10, check=True,
            env={**os.environ, "SYSTEM_VERSION_COMPAT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PlatformError(f"cannot read the macOS version: {exc}") from exc
    return completed.stdout.strip()


def detect_macos(*, version_plist: Path | None = None, sysctl=_sysctl_value,
                 machine: str | None = None) -> Platform:
    """Platform("macos", <major>, <arch>, "darwin").

    A process translated by Rosetta 2 reports x86_64 on Apple silicon; it is
    refused instead of guessed, because the Homebrew prefix, the pinned
    darwin artifacts and every LaunchAgent PATH depend on the native
    architecture.
    """

    version = _macos_product_version(version_plist or MACOS_VERSION_PLIST)
    release = parse_macos_version(version)
    if sysctl("sysctl.proc_translated") == "1":
        raise PlatformError(
            "this shell runs under Rosetta 2 (x86_64 emulation) on an Apple "
            "silicon Mac; open a native terminal (arch -arm64 /bin/zsh -l) and "
            "rerun"
        )
    architecture = normalize_architecture(machine or os.uname().machine)
    return Platform("macos", release, architecture, "darwin")


def homebrew_prefix(platform: Platform, env: dict[str, str] | None = None) -> Path | None:
    """Homebrew's prefix for ``platform`` (None off macOS): an absolute
    ``HOMEBREW_PREFIX`` from the environment, else the architecture's
    default (/opt/homebrew on Apple silicon, /usr/local on Intel)."""

    if not platform.is_darwin:
        return None
    env = os.environ if env is None else env
    declared = (env.get("HOMEBREW_PREFIX") or "").strip()
    if declared.startswith("/"):
        return Path(declared.rstrip("/") or "/")
    return Path(HOMEBREW_PREFIXES.get(platform.architecture, "/opt/homebrew"))


def _macos_supported_text() -> str:
    arm = ", ".join(f"{r} ({MACOS_RELEASE_NAMES[r]})" if MACOS_RELEASE_NAMES[r] != r else r
                    for r in SUPPORTED_MACOS_RELEASES)
    intel = ", ".join(MACOS_INTEL_RELEASES)
    return f"{arm} on Apple silicon; {intel} on Intel (best effort)"


def require_supported_macos(platform: Platform) -> None:
    if platform.distribution != "macos":
        raise PlatformError(f"unsupported Darwin distribution {platform.distribution!r}")
    if platform.architecture not in SUPPORTED_MACOS_ARCHITECTURES:
        raise PlatformError(
            f"unsupported architecture {platform.architecture!r}; "
            f"supported macOS: {_macos_supported_text()}"
        )
    releases = (SUPPORTED_MACOS_RELEASES if platform.architecture == "arm64"
                else MACOS_INTEL_RELEASES)
    if platform.release not in releases:
        raise PlatformError(
            f"unsupported macOS release {platform.release!r} on "
            f"{platform.architecture}; supported: {_macos_supported_text()}"
        )


def require_supported_platform(platform: Platform) -> None:
    if platform.is_darwin:
        require_supported_macos(platform)
        return
    if platform.distribution != "ubuntu":
        raise PlatformError(
            f"unsupported distribution {platform.distribution!r}; "
            "this installer targets Ubuntu only"
        )
    if platform.release not in SUPPORTED_RELEASES:
        raise PlatformError(
            f"unsupported Ubuntu release {platform.release!r}; "
            f"supported: {', '.join(SUPPORTED_RELEASES)}"
        )
    if platform.architecture not in SUPPORTED_ARCHITECTURES:
        raise PlatformError(
            f"unsupported architecture {platform.architecture!r}; "
            f"supported: {', '.join(SUPPORTED_ARCHITECTURES)}"
        )


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolve_existing_prefix(path: Path) -> Path:
    """Resolve the longest existing prefix of ``path``, keeping the remainder.

    ``Path.resolve()`` on a not-yet-created path still resolves the parents it
    can see, which is what we need to catch a symlinked parent that escapes the
    owner's home. The walk stops at anything that exists *as a link*, including
    a dangling one: treating a dangling link as "not created yet" would hide
    that a later mkdir follows it somewhere else.
    """

    existing = path
    tail: list[str] = []
    while not os.path.lexists(existing):
        if existing.parent == existing:
            break
        tail.append(existing.name)
        existing = existing.parent
    resolved = existing.resolve()
    for name in reversed(tail):
        resolved = resolved / name
    return resolved


def _anchored_root(
    env: dict[str, str],
    variable: str,
    default: Path,
    home: Path,
    resolved_home: Path,
) -> Path:
    """Honour an XDG variable only when it stays inside the owner's home."""

    raw = env.get(variable, "").strip()
    if not raw:
        return default
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise PlatformError(f"{variable} must be an absolute path, got {raw!r}")
    # Both views must agree. Lexically outside means another user's tree even
    # if a link currently points back in; resolved outside means a symlinked
    # parent escapes, even though the spelling looks like it is under home.
    if not _is_within(candidate, home):
        raise PlatformError(
            f"{variable}={raw!r} is outside {home}; refusing to install into a "
            "path that may belong to another user"
        )
    resolved = _resolve_existing_prefix(candidate)
    if not _is_within(resolved, resolved_home):
        raise PlatformError(
            f"{variable}={raw!r} escapes {home} through a symlinked parent"
        )
    return candidate


def resolve_target(
    env: dict[str, str] | None = None,
    *,
    euid: int | None = None,
    getpwuid=pwd.getpwuid,
) -> Target:
    """Resolve the installation owner from the effective UID and passwd.

    The whole installer refuses to run as root, including under ``sudo``:
    ``SUDO_USER`` is an environment string, not proof of ownership, and
    trusting it would let a root shell write into somebody else's home.

    ``euid`` and ``getpwuid`` exist so tests can model other users; production
    callers pass neither.
    """

    env = dict(os.environ if env is None else env)
    uid = os.geteuid() if euid is None else euid
    if uid == 0:
        raise PlatformError(
            "refusing to run as root. Run this installer as your normal user; "
            "it will ask for sudo only for the package steps."
        )

    try:
        entry = getpwuid(uid)
    except KeyError as exc:
        raise PlatformError(f"uid {uid} has no passwd entry") from exc

    home = Path(entry.pw_dir)
    if not home.is_absolute():
        raise PlatformError(f"passwd home for {entry.pw_name!r} is not absolute")

    declared_home = env.get("HOME", "").strip()
    if declared_home and Path(declared_home) != home:
        # A mismatched HOME is how a config write ends up in the wrong tree.
        raise PlatformError(
            f"HOME={declared_home!r} does not match the passwd home {home} for "
            f"user {entry.pw_name!r}; refusing to guess which one is correct"
        )

    try:
        home_stat = home.stat()
    except FileNotFoundError:
        raise PlatformError(f"passwd home {home} does not exist") from None
    if home_stat.st_uid != uid:
        raise PlatformError(
            f"{home} is owned by uid {home_stat.st_uid}, not {uid}; refusing to "
            "install into a home the installing user does not own"
        )

    resolved_home = _resolve_existing_prefix(home)

    return Target(
        uid=uid,
        gid=entry.pw_gid,
        username=entry.pw_name,
        home=home,
        data_home=_anchored_root(
            env, "XDG_DATA_HOME", home / ".local" / "share", home, resolved_home
        ),
        state_home=_anchored_root(
            env, "XDG_STATE_HOME", home / ".local" / "state", home, resolved_home
        ),
        config_home=_anchored_root(
            env, "XDG_CONFIG_HOME", home / ".config", home, resolved_home
        ),
        cache_home=_anchored_root(
            env, "XDG_CACHE_HOME", home / ".cache", home, resolved_home
        ),
    )


_SESSION_TYPES = ("wayland", "x11")


def session_kind(env: dict[str, str] | None = None) -> str:
    """Classify the graphical session: ``wayland``, ``x11`` or ``none``."""

    env = dict(os.environ if env is None else env)
    declared = env.get("XDG_SESSION_TYPE", "").strip().lower()
    if declared in _SESSION_TYPES:
        return declared
    if env.get("WAYLAND_DISPLAY", "").strip():
        return "wayland"
    if env.get("DISPLAY", "").strip():
        return "x11"
    return "none"


_GENERATION_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")


def is_generation_id(value: str) -> bool:
    return bool(_GENERATION_RE.match(value))
