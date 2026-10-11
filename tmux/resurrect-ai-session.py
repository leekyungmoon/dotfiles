#!/usr/bin/env python3
"""Enrich a tmux-resurrect snapshot with proven Codex/Claude resume IDs.

resurrect-ai-session.py STATE_FILE    tmux-resurrect's post-save hook
resurrect-ai-session.py --self-test   exit 0 when process inspection works on
                                      this platform, else 1 with the reason

Processes are inspected through /proc on Linux and through libproc and
sysctl (installer/darwin_proc.py) on macOS.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Iterable
import unicodedata


UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
ROLLOUT_UUID_RE = re.compile(
    r"rollout-[^/]*-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$"
)
SESSION_META_MAX_BYTES = 64 * 1024
SAFE_OPTION_VALUE_RE = re.compile(r"[A-Za-z0-9_.:@/+\[\]-]{1,128}")

# macOS has no /proc. There the same facts (parent, start time, exact argv,
# open files) come from libproc and sysctl through installer/darwin_proc.py,
# which this script loads from its own checkout on first use. Linux never
# loads it: process_tree, open_codex_ids and child_commands keep their /proc
# code and only hand over to their darwin_* twins on macOS.
DARWIN = sys.platform == "darwin"
_darwin_proc = None  # installer/darwin_proc.py once it is known to work
_darwin_problem = None  # why it cannot be used, once that is known
_warned = False


def proc_stat(pid: int) -> tuple[int, int] | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        fields = text[text.rfind(")") + 2 :].split()
        return int(fields[1]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def proc_cmdline(pid: int) -> list[str]:
    try:
        return [
            item.decode(errors="replace")
            for item in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if item
        ]
    except OSError:
        return []


def process_tree(root_pid: int) -> list[tuple[int, int, list[str]]]:
    if DARWIN:
        return darwin_process_tree(root_pid)
    children: dict[int, list[int]] = {}
    starts: dict[int, int] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        stat = proc_stat(pid)
        if stat is not None:
            ppid, start = stat
            children.setdefault(ppid, []).append(pid)
            starts[pid] = start

    result: list[tuple[int, int, list[str]]] = []
    queue = [root_pid]
    seen: set[int] = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        queue.extend(children.get(pid, []))
        argv = proc_cmdline(pid)
        if argv:
            result.append((pid, starts.get(pid, 0), argv))
    return result


def _import_darwin_proc():
    """installer/darwin_proc.py of this checkout.

    tmux runs this script from the checkout's tmux/ directory, through the
    compatibility link, which resolve() follows: the checkout is two levels
    up. It goes first on sys.path for the import, so that another package
    named installer (pypa's, in site-packages) never stands in.
    """
    # The checkout is installer-owned source: importing from it must not
    # leave __pycache__ there.
    sys.dont_write_bytecode = True
    root = str(Path(__file__).resolve().parent.parent)
    added = root not in sys.path
    if added:
        sys.path.insert(0, root)
    try:
        from installer import darwin_proc
    finally:
        if added:
            sys.path.remove(root)
    return darwin_proc


def darwin_backend(warn: bool = True):
    """installer/darwin_proc.py when it can inspect processes, else None.

    Without it nothing about the programs in a pane can be proven, so every
    pane command is cleared, as on any system without /proc, and the
    snapshot is still saved: layout, directories and pane contents are
    restored, programs are not. That is said once on stderr (the tmux or
    launchd log), and --self-test reports it.
    """
    global _darwin_proc, _darwin_problem
    if _darwin_proc is None and _darwin_problem is None:
        try:
            module = _import_darwin_proc()
            if module.available():
                _darwin_proc = module
            else:
                _darwin_problem = module.unavailable_reason() or "no reason given"
        except Exception as error:  # a broken checkout must not stop the save
            _darwin_problem = f"cannot load installer/darwin_proc.py: {error}"
    if _darwin_proc is None and warn:
        warn_unavailable(_darwin_problem)
    return _darwin_proc


def warn_unavailable(problem: str | None) -> None:
    """One line on stderr per run, however many panes there are."""
    global _warned
    if _warned:
        return
    _warned = True
    print(
        f"resurrect AI hook: cannot inspect processes ({problem}); "
        "pane commands that cannot be proven are cleared from this snapshot",
        file=sys.stderr,
    )


def darwin_query(function, *args):
    """``function(*args)`` of installer/darwin_proc.py.

    Its calls answer None for whatever they cannot tell and are written
    never to raise. Should one raise all the same (on a macOS it does not
    know), that is a None too, with the warning: the save goes on without
    the commands that cannot be proven, instead of failing and keeping
    the previous snapshot.
    """
    try:
        return function(*args)
    except Exception as error:
        name = getattr(function, "__name__", "a call")
        warn_unavailable(f"darwin_proc.{name} failed: {error}")
        return None


def darwin_process_tree(root_pid: int) -> list[tuple[int, int, list[str]]]:
    """process_tree on macOS: the same walk over libproc's process table.

    The start time is in microseconds since the epoch (only its order
    matters). argv is exact, empty arguments included: macOS ps prints them
    too (as a double space), which requoted_command relies on.
    """
    backend = darwin_backend()
    if backend is None:
        return []
    pids = darwin_query(backend.all_pids)
    if pids is None:
        warn_unavailable("the process table cannot be read")
        return []
    children: dict[int, list[int]] = {}
    starts: dict[int, int] = {}
    for pid in pids:
        info = darwin_query(backend.bsd_info, pid)
        if info is not None:
            children.setdefault(info.ppid, []).append(pid)
            starts[pid] = round(info.start * 1000000)

    result: list[tuple[int, int, list[str]]] = []
    queue = [root_pid]
    seen: set[int] = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        queue.extend(children.get(pid, []))
        argv = darwin_query(backend.argv, pid)
        if argv:
            result.append((pid, starts.get(pid, 0), argv))
    return result


def program_name(name: str) -> str:
    """A program name as program_kind compares it: unchanged on Linux. On
    macOS, whose file systems ignore case by default, "Claude" or "CODEX"
    runs the same program as claude or codex, so in lower case."""
    return name.lower() if DARWIN else name


def program_kind(argv: list[str]) -> str | None:
    if not argv:
        return None
    name = program_name(Path(argv[0]).name)
    return name if name in {"codex", "claude"} else None


def codex_resume_id(argv: list[str]) -> str | None:
    value_options = {
        "-c", "--config", "--enable", "--disable", "-i", "--image",
        "-m", "--model", "--local-provider", "-p", "--profile",
        "-s", "--sandbox", "-a", "--ask-for-approval", "-C", "--cd",
        "--add-dir", "--remote", "--remote-auth-token-env",
    }
    bool_options = {
        "--oss", "--full-auto", "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust", "--search", "--no-alt-screen",
        "--approve-for-me", "--worktree", "--strict-config",
        "--include-non-interactive", "-h", "--help", "-V", "--version",
        "--last", "--all",
    }
    index = 1
    found_resume = False
    session_id: str | None = None
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            break
        if arg in value_options:
            if index + 1 >= len(argv):
                return None
            index += 2
            continue
        if any(
            arg.startswith(option + "=")
            for option in value_options
            if option.startswith("--")
        ):
            index += 1
            continue
        if arg in bool_options:
            index += 1
            continue
        if arg.startswith("-"):
            return None
        if not found_resume:
            if arg != "resume":
                return None
            found_resume = True
        elif session_id is None:
            if not UUID_RE.fullmatch(arg):
                return None
            session_id = arg
        index += 1
    return session_id if found_resume else None


def claude_resume_id(argv: list[str]) -> str | None:
    found: set[str] = set()
    for index, arg in enumerate(argv[1:], start=1):
        if arg == "--":
            break
        if arg in {"--resume", "-r"} and index + 1 < len(argv):
            candidate = argv[index + 1]
            if UUID_RE.fullmatch(candidate):
                found.add(candidate)
        elif arg.startswith("--resume="):
            candidate = arg.split("=", 1)[1]
            if UUID_RE.fullmatch(candidate):
                found.add(candidate)
    return next(iter(found)) if len(found) == 1 else None


def codex_hooks_disabled(argv: list[str]) -> bool:
    for index, arg in enumerate(argv):
        if arg == "--":
            break
        if arg == "--disable=hooks":
            return True
        if arg == "--disable" and index + 1 < len(argv) and argv[index + 1] == "hooks":
            return True
    return False


def safe_claude_flags(argv: list[str]) -> list[str]:
    result: list[str] = []
    value_flags = {"--model", "--effort", "--name", "-n"}
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            break
        if arg in value_flags:
            value = argv[index + 1] if index + 1 < len(argv) else ""
            if SAFE_OPTION_VALUE_RE.fullmatch(value):
                result.extend((arg, value))
            index += 2
            continue
        index += 1
    return result


def open_codex_ids(pid: int) -> tuple[list[str], list[tuple[Path, str]]]:
    if DARWIN:
        return darwin_open_codex_ids(pid)
    locks: list[str] = []
    rollouts: list[tuple[Path, str]] = []
    try:
        fds = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return locks, rollouts
    session_root = str(Path.home() / ".codex/sessions") + "/"
    for fd in fds:
        try:
            raw_target = os.readlink(fd)
            target = Path(raw_target)
        except OSError:
            continue
        if (
            target.parent.name == "thread-writer-locks"
            and target.suffix == ".lock"
            and UUID_RE.fullmatch(target.stem)
        ):
            locks.append(target.stem)
        if raw_target.startswith(session_root):
            match = ROLLOUT_UUID_RE.search(raw_target)
            if match:
                rollouts.append((target, match.group(1)))
    return sorted(set(locks)), sorted(set(rollouts))


def codex_session_prefixes() -> tuple[str, ...]:
    """Where an open rollout must be for darwin_open_codex_ids: ~/.codex/
    sessions/ as HOME names it, resolved, and resolved under
    /System/Volumes/Data, the volume behind the firmlinked home folders,
    in case libproc names a file that way. In NFC, as darwin_open_codex_ids
    compares them: APFS takes every Unicode normalization of a name for
    the same name, so the kernel may spell it differently than HOME."""
    root = str(Path.home() / ".codex/sessions")
    real = os.path.realpath(root)
    prefixes = {root + "/", real + "/", "/System/Volumes/Data" + real + "/"}
    return tuple(sorted(unicodedata.normalize("NFC", prefix) for prefix in prefixes))


def darwin_open_codex_ids(pid: int) -> tuple[list[str], list[tuple[Path, str]]]:
    """open_codex_ids on macOS: the same checks, on libproc's names for the
    files pid holds open. When they cannot be read there is no live ID, and
    ai_resume_command's fail-closed rules apply."""
    locks: list[str] = []
    rollouts: list[tuple[Path, str]] = []
    backend = darwin_backend()
    files = darwin_query(backend.open_vnodes, pid) if backend is not None else None
    if files is None:
        return locks, rollouts
    session_roots = codex_session_prefixes()
    for entry in files:
        raw_target = entry.path
        if entry.nlink == 0 or not raw_target.startswith("/"):
            # Unlinked, or the kernel has no name for it. On Linux such a
            # file reads "<path> (deleted)", which neither check matches.
            continue
        target = Path(raw_target)
        if (
            target.parent.name == "thread-writer-locks"
            and target.suffix == ".lock"
            and UUID_RE.fullmatch(target.stem)
        ):
            locks.append(target.stem)
        if unicodedata.normalize("NFC", raw_target).startswith(session_roots):
            match = ROLLOUT_UUID_RE.search(raw_target)
            if match:
                rollouts.append((target, match.group(1)))
    return sorted(set(locks)), sorted(set(rollouts))


def session_metadata(
    conversation_id: str, rollouts: Iterable[tuple[Path, str]]
) -> dict[str, object] | None:
    paths = [path for path, candidate in rollouts if candidate == conversation_id]
    paths.extend(sorted((Path.home() / ".codex/sessions").glob(
        f"*/*/*/*{conversation_id}.jsonl"
    )))
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        try:
            with path.open("r", encoding="utf-8") as stream:
                line = stream.readline(SESSION_META_MAX_BYTES + 1)
        except (OSError, UnicodeDecodeError):
            continue
        if len(line) > SESSION_META_MAX_BYTES:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict) or item.get("type") != "session_meta":
            continue
        payload = item.get("payload")
        if isinstance(payload, dict) and payload.get("id") == conversation_id:
            return payload
    return None


