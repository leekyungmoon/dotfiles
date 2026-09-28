"""Unit tests for install.py and installer.phases: parsing, gates and seams."""

from __future__ import annotations

import io
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
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
            self.assertTrue(cenv["TMUX_TMPDIR"].startswith(tempfile.gettempdir()))
        self.assertEqual(calls[-1][0][-1], "kill-server")
        self.assertFalse(os.path.exists(calls[-1][1]["TMUX_TMPDIR"]))
        return status, reason

    def test_good_config_passes(self):
        self.assertEqual(self.smoke("set -g status off\n")[0], "PASS")

    def test_bad_config_fails(self):
        status, reason = self.smoke("this-is-not-a-tmux-command\n")
        self.assertEqual(status, "FAIL")
        self.assertIn("config", reason)


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
        ok = phases.completion_lines([PhaseResult("packages", "PASS"),
                                      PhaseResult("login-shell", "RELOGIN_REQUIRED"),
                                      PhaseResult("gui", "PENDING_GUI", ["no session"])])
        text = "\n".join(ok)
        self.assertIn("You are all set!", text)
        self.assertIn("codex login", text)
        self.assertIn("Log out and back in", text)
        self.assertIn("no session", text)
        bad = "\n".join(phases.completion_lines([PhaseResult("smoke", "FAIL", ["zsh"])]))
        self.assertIn("You have   1 warnings or errors", bad)
        self.assertNotIn("codex login", bad)


if __name__ == "__main__":
    unittest.main()
