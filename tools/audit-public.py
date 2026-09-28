#!/usr/bin/env python3
"""Pre-publication audit of a git repository.

Scans everything a push of the given refs would publish on top of an upstream
base commit:

* every tracked file (and symlink text) in the tree at each ref;
* every blob introduced by commits in ``base..ref``, including files that a
  later commit modified or deleted, plus commit metadata (author, committer,
  message);
* ``.gitmodules`` URLs and gitlinks at each ref;
* the refs themselves: commits that do not descend from the base, merges of
  unrelated histories, tags.

History reachable from the base is "upstream ancestry": it is already public
and is not scanned for introduced content.

Source-machine strings (username, hostname, company domain, device names) are
never hardcoded here. They are read at runtime from ``--private-strings FILE``
(one string per line, kept outside the repository) and matched
case-insensitively.

Exit status: 0 when every finding is informational or accepted by the
allowlist, 1 when anything else was found, 2 on usage or git errors.

Allowlist format (``tools/public-audit.allowlist`` by default), one rule per
line, ``#`` starts a comment::

    kind | path-glob | literal | reason

``kind`` may be ``*``; ``literal`` may be empty (then any match of that kind
under the glob is accepted) and otherwise must be a substring of the matched
text or of the source line it was found on; ``reason`` is required. Pseudo
paths ``<commit-metadata>``, ``<tag-metadata>``, ``<ref>``, ``<commit>`` and
``.gitmodules`` name non-file findings.
"""

from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ZERO_SHA = "0" * 40
DEFAULT_ALLOWLIST = Path(__file__).resolve().parent / "public-audit.allowlist"
BINARY_SNIFF = 8000

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_INFO = "info"


class AuditError(Exception):
    """The audit could not run (bad arguments, git failure)."""


@dataclasses.dataclass
class Finding:
    kind: str
    severity: str
    ref_or_commit: str
    path: str
    line: int | None
    excerpt_redacted: str
    # Unredacted matched text; used only for allowlist matching, never output.
    raw: str = ""
    # Unredacted source line; used only for allowlist literals, never output.
    context: str = ""
    accepted: bool = False
    accept_reason: str = ""

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "severity": self.severity,
            "ref_or_commit": self.ref_or_commit,
            "path": self.path,
            "line": self.line,
            "excerpt_redacted": self.excerpt_redacted,
        }
        if self.accepted:
            payload["accepted"] = True
            payload["accept_reason"] = self.accept_reason
        return payload


# --------------------------------------------------------------------------
# Detectors


@dataclasses.dataclass(frozen=True)
class Pattern:
    kind: str
    severity: str
    regex: re.Pattern[str]
    group: int = 0


_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
)