def metadata_is_root(payload: dict[str, object]) -> bool:
    source = payload.get("source")
    if (
        payload.get("thread_source") == "subagent"
        or payload.get("parent_thread_id")
        or (isinstance(source, dict) and "subagent" in source)
    ):
        return False
    return payload.get("thread_source") == "user" or (
        isinstance(source, str) and source in {"cli", "vscode", "exec"}
    )


def claude_metadata_id(pid: int) -> str | None:
    try:
        with (Path.home() / f".claude/sessions/{pid}.json").open(
            "r", encoding="utf-8"
        ) as stream:
            raw = stream.read(SESSION_META_MAX_BYTES + 1)
        if len(raw) > SESSION_META_MAX_BYTES:
            return None
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    value = data.get("sessionId")
    return value if data.get("pid") == pid and isinstance(value, str) and UUID_RE.fullmatch(value) else None


# Directories of versioned installs: a launcher found there stops working at
# the next update (the native installer's versions/, Homebrew's Cellar and
# Caskroom).
VERSIONED_INSTALL_PARTS = ("/versions/", "/Cellar/", "/Caskroom/")
# Homebrew's bin directories (Apple silicon, Intel): a tmux server that
# launchd started may not have them on PATH.
DARWIN_CLAUDE_LAUNCHERS = ("/opt/homebrew/bin/claude", "/usr/local/bin/claude")
# resurrect.conf's @resurrect-processes pattern "/[^[:space:]]*/claude", as a
# whole path. \S also excludes every other Unicode space, so nothing it
# accepts can hold a character [[:space:]] matches in any locale.
RESTORABLE_LAUNCHER_RE = re.compile(r"/\S*/claude")


