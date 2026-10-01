"""Unit tests for install.py and installer.phases: parsing, gates and seams."""

from __future__ import annotations

import inspect
import io
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from installer import phases, ui  # noqa: E402
from installer.phases import PhaseResult  # noqa: E402
from installer.platform import Platform, Target  # noqa: E402
from installer.runner import Runner  # noqa: E402


class ScriptedRunner(Runner):
    def __init__(self, responses=None, which=None):
        self.responses = responses or {}
        self.whiches = which or {}
        self.calls = []

    def run(self, argv, *, timeout, check=True, env=None, input=None, cwd=None,
            read_only=False):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        rc = self.responses.get(tuple(argv[:3]), 0)
        return subprocess.CompletedProcess(argv, rc, b"", b"")

    def which(self, name):
        return self.whiches.get(name)


def make_target(home: Path) -> Target:
    return Target(uid=os.getuid(), gid=os.getgid(), username="fixture", home=home,
                  data_home=home / ".local/share", state_home=home / ".local/state",
                  config_home=home / ".config", cache_home=home / ".cache")


class TempHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pdf-cli-")
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.target = make_target(self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def ctx(self, runner=None) -> install.Context:
        return install.Context(target=self.target,
                               platform=Platform("ubuntu", "24.04", "amd64"),
                               runner=runner or ScriptedRunner(), env={},
                               run_id="20260101T000000Z-0123abcd")


class ParserTests(unittest.TestCase):
    def parse(self, *argv):
        return install.parse_args(list(argv))

    def test_bare_invocation_is_install(self):
        args = self.parse()
        self.assertEqual(args.command, "install")
        self.assertFalse(args.force or args.dry_run or args.no_packages)

    def test_upstream_style_install_flags(self):
        args = self.parse("-f", "--skip-vimplug", "--skip-zplug", "--no-packages",
                          "--no-gui", "--no-shell-change", "--dry-run")
        self.assertEqual(args.command, "install")
        opts = install.options_from(args)
        self.assertEqual(opts, install.Options(True, True, True, True, True, True, True))
        self.assertTrue(self.parse("--force").force)
        self.assertEqual(self.parse("install", "--no-gui").command, "install")
        self.assertTrue(self.parse("repair", "--skip-zplug").skip_zplug)

    def test_subcommands(self):
        self.assertTrue(self.parse("status", "--json").json)
        args = self.parse("restore", "--run", "20260101T000000Z-0123abcd",
                          "--id", "zshrc", "--id", "vimrc", "--force")
        self.assertEqual(args.ids, ["zshrc", "vimrc"])
        self.assertTrue(args.force)
        self.assertTrue(self.parse("restore", "--baseline").baseline)
        self.assertTrue(self.parse("gui-apply", "--autostart").autostart)
        self.assertEqual(self.parse("repair").command, "repair")
        args = self.parse("packages", "--only", "fzf", "--force")
        self.assertEqual((args.only, args.force), (["fzf"], True))

    def test_hidden_location_override(self):
        self.assertTrue(self.parse("--allow-any-location").allow_any_location)
        help_text = install.build_parser().format_help()
        self.assertNotIn("allow-any-location", help_text)
        listed = [line.split()[0] for line in help_text.splitlines()
                  if line.startswith("    ") and line.split()]
        self.assertIn("gui-apply", listed)
        self.assertNotIn("packages", listed)
        self.assertNotIn("SUPPRESS", help_text)

    def test_invalid_combinations(self):
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            for argv in (["restore"], ["restore", "--baseline", "--run", "x"],
                         ["frobnicate"], ["update"], ["--from-checkout", "/tmp"],
                         ["--no-such-flag"]):
                with self.assertRaises(SystemExit, msg=argv):
                    self.parse(*argv)


class MainGateTests(TempHome):
    def passwd(self, uid, shell="/usr/bin/zsh"):
        entry = pwd.struct_passwd(("fixture", "x", uid, uid, "", str(self.home), shell))

        def getpwuid(requested):
            if requested != uid:
                raise KeyError(requested)
            return entry
        return getpwuid

    def run_main(self, argv, **kw):
        out, err = io.StringIO(), io.StringIO()
        kw.setdefault("env", {"HOME": str(self.home), "NO_COLOR": "1"})
        kw.setdefault("euid", os.getuid())
        kw.setdefault("getpwuid", self.passwd(kw["euid"]))
        kw.setdefault("detect", lambda: Platform("ubuntu", "24.04", "amd64"))
        kw.setdefault("prompt", False)
        kw.setdefault("interactive", lambda argv: self.fail(f"interactive {argv}"))
        with redirect_stdout(out), redirect_stderr(err):
            rc = install.main(argv, **kw)
        return rc, out.getvalue(), err.getvalue()

    def test_root_is_refused_before_anything(self):
        runner = ScriptedRunner()
        rc, out, err = self.run_main(["--no-packages"], euid=0, runner=runner)
        self.assertEqual(rc, 2)
        self.assertIn("root", err)
        self.assertIn("@leekyungmoon's", out)  # the logo comes first, like upstream
        self.assertEqual(runner.calls, [])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_unsupported_platform_is_refused(self):
        rc, _, err = self.run_main([], runner=ScriptedRunner(),
                                   detect=lambda: Platform("debian", "12", "amd64"))
        self.assertEqual(rc, 2)
        self.assertIn("debian", err)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_unsupported_release_and_arch_are_refused(self):
        for platform in (Platform("ubuntu", "20.04", "amd64"),
                         Platform("ubuntu", "24.04", "riscv64")):
            rc, _, err = self.run_main([], runner=ScriptedRunner(),
                                       detect=lambda p=platform: p)
            self.assertEqual(rc, 2, platform)
            self.assertEqual(list(self.home.iterdir()), [])

    def test_run_outside_dotfiles_is_refused(self):
        elsewhere = Path(self._tmp.name) / "elsewhere"
        elsewhere.mkdir()
        runner = ScriptedRunner()
        for argv in ([], ["repair"], ["gui-apply"], ["packages", "--only", "fzf"]):
            rc, _, err = self.run_main(argv, runner=runner, here=elsewhere)
            self.assertEqual(rc, 2, argv)
            self.assertIn("clone", err)
            self.assertIn(str(self.home / ".dotfiles"), err)
            self.assertIn("git clone --recursive", err)
        self.assertEqual(runner.calls, [])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_dotfiles_symlink_to_checkout_is_accepted(self):
        real = Path(self._tmp.name) / "real-checkout"
        real.mkdir()
        os.symlink(real, self.home / ".dotfiles")
        self.assertIsNone(install.location_error(real, self.target))
        self.assertIsNone(install.location_error(self.home / ".dotfiles", self.target))
        self.assertIsNotNone(install.location_error(self.home, self.target))

    def test_allow_any_location_reaches_the_dry_run_plan(self):
        from installer.runner import DryRunRunner
        rc, out, _ = self.run_main(["--dry-run", "--no-packages", "--allow-any-location"],
                                   here=Path(self._tmp.name), runner=DryRunRunner())
        # Not a checkout, so preflight fails, but only after the location gate.
        self.assertIn("Checking platform", out)
        self.assertIn("is not a git checkout", out)
        self.assertIn("dry run: nothing was changed", out)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_status_and_restore_work_from_anywhere(self):
        rc, out, _ = self.run_main(["status"], here=Path("/"))
        self.assertEqual(rc, 0)
        self.assertIn(str(self.home / ".dotfiles"), out)

    def test_status_on_empty_home(self):
        rc, out, _ = self.run_main(["status", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertIsNone(payload["last_run"])

    def test_restore_rejects_bad_run_id(self):
        rc, _, _ = self.run_main(["restore", "--run", "../../etc"])
        self.assertEqual(rc, 2)


class SeamTests(TempHome):
    def test_disabled_seams_are_skipped(self):
        ctx = self.ctx()
        self.assertEqual(install.packages_phase(ctx, disabled=True, dry_run=False).status,
                         "SKIPPED")
        self.assertEqual(install.gui_phase(ctx, disabled=True).status, "SKIPPED")

    def test_absent_seams_are_skipped_not_passed(self):
        self.assertIsNone(install.load_seam("no_such_module_xyz", "run"))
        ctx = self.ctx()
        with mock.patch.object(install, "load_seam", return_value=None):
            p = install.packages_phase(ctx, disabled=False, dry_run=False)
            g = install.gui_phase(ctx, disabled=False)
        self.assertEqual((p.status, p.reasons), ("SKIPPED", ["packages-phase-not-available"]))
        self.assertEqual((g.status, g.reasons), ("SKIPPED", ["gui-phase-not-available"]))

    def test_seam_results_are_coerced_and_errors_fail(self):
        ctx = self.ctx()
        seen = {}

        def fake_packages(target, platform, runner, *, dry_run):
            seen["dry_run"] = dry_run
            return {"phase": "packages", "status": "AUTH_REQUIRED", "reasons": ["x"],
                    "details": {}}

        with mock.patch.object(install, "load_seam", return_value=fake_packages):
            result = install.packages_phase(ctx, disabled=False, dry_run=True)
        self.assertEqual(result.status, "AUTH_REQUIRED")
        self.assertTrue(seen["dry_run"])

        def boom(target, runner, env):
            raise RuntimeError("no display")

        with mock.patch.object(install, "load_seam", return_value=boom):
            result = install.gui_phase(ctx, disabled=False)
        self.assertEqual(result.status, "FAIL")

        with mock.patch.object(install, "load_seam",
                               return_value=lambda *a, **k: {"status": "BOGUS"}):
            result = install.gui_phase(ctx, disabled=False)
        self.assertEqual(result.status, "FAIL")

    def test_extra_entries_from_packages_and_gui(self):
        from installer.transaction import DesiredEntry
        ctx = self.ctx()
        gui_entry = DesiredEntry("gui-autostart", self.home / ".config/autostart/x.desktop",
                                 "file", content=b"x", mode=0o644)
        link = {"id": "tool-fzf", "dest": str(self.home / ".local/bin/fzf"),
                "link_text": "/opt/fzf"}

        def seam(module, name):
            return {("packages", "link_requests"): lambda details: [
                        DesiredEntry(e["id"], Path(e["dest"]), "symlink",
                                     link_text=e["link_text"]) for e in details["links"]],
                    ("gui", "autostart_desired_entry"): lambda target: gui_entry,
                    }.get((module, name))

        packages = PhaseResult("packages", "PASS", [], {"links": [link]})
        with mock.patch.object(install, "load_seam", side_effect=seam):
            entries, warnings = install.extra_desired_entries(ctx, packages, no_gui=False)
            self.assertEqual([e.id for e in entries], ["tool-fzf", "gui-autostart"])
            self.assertEqual(warnings, [])
            entries, _ = install.extra_desired_entries(ctx, packages, no_gui=True)
            self.assertEqual([e.id for e in entries], ["tool-fzf"])

        def broken(module, name):
            if name == "autostart_desired_entry":
                def fail(target):
                    raise OSError("template missing")
                return fail
            return None

        with mock.patch.object(install, "load_seam", side_effect=broken):
            entries, warnings = install.extra_desired_entries(ctx, None, no_gui=False)
        self.assertEqual(entries, [])
        self.assertIn("template missing", warnings[0])

    def test_packages_command_rejects_unknown_names_before_mutation(self):
        runner = ScriptedRunner()
        with redirect_stderr(io.StringIO()):
            rc = install.cmd_packages(self.ctx(runner), ["definitely-not-a-package"],
                                      False)
        self.assertEqual(rc, 2)
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.target.state_root.exists())


class PhaseResultTests(unittest.TestCase):
    def test_status_validation_and_ordering(self):
        with self.assertRaises(ValueError):
            PhaseResult("x", "OK")
        results = [PhaseResult("a", "PASS"), PhaseResult("b", "SKIPPED"),
                   PhaseResult("c", "RELOGIN_REQUIRED")]
        self.assertEqual(phases.overall_status(results), "RELOGIN_REQUIRED")
        self.assertEqual(phases.exit_code(results), 0)
        results.append(PhaseResult("d", "FAIL"))
        self.assertEqual(phases.overall_status(results), "FAIL")
        self.assertEqual(phases.exit_code(results), 1)
        self.assertEqual(phases.overall_status([PhaseResult("a", "PASS")]), "PASS")

    def test_write_status_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = make_target(Path(tmp))
            path = phases.write_status(target, {"x": Path("/a"), "b": b"secret"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.state_root.stat().st_mode & 0o777, 0o700)
            data = json.loads(path.read_text())
            self.assertEqual(data, {"x": "/a", "b": "<6 bytes>"})


class LoginShellTests(TempHome):
    def test_already_zsh(self):
        with redirect_stdout(io.StringIO()) as out:
            r = phases.login_shell_phase(self.target, ScriptedRunner(),
                                         current_shell="/usr/bin/zsh", allow_change=True)
        self.assertEqual(r.status, "PASS")
        self.assertIn("$SHELL is already zsh.", out.getvalue())

    def test_no_shell_change(self):
        runner = ScriptedRunner(which={"zsh": "/usr/bin/zsh"})
        r = phases.login_shell_phase(self.target, runner, current_shell="/bin/bash",
                                     allow_change=False)
        self.assertEqual(r.status, "SKIPPED")
        self.assertEqual(runner.calls, [])

    def test_chsh_success_and_failure(self):
        with redirect_stdout(io.StringIO()) as out:
            ok = phases.login_shell_phase(
                self.target, ScriptedRunner(which={"zsh": "/usr/bin/zsh"}),
                current_shell="/bin/bash", allow_change=True, interactive=lambda a: 0)
            bad = phases.login_shell_phase(
                self.target, ScriptedRunner(which={"zsh": "/usr/bin/zsh"}),
                current_shell="/bin/bash", allow_change=True, interactive=lambda a: 1)
        self.assertEqual(ok.status, "RELOGIN_REQUIRED")
        self.assertEqual(bad.status, "FAIL")
        self.assertIn("Please type your password if you wish to change the default "
                      "shell to ZSH", out.getvalue())
        self.assertIn("Successfully changed the default shell, please re-login",
                      out.getvalue())


class AccountRunner(ScriptedRunner):
    """ScriptedRunner that also answers ``passwd -S`` with a status letter."""

    def __init__(self, password, sudo_rc=0, chsh_rc=0):
        super().__init__(which={"zsh": "/usr/bin/zsh", "sudo": "/usr/bin/sudo"})
        self.password, self.sudo_rc, self.chsh_rc = password, sudo_rc, chsh_rc

    def run(self, argv, *, timeout, check=True, env=None, input=None, cwd=None,
            read_only=False):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv == ["passwd", "-S"]:
            out = f"fixture {self.password} 2026-01-01 0 99999 7 -1\n".encode()
            return subprocess.CompletedProcess(argv, 0, out, b"")
        if argv == ["sudo", "-n", "true"]:
            return subprocess.CompletedProcess(argv, self.sudo_rc, b"", b"")
        if argv[:3] == ["sudo", "-n", "chsh"]:
            return subprocess.CompletedProcess(argv, self.chsh_rc, b"", b"denied")
        raise AssertionError(argv)


class LoginShellGateTests(TempHome):
    def phase(self, runner, **kw):
        chsh = []

        def interactive(argv):
            chsh.append(argv)
            return 0

        with redirect_stdout(io.StringIO()) as out:
            r = phases.login_shell_phase(self.target, runner, current_shell="/bin/bash",
                                         allow_change=True, interactive=interactive,
                                         **kw)
        return r, chsh, out.getvalue()

    def test_failed_smoke_leaves_the_login_shell(self):
        # REQ-7: chsh only after the smoke checks passed.
        runner = AccountRunner("P")
        r, chsh, out = self.phase(runner, checks_passed=False)
        self.assertEqual(r.status, "SKIPPED")
        self.assertEqual(chsh, [])
        self.assertEqual(runner.calls, [])
        self.assertIn("smoke-failed", r.reasons[0])
        self.assertIn("chsh -s", r.details["command"])
        text = "\n".join(phases.completion_lines([r]))
        self.assertIn("zsh is not your login shell yet", text)
        self.assertIn("chsh -s", text)

    def test_password_account_uses_plain_chsh(self):
        r, chsh, out = self.phase(AccountRunner("P"))
        self.assertEqual(r.status, "RELOGIN_REQUIRED")
        self.assertEqual(len(chsh), 1)
        self.assertEqual(chsh[0][:2], ["chsh", "-s"])
        self.assertIn("Please type your password", out)

    def test_locked_account_with_passwordless_sudo_uses_sudo(self):
        # PLAT-4: cloud-init users have a locked password and NOPASSWD sudo.
        runner = AccountRunner("L")
        r, chsh, out = self.phase(runner)
        self.assertEqual(r.status, "RELOGIN_REQUIRED")
        self.assertEqual(chsh, [])
        self.assertEqual(runner.calls[-1][:4], ["sudo", "-n", "chsh", "-s"])
        self.assertEqual(runner.calls[-1][-1], "fixture")
        self.assertNotIn("Please type your password", out)

    def test_locked_account_without_sudo_gets_the_sudo_remedy(self):
        runner = AccountRunner("L", sudo_rc=1)
        r, chsh, out = self.phase(runner)
        self.assertEqual(chsh, [])  # a chsh prompt could never succeed
        self.assertNotEqual(r.status, "FAIL")
        self.assertIn("sudo chsh -s", r.reasons[0])
        self.assertTrue(r.reasons[0].endswith(" fixture"))
        self.assertFalse(any(c[:3] == ["sudo", "-n", "chsh"] for c in runner.calls))

    def test_sudo_chsh_failure_names_the_remedy(self):
        r, _, _ = self.phase(AccountRunner("NP", chsh_rc=1))
        self.assertEqual(r.status, "FAIL")
        self.assertIn("sudo chsh -s", r.reasons[0])

    def test_plain_chsh_failure_mentions_the_sudo_form(self):
        runner = AccountRunner("P")
        with redirect_stdout(io.StringIO()):
            r = phases.login_shell_phase(self.target, runner, current_shell="/bin/bash",
                                         allow_change=True, interactive=lambda a: 1)
        self.assertEqual(r.status, "FAIL")
        self.assertIn("chsh -s", r.reasons[0])
        self.assertIn("sudo chsh -s", r.reasons[0])

    def test_pipeline_skips_chsh_after_failed_smoke(self):
        # REQ-7 through run_pipeline: smoke FAIL must not reach chsh.
        ctx = self.ctx()
        ctx.current_shell = "/bin/bash"
        ctx.interactive = lambda argv: self.fail(f"chsh ran: {argv}")
        passed = lambda name: PhaseResult(name, "PASS")  # noqa: E731
        with mock.patch.object(install, "preflight_phase", return_value=passed("preflight")), \
                mock.patch.object(install, "packages_phase",
                                  return_value=PhaseResult("packages", "SKIPPED")), \
                mock.patch.object(install, "extra_desired_entries", return_value=([], [])), \
                mock.patch.object(install, "transaction_phase",
                                  return_value=(passed("transaction"), False)), \
                mock.patch.object(phases, "post_install_phase",
                                  return_value=passed("post-install")), \
                mock.patch.object(phases, "smoke_phase", return_value=PhaseResult(
                    "smoke", "FAIL", ["zsh -i -c exit returned 1"])), \
                mock.patch.object(phases, "git_identity_phase",
                                  return_value=passed("git-identity")), \
                mock.patch.object(phases, "auth_phase", create=True,
                                  return_value=PhaseResult("auth", "SKIPPED")), \
                mock.patch.object(install, "gui_phase",
                                  return_value=PhaseResult("gui", "SKIPPED")), \
                redirect_stdout(io.StringIO()):
            rc = install.run_pipeline(ctx, "install", install.Options())
        self.assertEqual(rc, 1)
        status = {p["phase"]: p for p in phases.read_status(self.target)["phases"]}
        self.assertEqual(status["login-shell"]["status"], "SKIPPED")
        self.assertIn("smoke-failed", status["login-shell"]["reasons"][0])


class GenerationTests(TempHome):
    """{state}/personal-dotfiles/generation: "<generation>\\n", 0600, atomic,
    from the checkout's content, written at the end of the post actions."""

    def pipeline(self, tx_status="PASS", ctx=None):
        ctx = ctx or self.ctx()
        ctx.current_shell = "/usr/bin/zsh"
        passed = lambda name: PhaseResult(name, "PASS")  # noqa: E731
        seen = {}
        marker = self.target.state_root / "generation"

        def post(*args, **kwargs):
            seen["at_post_install"] = marker.read_text() if marker.exists() else None
            seen["tmux_env"] = kwargs.get("tmux_env")
            seen["generation"] = kwargs["record_generation"]()
            seen["after_record"] = marker.read_text()
            return passed("post-install")

        with mock.patch.object(install, "preflight_phase", return_value=passed("preflight")), \
                mock.patch.object(install, "packages_phase",
                                  return_value=PhaseResult("packages", "SKIPPED")), \
                mock.patch.object(install, "extra_desired_entries", return_value=([], [])), \
                mock.patch.object(install, "transaction_phase",
                                  return_value=(PhaseResult("transaction", tx_status,
                                                            ["x"] if tx_status == "FAIL"
                                                            else []), False)), \
                mock.patch.object(phases, "post_install_phase", side_effect=post), \
                mock.patch.object(phases, "smoke_phase", return_value=passed("smoke")), \
                mock.patch.object(phases, "git_identity_phase",
                                  return_value=passed("git-identity")), \
                mock.patch.object(phases, "auth_phase",
                                  return_value=PhaseResult("auth", "SKIPPED")), \
                mock.patch.object(install, "gui_phase",
                                  return_value=PhaseResult("gui", "SKIPPED")), \
                redirect_stdout(io.StringIO()):
            rc = install.run_pipeline(ctx, "install", install.Options())
        return rc, seen

    def git_repo(self) -> tuple[Path, dict]:
        repo = Path(self._tmp.name) / "checkout"
        repo.mkdir()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home),
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

        def git(*args):
            subprocess.run(["git", "-C", str(repo), *args], env=env, check=True,
                           capture_output=True)
        git("init", "-q")
        (repo / "zshrc").write_text("alias td='tmux detach'\n")
        git("add", "zshrc")
        git("commit", "-q", "-m", "one")
        return repo, git

    def isolated_git_runner(self):
        class GitRunner(Runner):
            def run(inner, argv, **kw):
                env = dict(kw.pop("env", None) or {})
                env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                            "PATH": "/usr/bin:/bin"})
                return Runner.run(inner, argv, env=env, **kw)
        return GitRunner()

    def test_post_actions_write_the_content_generation(self):
        repo, _ = self.git_repo()
        ctx = self.ctx(self.isolated_git_runner())
        ctx.repo_root = repo
        rc, seen = self.pipeline(ctx=ctx)
        self.assertEqual(rc, 0)
        marker = self.target.state_root / "generation"
        generation = install.checkout_generation(ctx.runner, repo, {})
        self.assertRegex(generation, r"^[0-9a-f]{64}$")
        self.assertNotIn(ctx.run_id, generation)
        self.assertEqual(marker.read_text(), f"{generation}\n")
        self.assertEqual(seen["generation"], (None, generation))  # (previous, new)
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.target.state_root.stat().st_mode & 0o777, 0o700)
        # Not before the post actions: nothing existed when they started, and
        # the write happens inside them (after plugins, before converge).
        self.assertIsNone(seen["at_post_install"])
        self.assertEqual(seen["after_record"], f"{generation}\n")
        self.assertEqual(seen["tmux_env"], {})
        self.assertEqual(sorted(p.name for p in self.target.state_root.iterdir()),
                         ["generation", "status.json"])

    def test_generation_follows_content_not_the_run(self):
        repo, git = self.git_repo()
        runner = self.isolated_git_runner()
        first = install.checkout_generation(runner, repo, {})
        self.assertEqual(install.checkout_generation(runner, repo, {}), first)
        # A local edit counts; reverting it gives the first generation back.
        (repo / "zshrc").write_text("alias td='tmux detach'\nalias x=y\n")
        edited = install.checkout_generation(runner, repo, {})
        self.assertNotEqual(edited, first)
        (repo / "zshrc").write_text("alias td='tmux detach'\nalias x=z\n")
        self.assertNotEqual(install.checkout_generation(runner, repo, {}), edited)
        (repo / "zshrc").write_text("alias td='tmux detach'\n")
        self.assertEqual(install.checkout_generation(runner, repo, {}), first)
        # An untracked file counts, and so does its content (git status
        # lists only its name); an ignored file does not.
        (repo / "new").write_text("x")
        untracked = install.checkout_generation(runner, repo, {})
        self.assertNotEqual(untracked, first)
        (repo / "new").write_text("y")
        self.assertNotEqual(install.checkout_generation(runner, repo, {}), untracked)
        (repo / "new").write_text("x")
        self.assertEqual(install.checkout_generation(runner, repo, {}), untracked)
        (repo / "sub").mkdir()
        (repo / "sub" / "deep").write_text("1")
        deep = install.checkout_generation(runner, repo, {})
        (repo / "sub" / "deep").write_text("2")
        self.assertNotEqual(install.checkout_generation(runner, repo, {}), deep)
        shutil.rmtree(repo / "sub")
        self.assertEqual(install.checkout_generation(runner, repo, {}), untracked)
        (repo / "link").symlink_to("new")
        linked = install.checkout_generation(runner, repo, {})
        (repo / "link").unlink()
        (repo / "link").symlink_to("other")
        self.assertNotEqual(install.checkout_generation(runner, repo, {}), linked)
        (repo / "link").unlink()
        (repo / ".git" / "info" / "exclude").write_text("ignored.txt\n")
        (repo / "ignored.txt").write_text("1")
        self.assertEqual(install.checkout_generation(runner, repo, {}), untracked)
        (repo / "ignored.txt").write_text("2")
        self.assertEqual(install.checkout_generation(runner, repo, {}), untracked)
        git("add", "new")
        git("commit", "-q", "-m", "two")
        second = install.checkout_generation(runner, repo, {})
        self.assertNotIn(second, (first, edited))
        # A caller's GIT_DIR/GIT_INDEX_FILE never redirect it.
        self.assertEqual(install.checkout_generation(
            runner, repo, {"GIT_DIR": "/nonexistent", "GIT_INDEX_FILE": "/x"}), second)
        # Read-only: every git call skips optional locks, and the diff is
        # plumbing (diff-index never refreshes the index).
        recorded = ScriptedRunner()
        install.checkout_generation(recorded, repo, {})
        self.assertEqual([c[:4] for c in recorded.calls],
                         [["git", "--no-optional-locks", "-C", str(repo)]] * 4)
        self.assertEqual([c[4:] for c in recorded.calls],
                         [["rev-parse", "--verify", "-q", "HEAD"],
                          ["status", "--porcelain=v1", "-z"],
                          ["diff-index", "-p", "--no-ext-diff", "--no-textconv",
                           "--no-color", "--binary", "HEAD", "--"],
                          ["ls-files", "-o", "--exclude-standard", "-z"]])

    def test_generation_never_writes_the_index(self):
        # A stat-dirty tracked file (touched, same content) made 'git diff'
        # rewrite .git/index and take index.lock in the user's checkout.
        repo, _ = self.git_repo()
        runner = self.isolated_git_runner()
        first = install.checkout_generation(runner, repo, {})
        index = repo / ".git" / "index"
        later = time.time() + 100
        os.utime(repo / "zshrc", (later, later))
        before = (index.read_bytes(), index.stat().st_mtime_ns)
        self.assertEqual(install.checkout_generation(runner, repo, {}), first)
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before)
        self.assertFalse((repo / ".git" / "index.lock").exists())
        # A real edit still counts, and still writes nothing.
        (repo / "zshrc").write_text("alias td='tmux detach'\nalias q=r\n")
        self.assertNotEqual(install.checkout_generation(runner, repo, {}), first)
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before)

    def test_unchanged_repair_keeps_the_generation(self):
        repo, _ = self.git_repo()
        ctx = self.ctx(self.isolated_git_runner())
        ctx.repo_root = repo
        _, first = self.pipeline(ctx=ctx)
        ctx2 = self.ctx(self.isolated_git_runner())
        ctx2.repo_root = repo
        ctx2.run_id = "20260102T000000Z-89abcdef"
        _, second = self.pipeline(ctx=ctx2)
        generation = first["generation"][1]
        # The second run sees the first's generation as the previous one.
        self.assertEqual(second["generation"], (generation, generation))
        self.assertEqual(second["at_post_install"], f"{generation}\n")
        self.assertFalse(phases.generation_changed(*reversed(second["generation"])))

    def test_checkout_without_git_is_hashed_by_its_files(self):
        plain = Path(self._tmp.name) / "plain"
        plain.mkdir()
        (plain / "a").write_text("1")
        runner = ScriptedRunner(responses={("git", "--no-optional-locks", "-C"): 128})
        first = install.checkout_generation(runner, plain, {})
        self.assertEqual(install.checkout_generation(runner, plain, {}), first)
        (plain / "a").write_text("2")
        self.assertNotEqual(install.checkout_generation(runner, plain, {}), first)

    def test_failed_transaction_keeps_the_previous_generation(self):
        install.write_generation(self.target, "previous")
        with mock.patch.object(phases, "post_install_phase",
                               side_effect=AssertionError("post actions ran")):
            rc, _ = self.pipeline(tx_status="FAIL")
        self.assertEqual(rc, 1)
        self.assertEqual((self.target.state_root / "generation").read_text(),
                         "previous\n")

    def test_rewrite_is_atomic_and_private(self):
        path = install.write_generation(self.target, "a")
        os.chmod(path, 0o644)
        with mock.patch.object(install.os, "replace", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                install.write_generation(self.target, "b")
        self.assertEqual(path.read_text(), "a\n")  # never half-written
        self.assertEqual([p.name for p in path.parent.iterdir()], ["generation"])
        install.write_generation(self.target, "c")
        self.assertEqual((path.read_text(), path.stat().st_mode & 0o777), ("c\n", 0o600))
        for bad in ("", "a\nb", "a\tb"):
            with self.assertRaises(ValueError):
                install.write_generation(self.target, bad)

    def test_record_generation_returns_the_previous_one(self):
        repo, _ = self.git_repo()
        ctx = self.ctx(self.isolated_git_runner())
        ctx.repo_root = repo
        self.assertIsNone(install.read_generation(self.target))
        previous, generation = install.record_generation(ctx)
        self.assertEqual((previous, install.read_generation(self.target)), (None, generation))
        self.assertEqual(install.record_generation(ctx), (generation, generation))
        install.write_generation(self.target, "older")
        self.assertEqual(install.record_generation(ctx), ("older", generation))
        # Not a regular file, or unreadable: as if none was recorded.
        path = self.target.state_root / "generation"
        path.unlink()
        path.mkdir()
        self.assertIsNone(install.read_generation(self.target))

    def test_unchanged_generation_reaches_the_converge(self):
        seen = {}

        def converge(target, runner, env, **kw):
            seen.update(kw)
            return [], {"outcome": "converged", "respawn": "generation-unchanged",
                        "respawned_panes": 0, "busy_panes": 0}

        out = io.StringIO()
        with mock.patch.object(phases, "converge_running_tmux", side_effect=converge), \
                redirect_stdout(out):
            r = phases.post_install_phase(
                self.target, ScriptedRunner(), repo_root=self.home,
                run_id="20260101T000000Z-0123abcd", systemd_units_applied=False,
                record_generation=lambda: ("g1", "g1"))
        self.assertEqual((seen["previous_generation"], seen["generation"]), ("g1", "g1"))
        self.assertFalse(r.details["generation_changed"])
        self.assertEqual(out.getvalue().strip().splitlines(),
                         ["tmux: applied the new config"])

    def test_generation_write_failure_is_reported_not_fatal(self):
        seen = {}

        def converge(target, runner, env, **kw):
            seen.update(kw)
            return [], {"outcome": "no-running-server", "respawned_panes": 0,
                        "busy_panes": 0}

        def boom():
            raise OSError("ro")

        with mock.patch.object(phases, "converge_running_tmux", side_effect=converge):
            r = phases.post_install_phase(
                self.target, ScriptedRunner(), repo_root=self.home,
                run_id="20260101T000000Z-0123abcd", systemd_units_applied=False,
                record_generation=boom)
        self.assertNotEqual(r.status, "FAIL")
        self.assertIn("could not write the generation file: ro", r.reasons)
        # So no shell is respawned: no generation, no change.
        self.assertEqual((seen["generation"], seen["previous_generation"]), (None, None))

    def test_order_plugins_systemd_generation_converge(self):
        events = []
        runner = ScriptedRunner(which={"systemctl": "/bin/systemctl", "zsh": "/fake/zsh"})
        original = runner.run

        def run(argv, **kw):
            events.append(" ".join(map(str, argv[:3])))
            return original(argv, **kw)
        runner.run = run

        def converge(target, runner_, env, **kw):
            events.append(f"converge {kw['previous_generation']} {kw['generation']}")
            return [], {"outcome": "converged", "respawned_panes": 2, "busy_panes": 3}

        def record():
            events.append("generation")
            return "g1", "g2"

        out = io.StringIO()
        with mock.patch.object(phases, "install_tmux_plugins",
                               side_effect=lambda *a: (events.append("tmux-plugins")
                                                       or ("PASS", [], {}))), \
                mock.patch.object(phases, "converge_running_tmux", side_effect=converge), \
                redirect_stdout(out):
            r = phases.post_install_phase(
                self.target, runner, repo_root=self.home,
                run_id="20260101T000000Z-0123abcd", systemd_units_applied=True,
                record_generation=record)
        self.assertEqual(r.status, "PASS", r.reasons)
        self.assertEqual(r.details["generation"], "g2")
        self.assertTrue(r.details["generation_changed"])
        order = [e.split()[0] if not e.startswith("systemctl") else e for e in events]
        self.assertEqual(order[0], "tmux-plugins")
        self.assertLess(order.index("systemctl --user daemon-reload"),
                        order.index("/fake/zsh"))
        self.assertEqual(order[-2:], ["generation", "converge"])
        self.assertEqual(events[-1], "converge g1 g2")
        # One neutral summary line, nothing about an earlier state.
        printed = out.getvalue().strip().splitlines()
        self.assertEqual(printed, ["tmux: applied the new config; restarted 2 idle shells"])
        self.assertEqual(r.reasons, [])


# One rule for "an interactive shell invocation", shared by the tmux
# converge (installer/phases.py) and bin/dotfiles' parent check.
INTERACTIVE_ARGVS = (
    ["-zsh"], ["zsh"], ["zsh", "-il"], ["zsh", "-l"], ["/bin/bash", "-i"],
    ["bash", "--login"], ["-bash"], ["fish", "-l"], ["sh", "+x"], ["-zsh", "-l"],
    ["/usr/bin/zsh", "--login"])
NON_INTERACTIVE_ARGVS = (
    [], [""], ["bash", "-c", "read -p continue? x"], ["zsh", "-ic", "x"],
    ["-zsh", "-c", "x"], ["-bash", "-c", "dotfiles update; true"],
    ["-bash", "-lc", "x"], ["bash", "script.sh"], ["sh", "./x"],
    ["-bash", "script.sh"], ["zsh", "-o", "nolog"], ["bash", "--rcfile", "f"],
    ["bash", "--rcfile=f"], ["fish", "--command=x"], ["fish", "--init-command", "x"],
    ["bash", "-"], ["bash", "--"], ["zsh", "-l", "x.zsh"], ["bash", "x"],
    ["zsh", "-i", "--", "x"])


class ConvergeUnitTests(TempHome):
    """The pieces of converge_running_tmux that need no tmux server."""

    def test_reset_lines(self):
        defaults = phases.TmuxDefaults(
            keys=["bind-key -T prefix d detach-client"],
            notes=[("prefix", "d", "Detach the current client"), ("prefix", "'", "it's")],
            options={"-g": {"default-shell": ("default-shell /usr/bin/zsh",),
                            "status": ("status on",),
                            "status-format": ('status-format[0] "x"',)},
                     "-gw": {"automatic-rename": ("automatic-rename on",)},
                     "-s": {"escape-time": ("escape-time 500",)}},
            hooks={"-g": {"session-created": ("session-created",)}})
        lines = phases._reset_lines(
            defaults,
            keys=["bind-key -T prefix s choose-tree", "bind-key -T resize-pane k x"],
            options={"-g": {"default-shell": ("default-shell /bin/sh",),
                            "status": ("status on",),
                            "status-format": ('status-format[0] "y"',),
                            "@plugin": ("@plugin tpm",),
                            "@tmux-restore-complete": ("@tmux-restore-complete on",),
                            "@continuum-save-last-timestamp": ("@c 1",),
                            "only-in-another-version": ("only-in-another-version 1",)},
                     "-gw": {"automatic-rename": ("automatic-rename off",)},
                     "-s": {"escape-time": ("escape-time 0",)}},
            hooks={"-g": {"session-created": ("session-created[0] x",)}})
        self.assertEqual(lines[:2], ["unbind-key -a -T 'prefix'",
                                     "unbind-key -a -T 'resize-pane'"])
        self.assertIn("bind-key -T prefix d detach-client", lines)
        self.assertIn("bind-key -N 'Detach the current client' -T 'prefix' 'd'", lines)
        self.assertIn('bind-key -N "it\'s" -T \'prefix\' "\'"', lines)
        self.assertIn("set-option -g default-shell /usr/bin/zsh", lines)  # not -u
        self.assertIn("set-option -gu status-format", lines)              # arrays: -u
        self.assertIn("set-option -gu @plugin", lines)
        self.assertIn("set-option -gw automatic-rename on", lines)
        self.assertIn("set-option -s escape-time 500", lines)
        self.assertIn("set-hook -gu session-created", lines)
        joined = "\n".join(lines)
        self.assertNotIn("set-option -g status on", joined)  # already the default
        self.assertNotIn("restore-complete", joined)       # runtime state kept
        self.assertNotIn("continuum-save-last", joined)
        self.assertNotIn("only-in-another-version", joined)

    def test_config_is_inlined_after_the_reset(self):
        conf = self.home / ".tmux.conf"
        conf.write_text("set -g status off\n%if 1\nset -g mouse on\n%endif\n")
        self.assertEqual(phases.default_config_files(self.target)[-1], conf)
        lines = phases._config_lines([conf])
        self.assertEqual(lines[1:-1], ["set -g status off", "%if 1", "set -g mouse on",
                                       "%endif"])
        self.assertTrue(lines[0].startswith("#"))

    def test_respawn_argv(self):
        path = "/usr/bin:/bin"
        self.assertEqual(phases._respawn_argv("zsh -il", "/bin/sh", path),
                         ["zsh", "-il"])
        self.assertEqual(phases._respawn_argv("cat x; exec zsh", "/bin/sh", path),
                         ["cat x; exec zsh"])
        self.assertEqual(phases._respawn_argv("", "/bin/sh", path), ["/bin/sh", "-l"])
        self.assertIsNone(phases._respawn_argv("", "relative", path))
        self.assertIsNone(phases._respawn_argv("no-such-shell-xyz -l", "/bin/sh", path))

    def test_interactive_invocation(self):
        for argv in INTERACTIVE_ARGVS:
            self.assertTrue(phases.interactive_invocation(argv), argv)
        for argv in NON_INTERACTIVE_ARGVS:
            self.assertFalse(phases.interactive_invocation(argv), argv)

    def test_interactive_invocation_parity_with_bin_dotfiles(self):
        """bin/dotfiles applies the same rule to its parent shell. It cannot
        import installer/ (it runs before the checkout is complete), so the
        two are kept in parity here, on the same vectors."""
        from importlib.machinery import SourceFileLoader
        import importlib.util
        loader = SourceFileLoader("dotfiles_cmd_parity", str(REPO_ROOT / "bin" / "dotfiles"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "argv", ["dotfiles"]):
            loader.exec_module(module)
        rule = next((getattr(module, name) for name in
                     ("interactive_invocation", "_interactive_invocation")
                     if callable(getattr(module, name, None))), None)
        if rule is None:
            self.skipTest("bin/dotfiles has no interactive_invocation(argv) yet")
        for argv in INTERACTIVE_ARGVS + NON_INTERACTIVE_ARGVS:
            self.assertEqual(bool(rule(list(argv))), phases.interactive_invocation(argv),
                             argv)

    def hook_file(self) -> Path:
        path = phases.shell_hook_path(self.target)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.touch(mode=0o600)
        return path

    def holder(self, path: Path, *, cloexec=True, then="exec sleep 30"):
        """A child that holds a read-only fd on ``path`` (as the zsh module
        does), then runs ``then`` with /bin/sh."""
        code = ("import os, sys\n"
                f"fd = os.open({str(path)!r}, os.O_RDONLY | "
                f"{'os.O_CLOEXEC' if cloexec else '0'})\n"
                f"os.set_inheritable(fd, {not cloexec!r})\n"
                "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
                f"os.execv('/bin/sh', ['sh', '-c', {then!r}])\n")
        child = subprocess.Popen([sys.executable, "-B", "-c", code], stdout=subprocess.PIPE,
                                 start_new_session=True)
        self.addCleanup(lambda: (child.kill(), child.wait(), child.stdout.close()))
        self.assertEqual(child.stdout.readline(), b"ready\n")
        return child

    def wait_exec(self, pid: int, comm: str):
        for _ in range(200):
            stat = phases._proc_stat(pid)
            if stat and stat[0] == comm:
                return
            time.sleep(0.01)
        self.fail(f"{pid} never became {comm}")

    def test_shell_hooked(self):
        path = self.hook_file()
        self.assertEqual(path, self.target.state_root / "shell-hook")
        # The fd survives the holder's exec only when it is inheritable:
        # a close-on-exec fd (what the zsh module uses) closes on 'exec bash'.
        kept = self.holder(path, cloexec=False)
        dropped = self.holder(path, cloexec=True)
        self.wait_exec(kept.pid, "sleep")
        self.wait_exec(dropped.pid, "sleep")
        self.assertTrue(phases.shell_hooked(kept.pid, path))
        self.assertFalse(phases.shell_hooked(dropped.pid, path))
        self.assertFalse(phases.shell_hooked(os.getpid(), path))
        self.assertIsNone(phases.shell_hooked(2 ** 22 + 7, path))  # no such process
        # The file removed (or replaced) since: still the hook.
        path.unlink()
        self.assertTrue(phases.shell_hooked(kept.pid, path))
        path.touch(mode=0o600)
        self.assertTrue(phases.shell_hooked(kept.pid, path))
        # Another state directory's hook file (another XDG_STATE_HOME): hooked.
        other = self.home / "elsewhere" / "personal-dotfiles" / "shell-hook"
        self.assertTrue(phases.shell_hooked(kept.pid, other))
        # The same inode under another name (a symlinked state directory).
        alias_dir = self.home / "alias"
        alias_dir.symlink_to(self.target.state_root)
        third = self.holder(alias_dir / "shell-hook", cloexec=False)
        self.assertTrue(phases.shell_hooked(third.pid, path))
        # A file merely named shell-hook elsewhere is not the hook.
        stray = self.home / "shell-hook"
        stray.touch()
        fourth = self.holder(stray, cloexec=False)
        self.assertFalse(phases.shell_hooked(fourth.pid, path))

    def test_shell_hooked_cannot_tell(self):
        with mock.patch.object(phases.os, "listdir", side_effect=PermissionError("no")):
            self.assertIsNone(phases.shell_hooked(os.getpid(), self.hook_file()))

    def test_shell_restart_check_without_a_terminal(self):
        def shell(script):
            return subprocess.Popen(["/bin/sh", "-c", script], start_new_session=True,
                                    cwd=self.home)

        check = dict(hook_path=self.hook_file())
        lone = shell("sleep 30")
        try:
            # Not a zsh (and not a terminal's foreground group): never.
            self.assertEqual(phases.shell_restart_check(lone.pid, "/dev/pts/0", **check),
                             (None, "not-zsh"))
        finally:
            lone.kill()
            lone.wait()
        zsh = shutil.which("zsh", path="/usr/bin:/bin")
        if zsh:
            # A zsh that is not its terminal's foreground group: busy.
            waiting = subprocess.Popen([zsh, "-f"], stdin=subprocess.PIPE,
                                       start_new_session=True, cwd=self.home)
            try:
                self.wait_exec(waiting.pid, "zsh")
                self.assertEqual(phases.shell_restart_check(waiting.pid, "/dev/pts/0",
                                                            **check), (None, "busy"))
            finally:
                waiting.kill()
                waiting.wait()
                waiting.stdin.close()
        self.assertEqual(phases.shell_restart_check(os.getpid(), "/dev/pts/0", **check),
                         (None, "not-zsh"))
        self.assertEqual(phases.shell_restart_check(2 ** 22 + 7, "/dev/pts/0", **check),
                         (None, "gone"))

    def test_exe_name_of_an_upgraded_binary(self):
        with mock.patch.object(phases.os, "readlink", return_value="/usr/bin/zsh (deleted)"):
            self.assertEqual(phases._exe_name(1), "zsh")

    def test_pane_check_uses_what_tmux_knows(self):
        fields = ["%1", str(os.getpid()), "0", "0", "0", "30", "/dev/pts/0", "zsh",
                  "1", "0", "s"]
        hook = self.hook_file()
        for index, value in ((2, "1"), (3, "1"), (4, "1"), (1, "x")):
            changed = list(fields)
            changed[index] = value
            self.assertEqual(phases._pane_check(changed, hook), (None, "busy"), index)
        for current in ("bash", "sh", "fish", "-bash", "python3"):
            changed = list(fields)
            changed[7] = current
            self.assertEqual(phases._pane_check(changed, hook), (None, "not-zsh"))
        # A zsh as tmux sees it, but /proc says python: not a zsh either.
        self.assertEqual(phases._pane_check(fields, hook), (None, "not-zsh"))

    def test_generation_changed(self):
        self.assertTrue(phases.generation_changed("g2", "g1"))
        self.assertTrue(phases.generation_changed("g1", None))  # first install
        self.assertFalse(phases.generation_changed("g1", "g1"))
        self.assertFalse(phases.generation_changed(None, "g1"))  # none written
        self.assertFalse(phases.generation_changed(None, None))

    def test_converge_summary(self):
        self.assertEqual(phases.converge_summary({"outcome": "converged",
                                                  "respawned_panes": 1}),
                         "tmux: applied the new config; restarted 1 idle shell")
        self.assertEqual(phases.converge_summary({"outcome": "converged",
                                                  "respawned_panes": 0}),
                         "tmux: applied the new config; restarted 0 idle shells")
        self.assertEqual(phases.converge_summary({"outcome": "converged",
                                                  "respawn": "generation-unchanged"}),
                         "tmux: applied the new config")
        for outcome in ("failed", "no-running-server", "running-server-uses-another-config"):
            self.assertIsNone(phases.converge_summary({"outcome": outcome}))

    def test_wait_out_continuum_restore(self):
        runner = ScriptedRunner()
        slept = []
        with mock.patch.object(phases, "_sleep", side_effect=slept.append), \
                mock.patch.object(phases.time, "time", return_value=1000.0):
            self.assertEqual(phases._wait_out_continuum_restore(
                "tmux", runner, {}, "995"), 7.0)
            self.assertEqual(phases._wait_out_continuum_restore(
                "tmux", runner, {}, "900"), 0.0)
            self.assertEqual(phases._wait_out_continuum_restore(
                "tmux", runner, {}, ""), 0.0)
        self.assertEqual(slept, [7.0])


class AuthTests(TempHome):
    class CliRunner(ScriptedRunner):
        def __init__(self, answers):
            super().__init__()
            self.answers = answers

        def run(self, argv, *, timeout, check=True, env=None, input=None, cwd=None,
                read_only=False):
            argv = [str(a) for a in argv]
            self.calls.append(argv)
            self.envs = getattr(self, "envs", []) + [env]
            rc, out, err = self.answers[(Path(argv[0]).name, *argv[1:])]
            return subprocess.CompletedProcess(argv, rc, out, err)

    CODEX_HELP = b"Manage login\n\nCommands:\n  status  Show login status\n  help  x\n"
    CLAUDE_HELP = (b"Usage: claude auth [options] [command]\n\nCommands:\n"
                   b"  login [options]   Sign in\n  status [options]  Show status\n")

    def install_cli(self, *names):
        bin_dir = self.home / ".local" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            (bin_dir / name).write_text("#!/bin/sh\nexit 99\n")
            os.chmod(bin_dir / name, 0o755)

    def test_not_signed_in_is_auth_required(self):
        # REQ-14: AUTH_REQUIRED is produced from the tools' own status commands.
        self.install_cli("codex", "claude")
        runner = self.CliRunner({
            ("codex", "login", "--help"): (0, self.CODEX_HELP, b""),
            ("codex", "login", "status"): (1, b"", b"Not logged in\n"),
            ("claude", "auth", "--help"): (0, self.CLAUDE_HELP, b""),
            ("claude", "auth", "status", "--json"):
                (1, b'{"loggedIn": false, "authMethod": "none"}', b""),
        })
        r = phases.auth_phase(self.target, runner, {"PATH": "/usr/bin"})
        self.assertEqual(r.status, "AUTH_REQUIRED")
        self.assertEqual(r.details, {"codex": "not-signed-in", "claude": "not-signed-in"})
        self.assertTrue(any("codex login" in reason for reason in r.reasons))
        self.assertTrue(any("claude auth login" in reason for reason in r.reasons))
        self.assertTrue(all(e["HOME"] == str(self.home) for e in runner.envs))
        self.assertEqual(phases.exit_code([r]), 0)
        text = "\n".join(phases.completion_lines([PhaseResult("packages", "PASS"), r]))
        self.assertIn("auth needs sign-in", text)
        summary = phases.format_summary([r])
        self.assertIn("auth           AUTH_REQUIRED", summary)

    def test_signed_in_passes_and_account_details_are_not_kept(self):
        self.install_cli("codex", "claude")
        runner = self.CliRunner({
            ("codex", "login", "--help"): (0, self.CODEX_HELP, b""),
            ("codex", "login", "status"): (0, b"Logged in as someone\n", b""),
            ("claude", "auth", "--help"): (0, self.CLAUDE_HELP, b""),
            ("claude", "auth", "status", "--json"):
                (0, b'{"loggedIn": true, "email": "someone@example.invalid"}', b""),
        })
        r = phases.auth_phase(self.target, runner, {})
        self.assertEqual(r.status, "PASS")
        self.assertNotIn("example.invalid", json.dumps(r.to_dict()))
        self.assertNotIn("someone", json.dumps(r.to_dict()))
        text = "\n".join(phases.completion_lines([PhaseResult("packages", "PASS"), r]))
        self.assertNotIn("Sign in", text)

    def test_unverifiable_tools_do_not_claim_either_way(self):
        self.install_cli("codex", "claude")
        runner = self.CliRunner({
            ("codex", "login", "--help"): (0, b"Commands:\n  help  x\n", b""),
            ("claude", "auth", "--help"): (1, b"", b"unknown command"),
        })
        r = phases.auth_phase(self.target, runner, {})
        self.assertEqual(r.status, "SKIPPED")
        self.assertEqual(r.details, {"codex": "unverified", "claude": "unverified"})
        self.assertEqual(len(runner.calls), 2)  # no status call without a listing
        text = "\n".join(phases.completion_lines([r]))
        self.assertIn("not verifiable here", text)
        self.assertIn("codex login", text)

    def test_nothing_installed_is_skipped(self):
        runner = self.CliRunner({})
        r = phases.auth_phase(self.target, runner, {})
        self.assertEqual(r.status, "SKIPPED")
        self.assertEqual(runner.calls, [])


class ToolLinkTests(TempHome):
    """REQ-5 / REQ-9: the ~/.local/bin tool links through the transaction."""

    def setUp(self):
        super().setUp()
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / "manifests").mkdir(parents=True)
        (self.repo / "manifests" / "tools.json").write_text(
            json.dumps({"tools": {"neovim": {}, "fzf": {}}}))
        (self.repo / "bashrc").write_text("# bashrc\n")
        (self.repo / "manifests" / "managed-paths.json").write_text(json.dumps(
            {"schema": 1, "entries": [{"id": "bashrc", "dest": "{home}/.bashrc",
                                       "kind": "symlink", "source": "bashrc"}]}))
        self.bin_dir = self.target.data_home / "personal-dotfiles" / "bin"
        self.nvim = self.home / ".local" / "bin" / "nvim"
        self.fzf = self.home / ".local" / "bin" / "fzf"

    def ctx(self, runner=None):
        from installer.transaction import new_run_id
        ctx = super().ctx(runner)
        ctx.repo_root = self.repo
        ctx.run_id = new_run_id()  # every transaction needs its own run id
        return ctx

    def links(self, *names):
        return [{"id": f"tool-link-{n}", "dest": str(self.home / ".local/bin" / n),
                 "kind": "symlink", "link_text": str(self.bin_dir / n)} for n in names]

    def apply(self, packages_result):
        """The pipeline's link step: extra entries, then the transaction."""

        from installer import packages
        ctx = self.ctx()
        real = install.load_seam

        def load(module, name):
            if (module, name) == ("packages", "link_requests"):
                return packages.link_requests
            return None if module == "gui" else real(module, name)

        with mock.patch.object(install, "load_seam", side_effect=load), \
                redirect_stdout(io.StringIO()):
            extra, _ = install.extra_desired_entries(ctx, packages_result, no_gui=True)
            result, _ = install.transaction_phase(ctx, extra_entries=extra)
        self.assertEqual(result.status, "PASS", result.reasons)
        return result

    def test_skipped_or_failed_packages_keep_installed_links(self):
        self.apply(PhaseResult("packages", "PASS", [], {"links": self.links("nvim", "fzf")}))
        self.assertEqual(os.readlink(self.nvim), str(self.bin_dir / "nvim"))
        for skipped in (PhaseResult("packages", "SKIPPED", ["--no-packages"]),
                        PhaseResult("packages", "FAIL", ["fzf: boom"],
                                    {"links": self.links("nvim")})):
            result = self.apply(skipped)
            self.assertEqual(result.details["result"]["retired"], [], skipped)
            self.assertEqual(os.readlink(self.nvim), str(self.bin_dir / "nvim"))
            self.assertEqual(os.readlink(self.fzf), str(self.bin_dir / "fzf"))
        # A complete packages run still decides: a dropped link is retired.
        result = self.apply(PhaseResult("packages", "PASS", [],
                                        {"links": self.links("nvim")}))
        self.assertEqual(result.details["result"]["retired"], ["tool-link-fzf"])
        self.assertFalse(os.path.lexists(self.fzf))
        self.assertTrue(self.nvim.is_symlink())

    def seam(self, links):
        from installer import packages
        seen = {}

        def run(target, platform, runner, *, dry_run=False, only=None):
            seen["only"] = only
            return {"phase": "packages", "status": "PASS", "reasons": [],
                    "details": {"links": links, "tools_selected": list(only or [])}}

        def load(module, name):
            if (module, name) == ("packages", "run_packages_phase"):
                return run
            if (module, name) == ("packages", "link_requests"):
                return packages.link_requests
            return None
        return load, seen

    def test_install_one_tool_creates_its_link(self):
        load, seen = self.seam(self.links("nvim"))
        with mock.patch.object(install, "load_seam", side_effect=load), \
                redirect_stdout(io.StringIO()) as out:
            rc = install.cmd_packages(self.ctx(), ["neovim"], False)
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(seen["only"], ["neovim"])
        self.assertEqual(os.readlink(self.nvim), str(self.bin_dir / "nvim"))
        status = {p["phase"]: p["status"] for p in phases.read_status(self.target)["phases"]}
        self.assertEqual(status, {"packages": "PASS", "transaction": "PASS"})
        # A never-installed home gets the link only, not every managed path.
        self.assertFalse(os.path.lexists(self.home / ".bashrc"))

    def test_install_one_tool_on_an_installed_home_retires_nothing(self):
        from installer import packages
        nvim_link = packages.link_requests({"links": self.links("nvim")})
        with redirect_stdout(io.StringIO()):
            installed, _ = install.transaction_phase(self.ctx(), extra_entries=nvim_link)
        self.assertEqual(installed.status, "PASS", installed.reasons)

        load, _ = self.seam(self.links("fzf"))
        with mock.patch.object(install, "load_seam", side_effect=load), \
                redirect_stdout(io.StringIO()) as out:
            rc = install.cmd_packages(self.ctx(), ["fzf"], False)
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(os.readlink(self.fzf), str(self.bin_dir / "fzf"))
        self.assertEqual(os.readlink(self.nvim), str(self.bin_dir / "nvim"))
        self.assertEqual(os.readlink(self.home / ".bashrc"),
                         str(self.target.repo_root / "bashrc"))
        tx = {p["phase"]: p for p in phases.read_status(self.target)["phases"]}
        self.assertEqual(tx["transaction"]["details"]["result"]["retired"], [])
        from installer import transaction
        self.assertEqual(set(transaction.load_status(self.target)["entries"]),
                         {"bashrc", "tool-link-nvim", "tool-link-fzf"})

    def test_dry_run_creates_no_link(self):
        load, _ = self.seam(self.links("nvim"))
        with mock.patch.object(install, "load_seam", side_effect=load), \
                redirect_stdout(io.StringIO()):
            install.cmd_packages(self.ctx(), ["neovim"], True)
        self.assertFalse(os.path.lexists(self.nvim))


class DriftReportingTests(TempHome):
    def test_kept_local_changes_are_reported(self):
        from installer.transaction import DesiredEntry
        entry = DesiredEntry("gitconfig", self.home / ".gitconfig", "file",
                             content=b"x", mode=0o644)
        other = DesiredEntry("zshrc", self.home / ".zshrc", "symlink", link_text="/z")

        class Applied:
            changed, retired, drifted, backup_dir = [], [], [], None

        for field, kept in (("kept", ["gitconfig"]), ("drifted_kept", [str(entry.dest)]),
                            ("kept", [{"id": "gitconfig", "dest": str(entry.dest)}])):
            applied = Applied()
            setattr(applied, field, kept)
            self.assertEqual(install.kept_local_changes(applied, [entry, other]),
                             [("gitconfig", str(entry.dest))])
            with redirect_stdout(io.StringIO()) as out:
                install.report_entries(self.ctx(), [entry, other], {}, applied, {})
            lines = out.getvalue().splitlines()
            self.assertIn(ui.target_line(entry.dest,
                                         "kept your local changes (use -f to overwrite)"),
                          lines)
            self.assertIn(ui.target_line(other.dest, "already up-to-date"), lines)
        self.assertEqual(install.kept_local_changes(Applied(), [entry]), [])

    def test_real_transaction_keeps_a_changed_copy_until_forced(self):
        # Wiring against installer.transaction: a copied file the user
        # changed is kept (YELLOW line + reason) and -f overwrites it.
        from installer import transaction
        if "force" not in inspect.signature(transaction.Transaction.apply).parameters:
            self.skipTest("transaction without keep/force support")
        repo = Path(self._tmp.name) / "repo"
        (repo / "manifests").mkdir(parents=True)
        (repo / "gitconfig.stub").write_text("[include]\n")
        (repo / "manifests" / "managed-paths.json").write_text(json.dumps(
            {"schema": 1, "entries": [{"id": "gitconfig", "dest": "{home}/.gitconfig",
                                       "kind": "copy", "source": "gitconfig.stub"}]}))
        dest = self.home / ".gitconfig"

        def run(force=False):
            ctx = self.ctx()
            ctx.repo_root = repo
            ctx.run_id = transaction.new_run_id()
            with redirect_stdout(io.StringIO()) as out:
                result, _ = install.transaction_phase(ctx, force=force)
            self.assertEqual(result.status, "PASS", result.reasons)
            return result, out.getvalue()

        run()
        dest.write_text("[include]\n[user]\n\tname = Me\n")
        result, out = run()
        self.assertIn(ui.target_line(dest, "kept your local changes (use -f to overwrite)"),
                      out.splitlines())
        self.assertIn("(use -f to overwrite)", result.reasons[0])
        self.assertIn("name = Me", dest.read_text())
        result, out = run(force=True)
        self.assertEqual(dest.read_text(), "[include]\n")
        self.assertIn("your local changes were overwritten: -f", out)

    def test_force_is_passed_only_when_apply_accepts_it(self):
        class Old:
            def apply(self, desired, *, generation):
                pass

        class New:
            def apply(self, desired, *, generation, force=False):
                pass

        self.assertEqual(install._apply_kwargs(Old(), {}, True), {"generation": {}})
        self.assertEqual(install._apply_kwargs(New(), {}, True),
                         {"generation": {}, "force": True})
        self.assertEqual(install._apply_kwargs(New(), {}, False),
                         {"generation": {}, "force": False})


class PostInstallTests(TempHome):
    def test_unreachable_user_manager_is_relogin(self):
        runner = ScriptedRunner(
            responses={("systemctl", "--user", "show-environment"): 1},
            which={"systemctl": "/bin/systemctl"})
        repo_root = self.home / "repo"
        repo_root.mkdir()
        r = phases.post_install_phase(self.target, runner, repo_root=repo_root,
                                      run_id="20260101T000000Z-0123abcd",
                                      systemd_units_applied=True)
        self.assertEqual(r.status, "RELOGIN_REQUIRED")
        self.assertIn("systemd-user-manager-unavailable", r.reasons)
        self.assertNotIn(["systemctl", "--user", "daemon-reload"], runner.calls)

    def reachable_post(self, runner):
        repo_root = self.home / "repo"
        repo_root.mkdir(exist_ok=True)
        return phases.post_install_phase(self.target, runner, repo_root=repo_root,
                                         run_id="20260101T000000Z-0123abcd",
                                         systemd_units_applied=True)

    def enable_timer(self):
        timer = "tmux-resurrect-autosave.timer"
        wants = self.target.config_home / "systemd/user/timers.target.wants" / timer
        wants.parent.mkdir(parents=True)
        os.symlink(wants.parent.parent / timer, wants)

    def test_reachable_manager_starts_the_autosave_timer(self):
        # REQ-4: daemon-reload alone does not start a newly wanted timer.
        self.enable_timer()
        runner = ScriptedRunner(which={"systemctl": "/bin/systemctl"})
        r = self.reachable_post(runner)
        self.assertEqual(r.status, "PASS", r.reasons)
        reload = runner.calls.index(["systemctl", "--user", "daemon-reload"])
        start = runner.calls.index(["systemctl", "--user", "start",
                                    "tmux-resurrect-autosave.timer"])
        self.assertLess(reload, start)
        self.assertEqual(r.details["autosave_timer"], "started")
        # Enabling stays the manifest's wants link: no 'enable' call.
        self.assertFalse(any("enable" in c for c in runner.calls))

    def test_timer_start_failure_is_reported(self):
        self.enable_timer()
        runner = ScriptedRunner(responses={("systemctl", "--user", "start"): 1},
                                which={"systemctl": "/bin/systemctl"})
        r = self.reachable_post(runner)
        self.assertEqual(r.status, "FAIL")
        self.assertTrue(any("start tmux-resurrect-autosave.timer failed" in reason
                            for reason in r.reasons))

    def test_timer_not_enabled_is_not_started(self):
        runner = ScriptedRunner(which={"systemctl": "/bin/systemctl"})
        r = self.reachable_post(runner)
        self.assertEqual(r.status, "PASS")
        self.assertEqual(r.details["autosave_timer"], "not-enabled")
        self.assertFalse(any(c[:3] == ["systemctl", "--user", "start"]
                             for c in runner.calls))

    def test_plugin_updates_follow_skip_flags(self):
        repo_root = self.home / "repo"
        repo_root.mkdir()
        which = {"zsh": "/fake/zsh", "nvim": "/fake/nvim"}

        def post(runner, **kw):
            return phases.post_install_phase(self.target, runner, repo_root=repo_root,
                                             run_id="20260101T000000Z-0123abcd",
                                             systemd_units_applied=False, **kw)

        runner = ScriptedRunner(which=which)
        r = post(runner)
        self.assertEqual(r.status, "PASS")
        self.assertEqual([c[0] for c in runner.calls], ["/fake/zsh", "/fake/nvim"])
        self.assertIn("antidote update", runner.calls[0][-1])
        self.assertIn("lua require('lazy').update { wait = true }", runner.calls[1])
        self.assertEqual((r.details["zsh_plugins"], r.details["vim_plugins"]),
                         ("updated", "updated"))

        runner = ScriptedRunner(which=which)
        r = post(runner, skip_zplug=True, skip_vimplug=True)
        self.assertEqual(runner.calls, [])
        self.assertEqual((r.details["zsh_plugins"], r.details["vim_plugins"]),
                         ("--skip-zplug", "--skip-vimplug"))

        runner = ScriptedRunner(
            responses={("/fake/zsh", "-c", phases.ZSH_PLUGIN_SCRIPT): 1}, which=which)
        r = post(runner, skip_vimplug=True)
        self.assertEqual(r.status, "FAIL")
        self.assertTrue(any("zsh plugins failed" in reason for reason in r.reasons))

        r = post(ScriptedRunner())
        self.assertEqual(r.status, "PASS")
        self.assertEqual(r.details["vim_plugins"], "nvim-not-installed")

    def test_converge_without_tmux_is_a_no_op(self):
        r = self.reachable_post(ScriptedRunner())
        self.assertEqual(r.details["tmux_converge"],
                         {"outcome": "no-running-server", "respawned_panes": 0,
                          "busy_panes": 0})

    def test_converge_error_is_reported_not_fatal(self):
        with mock.patch.object(phases, "converge_running_tmux",
                               side_effect=RuntimeError("boom")):
            r = self.reachable_post(ScriptedRunner())
        self.assertNotEqual(r.status, "FAIL")
        self.assertEqual(r.details["tmux_converge"]["outcome"], "failed")
        self.assertTrue(any("boom" in reason for reason in r.reasons))

    def test_converge_addresses_tmux_with_the_unstripped_env(self):
        seen = {}

        def converge(target, runner, env, **kw):
            seen["env"] = env
            return [], {"outcome": "no-running-server", "respawned_panes": 0,
                        "busy_panes": 0}

        with mock.patch.object(phases, "converge_running_tmux", side_effect=converge):
            phases.post_install_phase(
                self.target, ScriptedRunner(), repo_root=self.home,
                run_id="20260101T000000Z-0123abcd", systemd_units_applied=False,
                env={"HOME": "x"}, tmux_env={"TMUX_TMPDIR": "/private"})
        self.assertEqual(seen["env"], {"TMUX_TMPDIR": "/private"})

    def test_bad_plugin_manifest_fails(self):
        repo_root = self.home / "repo"
        (repo_root / "manifests").mkdir(parents=True)
        (repo_root / "manifests" / "tmux-plugins.json").write_text(json.dumps(
            {"schema": 1, "plugins": [{"name": "../x", "url": "u", "commit": "0" * 40}]}))
        r = phases.post_install_phase(self.target, ScriptedRunner(), repo_root=repo_root,
                                      run_id="20260101T000000Z-0123abcd",
                                      systemd_units_applied=False)
        self.assertEqual(r.status, "FAIL")


def _real_tmux() -> str | None:
    for candidate in ("/usr/bin/tmux", "/bin/tmux"):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


@unittest.skipIf(_real_tmux() is None, "tmux is not installed")
class TmuxSmokeTests(TempHome):
    """Real tmux on a private socket; never the caller's server."""

    class TmuxRunner(Runner):
        def which(self, name):
            return _real_tmux() if name == "tmux" else None

    def smoke(self, conf: str):
        (self.home / ".tmux.conf").write_text(conf)
        env = phases.child_env(self.target, {"PATH": "/usr/bin:/bin",
                                             "TMUX": "/tmp/should-be-dropped,1,0"})
        self.assertNotIn("TMUX", env)
        runner = self.TmuxRunner()
        calls = []
        original = runner.run

        def spy(argv, **kw):
            calls.append((list(argv), dict(kw.get("env") or {})))
            return original(argv, **kw)

        runner.run = spy
        status, reason = phases.smoke_tmux(self.target, runner, env)
        for argv, cenv in calls:
            self.assertIn("-L", argv)
            self.assertNotIn("TMUX", cenv)
            self.assertTrue(os.path.basename(cenv["TMUX_TMPDIR"]).startswith("pdfs-"))
            name = argv[argv.index("-L") + 1]
            socket = os.path.join(cenv["TMUX_TMPDIR"], f"tmux-{os.getuid()}", name)
            self.assertLessEqual(len(socket), 100)
        # One short -L name for every call, and the server is killed by it.
        names = {argv[argv.index("-L") + 1] for argv, _ in calls}
        self.assertEqual(len(names), 1)
        self.assertLessEqual(len(names.pop()), 16)
        self.assertEqual(calls[-1][0][-1], "kill-server")
        self.assertFalse(os.path.exists(calls[-1][1]["TMUX_TMPDIR"]))
        return status, reason

    def test_good_config_passes(self):
        self.assertEqual(self.smoke("set -g status off\n")[0], "PASS")

    def test_bad_config_fails(self):
        status, reason = self.smoke("this-is-not-a-tmux-command\n")
        self.assertEqual(status, "FAIL")
        self.assertIn("config", reason)

    def test_long_tmpdir_still_passes(self):
        # TQ-5: a long per-session TMPDIR used to push the socket path past
        # sun_path, so a good config failed with 'File name too long'.
        long_root = Path(self._tmp.name) / ("t" * 60) / ("u" * 60)
        long_root.mkdir(parents=True)
        with mock.patch.object(tempfile, "tempdir", str(long_root)):
            self.assertEqual(tempfile.gettempdir(), str(long_root))
            status, reason = self.smoke("set -g status off\n")
            self.assertEqual((status, reason), ("PASS", ""))
            self.assertEqual(self.smoke("this-is-not-a-tmux-command\n")[0], "FAIL")
        self.assertEqual(list(long_root.iterdir()), [])

    def test_no_short_directory_is_a_clear_failure(self):
        with mock.patch.object(phases, "_short_socket_dir", return_value=None):
            status, reason = phases.smoke_tmux(self.target, self.TmuxRunner(), {})
        self.assertEqual(status, "FAIL")
        self.assertIn("TMPDIR=/tmp", reason)


class GitIdentityTests(TempHome):
    class GitConfigRunner(ScriptedRunner):
        """Real ``git config --file`` (touches only the given file)."""

        def run(self, argv, *, timeout, check=True, env=None, input=None, cwd=None,
                read_only=False):
            argv = [str(a) for a in argv]
            self.calls.append(argv)
            if argv[:3] != ["git", "config", "--file"]:
                raise AssertionError(argv)
            return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                       "GIT_CONFIG_NOSYSTEM": "1",
                                       "GIT_CONFIG_GLOBAL": "/dev/null",
                                       "HOME": str(Path(argv[3]).parent)})

    def runner(self):
        return self.GitConfigRunner(which={"git": shutil.which("git")})

    def phase(self, **kw):
        with redirect_stdout(io.StringIO()) as out:
            result = phases.git_identity_phase(self.target, self.runner(), **kw)
        return result, out.getvalue()

    def test_without_terminal_prints_the_commands(self):
        result, out = self.phase(prompt=None)
        self.assertEqual(result.status, "SKIPPED")
        secret = self.home / ".gitconfig.secret"
        self.assertIn(f"git config --file {secret} user.name", out)
        self.assertEqual(secret.read_text(), "# vim: set ft=gitconfig:\n")
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
        lines = phases.completion_lines([result])
        self.assertTrue(any("user.email" in line for line in lines))

    def test_prompt_writes_the_secret_file_only(self):
        answers = iter(["Fixture Name", "fixture@example.invalid"])
        questions = []

        def prompt(question):
            questions.append(question)
            return next(answers)

        result, out = self.phase(prompt=prompt)
        self.assertEqual(result.status, "PASS")
        self.assertIn("(git config user.name) Please input your name", questions[0])
        self.assertIn("(git config user.email) Please input your email", questions[1])
        text = (self.home / ".gitconfig.secret").read_text()
        self.assertIn("Fixture Name", text)
        self.assertIn("fixture@example.invalid", text)
        self.assertFalse((self.home / ".gitconfig").exists())
        # Already configured: no prompt.
        result, out = self.phase(prompt=lambda q: self.fail("prompted again"))
        self.assertEqual(result.status, "PASS")
        self.assertIn("user.name  : Fixture Name", out)

    def test_empty_answer_is_not_written(self):
        result, _ = self.phase(prompt=lambda q: "")
        self.assertEqual(result.status, "SKIPPED")
        self.assertNotIn("user", (self.home / ".gitconfig.secret").read_text()
                         .replace("# vim: set ft=gitconfig:", ""))


