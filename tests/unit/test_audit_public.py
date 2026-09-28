"""Unit tests for tools/audit-public.py against scratch git repositories.

Synthetic secrets are assembled at runtime so this file itself never contains
a string the audit would flag.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
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
        code, payload, _ = self.run_audit()
        self.assertEqual(code, 0, payload)

        self._gitmodules("git@git.internal.test:team/fasd.git")
        repo.commit("point fasd elsewhere")
        code, payload, _ = self.run_audit()
        self.assertEqual(code, 1)
        kinds = self.kinds(payload)
        self.assertIn("submodule-url", kinds)
        self.assertIn("submodule-url-changed", kinds)

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
