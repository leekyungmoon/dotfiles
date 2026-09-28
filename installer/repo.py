"""Read-only helpers for the ``~/.dotfiles`` git checkout.

As in upstream, ``~/.dotfiles`` *is* the git checkout: ``etc/install``
clones it, ``install.py`` runs from it and the managed symlinks point into it,
and ``dotfiles update`` fast-forwards it in place (see ``bin/dotfiles``).
The installer itself never clones, pulls, resets or cleans the checkout; it
only reads it here and, when upstream's check finds uninitialized
submodules, runs ``git submodule update --init --recursive`` like upstream.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

DEFAULT_REPO_URL = "https://github.com/leekyungmoon/dotfiles.git"
GITHUB_URL = "https://github.com/leekyungmoon/dotfiles"

GIT_TIMEOUT = 120.0
SUBMODULE_TIMEOUT = 900.0

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Upstream's wording for 'git submodule status' flags.
SUBMODULE_STATUS = {"+": "needs update", "-": "not initialized", "U": "conflict!"}


class RepoError(Exception):
    """The checkout could not be inspected or its submodules are broken."""


@dataclasses.dataclass(frozen=True)
class CheckoutInfo:
    path: Path
    commit: str
    origin_url: str | None
    branch: str | None


def source_url(env: dict[str, str] | None = None) -> str:
    env = env or {}
    return env.get("DOTFILES_REPO_URL", "").strip() or DEFAULT_REPO_URL


def source_ref(env: dict[str, str] | None = None) -> str | None:
    env = env or {}
    return env.get("DOTFILES_REF", "").strip() or None


def normalize_url(url: str | None) -> str:
    """Compare clone URLs loosely: scheme spelling, ``.git`` and ``/``."""

    text = (url or "").strip()
    match = re.match(r"^(?:ssh://)?git@github\.com[:/](.+)$", text)
    if match:
        text = "https://github.com/" + match.group(1)
    text = re.sub(r"^git://github\.com/", "https://github.com/", text)
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    return text.rstrip("/")


def same_repository(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and normalize_url(a) == normalize_url(b)


def _git(runner, args: list[str], *, timeout: float = GIT_TIMEOUT,
         check: bool = True, read_only: bool = False):
    return runner.run(["git", *args], timeout=timeout, check=check,
                      read_only=read_only)


def _out(completed) -> str:
    data = completed.stdout or b""
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    return data.strip()


def is_checkout(path: Path) -> bool:
    return (Path(path) / ".git").exists()


def rev_parse(runner, repo: Path, rev: str = "HEAD") -> str:
    return _out(_git(runner, ["-C", str(repo), "rev-parse", "--verify",
                              f"{rev}^{{commit}}"], read_only=True))


def short_head(runner, repo: Path) -> str:
    return _out(_git(runner, ["-C", str(repo), "rev-parse", "--short", "HEAD"],
                     read_only=True))


def remote_url(runner, repo: Path, remote: str = "origin") -> str | None:
    completed = _git(runner, ["-C", str(repo), "remote", "get-url", remote],
                     check=False, read_only=True)
    if completed.returncode != 0:
        return None
    return _out(completed) or None


def current_branch(runner, repo: Path) -> str | None:
    completed = _git(runner, ["-C", str(repo), "symbolic-ref", "--short", "-q",
                              "HEAD"], check=False, read_only=True)
    if completed.returncode != 0:
        return None
    return _out(completed) or None


def is_dirty(runner, repo: Path) -> bool:
    """Tracked changes (staged or not); untracked files do not count."""

    completed = _git(runner, ["-C", str(repo), "status", "--porcelain",
                              "--untracked-files=no"], read_only=True)
    return bool(_out(completed))


def is_ancestor(runner, repo: Path, ancestor: str, descendant: str) -> bool:
    completed = _git(runner, ["-C", str(repo), "merge-base", "--is-ancestor",
                              ancestor, descendant], check=False, read_only=True)
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise RepoError(f"cannot compare {ancestor} and {descendant}")


def checkout_info(runner, repo: Path) -> CheckoutInfo:
    repo = Path(repo)
    if not is_checkout(repo):
        raise RepoError(f"{repo} is not a git checkout")
    return CheckoutInfo(path=repo, commit=rev_parse(runner, repo),
                        origin_url=remote_url(runner, repo),
                        branch=current_branch(runner, repo))


def submodule_issues(runner, repo: Path) -> list[tuple[str, str]]:
    """``(path, flag)`` for every submodule whose status flag is not blank."""

    completed = _git(runner, ["-C", str(repo), "submodule", "status",
                              "--recursive"], read_only=True)
    raw = completed.stdout or b""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    issues = []
    for line in text.splitlines():  # the leading flag column is significant
        if not line or line[0] == " ":
            continue
        fields = line[1:].split()
        issues.append((fields[1] if len(fields) > 1 else line, line[0]))
    return issues


def verify_submodules(runner, repo: Path) -> None:
    issues = submodule_issues(runner, repo)
    if issues:
        raise RepoError("submodules not checked out: " + ", ".join(
            f"{path} ({SUBMODULE_STATUS.get(flag, flag)})" for path, flag in issues))


def update_submodules(runner, repo: Path) -> None:
    """Upstream's fix-up: ``git submodule update --init --recursive --jobs 8``."""

    _git(runner, ["-C", str(repo), "submodule", "update", "--init", "--recursive",
                  "--jobs", "8"], timeout=SUBMODULE_TIMEOUT)
