#!/usr/bin/env python3
"""Enrich a tmux-resurrect snapshot with proven Codex/Claude resume IDs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Iterable


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


def program_kind(argv: list[str]) -> str | None:
    if not argv:
        return None
    name = Path(argv[0]).name
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


def claude_executable() -> str:
    """Absolute Claude launcher to restore with (stable across updates).

    The live process argv[0] may point into a versioned install directory, so
    use the launcher found on PATH at save time, else ~/.local/bin/claude.
    resurrect.conf's @resurrect-processes matches any absolute path ending in
    /claude, so both forms are restored.
    """
    found = shutil.which("claude")
    if found and os.path.isabs(found):
        return found
    return str(Path.home() / ".local/bin/claude")


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
        argv = next(
            argv for pid, _, argv, _ in candidates
            if claude_resume_id(argv) == conversation_id
            or claude_metadata_id(pid) == conversation_id
        )
        return [claude_executable(), *safe_claude_flags(argv), "--resume", conversation_id]

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
        elif fields[9] in {"codex", "claude"} or saved_command_is_ai(fields[10]):
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
    ))


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


def main(argv: list[str]) -> int:
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