def claude_executable(exec_path: str | None = None) -> str | None:
    """Absolute Claude launcher to restore with (stable across updates).

    The live process argv[0] may point into a versioned install directory, so
    use the launcher found on PATH at save time, else ~/.local/bin/claude.
    resurrect.conf's @resurrect-processes matches any absolute path ending in
    /claude, so both forms are restored.

    On macOS (darwin_claude_executable) the launcher the live process was
    started with comes first.
    """
    if DARWIN:
        return darwin_claude_executable(exec_path)
    found = shutil.which("claude")
    if found and os.path.isabs(found):
        return found
    return str(Path.home() / ".local/bin/claude")


def darwin_claude_executable(exec_path: str | None) -> str | None:
    """claude_executable on macOS.

    ``exec_path`` is the path the live process was started with (execve's,
    as the shell found it on PATH: ~/.local/bin/claude or
    /opt/homebrew/bin/claude, not the versioned file such a symlink points
    to). It comes first because the tmux server that runs this hook may
    have been started by launchd, with a PATH that lacks the launcher. Then
    PATH, ~/.local/bin/claude and Homebrew's bin directories. A candidate
    counts only when it is an executable file and resurrect.conf's
    "/[^[:space:]]*/claude" matches it; without one this is None and the
    pane stays unresolved (cleared).
    """
    candidates: list[str] = []
    if exec_path and not any(part in exec_path for part in VERSIONED_INSTALL_PARTS):
        candidates.append(exec_path)
    found = shutil.which("claude")
    if found:
        candidates.append(found)
    candidates.append(str(Path.home() / ".local/bin/claude"))
    candidates.extend(DARWIN_CLAUDE_LAUNCHERS)
    for candidate in candidates:
        if (
            restorable_launcher(candidate)
            and os.path.isfile(candidate)
            and os.access(candidate, os.X_OK)
        ):
            return candidate
    return None