class UiTests(unittest.TestCase):
    def tearDown(self):
        ui.configure(enabled=False)

    class Tty(io.StringIO):
        def isatty(self):
            return True

    def test_colors_only_on_a_tty_without_no_color(self):
        self.assertFalse(ui.colors_wanted(io.StringIO(), {}))
        self.assertTrue(ui.colors_wanted(self.Tty(), {}))
        self.assertFalse(ui.colors_wanted(self.Tty(), {"NO_COLOR": ""}))
        self.assertFalse(ui.colors_wanted(self.Tty(), {"NO_COLOR": "1"}))
        ui.configure(stream=io.StringIO(), env={})
        self.assertEqual(ui.GREEN("ok"), "ok")
        ui.configure(stream=self.Tty(), env={})
        self.assertEqual(ui.GREEN("ok"), "\033[0;32mok\033[0m")
        self.assertEqual(ui.CYAN("x"), "\033[0;36mx\033[0m")

    def test_boxed_headers_like_upstream(self):
        ui.configure(enabled=False)
        box = ui.boxed("Creating symbolic links", ui.CYAN, use_bold=True).splitlines()
        self.assertEqual(box[1], "┃ Creating symbolic links  ┃")
        self.assertEqual(box[0], "┏" + "━" * 26 + "┓")
        self.assertEqual(box[2], "┗" + "━" * 26 + "┛")
        thin = ui.boxed("x").splitlines()
        self.assertEqual(thin, ["┌────┐", "│ x  │", "└────┘"])
        out = io.StringIO()
        ui.section("Post actions", stream=out)
        self.assertIn("┃ Post actions  ┃", out.getvalue())

    def test_target_lines(self):
        ui.configure(enabled=False)
        line = ui.target_line("/h/.zshrc", "symlink created from '/h/.dotfiles/zsh/zshrc'")
        self.assertEqual(line, "{:60s} : {}".format(
            "/h/.zshrc", "symlink created from '/h/.dotfiles/zsh/zshrc'"))
        ui.configure(enabled=True)
        self.assertTrue(ui.target_line("a", "b").startswith("\033[0;34ma\033[0m"))

    def test_logo_is_attributed(self):
        self.assertIn("@leekyungmoon's", ui.LOGO)
        self.assertIn("@leekyungmoon's", install.__doc__)
        self.assertNotIn("wookayin", install.__doc__)

    def test_completion_lines(self):
        ui.configure(enabled=False)
        ok = phases.completion_lines([PhaseResult("packages", "PASS", [],
                                                  {"tools_selected": ["codex", "claude-code"]}),
                                      PhaseResult("login-shell", "RELOGIN_REQUIRED"),
                                      PhaseResult("gui", "PENDING_GUI", ["no session"])])
        text = "\n".join(ok)
        self.assertIn("You are all set!", text)
        self.assertIn("codex login", text)
        self.assertIn("Log out and back in", text)
        self.assertIn("no session", text)
        # A CLI the user declined is not listed in the sign-in reminder.
        claude_only = "\n".join(phases.completion_lines([PhaseResult(
            "packages", "PASS", [], {"tools_selected": ["node", "claude-code"]})]))
        self.assertIn("claude", claude_only)
        self.assertNotIn("codex login", claude_only)
        neither = "\n".join(phases.completion_lines([PhaseResult(
            "packages", "PASS", [], {"tools_selected": ["node", "neovim"]})]))
        self.assertNotIn("Sign in to the AI CLIs", neither)
        # A running tmux server was converged: nothing is left for the user to
        # do, so there is no "restart your shell" or before-install wording.
        for outcome in ("converged", "no-running-server", "failed"):
            text = "\n".join(phases.completion_lines([PhaseResult(
                "post-install", "PASS", [], {"tmux_converge": {
                    "outcome": outcome, "respawned_panes": 2, "busy_panes": 1}})]))
            for wording in ("exec zsh", "restart shell", "already running",
                            "already open", "before"):
                self.assertNotIn(wording, text, outcome)
        bad = "\n".join(phases.completion_lines([PhaseResult("smoke", "FAIL", ["zsh"])]))
        self.assertIn("You have   1 warnings or errors", bad)
        self.assertNotIn("codex login", bad)