TOKEN_PATTERNS: tuple[Pattern, ...] = (
    Pattern("github-token", SEVERITY_HIGH,
            re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    Pattern("github-token", SEVERITY_HIGH,
            re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}")),
    Pattern("anthropic-key", SEVERITY_HIGH,
            re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    Pattern("openai-key", SEVERITY_HIGH,
            re.compile(r"\bsk-(?!ant-)(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    Pattern("aws-access-key", SEVERITY_HIGH,
            re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    Pattern("aws-secret-key", SEVERITY_HIGH,
            re.compile(
                r"(?i)aws_?secret_?(?:access_?)?key\w*[\"']?\s*[:=]\s*[\"']?"
                r"([A-Za-z0-9/+=]{40})\b"),
            group=1),
    Pattern("slack-token", SEVERITY_HIGH,
            re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    Pattern("gitlab-token", SEVERITY_HIGH,
            re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    Pattern("google-api-key", SEVERITY_HIGH,
            re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
)

_GENERIC_SECRET = re.compile(
    r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)\w*"
    r"[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9+/=_\-.]{16,})"
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_HOME_PATH = re.compile(r"(?<![\w.])/(home|Users)/([A-Za-z][A-Za-z0-9._\-]*)")

# Generic names that stand for "some user" rather than identifying anyone.
HOME_PLACEHOLDERS = frozenset({
    "user", "username", "user-name", "user_name", "yourname", "your-name",
    "your_name", "you", "me", "name", "example", "foo", "bar", "someone",
    "somebody", "runner", "linuxbrew", "shared", "fixture-user", "test",
    "tester", "alice", "bob",
})

# Domains reserved for documentation (RFC 2606) and non-identifying hosts.
EMAIL_PLACEHOLDER_DOMAINS = frozenset({
    "example.com", "example.org", "example.net", "example.invalid",
    "localhost", "domain.com", "email.com",
})

CREDENTIAL_BASENAMES = frozenset({
    ".netrc", "_netrc", ".git-credentials", ".pgpass", "credentials",
    "credentials.json", ".env", ".envrc.secret",
})
CREDENTIAL_SUFFIXES = (".p12", ".pfx", ".keystore", ".jks")


def forbidden_path_reason(path: str) -> str | None:
    parts = path.split("/")
    base = parts[-1]
    if path == "zsh/zsh.d/ssh-password-cache.zsh":
        return "password cache helper"
    if " DEST" in path:
        return "copy-destination artifact"
    for part in parts[:-1] + [base]:
        if part in (".omx", ".codex", ".claude"):
            return f"agent state directory {part}"
    if base.endswith(".pem"):
        return "PEM file"
    if base.startswith("id_rsa") or base.startswith("id_ed25519") \
            or base.startswith("id_ecdsa") or base.startswith("id_dsa"):
        return "SSH key file"
    if base == ".gitconfig.secret":
        return "secret git config"
    return None


def credential_path_reason(path: str) -> str | None:
    base = path.rsplit("/", 1)[-1]
    if base in CREDENTIAL_BASENAMES:
        return f"credentials-looking file {base}"
    if base.endswith(CREDENTIAL_SUFFIXES):
        return "certificate/keystore container"
    return None


def load_private_strings(path: Path | None, repo: Path) -> list[str]:
    if path is None:
        return []
    resolved = path.resolve()
    repo_resolved = repo.resolve()
    if resolved == repo_resolved or repo_resolved in resolved.parents:
        raise AuditError(
            "--private-strings file must live outside the audited repository"
        )
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuditError(f"cannot read private strings file: {exc}") from exc
    values: list[str] = []
    for line in text.splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if value.lower() not in (v.lower() for v in values):
            values.append(value)
    return values


class Redactor:
    """Produces output-safe excerpts. Private strings never reach output."""

    def __init__(self, private_strings: list[str]) -> None:
        self._private = [
            (re.compile(re.escape(s), re.IGNORECASE), f"[PRIVATE-{i + 1}]")
            for i, s in enumerate(private_strings)
        ]

    def scrub(self, text: str) -> str:
        for regex, label in self._private:
            text = regex.sub(label, text)
        return text

    @staticmethod
    def mask(secret: str, keep: int = 4) -> str:
        keep = min(keep, max(len(secret) // 4, 1))
        return secret[:keep] + "…[REDACTED]"

    def sanitize(self, text: str) -> str:
        """Mask every detector match in free text (used around a finding)."""
        text = _PRIVATE_KEY.sub("[PRIVATE-KEY-HEADER]", text)
        for pattern in TOKEN_PATTERNS:
            text = pattern.regex.sub(
                lambda m: self.mask(m.group(0)), text)
        text = _GENERIC_SECRET.sub(
            lambda m: m.group(0)[: m.start(1) - m.start(0)] + self.mask(m.group(1)),
            text)
        text = _EMAIL.sub(lambda m: _email_mask(m.group(0)), text)
        text = _HOME_PATH.sub(
            lambda m: m.group(0) if m.group(2).lower() in HOME_PLACEHOLDERS
            else _home_mask(m), text)
        return self.scrub(text)

    def excerpt(self, line: str, start: int, end: int, replacement: str) -> str:
        left = self.sanitize(line[:start]).lstrip()
        right = self.sanitize(line[end:]).rstrip()
        if len(left) > 40:
            left = "…" + left[-40:]
        if len(right) > 40:
            right = right[:40] + "…"
        return left + replacement + right


def _email_mask(email: str) -> str:
    local, _, domain = email.partition("@")
    return (local[:1] or "?") + "***@" + (domain[:1] or "?") + "***"


def _home_mask(match: re.Match[str]) -> str:
    return f"/{match.group(1)}/{match.group(2)[:1]}***"


class Scanner:
    def __init__(self, private_strings: list[str], redactor: Redactor) -> None:
        self.private_strings = private_strings
        self._private_regex = [
            re.compile(re.escape(s), re.IGNORECASE) for s in private_strings
        ]
        self.redactor = redactor

    # Path-level checks --------------------------------------------------
    def scan_path(self, path: str, where: str) -> list[Finding]:
        findings: list[Finding] = []
        reason = forbidden_path_reason(path)
        if reason:
            findings.append(Finding(
                "forbidden-path", SEVERITY_HIGH, where, self.redactor.scrub(path),
                None, reason, raw=path))
        reason = credential_path_reason(path)
        if reason:
            findings.append(Finding(
                "credential-file", SEVERITY_HIGH, where, self.redactor.scrub(path),
                None, reason, raw=path))
        for i, regex in enumerate(self._private_regex):
            if regex.search(path):
                findings.append(Finding(
                    "private-string", SEVERITY_HIGH, where,
                    self.redactor.scrub(path), None,
                    f"path contains private string #{i + 1}", raw=path))
        for m in _HOME_PATH.finditer(path):
            if m.group(2).lower() not in HOME_PLACEHOLDERS:
                findings.append(Finding(
                    "home-path", SEVERITY_MEDIUM, where,
                    self.redactor.scrub(path), None, "path names a home directory",
                    raw=m.group(0)))
        return findings

    # Text checks -----------------------------------------------------------
    def scan_text(self, text: str, where: str, path: str) -> list[Finding]:
        findings: list[Finding] = []
        shown_path = self.redactor.scrub(path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            findings.extend(self._scan_line(line, lineno, where, shown_path))
        return findings

    def _add(self, out: list[Finding], kind: str, severity: str, where: str,
             path: str, lineno: int, line: str, start: int, end: int,
             replacement: str, raw: str) -> None:
        out.append(Finding(
            kind, severity, where, path, lineno,
            self.redactor.excerpt(line, start, end, replacement), raw=raw,
            context=line))

    def _scan_line(self, line: str, lineno: int, where: str,
                   path: str) -> list[Finding]:
        out: list[Finding] = []
        if "PRIVATE KEY" in line:
            m = _PRIVATE_KEY.search(line)
            if m:
                self._add(out, "private-key", SEVERITY_HIGH, where, path, lineno,
                          line, m.start(), m.end(), m.group(0), m.group(0))
        token_spans: list[tuple[int, int]] = []
        for pattern in TOKEN_PATTERNS:
            for m in pattern.regex.finditer(line):
                s, e = m.span(pattern.group)
                value = m.group(pattern.group)
                token_spans.append((s, e))
                self._add(out, pattern.kind, pattern.severity, where, path, lineno,
                          line, s, e, self.redactor.mask(value), value)
        for m in _GENERIC_SECRET.finditer(line):
            s, e = m.span(1)
            value = m.group(1)
            if any(s < te and ts < e for ts, te in token_spans):
                continue
            if not (re.search(r"[0-9]", value) and re.search(r"[A-Za-z]", value)):
                continue
            self._add(out, "generic-secret", SEVERITY_MEDIUM, where, path, lineno,
                      line, s, e, self.redactor.mask(value), value)
        if "@" in line:
            for m in _EMAIL.finditer(line):
                local, _, domain = m.group(0).lower().rpartition("@")
                if local == "git":
                    continue  # SSH remote syntax (git@host:owner/repo)
                if domain in EMAIL_PLACEHOLDER_DOMAINS or domain.endswith(".example"):
                    continue
                self._add(out, "email", SEVERITY_MEDIUM, where, path, lineno, line,
                          m.start(), m.end(), _email_mask(m.group(0)), m.group(0))
        if "/home/" in line or "/Users/" in line:
            for m in _HOME_PATH.finditer(line):
                if m.group(2).lower() in HOME_PLACEHOLDERS:
                    continue
                self._add(out, "home-path", SEVERITY_MEDIUM, where, path, lineno,
                          line, m.start(), m.end(), _home_mask(m), m.group(0))
        lowered = line.lower()
        for i, (value, regex) in enumerate(zip(self.private_strings,
                                               self._private_regex)):
            if value.lower() in lowered:
                m = regex.search(line)
                assert m is not None
                self._add(out, "private-string", SEVERITY_HIGH, where, path, lineno,
                          line, m.start(), m.end(), f"[PRIVATE-{i + 1}]", m.group(0))
        return out


# --------------------------------------------------------------------------
# Git access


class Git:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self._env = {k: v for k, v in os.environ.items()
                     if not k.startswith("GIT_")}
        self._env["GIT_CONFIG_NOSYSTEM"] = "1"
        self._env["LC_ALL"] = "C"
        self._batch: subprocess.Popen[bytes] | None = None

    def run(self, *args: str, check: bool = True, input: bytes | None = None) -> bytes:
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.repo), *args],
                capture_output=True, env=self._env, input=input, timeout=600,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AuditError(f"git {args[0]} failed to run: {exc}") from exc
        if check and completed.returncode != 0:
            tail = completed.stderr.decode("utf-8", "replace").strip()[-300:]
            raise AuditError(f"git {' '.join(args[:3])} failed: {tail}")
        return completed.stdout

    def ok(self, *args: str) -> bool:
        completed = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True, env=self._env, timeout=600,
        )
        return completed.returncode == 0

    def rev(self, name: str) -> str:
        out = self.run("rev-parse", "--verify", "--quiet", name + "^{commit}",
                       check=False).decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", out):
            raise AuditError(f"cannot resolve {name!r} to a commit")
        return out

    def is_ancestor(self, older: str, newer: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", older, newer)

    def blob(self, sha: str) -> bytes:
        if self._batch is None:
            self._batch = subprocess.Popen(
                ["git", "-C", str(self.repo), "cat-file", "--batch"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=self._env,
            )
        assert self._batch.stdin and self._batch.stdout
        self._batch.stdin.write(sha.encode() + b"\n")
        self._batch.stdin.flush()
        header = self._batch.stdout.readline().decode().split()
        if len(header) < 3 or header[1] == "missing":
            raise AuditError(f"object {sha} is missing")
        size = int(header[2])
        data = self._batch.stdout.read(size)
        self._batch.stdout.read(1)
        return data

    def close(self) -> None:
        if self._batch is not None:
            assert self._batch.stdin and self._batch.stdout
            self._batch.stdin.close()
            self._batch.stdout.close()
            self._batch.wait(timeout=30)
            self._batch = None

    def ls_tree(self, commit: str) -> list[tuple[str, str, str, str]]:
        """Return (mode, type, sha, path) for every entry, recursively."""
        out = self.run("ls-tree", "-r", "-z", "--full-tree", commit)
        entries = []
        for record in out.split(b"\0"):
            if not record:
                continue
            meta, _, path = record.partition(b"\t")
            mode, typ, sha = meta.decode().split()
            entries.append((mode, typ, sha, path.decode("utf-8", "replace")))
        return entries

    def introduced(self, commit: str) -> list[tuple[str, str, str]]:
        """(new_mode, new_sha, path) of every added/modified entry of a commit,
        against each parent (merges included) or the empty tree for roots."""
        out = self.run("diff-tree", "-r", "-m", "--root", "--no-renames",
                       "--no-abbrev", "--no-commit-id", "-z", commit)
        fields = out.split(b"\0")
        result = []
        i = 0
        while i < len(fields):
            meta = fields[i]
            if not meta.startswith(b":"):
                i += 1
                continue
            parts = meta[1:].decode().split()
            path = fields[i + 1].decode("utf-8", "replace") if i + 1 < len(fields) else ""
            i += 2
            new_mode, new_sha, status = parts[1], parts[3], parts[4]
            if status.startswith("D") or new_sha == ZERO_SHA:
                continue
            result.append((new_mode, new_sha, path))
        return result


# --------------------------------------------------------------------------
# Allowlist


@dataclasses.dataclass(frozen=True)
class AllowRule:
    kind: str
    glob: str
    literal: str
    reason: str
    lineno: int

    def accepts(self, finding: Finding) -> bool:
        if self.kind != "*" and self.kind != finding.kind:
            return False
        if not fnmatch.fnmatchcase(finding.path, self.glob):
            return False
        if self.literal and self.literal not in finding.raw \
                and self.literal not in finding.context:
            return False
        return True


def load_allowlist(path: Path | None) -> list[AllowRule]:
    if path is None or not path.exists():
        return []
    rules: list[AllowRule] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = [p.strip() for p in stripped.split("|")]
        if len(parts) != 4:
            raise AuditError(
                f"{path.name}:{lineno}: expected 'kind | path-glob | literal | reason'"
            )
        kind, glob, literal, reason = parts
        if not kind or not glob:
            raise AuditError(f"{path.name}:{lineno}: kind and path-glob are required")
        if not reason:
            raise AuditError(f"{path.name}:{lineno}: a reason is required")
        rules.append(AllowRule(kind, glob, literal, reason, lineno))
    return rules


# --------------------------------------------------------------------------
# Audit


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:BINARY_SNIFF]


def _parse_gitmodules(text: str) -> dict[str, dict[str, str]]:
    modules: dict[str, dict[str, str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        m = re.fullmatch(r'\[submodule\s+"([^"]+)"\]', line)
        if m:
            current = m.group(1)
            modules.setdefault(current, {})
            continue
        if line.startswith("["):
            current = None
            continue
        if current is not None and "=" in line:
            key, _, value = line.partition("=")
            modules[current][key.strip().lower()] = value.strip()
    return modules


_PUBLIC_GITHUB_URL = re.compile(
    r"https://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+?(?:\.git)?/?"
)


class Audit:
    def __init__(self, repo: Path, base: str, refs: list[str],
                 private_strings: list[str], allowlist: list[AllowRule]) -> None:
        self.git = Git(repo)
        self.redactor = Redactor(private_strings)
        self.scanner = Scanner(private_strings, self.redactor)
        self.allowlist = allowlist
        self.base_name = base
        self.ref_names = refs
        self.findings: list[Finding] = []
        self._seen: set[tuple[object, ...]] = set()
        self._scanned_blobs: dict[tuple[str, str], list[Finding]] = {}
        self.stats: dict[str, int] = {
            "refs": 0, "tree_files": 0, "introduced_commits": 0,
            "introduced_blobs": 0, "upstream_ancestry_commits": 0,
        }

    # helpers ---------------------------------------------------------------
    def _emit(self, finding: Finding, key: tuple[object, ...]) -> None:
        if key in self._seen:
            return
        self._seen.add(key)
        self.findings.append(finding)

    def _short(self, sha: str) -> str:
        return sha[:12]

    def _scan_blob(self, mode: str, sha: str, path: str, where: str,
                   upstream_blobs: set[str]) -> None:
        for f in self.scanner.scan_path(path, where):
            self._emit(f, (f.kind, "path", path, f.raw))
        if mode == "160000":
            return
        cache_key = (sha, path)
        if cache_key not in self._scanned_blobs:
            data = self.git.blob(sha)
            if _is_binary(data):
                found: list[Finding] = []
            else:
                found = self.scanner.scan_text(
                    data.decode("utf-8", "replace"), where, path)
            if sha in upstream_blobs:
                # Byte-identical to published upstream content.
                for f in found:
                    if f.kind != "private-string":
                        f.severity = SEVERITY_INFO
                        f.excerpt_redacted = "(upstream content) " + f.excerpt_redacted
            self._scanned_blobs[cache_key] = found
            for f in found:
                self._emit(f, (f.kind, sha, path, f.line, f.raw))

    def _scan_metadata(self, commit: str) -> None:
        raw = self.git.run("cat-file", "commit", commit).decode("utf-8", "replace")
        header, _, message = raw.partition("\n\n")
        where = self._short(commit)
        lines = [l for l in header.splitlines() if l.startswith(("author ", "committer ", "tagger "))]
        text = "\n".join(lines) + "\n" + message
        for f in self.scanner.scan_text(text, where, "<commit-metadata>"):
            self._emit(f, (f.kind, "meta", commit, f.line, f.raw))

    # phases ------------------------------------------------------------------
    def run(self) -> None:
        try:
            self._run()
        finally:
            self.git.close()

    def _run(self) -> None:
        base = self.git.rev(self.base_name)
        self.stats["upstream_ancestry_commits"] = int(
            self.git.run("rev-list", "--count", base).decode().strip())
        base_tree = self.git.ls_tree(base)
        upstream_blobs = {sha for _, typ, sha, _ in base_tree if typ == "blob"}
        base_modules = self._modules_at(base)
        base_gitlinks = {p: sha for mode, _, sha, p in base_tree if mode == "160000"}

        refs = self.ref_names or self._default_refs()
        resolved: list[tuple[str, str]] = []
        for name in refs:
            sha = self.git.rev(name)
            resolved.append((name, sha))
        self.stats["refs"] = len(resolved)

        for name, sha in resolved:
            self._check_ref_ancestry(name, sha, base)
        self._check_tags(base, {n for n, _ in resolved})

        # (a) trees at each ref.
        for name, sha in resolved:
            if self.git.is_ancestor(sha, base):
                continue  # upstream ancestry (including base itself)
            tree = self.git.ls_tree(sha)
            self.stats["tree_files"] += len(tree)
            for mode, typ, bsha, path in tree:
                self._scan_blob(mode, bsha, path, name, upstream_blobs)
            self._check_submodules(name, sha, tree, base_modules, base_gitlinks)

        # (b) history introduced since base.
        commits: list[str] = []
        seen_commits: set[str] = set()
        for _, sha in resolved:
            out = self.git.run("rev-list", "--reverse", sha, "--not", base).decode()
            for c in out.split():
                if c not in seen_commits:
                    seen_commits.add(c)
                    commits.append(c)
        self.stats["introduced_commits"] = len(commits)
        self._check_history_shape(commits, [s for _, s in resolved], base)
        blobs: set[tuple[str, str]] = set()
        for commit in commits:
            self._scan_metadata(commit)
            for mode, bsha, path in self.git.introduced(commit):
                blobs.add((bsha, path))
                self._scan_blob(mode, bsha, path, self._short(commit), upstream_blobs)
        self.stats["introduced_blobs"] = len(blobs)

        self._apply_allowlist()

    def _default_refs(self) -> list[str]:
        out = self.git.run("for-each-ref", "--format=%(refname)",
                           "refs/heads", "refs/tags").decode().split()
        refs = ["HEAD"] + out
        return refs

    def _check_ref_ancestry(self, name: str, sha: str, base: str) -> None:
        shown = self.redactor.scrub(name)
        for f in self.scanner.scan_path(name, shown):
            if f.kind == "private-string":
                f.path = "<ref-name>"
                self._emit(f, ("ref-private", name))
        if sha != base and self.git.is_ancestor(base, sha):
            return
        if self.git.is_ancestor(sha, base):
            self._emit(Finding(
                "upstream-ancestry", SEVERITY_INFO, shown, "<ref>", None,
                "ref points into upstream history before base; not scanned"),
                ("ancestry", name))
            return
        self._emit(Finding(
            "ref-not-descending", SEVERITY_HIGH, shown, "<ref>", None,
            f"{shown} does not descend from base {self._short(base)}", raw=name),
            ("ref-not-desc", name))

    def _check_tags(self, base: str, audited: set[str]) -> None:
        out = self.git.run("for-each-ref", "--format=%(refname) %(objecttype)",
                           "refs/tags").decode()
        for line in out.splitlines():
            ref, _, objtype = line.partition(" ")
            if ref not in audited:
                sha = self.git.rev(ref)
                self._check_ref_ancestry(ref, sha, base)
            if objtype == "tag":
                raw = self.git.run("cat-file", "tag", ref).decode("utf-8", "replace")
                header, _, message = raw.partition("\n\n")
                tagger = [l for l in header.splitlines() if l.startswith("tagger ")]
                text = "\n".join(tagger) + "\n" + message
                for f in self.scanner.scan_text(text, self.redactor.scrub(ref),
                                                "<tag-metadata>"):
                    self._emit(f, (f.kind, "tag", ref, f.line, f.raw))

    def _check_history_shape(self, commits: list[str], tips: list[str],
                             base: str) -> None:
        if not commits:
            return
        descending = set(self.git.run(
            "rev-list", "--ancestry-path", *tips, "--not", base
        ).decode().split())
        for commit in commits:
            parents = self.git.run("rev-list", "--parents", "-n", "1",
                                   commit).decode().split()[1:]
            where = self._short(commit)
            if not parents:
                self._emit(Finding(
                    "unrelated-history", SEVERITY_HIGH, where, "<commit>", None,
                    "root commit unrelated to base"), ("root", commit))
            if len(parents) > 1:
                for parent in parents:
                    if not self.git.run("merge-base", base, parent,
                                        check=False).strip():
                        self._emit(Finding(
                            "unrelated-history", SEVERITY_HIGH, where, "<commit>",
                            None, f"merge brings in history unrelated to base "
                            f"(parent {self._short(parent)})"),
                            ("merge", commit, parent))
            if commit not in descending:
                self._emit(Finding(
                    "commit-not-descending", SEVERITY_HIGH, where, "<commit>", None,
                    f"commit does not descend from base {self._short(base)}"),
                    ("notdesc", commit))

    def _modules_at(self, commit: str) -> dict[str, dict[str, str]]:
        out = self.git.run("ls-tree", commit, "--", ".gitmodules")
        if not out.strip():
            return {}
        sha = out.decode().split()[2]
        return _parse_gitmodules(self.git.blob(sha).decode("utf-8", "replace"))

    def _check_submodules(self, name: str, sha: str,
                          tree: list[tuple[str, str, str, str]],
                          base_modules: dict[str, dict[str, str]],
                          base_gitlinks: dict[str, str]) -> None:
        modules = self._modules_at(sha)
        by_path = {m.get("path", key): (key, m) for key, m in modules.items()}
        base_by_path = {m.get("path", key): m for key, m in base_modules.items()}
        shown_ref = self.redactor.scrub(name)
        for sub_path, (key, module) in by_path.items():
            url = module.get("url", "")
            shown_url = self.redactor.scrub(url)
            if not _PUBLIC_GITHUB_URL.fullmatch(url):
                self._emit(Finding(
                    "submodule-url", SEVERITY_HIGH, shown_ref, ".gitmodules", None,
                    f"submodule {self.redactor.scrub(sub_path)}: non-public or "
                    f"non-https GitHub URL {shown_url}", raw=url),
                    ("sm-url", sub_path, url))
            upstream = base_by_path.get(sub_path)
            if upstream is not None and upstream.get("url", "") != url:
                self._emit(Finding(
                    "submodule-url-changed", SEVERITY_HIGH, shown_ref, ".gitmodules",
                    None, f"submodule {self.redactor.scrub(sub_path)}: URL changed "
                    f"from upstream to {shown_url}", raw=url),
                    ("sm-changed", sub_path, url))
            elif upstream is None:
                self._emit(Finding(
                    "submodule-added", SEVERITY_INFO, shown_ref, ".gitmodules", None,
                    f"submodule {self.redactor.scrub(sub_path)} not present upstream: "
                    f"{shown_url}", raw=url), ("sm-added", sub_path, url))
        for mode, _, gsha, path in tree:
            if mode != "160000":
                continue
            shown = self.redactor.scrub(path)
            if path not in by_path:
                self._emit(Finding(
                    "submodule-unmapped", SEVERITY_HIGH, shown_ref, shown, None,
                    "gitlink without a .gitmodules entry"), ("sm-unmapped", path))
            elif base_gitlinks.get(path) not in (None, gsha):
                self._emit(Finding(
                    "submodule-commit-changed", SEVERITY_INFO, shown_ref, shown, None,
                    f"gitlink moved to {self._short(gsha)}; verify it is published "
                    "in the submodule's public repository"),
                    ("sm-commit", path, gsha))

    def _apply_allowlist(self) -> None:
        for finding in self.findings:
            for rule in self.allowlist:
                if rule.accepts(finding):
                    finding.accepted = True
                    finding.accept_reason = rule.reason
                    break

    # results -------------------------------------------------------------------
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings
                if f.severity != SEVERITY_INFO and not f.accepted]

    def summary(self) -> dict[str, object]:
        by_kind: dict[str, int] = {}
        for f in self.blocking():
            by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
        return {
            **self.stats,
            "findings": len(self.findings),
            "blocking": len(self.blocking()),
            "accepted": sum(1 for f in self.findings if f.accepted),
            "info": sum(1 for f in self.findings
                        if f.severity == SEVERITY_INFO and not f.accepted),
            "blocking_by_kind": dict(sorted(by_kind.items())),
            "passed": not self.blocking(),
        }


def render_human(audit: Audit) -> str:
    lines: list[str] = []
    order = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_INFO: 2}
    for f in sorted(audit.findings, key=lambda f: (f.accepted, order[f.severity],
                                                   f.kind, f.path, f.line or 0)):
        status = "ACCEPTED" if f.accepted else f.severity.upper()
        loc = f.path + (f":{f.line}" if f.line else "")
        lines.append(f"[{status}] {f.kind} {f.ref_or_commit} {loc}: {f.excerpt_redacted}")
        if f.accepted:
            lines.append(f"    reason: {f.accept_reason}")
    s = audit.summary()
    lines.append("")
    lines.append(
        f"refs={s['refs']} tree_files={s['tree_files']} "
        f"introduced_commits={s['introduced_commits']} "
        f"introduced_blobs={s['introduced_blobs']}")
    lines.append(
        f"upstream ancestry: {s['upstream_ancestry_commits']} commits "
        "(not scanned for introduced content)")
    lines.append(
        f"findings={s['findings']} blocking={s['blocking']} "
        f"accepted={s['accepted']} info={s['info']}")
    lines.append("RESULT: " + ("PASS" if s["passed"] else "FAIL"))
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pre-publication audit.")
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--base", required=True,
                        help="upstream base commit the published refs build on")
    parser.add_argument("--ref", action="append", default=[],
                        help="ref to audit (repeatable; default: HEAD, branches, tags)")
    parser.add_argument("--private-strings", type=Path, default=None)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        repo = args.repo
        if not (repo / ".git").exists() and not (repo / "HEAD").exists():
            raise AuditError(f"{repo} is not a git repository")
        private = load_private_strings(args.private_strings, repo)
        allowlist = load_allowlist(args.allowlist)
        audit = Audit(repo, args.base, args.ref, private, allowlist)
        audit.run()
    except AuditError as exc:
        print(f"audit-public: error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        payload = {
            "findings": [f.to_json() for f in audit.findings],
            "summary": audit.summary(),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_human(audit))
    return 1 if audit.blocking() else 0


if __name__ == "__main__":
    sys.exit(main())
