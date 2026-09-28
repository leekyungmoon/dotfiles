"""Unit tests for tools/audit-public.py against scratch git repositories.

Synthetic secrets are assembled at runtime so this file itself never contains
a string the audit would flag.
"""

from __future__ import annotations

import contextlib
import gzip
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

AUDIT_PATH = REPO_ROOT / "tools" / "audit-public.py"
_spec = importlib.util.spec_from_file_location("audit_public", AUDIT_PATH)
audit = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["audit_public"] = audit
_spec.loader.exec_module(audit)

GH_TOKEN = "gh" + "p_" + "A1b2C3d4" * 4 + "E5f6"
AWS_KEY = "AK" + "IA" + "ABCDEFGH23456789"
HOME_PATH = "/ho" + "me/" + "zed" + "/projects/x"
PRIVATE = "Acme" + "-Laptop"
# A login that is a substring of the public handle (like the real one).
LOGIN = "zed" + "kim"
HANDLE = "lee" + LOGIN


class ScratchRepo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root.parent / "gh-home"
        self.home.mkdir(exist_ok=True)
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.com",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.com",
            "LC_ALL": "C",
        }
        root.mkdir()
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.root), *args], env=self.env,
            capture_output=True, text=True, check=True)
        return completed.stdout.strip()

    def write(self, path: str, text: str) -> None:
        dest = self.root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        self.git("add", "--", path)

    def write_bytes(self, path: str, data: bytes) -> None:
        dest = self.root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        self.git("add", "--", path)

    def remove(self, path: str) -> None:
        self.git("rm", "-q", "--", path)

    def commit(self, message: str) -> str:
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")


class AuditTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = ScratchRepo(self.tmp / "repo")
        self.repo.write("README.md", "upstream project\n")
        self.repo.write("zsh/zshrc", "export EDITOR=vim\n")
        self.base = self.repo.commit("upstream base")
        self.no_allowlist = self.tmp / "none.allowlist"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_audit(self, *extra: str, allowlist: Path | None = None
                  ) -> tuple[int, dict, str]:
        argv = ["--repo", str(self.repo.root), "--base", self.base,
                "--allowlist", str(allowlist or self.no_allowlist), "--json", *extra]
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = audit.main(argv)
        text = out.getvalue()
        payload = json.loads(text) if text.strip() else {}
        return code, payload, text + err.getvalue()

    def kinds(self, payload: dict, *, blocking_only: bool = True) -> list[str]:
        return [f["kind"] for f in payload["findings"]
                if not blocking_only
                or (f["severity"] != "info" and not f.get("accepted"))]


class CleanRepoTests(AuditTestCase):
    def test_clean_repo_passes(self) -> None:
        self.repo.write("tmux/tmux.conf", "set -g mouse on\n")
        self.repo.commit("tmux: enable mouse")
        code, payload, _ = self.run_audit()
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["summary"]["passed"])
        self.assertEqual(payload["summary"]["introduced_commits"], 1)
        self.assertGreaterEqual(payload["summary"]["upstream_ancestry_commits"], 1)

    def test_unchanged_upstream_content_is_not_blocking(self) -> None:
        # Content already published upstream is reported as info only.
        repo = ScratchRepo(self.tmp / "repo2")
        repo.write("notes.txt", f"see {HOME_PATH}\n")
        base = repo.commit("upstream")
        repo.write("other.txt", "hello\n")
        repo.commit("fork")
        self.repo, self.base = repo, base
        code, payload, _ = self.run_audit()
        self.assertEqual(code, 0, payload)
        self.assertIn("home-path", self.kinds(payload, blocking_only=False))