def restorable_launcher(path: str) -> bool:
    """An absolute, printable path without whitespace that ends in /claude,
    which is what resurrect.conf's @resurrect-processes restores."""
    return bool(RESTORABLE_LAUNCHER_RE.fullmatch(path)) and path.isprintable()


def launcher_path(pid: int) -> str | None:
    """The path pid was started with, on macOS; None on Linux, where
    claude_executable does not use it."""
    if not DARWIN:
        return None
    backend = darwin_backend()
    return darwin_query(backend.exec_path, pid) if backend is not None else None


def ai_resume_command(pane_pid: int) -> list[str] | None:
    candidates = [
        (pid, start, argv, program_kind(argv))
        for pid, start, argv in process_tree(pane_pid)
        if program_kind(argv) is not None
    ]
    if not candidates:
        return None
    kinds = {kind for _, _, _, kind in candidates}
    if len(kinds) != 1:
        return []
    kind = next(iter(kinds))
    candidates.sort(key=lambda item: (item[1], item[0]))

    if kind == "claude":
        observed = {
            value
            for pid, _, argv, _ in candidates
            for value in (claude_resume_id(argv), claude_metadata_id(pid))
            if value is not None
        }
        if len(observed) != 1:
            return []
        conversation_id = next(iter(observed))
        pid, argv = next(
            (pid, argv) for pid, _, argv, _ in candidates
            if claude_resume_id(argv) == conversation_id
            or claude_metadata_id(pid) == conversation_id
        )
        launcher = claude_executable(launcher_path(pid))
        if launcher is None:
            return []
        return [launcher, *safe_claude_flags(argv), "--resume", conversation_id]

    explicit = {
        value
        for _, _, argv, _ in candidates
        for value in (codex_resume_id(argv),)
        if value is not None
    }
    if len(explicit) > 1:
        return []
    # The live thread (writer lock / open rollout) wins over the command line:
    # /new or /resume inside the TUI switches threads without changing argv,
    # so a "codex resume <id>" argv only names the thread the process began
    # with.
    locks: set[str] = set()
    rollouts: set[tuple[Path, str]] = set()
    for pid, _, _, _ in candidates:
        candidate_locks, candidate_rollouts = open_codex_ids(pid)
        locks.update(candidate_locks)
        rollouts.update(candidate_rollouts)
    live_ids = locks | {item[1] for item in rollouts}
    root_ids = {
        value
        for value in live_ids
        if (payload := session_metadata(value, rollouts)) is not None
        and metadata_is_root(payload)
    }
    if len(root_ids) == 1:
        conversation_id = next(iter(root_ids))
    elif explicit and not root_ids and live_ids <= explicit:
        # No verified root: the argv ID stands only when no live evidence
        # contradicts it (none at all, or only that same ID).
        conversation_id = next(iter(explicit))
    else:
        return []

    source_argv = next(
        (argv for _, _, argv, _ in candidates if codex_resume_id(argv) == conversation_id),
        candidates[0][2],
    )
    command = ["codex", "resume", conversation_id]
    if codex_hooks_disabled(source_argv):
        command.extend(("--disable", "hooks"))
    return command


