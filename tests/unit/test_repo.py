"""Unit tests for installer.repo: read-only helpers for the ~/.dotfiles checkout."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from installer import repo  # noqa: E402
from installer.runner import DryRunRunner, Runner  # noqa: E402


def _isolated_env(root: Path) -> dict[str, str]:
    (root / "githome").mkdir(exist_ok=True)
    config = root / "gitconfig"
    config.write_text("[user]\n\tname = Fixture\n\temail = f@example.invalid\n"
                      "[init]\n\tdefaultBranch = main\n"
                      "[protocol \"file\"]\n\tallow = always\n")
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(root / "githome"), "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C"}


class GitRunner(Runner):
    """Real git with a private config so the user's ~/.gitconfig is ignored."""

    def __init__(self, root: Path):
        self.env = _isolated_env(root)
        self.calls: list[list[str]] = []

    def run(self, argv, *, timeout, check=True, env=None, input=None, cwd=None,
            read_only=False):
        self.calls.append([str(a) for a in argv])
        return super().run(argv, timeout=timeout, check=check, env=self.env,
                           input=input, cwd=cwd, read_only=read_only)

    def git(self, *args) -> str:
        return subprocess.run(["git", *args], env=self.env, check=True,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE).stdout.decode().strip()

    def commit(self, repo_dir: Path, name: str, text: str) -> str:
        (repo_dir / name).write_text(text)
        self.git("-C", str(repo_dir), "add", name)
        self.git("-C", str(repo_dir), "commit", "--quiet", "-m", name)
        return self.git("-C", str(repo_dir), "rev-parse", "HEAD")


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pdf-repo-")
        self.root = Path(self._tmp.name)
        self.runner = GitRunner(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def make_repo(self, name: str) -> Path:
        path = self.root / name
        self.runner.git("init", "--quiet", str(path))
        return path


class UrlTests(unittest.TestCase):
    def test_source_url_and_ref_from_env(self):
        self.assertEqual(repo.source_url({}), repo.DEFAULT_REPO_URL)
        self.assertEqual(repo.DEFAULT_REPO_URL, "https://github.com/leekyungmoon/dotfiles.git")
        self.assertEqual(repo.source_url({"DOTFILES_REPO_URL": " /m.git "}), "/m.git")
        self.assertIsNone(repo.source_ref({}))
        self.assertEqual(repo.source_ref({"DOTFILES_REF": "dev"}), "dev")

    def test_same_repository_spellings(self):
        https = "https://github.com/leekyungmoon/dotfiles"
        for other in (https + ".git", https + "/", "git@github.com:leekyungmoon/dotfiles.git",
                      "ssh://git@github.com/leekyungmoon/dotfiles.git"):
            self.assertTrue(repo.same_repository(https, other), other)
        self.assertFalse(repo.same_repository(https, "https://github.com/wookayin/dotfiles"))
        self.assertFalse(repo.same_repository(None, https))
        self.assertTrue(repo.same_repository("/tmp/mirror.git", "/tmp/mirror.git/"))


class CheckoutTests(RepoTestCase):
    def test_checkout_info_and_dirtiness(self):
        upstream = self.make_repo("upstream")
        head = self.runner.commit(upstream, "a", "committed")
        checkout = self.root / "checkout"
        self.runner.git("clone", "--quiet", str(upstream), str(checkout))
        info = repo.checkout_info(self.runner, checkout)
        self.assertEqual((info.commit, info.origin_url, info.branch),
                         (head, str(upstream), "main"))
        self.assertEqual(repo.short_head(self.runner, checkout), head[:len(
            repo.short_head(self.runner, checkout))])
        self.assertFalse(repo.is_dirty(self.runner, checkout))
        (checkout / "untracked").write_text("x")
        self.assertFalse(repo.is_dirty(self.runner, checkout))  # untracked is fine
        (checkout / "a").write_text("edit")
        self.assertTrue(repo.is_dirty(self.runner, checkout))

    def test_not_a_checkout(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.assertFalse(repo.is_checkout(plain))
        with self.assertRaises(repo.RepoError):
            repo.checkout_info(self.runner, plain)
        self.assertIsNone(repo.remote_url(self.runner, plain))

    def test_helpers_are_read_only_under_dry_run(self):
        upstream = self.make_repo("ro")
        self.runner.commit(upstream, "a", "1")
        env = self.runner.env

        class IsolatedDry(DryRunRunner):
            def run(self, argv, **kw):
                kw["env"] = env
                return super().run(argv, **kw)

        dry = IsolatedDry()
        info = repo.checkout_info(dry, upstream)
        self.assertEqual(len(info.commit), 40)
        repo.is_dirty(dry, upstream)
        self.assertEqual(dry.recorded, [])

    def test_is_ancestor(self):
        work = self.make_repo("work")
        a = self.runner.commit(work, "f", "a")
        b = self.runner.commit(work, "f", "b")
        self.assertTrue(repo.is_ancestor(self.runner, work, a, b))
        self.assertFalse(repo.is_ancestor(self.runner, work, b, a))
        with self.assertRaises(repo.RepoError):
            repo.is_ancestor(self.runner, work, "0" * 40, b)


class SubmoduleTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        sub = self.make_repo("sub")
        self.runner.commit(sub, "s", "sub")
        self.upstream = self.make_repo("super")
        self.runner.commit(self.upstream, "a", "1")
        self.runner.git("-C", str(self.upstream), "-c", "protocol.file.allow=always",
                        "submodule", "--quiet", "add", str(sub), "mod")
        self.runner.git("-C", str(self.upstream), "commit", "--quiet", "-m", "sub")

    def test_uninitialized_submodule_is_reported_and_fixed_like_upstream(self):
        shallow = self.root / "noinit"
        self.runner.git("clone", "--quiet", str(self.upstream), str(shallow))
        self.assertEqual(repo.submodule_issues(self.runner, shallow), [("mod", "-")])
        with self.assertRaises(repo.RepoError) as caught:
            repo.verify_submodules(self.runner, shallow)
        self.assertIn("not initialized", str(caught.exception))
        repo.update_submodules(self.runner, shallow)
        self.assertIn(["git", "-C", str(shallow), "submodule", "update", "--init",
                       "--recursive", "--jobs", "8"], self.runner.calls)
        repo.verify_submodules(self.runner, shallow)
        self.assertEqual((shallow / "mod" / "s").read_text(), "sub")

    def test_recursive_clone_has_no_issues(self):
        full = self.root / "full"
        self.runner.git("clone", "--quiet", "--recursive", str(self.upstream), str(full))
        self.assertEqual(repo.submodule_issues(self.runner, full), [])


class GitignoreTests(unittest.TestCase):
    def test_bytecode_is_ignored(self):
        lines = (REPO_ROOT / ".gitignore").read_text().splitlines()
        self.assertIn("__pycache__/", lines)


if __name__ == "__main__":
    unittest.main()