class HistoryTests(AuditTestCase):
    def test_token_in_later_deleted_commit_is_found_and_redacted(self) -> None:
        self.repo.write("secret.txt", f"token {GH_TOKEN}\naws {AWS_KEY}\n")
        leaked = self.repo.commit("oops")
        self.repo.remove("secret.txt")
        self.repo.commit("remove secret")
        code, payload, raw = self.run_audit()
        self.assertEqual(code, 1)
        hits = [f for f in payload["findings"] if f["kind"] == "github-token"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["ref_or_commit"], leaked[:12])
        self.assertEqual(hits[0]["path"], "secret.txt")
        self.assertEqual(hits[0]["line"], 1)
        self.assertIn("aws-access-key", self.kinds(payload))
        # Redaction: only a short prefix of any secret reaches the output.
        self.assertNotIn(GH_TOKEN, raw)
        self.assertNotIn(AWS_KEY, raw)
        self.assertNotIn(GH_TOKEN[:12], raw)
        self.assertIn("[REDACTED]", hits[0]["excerpt_redacted"])

    def test_human_report_is_redacted(self) -> None:
        self.repo.write("cfg", f"api_key = {GH_TOKEN}\n")
        self.repo.commit("add")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = audit.main(["--repo", str(self.repo.root), "--base", self.base,
                               "--allowlist", str(self.no_allowlist)])
        self.assertEqual(code, 1)
        self.assertIn("RESULT: FAIL", out.getvalue())
        self.assertNotIn(GH_TOKEN, out.getvalue())

    def test_private_string_found_and_never_printed(self) -> None:
        strings = self.tmp / "private.txt"
        strings.write_text(f"# local only\n{PRIVATE}\n", encoding="utf-8")
        self.repo.write("zsh/host.zsh", f"HOST={PRIVATE.lower()}\n")
        self.repo.commit(f"configure {PRIVATE.upper()}")
        code, payload, raw = self.run_audit("--private-strings", str(strings))
        self.assertEqual(code, 1)
        hits = [f for f in payload["findings"] if f["kind"] == "private-string"]
        paths = {f["path"] for f in hits}
        self.assertIn("zsh/host.zsh", paths)
        self.assertIn("<commit-metadata>", paths)
        self.assertNotIn(PRIVATE.lower(), raw.lower())
        self.assertTrue(all("[PRIVATE-1]" in f["excerpt_redacted"] for f in hits))

    def test_private_strings_file_inside_repo_is_refused(self) -> None:
        inside = self.repo.root / "private.txt"
        inside.write_text("x\n", encoding="utf-8")
        code, _, raw = self.run_audit("--private-strings", str(inside))
        self.assertEqual(code, 2)
        self.assertIn("outside", raw)

    def test_home_path_found(self) -> None:
        self.repo.write("bin/tool", f"#!/bin/sh\nexec {HOME_PATH}/run\n")
        self.repo.write("bin/ok", "#!/bin/sh\ncd /home/user/work\n")
        self.repo.commit("tools")
        code, payload, raw = self.run_audit()
        self.assertEqual(code, 1)
        hits = [f for f in payload["findings"] if f["kind"] == "home-path"]
        self.assertEqual([(f["path"], f["line"]) for f in hits], [("bin/tool", 2)])
        self.assertNotIn(HOME_PATH, raw)

    def test_forbidden_paths_found(self) -> None:
        self.repo.write(".claude/settings.json", "{}\n")
        self.repo.write("zsh/zsh.d/ssh-password-cache.zsh", "# cache\n")
        self.repo.write("keys/id_ed25519.pub", "ssh-ed25519 AAAA\n")
        self.repo.write("zsh/zshrc DEST", "copy\n")
        self.repo.write(".netrc", "machine x\n")
        self.repo.commit("add junk")
        code, payload, _ = self.run_audit()
        self.assertEqual(code, 1)
        forbidden = {f["path"] for f in payload["findings"]
                     if f["kind"] == "forbidden-path"}
        self.assertEqual(forbidden, {
            ".claude/settings.json", "zsh/zsh.d/ssh-password-cache.zsh",
            "keys/id_ed25519.pub", "zsh/zshrc DEST"})
        self.assertIn("credential-file", self.kinds(payload))

    def test_email_reported_and_allowlist_accepts_it(self) -> None:
        email = "maintainer" + "@" + "corp-fixture.test"
        self.repo.write("docs/CONTACT", f"mail {email}\n")
        self.repo.commit("contact")
        code, payload, raw = self.run_audit()
        self.assertEqual(code, 1)
        self.assertEqual(self.kinds(payload), ["email"])
        self.assertNotIn(email, raw)

        allow = self.tmp / "allow"
        allow.write_text("# rules\nemail | docs/* | | published project contact\n",
                         encoding="utf-8")
        code, payload, _ = self.run_audit(allowlist=allow)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["summary"]["accepted"], 1)
        accepted = [f for f in payload["findings"] if f.get("accepted")]
        self.assertEqual(accepted[0]["accept_reason"], "published project contact")

    def test_allowlist_literal_must_match(self) -> None:
        self.repo.write("docs/CONTACT", "a" + "@" + "one.test\nb" + "@" + "two.test\n")
        self.repo.commit("contact")
        allow = self.tmp / "allow"
        allow.write_text("email | docs/* | a@one | first is public\n", encoding="utf-8")
        code, payload, _ = self.run_audit(allowlist=allow)
        self.assertEqual(code, 1)
        self.assertEqual(self.kinds(payload), ["email"])
        blocking = [f for f in payload["findings"] if not f.get("accepted")]
        self.assertEqual(blocking[0]["line"], 2)

    def test_allowlist_literal_matches_source_line(self) -> None:
        self.repo.write("git/gitconfig", "[url]\n  insteadOf = git" + "@" +
                        "github.com:\n")
        self.repo.commit("ssh remotes are not emails; trailer is accepted",)
        self.repo.git("commit", "-q", "--amend", "-m",
                      "msg\n\nCo-Authored-By: Bot <bot" + "@" + "vendor.test>")
        code, payload, _ = self.run_audit()
        self.assertEqual(code, 1)
        self.assertEqual(self.kinds(payload), ["email"])
        allow = self.tmp / "allow"
        allow.write_text("email | <commit-metadata> | Co-Authored-By: Bot | trailer\n",
                         encoding="utf-8")
        code, payload, _ = self.run_audit(allowlist=allow)
        self.assertEqual(code, 0, payload)

    def test_repository_allowlist_parses(self) -> None:
        rules = audit.load_allowlist(audit.DEFAULT_ALLOWLIST)
        self.assertTrue(all(rule.reason for rule in rules))

    def test_allowlist_rule_without_reason_is_rejected(self) -> None:
        allow = self.tmp / "allow"
        allow.write_text("email | docs/* | |\n", encoding="utf-8")
        code, _, raw = self.run_audit(allowlist=allow)
        self.assertEqual(code, 2)
        self.assertIn("reason", raw)