class AiCliChoiceTests(unittest.TestCase):
    """Claude Code and the Codex CLI are each asked about once, separately."""

    def setUp(self):
        ui.configure(enabled=False)
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name)
        self.target = Target(uid=os.getuid(), gid=os.getgid(), username="fixture",
                             home=home, data_home=home / ".local/share",
                             state_home=home / ".local/state", config_home=home / ".config",
                             cache_home=home / ".cache")

    def tearDown(self):
        self._tmp.cleanup()

    def run_choices(self, answers=None, **flags):
        asked = []

        def prompt(question):
            asked.append(question)
            return answers.pop(0) if answers else ""

        ctx = install.Context(target=self.target, platform=None, runner=None, env={},
                              run_id="r", prompt=prompt if answers is not None else None)
        with redirect_stdout(io.StringIO()):
            exclude = install.ai_cli_exclusions(ctx, install.Options(**flags))
        return exclude, asked

    def test_asks_each_cli_separately(self):
        exclude, asked = self.run_choices(["y", "n"])
        self.assertEqual(asked, ["Install Claude Code? [Y/n] ", "Install Codex CLI? [Y/n] "])
        self.assertEqual(exclude, ["codex", "oh-my-codex"])

    def test_declining_claude_keeps_codex(self):
        exclude, _ = self.run_choices(["no", "yes"])
        self.assertEqual(exclude, ["claude-code"])

    def test_enter_means_yes(self):
        exclude, _ = self.run_choices(["", ""])
        self.assertEqual(exclude, [])

    def test_answers_are_remembered_for_updates(self):
        self.run_choices(["n", "y"])
        exclude, asked = self.run_choices(["y", "y"])
        self.assertEqual(asked, [])
        self.assertEqual(exclude, ["claude-code"])
        mode = (self.target.state_root / "choices.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_flags_win_over_questions_and_memory(self):
        self.run_choices(["n", "n"])
        exclude, asked = self.run_choices(["n", "n"], claude_code=True, codex=True)
        self.assertEqual(asked, [])
        self.assertEqual(exclude, [])

    def test_no_terminal_and_no_memory_installs_both(self):
        exclude, asked = self.run_choices(None)
        self.assertEqual((exclude, asked), ([], []))

    def test_cli_flags_parse_tristate(self):
        args = install.build_parser().parse_args(["install", "--no-codex"])
        opts = install.options_from(args)
        self.assertIsNone(opts.claude_code)
        self.assertIs(opts.codex, False)


if __name__ == "__main__":
    unittest.main()