def child_commands(pane_pid: int) -> list[list[str]]:
    """argv of every direct child of the pane process (what resurrect's
    default "ps" save strategy records as the pane command)."""
    if DARWIN:
        return darwin_child_commands(pane_pid)
    result: list[list[str]] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return result
    for entry in entries:
        if not entry.isdigit():
            continue
        stat = proc_stat(int(entry))
        if stat is not None and stat[0] == pane_pid:
            argv = proc_cmdline(int(entry))
            if argv:
                result.append(argv)
    return result


def darwin_child_commands(pane_pid: int) -> list[list[str]]:
    """child_commands on macOS, with each child's exact argv."""
    backend = darwin_backend()
    if backend is None:
        return []
    pids = darwin_query(backend.children, pane_pid)
    if pids is None:
        warn_unavailable("the process table cannot be read")
        return []
    result: list[list[str]] = []
    for pid in pids:
        argv = darwin_query(backend.argv, pid)
        if argv:
            result.append(argv)
    return result


def requoted_command(saved: str, children: Iterable[list[str]]) -> str:
    """Shell-quoted form of the saved pane command, or "" when unproven.

    resurrect's "ps" strategy stores argv joined by spaces with no quoting,
    and a restore types that text into the pane's shell.  Rebuild it with
    shlex.join from the live argv it came from; when no live child matches
    the saved text exactly, or an argument cannot be typed back safely
    (control characters, undecodable bytes), keep nothing.
    """
    if not saved:
        return ""
    matches = {
        tuple(argv) for argv in children
        if " ".join(argv) == saved
    }
    if len(matches) != 1:
        return ""
    argv = list(next(iter(matches)))
    if any(
        not arg.isprintable() or "\ufffd" in arg
        for arg in argv
    ):
        return ""
    return shlex.join(argv)