def _alnum(n: int) -> str:
    return ("Q7w" + "R2t" + "Y5u" + "P9z")[:12] * (n // 12) + "Kx4m9Vb2Nq8L"[: n % 12]


class SecretFormatTests(AuditTestCase):
    """PRIV-1: common secret shapes, including prefixed env assignments."""

    def _lines(self) -> tuple[list[str], list[str]]:
        jwt = "ey" + "J" + _alnum(20) + ".ey" + "J" + _alnum(24) + "." + _alnum(30)
        positives = [
            "export GITHUB" + "_TOKEN=" + _alnum(36),
            "DB_PASS" + "WORD=" + "s3cretValue42x",
            "export HF_TOKEN=" + "hf" + "_" + _alnum(34),
            "export NPM_TOKEN=" + "npm" + "_" + _alnum(36),
            "pass" + "word=" + "Hunter" + "2!Hunter" + "2!",
            "curl -H 'Authorization: Bear" + "er " + jwt + "' https://x.test",
            "Authorization: Bas" + "ic " + "ZGVwbG95OnMzY3JldDQy",
            "git clone https://deploy:" + "pa55" + "word1" + "@git.test/x.git",
            "client_sec" + "ret: \"" + "a8f" + "Pq2" + "Zr7" + "Lm" + "\"",
            "MY_API" + "_KEY = '" + "k3y" + "-" + _alnum(16) + "'",
            "ssh admin@" + "10" + ".20.30.40",
        ]
        negatives = [
            'export GITHUB_TOKEN="$GITHUB_TOKEN"',
            "password = getpass.getpass()",
            "token=${TOKEN:-}",
            "api_key: <your-api-key>",
            "max_tokens=4096",
            "password: required",
            "unknown-token    = red,bold",
            'curl -H "Authorization: Bearer $TOKEN"',
            "SSH_AUTH_SOCK=/run/user/1000/ssh-agent.socket",
            "token_file = ~/.config/app/token",
            "version 1.10.2.3",
        ]
        return positives, negatives

    def test_common_secret_formats_are_flagged(self) -> None:
        positives, negatives = self._lines()
        self.repo.write("zsh/local.zsh", "\n".join(positives + negatives) + "\n")
        self.repo.commit("copy a local rc file")
        code, payload, raw = self.run_audit()
        self.assertEqual(code, 1)
        blocking_lines = {f["line"] for f in payload["findings"]
                          if f["path"] == "zsh/local.zsh" and f["severity"] != "info"}
        for lineno, line in enumerate(positives, start=1):
            with self.subTest(line=lineno):
                self.assertIn(lineno, blocking_lines, "not flagged")
        for lineno in range(len(positives) + 1, len(positives) + len(negatives) + 1):
            with self.subTest(negative=negatives[lineno - len(positives) - 1]):
                self.assertNotIn(lineno, blocking_lines, "false positive")
        for secret in (_alnum(36), "s3cretValue42x", "Hunter" + "2!Hunter" + "2!",
                       "pa55" + "word1", "ZGVwbG95OnMzY3JldDQy"):
            self.assertNotIn(secret, raw)
        kinds = set(self.kinds(payload))
        for kind in ("generic-secret", "huggingface-token", "npm-token", "jwt",
                     "bearer-token", "basic-auth", "url-credentials", "private-ip"):
            self.assertIn(kind, kinds)

    def test_internal_ssh_remote_host_flagged_public_forge_not(self) -> None:
        self.repo.write("git/remotes", "git" + "@" + "github.com:o/r.git\n"
                        "git" + "@" + "git.corp-fixture.test:team/r.git\n")
        self.repo.commit("remotes")
        code, payload, raw = self.run_audit()
        self.assertEqual(code, 1)
        hits = [f for f in payload["findings"] if f["kind"] == "ssh-remote-host"]
        self.assertEqual([f["line"] for f in hits], [2])
        self.assertNotIn("corp-fixture", raw)


class WordPrivateStringTests(AuditTestCase):
    """PRIV-2: a login contained in the public handle can still be audited."""

    def test_word_entry_flags_login_but_not_public_handle(self) -> None:
        strings = self.tmp / "private.txt"
        strings.write_text("word:" + LOGIN + "\n", encoding="utf-8")
        self.repo.write("zsh/env.zsh", "\n".join([
            "export USER=" + LOGIN,
            "cd /mnt/c/Users/" + LOGIN + "/work",
            "cd ~" + LOGIN + "/work",
            "dir C:\\Users\\" + LOGIN + "\\work",
            "url=https://github.com/" + HANDLE + "/dotfiles",
            "echo " + LOGIN.upper() + "_backup",
        ]) + "\n")
        self.repo.write("notes/" + LOGIN + ".txt", "x\n")
        self.repo.commit("env")
        code, payload, raw = self.run_audit("--private-strings", str(strings))
        self.assertEqual(code, 1)
        private = [f for f in payload["findings"] if f["kind"] == "private-string"]
        lines = {f["line"] for f in private if f["path"] == "zsh/env.zsh"}
        self.assertEqual(lines, {1, 2, 3, 4, 6})
        self.assertTrue(any(f["line"] is None and f["path"].startswith("notes/")
                            for f in private), private)
        self.assertNotIn(LOGIN, raw.replace(HANDLE, ""))

    def test_word_entry_in_repo_without_the_login_passes(self) -> None:
        strings = self.tmp / "private.txt"
        strings.write_text("# local\nword:" + LOGIN + "\n", encoding="utf-8")
        self.repo.write("README.md", "https://github.com/" + HANDLE + "/dotfiles\n")
        self.repo.commit("readme")
        code, payload, _ = self.run_audit("--private-strings", str(strings))
        self.assertEqual(code, 0, payload)

    def test_empty_word_entry_is_rejected(self) -> None:
        strings = self.tmp / "private.txt"
        strings.write_text("word:\n", encoding="utf-8")
        self.repo.write("a.txt", "a\n")
        self.repo.commit("a")
        code, _, raw = self.run_audit("--private-strings", str(strings))
        self.assertEqual(code, 2)
        self.assertIn("empty", raw)

    def test_wsl_windows_and_tilde_home_paths(self) -> None:
        name = "zed" + "doe"
        self.repo.write("bin/paths", "\n".join([
            "cd /mnt/c/Users/" + name + "/work",
            "cd C:/Users/" + name + "/work",
            "cd ~" + name + "/work",
            "cd ~/work",
            "see https://launchpad.net/~git-core/+archive",
            "cd /mnt/c/Users/user/work",
        ]) + "\n")
        self.repo.commit("paths")
        code, payload, raw = self.run_audit()
        self.assertEqual(code, 1)
        hits = sorted(f["line"] for f in payload["findings"] if f["kind"] == "home-path")
        self.assertEqual(hits, [1, 2, 3])
        self.assertNotIn(name, raw)


class BinaryBlobTests(AuditTestCase):
    """PRIV-4: binary and non-UTF-8 blobs are scanned as bytes."""

    def _private_file(self, *values: str) -> Path:
        strings = self.tmp / "private.txt"
        strings.write_text("\n".join(values) + "\n", encoding="utf-8")
        return strings

    def by_path(self, payload: dict) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for f in payload["findings"]:
            if f["severity"] != "info" and not f.get("accepted"):
                out.setdefault(f["path"], set()).add(f["kind"])
        return out

    def test_binary_and_encoded_blobs_are_scanned(self) -> None:
        company = "\uc608\uc2dc\ud68c\uc0ac"  # synthetic non-ASCII private string
        self.repo.write_bytes("ev/blob.bin", b"\0\x01" + GH_TOKEN.encode()
                              + b" " + HOME_PATH.encode() + b"\0")
        self.repo.write_bytes("ev/utf16.txt", ("path " + HOME_PATH + "\n").encode("utf-16"))
        self.repo.write_bytes("ev/note.gz", gzip.compress(HOME_PATH.encode() + b"\n"))
        self.repo.write_bytes("ev/shot.png", b"\x89PNG\r\n\x1a\n\0\0" + PRIVATE.encode())
        self.repo.write_bytes("ev/legacy.txt", ("memo " + company + "\n").encode("cp949"))
        self.repo.write_bytes("ev/utf8.bin", b"\0" + company.encode("utf-8"))
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            content = ("token " + GH_TOKEN + "\n").encode()
            info = tarfile.TarInfo("ho" + "me/zed/.zshrc")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        self.repo.write_bytes("ev/snap.tar.gz", buf.getvalue())
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("cfg/app.ini", "api_key = " + GH_TOKEN + "\n")
        self.repo.write_bytes("ev/cfg.zip", zbuf.getvalue())
        self.repo.commit("binary evidence")
        code, payload, raw = self.run_audit(
            "--private-strings", str(self._private_file(PRIVATE, company)))
        self.assertEqual(code, 1)
        found = self.by_path(payload)
        self.assertTrue({"github-token", "home-path", "binary-blob"} <= found["ev/blob.bin"])
        self.assertIn("home-path", found["ev/utf16.txt"])
        self.assertIn("home-path", found["ev/note.gz!<decompressed>"])
        self.assertIn("private-string", found["ev/shot.png"])
        self.assertIn("private-string", found["ev/legacy.txt"])
        self.assertNotIn("binary-blob", found["ev/legacy.txt"])  # text, not binary
        self.assertIn("private-string", found["ev/utf8.bin"])
        member = [p for p in found if p.startswith("ev/snap.tar.gz!")]
        self.assertEqual(len(member), 1, found)
        self.assertTrue({"github-token", "home-path"} <= found[member[0]])
        self.assertIn("github-token", found["ev/cfg.zip!/cfg/app.ini"])
        self.assertNotIn(GH_TOKEN, raw)
        self.assertNotIn(HOME_PATH, raw)
        self.assertNotIn(PRIVATE.lower(), raw.lower())
        self.assertNotIn(company, raw)

    def test_clean_binary_blocks_until_allowlisted(self) -> None:
        self.repo.write_bytes("assets/icon.png", b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR")
        self.repo.commit("icon")
        code, payload, _ = self.run_audit()
        self.assertEqual(code, 1)
        self.assertEqual(self.kinds(payload), ["binary-blob"])
        allow = self.tmp / "allow"
        allow.write_text("binary-blob | assets/*.png | | reviewed icon, no text\n",
                         encoding="utf-8")
        code, payload, _ = self.run_audit(allowlist=allow)
        self.assertEqual(code, 0, payload)

    def test_unexpandable_or_oversized_binary_is_unscanned(self) -> None:
        self.repo.write_bytes("a.zst", b"\x28\xb5\x2f\xfd\0\0" + b"x" * 32)
        self.repo.write_bytes("big.bin", b"\0" * 64)
        self.repo.commit("opaque")
        saved = audit.MAX_BINARY_SCAN
        audit.MAX_BINARY_SCAN = 32
        try:
            code, payload, _ = self.run_audit()
        finally:
            audit.MAX_BINARY_SCAN = saved
        self.assertEqual(code, 1)
        found = self.by_path(payload)
        self.assertIn("binary-unscanned", found["a.zst"])
        self.assertIn("binary-unscanned", found["big.bin"])

    def test_gzip_bomb_is_capped(self) -> None:
        self.repo.write_bytes("bomb.gz", gzip.compress(b"\0" * (1 << 20)))
        self.repo.commit("bomb")
        saved = audit.MAX_EXPANDED
        audit.MAX_EXPANDED = 1024
        try:
            code, payload, _ = self.run_audit()
        finally:
            audit.MAX_EXPANDED = saved
        self.assertEqual(code, 1)
        self.assertIn("binary-unscanned", self.by_path(payload)["bomb.gz"])


class BaseCheckTests(AuditTestCase):
    """PRIV-5: the base must be below the audited refs and published."""

    def test_base_equal_to_ref_is_refused(self) -> None:
        self.repo.write("secret.txt", "token " + GH_TOKEN + "\n")
        tip = self.repo.commit("fork")
        self.base = tip
        code, payload, raw = self.run_audit("--ref", "main")
        self.assertEqual(code, 2, payload)
        self.assertIn("is the base", raw)

    def test_ref_below_base_is_refused(self) -> None:
        self.repo.git("branch", "old", self.base)
        self.repo.write("secret.txt", "token " + GH_TOKEN + "\n")
        self.base = self.repo.commit("fork")
        code, _, raw = self.run_audit("--ref", "old")
        self.assertEqual(code, 2)
        self.assertIn("ancestor of --base", raw)

    def test_default_refs_all_at_base_are_refused(self) -> None:
        code, _, raw = self.run_audit()
        self.assertEqual(code, 2)
        self.assertIn("nothing would be scanned", raw)

    def _publish_upstream(self) -> Path:
        upstream = self.tmp / "upstream.git"
        subprocess.run(["git", "init", "-q", "--bare", str(upstream)],
                       env=self.repo.env, check=True, capture_output=True)
        # Push by path first: pushing through the remote would already
        # create the remote-tracking ref that only a fetch should create.
        self.repo.git("push", "-q", str(upstream), f"{self.base}:refs/heads/main")
        self.repo.git("remote", "add", "upstream", str(upstream))
        return upstream

    def test_base_published_in_upstream_is_verified(self) -> None:
        self._publish_upstream()
        self.repo.git("fetch", "-q", "upstream")
        self.repo.write("a.txt", "a\n")
        self.repo.commit("fork")
        code, payload, _ = self.run_audit("--ref", "main")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["summary"]["upstream_verified"], "upstream/main")

    def test_unpublished_base_is_refused(self) -> None:
        self._publish_upstream()
        self.repo.git("fetch", "-q", "upstream")
        self.repo.write("secret.txt", "token " + GH_TOKEN + "\n")
        local = self.repo.commit("local only, never published")
        self.repo.write("a.txt", "a\n")
        self.repo.commit("fork")
        self.base = local
        code, _, raw = self.run_audit("--ref", "main")
        self.assertEqual(code, 2)
        self.assertIn("not in the published history", raw)
        code, payload, _ = self.run_audit("--ref", "main", "--upstream-remote", "")
        self.assertEqual(code, 0, payload)  # explicitly skipped: base trusted
        self.assertIsNone(payload["summary"]["upstream_verified"])

    def test_configured_but_unfetched_upstream_is_refused(self) -> None:
        self._publish_upstream()
        self.repo.write("a.txt", "a\n")
        self.repo.commit("fork")
        code, _, raw = self.run_audit("--ref", "main")
        self.assertEqual(code, 2)
        self.assertIn("git fetch upstream", raw)

    def test_human_report_says_whether_upstream_was_verified(self) -> None:
        self.repo.write("a.txt", "a\n")
        self.repo.commit("fork")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            audit.main(["--repo", str(self.repo.root), "--base", self.base,
                        "--allowlist", str(self.no_allowlist)])
        self.assertIn("upstream: NOT verified", out.getvalue())


class SubmoduleTests(AuditTestCase):
    def _gitmodules(self, url: str) -> None:
        self.repo.write(".gitmodules",
                        f'[submodule "zsh/fasd"]\n\tpath = zsh/fasd\n\turl = {url}\n')

    def test_changed_submodule_url_flagged(self) -> None:
        repo = ScratchRepo(self.tmp / "repo2")
        self.repo = repo
        self._gitmodules("https://github.com/clvv/fasd.git")
        repo.write("README.md", "x\n")
        seed = repo.commit("seed")
        repo.git("update-index", "--add", "--cacheinfo", f"160000,{seed},zsh/fasd")
        self.base = repo.commit("upstream with submodule")
        repo.write("notes.txt", "fork\n")
        repo.commit("fork keeps the submodule")
        code, payload, _ = self.run_audit()
        self.assertEqual(code, 0, payload)

        self._gitmodules("git" + "@" + "git.internal.test:team/fasd.git")
        repo.commit("point fasd elsewhere")
        code, payload, _ = self.run_audit()
        self.assertEqual(code, 1)
        kinds = self.kinds(payload)
        self.assertIn("submodule-url", kinds)
        self.assertIn("submodule-url-changed", kinds)

    def _upstream_with_fasd(self) -> tuple[ScratchRepo, str]:
        repo = ScratchRepo(self.tmp / "repo2")
        self.repo = repo
        self._gitmodules("https://github.com/clvv/fasd.git")
        repo.write("README.md", "x\n")
        seed = repo.commit("seed")
        repo.git("update-index", "--add", "--cacheinfo", f"160000,{seed},zsh/fasd")
        self.base = repo.commit("upstream with submodule")
        return repo, seed

    def test_added_github_submodule_blocks_until_allowlisted(self) -> None:
        repo, seed = self._upstream_with_fasd()
        url = "https://github.com/" + HANDLE + "/private-work-notes.git"
        repo.write(".gitmodules",
                   '[submodule "zsh/fasd"]\n\tpath = zsh/fasd\n'
                   '\turl = https://github.com/clvv/fasd.git\n'
                   f'[submodule "notes"]\n\tpath = notes\n\turl = {url}\n')
        repo.git("update-index", "--add", "--cacheinfo", f"160000,{seed},notes")
        repo.commit("add notes submodule")
        code, payload, _ = self.run_audit()
        self.assertEqual(code, 1)
        self.assertEqual(self.kinds(payload), ["submodule-added"])
        allow = self.tmp / "allow"
        allow.write_text(f"submodule-added | .gitmodules | {url} | verified public\n",
                         encoding="utf-8")
        code, payload, _ = self.run_audit(allowlist=allow)
        self.assertEqual(code, 0, payload)

    def test_moved_gitlink_blocks(self) -> None:
        repo, _ = self._upstream_with_fasd()
        repo.git("update-index", "--cacheinfo", f"160000,{self.base},zsh/fasd")
        repo.commit("bump fasd")
        code, payload, _ = self.run_audit()
        self.assertEqual(code, 1)
        self.assertEqual(self.kinds(payload), ["submodule-commit-changed"])

    def test_unmapped_gitlink_flagged(self) -> None:
        self.repo.git("update-index", "--add", "--cacheinfo",
                      f"160000,{self.base},vendor/thing")
        self.repo.commit("add gitlink")
        code, payload, _ = self.run_audit()
        self.assertEqual(code, 1)
        self.assertIn("submodule-unmapped", self.kinds(payload))


class RefShapeTests(AuditTestCase):
    def test_ref_not_descending_from_base_flagged(self) -> None:
        self.repo.git("checkout", "-q", "--orphan", "other")
        self.repo.git("rm", "-rq", "--cached", ".")
        self.repo.write("other.txt", "unrelated\n")
        self.repo.commit("unrelated root")
        code, payload, _ = self.run_audit("--ref", "other")
        self.assertEqual(code, 1)
        kinds = self.kinds(payload)
        self.assertIn("ref-not-descending", kinds)
        self.assertIn("unrelated-history", kinds)
        self.assertIn("commit-not-descending", kinds)

    def test_merge_of_unrelated_history_flagged(self) -> None:
        self.repo.git("checkout", "-q", "--orphan", "other")
        self.repo.git("rm", "-rq", "--cached", ".")
        (self.repo.root / "README.md").unlink()
        (self.repo.root / "zsh" / "zshrc").unlink()
        self.repo.write("other.txt", "unrelated\n")
        self.repo.commit("unrelated root")
        self.repo.git("checkout", "-q", "-f", "main")
        self.repo.git("merge", "-q", "--allow-unrelated-histories", "-m", "merge",
                      "other")
        code, payload, _ = self.run_audit("--ref", "main")
        self.assertEqual(code, 1)
        kinds = self.kinds(payload)
        self.assertNotIn("ref-not-descending", kinds)
        self.assertIn("unrelated-history", kinds)
        self.assertIn("commit-not-descending", kinds)

    def test_upstream_tag_is_ancestry_and_fork_tag_scanned(self) -> None:
        self.repo.git("tag", "v-upstream", self.base)
        self.repo.write("a.txt", "a\n")
        self.repo.commit("fork")
        self.repo.git("tag", "-a", "v1", "-m", "release " + HOME_PATH)
        code, payload, raw = self.run_audit()
        self.assertEqual(code, 1)
        kinds = self.kinds(payload, blocking_only=False)
        self.assertIn("upstream-ancestry", kinds)
        tag_hits = [f for f in payload["findings"] if f["path"] == "<tag-metadata>"]
        self.assertEqual([f["kind"] for f in tag_hits], ["home-path"])
        self.assertNotIn(HOME_PATH, raw)


if __name__ == "__main__":
    unittest.main()
