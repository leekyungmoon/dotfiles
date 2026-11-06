"""Independent Sidekick selection and Copilot CLI login, without real credentials."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import install
from installer import packages, phases
from installer.platform import Platform, Target


class CopilotCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dotfiles-sidekick-")
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        self.target = Target(os.getuid(), os.getgid(), "fixture", home,
                             home / "data", home / "state", home / "config", home / "cache")
        self.runner = mock.Mock()
        self.runner.which.side_effect = lambda n: "/fixture/" + n
        self.ctx = install.Context(self.target, Platform("ubuntu", "24.04", "amd64"),
                                   self.runner, {}, "test", repo_root=ROOT)
        self.output = io.StringIO()
        self.stdout = redirect_stdout(self.output)
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def choice(self, value=None, **kwargs):
        return install.copilot_cli_choice(self.ctx, install.Options(copilot_cli=value, **kwargs))

    def plugin(self):
        p = self.target.data_home / "vim/plugged/sidekick.nvim/lua/sidekick/init.lua"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("return {}\n")

    def choices(self):
        return json.loads((self.target.state_root / "choices.json").read_text())

    def test_flags_have_independent_tristate(self):
        self.assertIsNone(install.options_from(install.parse_args([])).copilot_cli)
        for cmd in ("install", "repair"):
            for flag, value in (("--copilot-cli", True), ("--no-copilot-cli", False)):
                opts = install.options_from(install.parse_args([cmd, flag]))
                self.assertIs(opts.copilot_cli, value)
                self.assertIsNone(opts.copilot)

    def test_prompt_default_yes_preserves_other_choices_and_is_remembered(self):
        install._save_choices(self.target, {"copilot": False, "codex": True})
        self.ctx.prompt = mock.Mock(return_value="")
        self.assertTrue(self.choice())
        self.assertIn("Copilot CLI + Sidekick", self.ctx.prompt.call_args.args[0])
        self.assertEqual(self.choices(), {"copilot": False, "codex": True, "copilot-cli": True})
        self.ctx.prompt.reset_mock()
        self.assertTrue(self.choice())
        self.ctx.prompt.assert_not_called()

    def test_no_and_flags_override_saved_choice(self):
        self.ctx.prompt = mock.Mock(return_value="n")
        self.assertFalse(self.choice())
        self.assertIs(self.choices()["copilot-cli"], False)
        self.ctx.prompt.reset_mock()
        self.assertTrue(self.choice(True))
        self.assertFalse(self.choice(False))
        self.ctx.prompt.assert_not_called()

    def test_unattended_eof_and_skip_do_not_opt_in(self):
        self.assertFalse(self.choice())
        self.ctx.prompt = mock.Mock(return_value=None)
        self.assertFalse(self.choice())
        self.ctx.prompt.reset_mock()
        self.assertFalse(self.choice(skip_vimplug=True))
        self.ctx.prompt.assert_not_called()
        self.assertFalse(self.target.state_root.exists())

    def test_explicit_choice_works_with_no_packages(self):
        self.assertTrue(self.choice(True, no_packages=True))
        self.assertIs(self.choices()["copilot-cli"], True)

    def test_dry_run_does_not_persist_choice_or_start_login(self):
        with mock.patch.object(install, "preflight_phase", return_value=phases.PhaseResult("preflight", phases.PASS)), \
                mock.patch.object(install, "packages_phase", return_value=phases.PhaseResult("packages", phases.SKIPPED)) as packages_run, \
                mock.patch.object(phases, "copilot_cli_auth_phase") as auth:
            install.dry_run_install(self.ctx, install.Options(copilot_cli=True))
        self.assertNotIn("copilot-cli", packages_run.call_args.kwargs["exclude"])
        self.assertFalse(self.target.state_root.exists())
        auth.assert_not_called()

    def test_login_uses_vendor_command_and_target_environment_only(self):
        self.plugin()
        interactive = mock.Mock(return_value=0)
        result = phases.copilot_cli_auth_phase(
            self.target, self.runner, {"PATH": "/usr/bin", "HOME": "/wrong"},
            prompt=lambda _: "", interactive=interactive)
        self.assertEqual(result.status, phases.PASS)
        self.assertEqual(result.details["copilot-cli"], "login-completed")
        self.assertEqual(interactive.call_args.args[0], ["/fixture/copilot", "login"])
        self.assertEqual(interactive.call_args.kwargs["env"]["HOME"], str(self.target.home))
        self.runner.run.assert_not_called()

    def test_declining_or_unattended_is_unverified_not_signed_out(self):
        self.plugin()
        for prompt in (None, lambda _: "n", lambda _: None):
            interactive = mock.Mock()
            result = phases.copilot_cli_auth_phase(
                self.target, self.runner, prompt=prompt, interactive=interactive)
            self.assertEqual(result.status, phases.SKIPPED)
            self.assertEqual(result.details, {"copilot-cli": "unverified"})
            self.assertIn("/user", " ".join(result.reasons))
            interactive.assert_not_called()
        self.runner.run.assert_not_called()

    def test_nonzero_login_and_cancellation_are_reported(self):
        self.plugin()
        for interactive in (mock.Mock(return_value=1), mock.Mock(side_effect=KeyboardInterrupt)):
            result = phases.copilot_cli_auth_phase(
                self.target, self.runner, prompt=lambda _: "y", interactive=interactive)
            self.assertEqual(result.status, phases.AUTH_REQUIRED)

    def test_skip_or_plugin_failure_does_not_launch_login(self):
        for flags in ({"skip": True}, {"plugins_ready": False}):
            interactive = mock.Mock()
            result = phases.copilot_cli_auth_phase(
                self.target, self.runner, prompt=lambda _: "y", interactive=interactive, **flags)
            self.assertEqual(result.status, phases.SKIPPED)
            interactive.assert_not_called()

    def test_missing_plugin_or_tool_cannot_report_success(self):
        result = phases.copilot_cli_auth_phase(self.target, self.runner)
        self.assertEqual(result.status, phases.FAIL)
        self.plugin()
        self.runner.which.side_effect = lambda _: None
        result = phases.copilot_cli_auth_phase(self.target, self.runner)
        self.assertEqual(result.status, phases.FAIL)

    def test_pipeline_writes_choice_before_packages_and_plugins(self):
        self.plugin()
        self.ctx.interactive = mock.Mock(return_value=0)
        self.ctx.prompt = lambda _: "y"
        seen = []

        def pkg(*args, **kwargs):
            self.assertIs(self.choices()["copilot-cli"], True)
            self.assertNotIn("copilot-cli", kwargs["exclude"])
            seen.append("packages")
            return phases.PhaseResult("packages", phases.PASS)

        def post(*args, **kwargs):
            seen.append("plugins")
            return phases.PhaseResult("post-install", phases.PASS, [], {"vim_plugins": "updated"})

        def finish(ctx, cmd, results, **kwargs):
            self.assertEqual(results[-2].phase, "copilot-cli-auth")
            self.assertEqual(results[-2].status, phases.PASS)
            seen.append("finish")
            return 0

        with ExitStack() as stack:
            for name in ("preflight_phase", "gui_phase"):
                stack.enter_context(mock.patch.object(install, name, return_value=phases.PhaseResult(name, phases.PASS)))
            stack.enter_context(mock.patch.object(install, "packages_phase", side_effect=pkg))
            stack.enter_context(mock.patch.object(install, "extra_desired_entries", return_value=([], [])))
            stack.enter_context(mock.patch.object(install, "transaction_phase", return_value=(phases.PhaseResult("transaction", phases.PASS), False)))
            stack.enter_context(mock.patch.object(phases, "post_install_phase", side_effect=post))
            for name in ("smoke_phase", "login_shell_phase", "git_identity_phase", "auth_phase"):
                stack.enter_context(mock.patch.object(phases, name, return_value=phases.PhaseResult(name, phases.PASS)))
            stack.enter_context(mock.patch.object(install, "finish", side_effect=finish))
            rc = install.run_pipeline(self.ctx, "install", install.Options(
                claude_code=False, codex=False, copilot=False, copilot_cli=True))
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["packages", "plugins", "finish"])

    def test_pinned_package_has_both_supported_architectures_and_home_link(self):
        package_manifest, tools = packages.load_manifests()
        spec = tools["tools"]["copilot-cli"]
        self.assertEqual(spec["install"]["method"], "extract")
        self.assertEqual(set(spec["artifacts"]), {"amd64", "arm64"})
        self.assertIn("copilot", tools["home_links"])
        self.assertIn("COPILOT_OFFLINE=true", spec["verify"][0]["argv"])
        for artifact in spec["artifacts"].values():
            self.assertIn("github.com/github/copilot-cli/releases/download/", artifact["url"])
            self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")
        _, selected = packages._select(package_manifest, tools, packages.DEFAULT_GROUPS, None,
                                       ["copilot-cli"])
        self.assertNotIn("copilot-cli", selected)
        groups, selected = packages._select(package_manifest, tools, (), ["copilot-cli"])
        self.assertEqual(groups, [])
        self.assertEqual(selected, ["copilot-cli"])


if __name__ == "__main__":
    unittest.main()