def live_panes() -> dict[tuple[str, str, str], int]:
    pane_format = "\t".join((
        "#{session_name}", "#{window_index}", "#{pane_index}", "#{pane_pid}",
    ))
    completed = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", pane_format],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    result: dict[tuple[str, str, str], int] = {}
    for line in completed.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 4:
            raise RuntimeError("unexpected tmux list-panes row")
        key = tuple(fields[:3])
        if key in result:
            raise RuntimeError(f"duplicate live pane key: {key!r}")
        result[key] = int(fields[3])
    return result


def enrich_state(
    lines: Iterable[str],
    panes: dict[tuple[str, str, str], int],
    identity: Callable[[int], list[str] | None] | None = None,
    commands: Callable[[int], list[list[str]]] | None = None,
) -> list[str]:
    identity = identity or ai_resume_command
    commands = commands or child_commands
    result: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for line in lines:
        ending = "\n" if line.endswith("\n") else ""
        body = line[:-1] if ending else line
        fields = body.split("\t")
        if not fields or fields[0] != "pane":
            result.append(line)
            continue
        if len(fields) != 11:
            raise RuntimeError("unexpected tmux-resurrect pane row")
        key = (fields[1], fields[2], fields[5])
        if key in seen:
            raise RuntimeError(f"duplicate snapshot pane key: {key!r}")
        seen.add(key)
        if key not in panes:
            raise RuntimeError(f"snapshot pane is not live: {key!r}")
        command = identity(panes[key])
        if command is not None:
            fields[10] = ":" + (shlex.join(command) if command else "")
        elif program_name(fields[9]) in {"codex", "claude"} or saved_command_is_ai(fields[10]):
            fields[10] = ":"
        else:
            saved = fields[10][1:] if fields[10].startswith(":") else fields[10]
            fields[10] = ":" + requoted_command(saved, commands(panes[key]))
        result.append("\t".join(fields) + ending)
    return result


def saved_command_is_ai(full_command: str) -> bool:
    command = full_command[1:] if full_command.startswith(":") else full_command
    return bool(re.search(
        r"(?<![A-Za-z0-9_.-])(?:[^\s;|&()'\"]*/)?(?:codex|claude)"
        r"(?=[\s;|&()'\"]|$)",
        command,
        # program_kind's case rule: macOS runs CODEX as codex.
        re.IGNORECASE if DARWIN else 0,
    ))


def full_fsync(fd: int) -> None:
    """macOS: fsync() leaves the data in the drive's write cache, so a power
    loss right after a save could leave the snapshot that 'last' points to
    empty or cut short. F_FULLFSYNC flushes that cache as well; a file
    system that refuses it (some network mounts) keeps what fsync gave."""
    import fcntl

    command = getattr(fcntl, "F_FULLFSYNC", None)
    if command is None:
        return
    try:
        fcntl.fcntl(fd, command)
    except OSError:
        pass


