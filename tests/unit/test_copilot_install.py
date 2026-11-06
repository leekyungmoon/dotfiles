"""Optional Copilot selection, installer sequencing, and terminal login gates."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import install
from installer import phases
from installer.platform import Platform, Target


class ProbeRunner:
    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.calls = []

    def which(self, name):
        return "/fixture/bin/" + name if name in ("nvim", "node") else None

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        status = self.statuses.pop(0)
        if isinstance(status, Exception):
            raise status
        output = f"unrelated output\nDOTFILES_COPILOT_STATUS={status}\n".encode()
        return subprocess.CompletedProcess(argv, 0, output, b"")


class CopilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dotfiles-copilot-")
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        self.target = Target(os.getuid(), os.getgid(), "fixture", home,
                             home / "data", home / "state", home / "config", home / "cache")
        self.ctx = install.Context(self.target, Platform("ubuntu", "24.04", "amd64"),
                                   ProbeRunner(), {}, "test", repo_root=ROOT)
        self.output = io.StringIO()
        self.output_context = redirect_stdout(self.output)
        self.output_context.__enter__()
        self.addCleanup(self.output_context.__exit__, None, None, None)

    def choices(self):
        return json.loads((self.target.state_root / "choices.json").read_text())

    def plugin(self):
        path = self.target.data_home / "vim/plugged/copilot.vim/plugin/copilot.vim"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('" fixture\n')
        return path

    def test_cli_flags_are_tristate(self):
        self.assertIsNone(install.options_from(install.parse_args([])).copilot)
        for command in ("install", "repair"):
            for flag, expected in (("--copilot", True), ("--no-copilot", False)):
                opts = install.options_from(install.parse_args([command, flag]))
                self.assertIs(opts.copilot, expected)
                self.assertIsNone(opts.codex)

    def test_yes_prompt_is_remembered_and_preserves_cli_choices(self):
        install._save_choices(self.target, {"codex": False})
        self.ctx.prompt = mock.Mock(return_value="")
        self.assertTrue(install.copilot_choice(self.ctx, install.Options()))
        self.assertIn("[Y/n]", self.ctx.prompt.call_args.args[0])
        self.assertEqual(self.choices(), {"codex": False, "copilot": True})
        self.ctx.prompt.reset_mock()
        self.assertTrue(install.copilot_choice(self.ctx, install.Options()))
        self.ctx.prompt.assert_not_called()

    def test_no_and_explicit_flags_override_saved_choice(self):
        self.ctx.prompt = mock.Mock(return_value="n")
        self.assertFalse(install.copilot_choice(self.ctx, install.Options()))
        self.assertIs(self.choices()["copilot"], False)
        self.ctx.prompt.reset_mock()
        self.assertTrue(install.copilot_choice(self.ctx, install.Options(copilot=True)))
        self.assertFalse(install.copilot_choice(self.ctx, install.Options(copilot=False)))
        self.ctx.prompt.assert_not_called()

    def test_fresh_unattended_and_eof_do_not_enable_copilot(self):
        self.assertFalse(install.copilot_choice(self.ctx, install.Options()))
        self.ctx.prompt = mock.Mock(return_value=None)
        self.assertFalse(install.copilot_choice(self.ctx, install.Options()))
        self.assertFalse(self.target.state_root.exists())

    def test_skip_plugins_does_not_ask_but_explicit_disable_is_saved(self):
        self.ctx.prompt = mock.Mock(side_effect=AssertionError("unexpected prompt"))
        self.assertFalse(install.copilot_choice(self.ctx, install.Options(skip_vimplug=True)))
        self.assertFalse(install.copilot_choice(
            self.ctx, install.Options(skip_vimplug=True, copilot=False)))
        self.assertIs(self.choices()["copilot"], False)

    def test_dry_choice_does_not_write(self):
        self.assertTrue(install.copilot_choice(
            self.ctx, install.Options(copilot=True), record=False))
        self.assertFalse(self.target.state_root.exists())
        self.assertIn("would enable", self.output.getvalue())

    def test_invalid_saved_choice_does_not_enable_plugin(self):
        install._save_choices(self.target, {"copilot": "yes"})
        self.assertFalse(install.copilot_choice(self.ctx, install.Options()))

    def test_cannot_silently_ignore_choice_write_failure(self):
        with mock.patch.object(install, "_save_choices", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                install.copilot_choice(self.ctx, install.Options(copilot=True))

    def test_auth_checks_again_after_terminal_login(self):
        self.plugin()
        runner = ProbeRunner("NotSignedIn", "OK")
        prompt = mock.Mock(return_value="")
        interactive = mock.Mock(return_value=0)
        result = phases.copilot_auth_phase(
            self.target, runner, {"PATH": "/usr/bin", "HOME": "/wrong"},
            prompt=prompt, interactive=interactive)
        self.assertEqual(result.status, phases.PASS)
        self.assertEqual(result.details, {"copilot": "signed-in"})
        self.assertEqual(len(runner.calls), 2)
        argv = interactive.call_args.args[0]
        self.assertEqual(argv, ["/fixture/bin/nvim", "-c", "Copilot setup", "-c", "qall"])
        env = interactive.call_args.kwargs["env"]
        self.assertEqual(env["HOME"], str(self.target.home))
        self.assertEqual(env["XDG_STATE_HOME"], str(self.target.state_home))
        self.assertTrue(env["PATH"].startswith(str(self.target.home / ".local/bin")))
        self.assertEqual(runner.calls[0][1]["env"], env)
        self.assertIn("--headless", runner.calls[0][0])

    def test_signed_in_does_not_ask_or_launch_editor(self):
        self.plugin()
        for state in phases.COPILOT_SIGNED_IN:
            prompt, interactive = mock.Mock(), mock.Mock()
            result = phases.copilot_auth_phase(
                self.target, ProbeRunner(state), prompt=prompt, interactive=interactive)
            self.assertEqual(result.status, phases.PASS)
            prompt.assert_not_called()
            interactive.assert_not_called()

    def test_unattended_declined_or_eof_login_remains_auth_required(self):
        self.plugin()
        for answer in ("n", None, "no-terminal"):
            interactive = mock.Mock()
            prompt = None if answer == "no-terminal" else mock.Mock(return_value=answer)
            result = phases.copilot_auth_phase(
                self.target, ProbeRunner("NotSignedIn"), prompt=prompt, interactive=interactive)
            self.assertEqual(result.status, phases.AUTH_REQUIRED)
            self.assertIn(":Copilot setup", " ".join(result.reasons))
            interactive.assert_not_called()

    def test_editor_exit_alone_is_not_authentication(self):
        self.plugin()
        result = phases.copilot_auth_phase(
            self.target, ProbeRunner("NotSignedIn", "NotAuthorized"),
            prompt=lambda _: "y", interactive=lambda *a, **kw: 0)
        self.assertEqual(result.status, phases.AUTH_REQUIRED)

    def test_cancelled_or_failed_login_is_reported(self):
        self.plugin()
        for interactive in (mock.Mock(return_value=130), mock.Mock(side_effect=KeyboardInterrupt)):
            runner = ProbeRunner("NotSignedIn")
            result = phases.copilot_auth_phase(
                self.target, runner, prompt=lambda _: "y", interactive=interactive)
            self.assertEqual(result.status, phases.AUTH_REQUIRED)
            self.assertEqual(len(runner.calls), 1)

    def test_invalid_or_timed_out_probe_fails_without_exposing_output(self):
        self.plugin()
        for state in ("private-account-data", "unknown", RuntimeError("private-account-data")):
            interactive = mock.Mock()
            result = phases.copilot_auth_phase(
                self.target, ProbeRunner(state), prompt=lambda _: "y", interactive=interactive)
            self.assertEqual(result.status, phases.FAIL)
            self.assertNotIn("private-account-data", json.dumps(result.to_dict()))
            interactive.assert_not_called()

    def test_skipped_or_failed_plugin_update_cannot_trigger_autoinstall(self):
        for flags in ({"skip": True}, {"plugins_ready": False}):
            runner, interactive = ProbeRunner(), mock.Mock()
            result = phases.copilot_auth_phase(
                self.target, runner, prompt=lambda _: "y", interactive=interactive, **flags)
            self.assertEqual(result.status, phases.SKIPPED)
            self.assertEqual(runner.calls, [])
            interactive.assert_not_called()

    def test_missing_plugin_does_not_start_neovim(self):
        runner = ProbeRunner()
        result = phases.copilot_auth_phase(self.target, runner)
        self.assertEqual(result.status, phases.FAIL)
        self.assertEqual(runner.calls, [])

    def test_missing_prerequisite_fails(self):
        runner = ProbeRunner()
        runner.which = lambda _: None
        self.assertEqual(phases.copilot_auth_phase(self.target, runner).status, phases.FAIL)

    def test_plugin_install_uses_new_neovim_before_shell_path_is_refreshed(self):
        nvim = self.target.home / ".local/bin/nvim"
        nvim.parent.mkdir(parents=True)
        nvim.write_text("#!/bin/sh\nexit 99\n")
        nvim.chmod(0o755)
        for on_path in (None, "/usr/bin/old-nvim"):
            runner = mock.Mock()
            runner.which.return_value = on_path
            runner.run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
            statuses, reasons, details = phases.plugins_step(
                self.target, runner, env=phases.child_env(self.target, {"PATH": "/usr/bin"}),
                skip_zplug=True, skip_vimplug=False)
            self.assertEqual((statuses, reasons), ([], []))
            self.assertEqual(details["vim_plugins"], "updated")
            self.assertEqual(runner.run.call_args.args[0][0], str(nvim))

    def test_pipeline_selects_before_plugins_and_authenticates_after(self):
        self.plugin()
        self.ctx.runner = ProbeRunner("NotSignedIn", "OK")
        self.ctx.prompt = lambda _: "y"
        self.ctx.interactive = mock.Mock(return_value=0)
        seen = []

        def post(*args, **kwargs):
            self.assertIs(self.choices()["copilot"], True)
            seen.append("plugins")
            return phases.PhaseResult("post-install", phases.PASS, [], {"vim_plugins": "updated"})

        def finish(ctx, command, results, **kwargs):
            self.assertEqual(results[-2].phase, "copilot-auth")
            self.assertEqual(results[-2].status, phases.PASS)
            seen.append("finished")
            return 0

        with ExitStack() as stack:
            for name in ("preflight_phase", "gui_phase"):
                stack.enter_context(mock.patch.object(
                    install, name, return_value=phases.PhaseResult(name, phases.PASS)))
            packages = stack.enter_context(mock.patch.object(
                install, "packages_phase", return_value=phases.PhaseResult("packages", phases.PASS)))
            stack.enter_context(mock.patch.object(install, "extra_desired_entries", return_value=([], [])))
            stack.enter_context(mock.patch.object(install, "transaction_phase", return_value=(
                phases.PhaseResult("transaction", phases.PASS), False)))
            stack.enter_context(mock.patch.object(phases, "post_install_phase", side_effect=post))
            for name in ("smoke_phase", "login_shell_phase", "git_identity_phase", "auth_phase"):
                stack.enter_context(mock.patch.object(
                    phases, name, return_value=phases.PhaseResult(name, phases.PASS)))
            stack.enter_context(mock.patch.object(install, "finish", side_effect=finish))
            rc = install.run_pipeline(self.ctx, "install", install.Options(
                claude_code=False, codex=False, copilot=True, copilot_cli=False))
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["plugins", "finished"])
        self.assertEqual(packages.call_args.kwargs["exclude"],
                         ["claude-code", "codex", "oh-my-codex", "copilot-cli"])


if __name__ == "__main__":
    unittest.main()