def atomic_write(path: Path, content: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            if DARWIN:
                full_fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run(state_path: Path) -> None:
    marker_value = os.environ.get("TMUX_RESURRECT_HOOK_OK")
    marker = Path(marker_value) if marker_value else Path(str(state_path) + ".ai-ok")
    marker.unlink(missing_ok=True)
    lines = state_path.read_text(encoding="utf-8").splitlines(keepends=True)
    enriched = enrich_state(lines, live_panes())
    atomic_write(state_path, "".join(enriched))
    atomic_write(marker, state_path.name + "\n")


# --self-test starts a child with these arguments (an empty one, one with a
# space, one in Hangul) and holds a file named like Codex's
# thread-writer-locks/<uuid>.lock in a directory with a space and Hangul in
# its name: exact argv and exact file names are what the hook depends on.
SELF_TEST_ARGS = ("", "two words", "\ud55c\uae00")
SELF_TEST_ID = "00000000-0000-4000-8000-000000000000"
SELF_TEST_CHILD = (
    "import sys; sys.stdout.write('ready\\n'); sys.stdout.flush(); sys.stdin.read()"
)


def self_test_problem() -> str | None:
    """Why process inspection does not work here, or None when it does.

    It drives the hook's own functions: child_commands and process_tree
    must show a child's exact argv, and open_codex_ids a lock file this
    process holds open.
    """
    if DARWIN:
        if darwin_backend(warn=False) is None:
            return _darwin_problem
    elif not os.path.isdir("/proc/self/fd"):
        return "/proc is not available"
    arguments = ["-c", SELF_TEST_CHILD, *SELF_TEST_ARGS]
    # The /proc reader drops empty arguments; the macOS argv keeps them.
    expected = arguments if DARWIN else [arg for arg in arguments if arg]
    child = subprocess.Popen(
        [sys.executable, *arguments], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    try:
        ready, _, _ = select.select([child.stdout], [], [], 10)
        if not ready or child.stdout.readline() != b"ready\n":
            return "the test child process did not start"
        # argv[0] is not compared: a framework Python (python.org, Homebrew,
        # Xcode) re-executes itself as Python.app, which replaces it.
        if not any(argv[1:] == expected for argv in child_commands(os.getpid())):
            return "the exact argv of a child process cannot be read"
        if not any(
            pid == child.pid and argv[1:] == expected
            for pid, _, argv in process_tree(os.getpid())
        ):
            return "the process tree does not show a child process"
    finally:
        child.stdin.close()  # end of input: the child exits
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        child.stdout.close()
    with tempfile.TemporaryDirectory(prefix="resurrect-self-test.") as directory:
        lock = Path(directory, "two words \ud55c\uae00", "thread-writer-locks",
                    f"{SELF_TEST_ID}.lock")
        lock.parent.mkdir(parents=True)
        with lock.open("w"):
            locks, _ = open_codex_ids(os.getpid())
        if SELF_TEST_ID not in locks:
            return "the files a process holds open cannot be read"
    return None


def self_test() -> int:
    """--self-test: 0 when process inspection works here, else 1 with the
    reason on stderr. Without it every pane command would be cleared, so
    install, repair and status run this to report that instead of letting
    saves look complete."""
    try:
        problem = self_test_problem()
    except Exception as error:  # an unexpected failure is a failed test
        problem = f"unexpected error: {error}"
    if problem:
        print(f"resurrect AI hook self-test failed on {sys.platform}: {problem}",
              file=sys.stderr)
        return 1
    source = "libproc and sysctl" if DARWIN else "/proc"
    print(f"resurrect AI hook self-test: process inspection works ({source})")
    return 0


def main(argv: list[str]) -> int:
    if argv[1:] == ["--self-test"]:
        return self_test()
    if len(argv) != 2:
        print(f"usage: {argv[0]} STATE_FILE", file=sys.stderr)
        return 2
    try:
        run(Path(argv[1]))
    except Exception as error:
        print(f"resurrect AI enrichment failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
