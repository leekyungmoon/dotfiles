"""Phase results and the phases that run after the transaction.

Every phase reports a :class:`PhaseResult`. A phase that did not run is
``SKIPPED``, never ``PASS``. Post-install, smoke checks and the login shell
change all go through the injected runner so tests never touch the real
system manager, shell database or tmux server.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path

from installer import ui

PASS = "PASS"
FAIL = "FAIL"
SKIPPED = "SKIPPED"
PENDING_GUI = "PENDING_GUI"
RELOGIN_REQUIRED = "RELOGIN_REQUIRED"
AUTH_REQUIRED = "AUTH_REQUIRED"

# Reported by installer/gui.py on Ubuntu 22.04; permanent, never retried.
REMAPPER_UNSUPPORTED_REASON = "input-remapper-1.4-cannot-express-intent"
STATUSES = (PASS, FAIL, SKIPPED, PENDING_GUI, RELOGIN_REQUIRED, AUTH_REQUIRED)

# Worst first; the overall status of a run is the worst phase status.
_SEVERITY = (FAIL, AUTH_REQUIRED, RELOGIN_REQUIRED, PENDING_GUI, SKIPPED, PASS)

STATUS_SCHEMA = 1
PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

GIT_TIMEOUT = 300.0
SMOKE_TIMEOUT = 300.0
PLUGIN_TIMEOUT = 900.0
AUTH_TIMEOUT = 30.0

# Enabled by the manifest's timers.target.wants link; started after reload.
AUTOSAVE_TIMER = "tmux-resurrect-autosave.timer"

# sun_path is 108 bytes including the NUL; stay well below it.
SOCKET_PATH_MAX = 100


@dataclasses.dataclass
class PhaseResult:
    phase: str
    status: str
    reasons: list[str] = dataclasses.field(default_factory=list)
    details: dict = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown phase status {self.status!r}")

    def to_dict(self) -> dict:
        return {"phase": self.phase, "status": self.status,
                "reasons": list(self.reasons), "details": jsonable(self.details)}

    @classmethod
    def coerce(cls, value, phase: str) -> "PhaseResult":
        """Accept a PhaseResult, a compatible object or a contract dict."""

        if isinstance(value, cls):
            return value
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        elif dataclasses.is_dataclass(value):
            value = dataclasses.asdict(value)
        if not isinstance(value, dict):
            return cls(phase, FAIL, [f"phase returned {type(value).__name__}"])
        status = value.get("status")
        if status not in STATUSES:
            return cls(phase, FAIL, [f"phase returned unknown status {status!r}"])
        return cls(str(value.get("phase") or phase), status,
                   [str(r) for r in value.get("reasons") or []],
                   dict(value.get("details") or {}))


def jsonable(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "__dict__"):
        return jsonable(vars(value))
    return str(value)


def worst(statuses) -> str:
    present = set(statuses)
    for status in _SEVERITY:
        if status in present:
            return status
    return PASS


def overall_status(results: list[PhaseResult]) -> str:
    return worst(r.status for r in results)


def exit_code(results: list[PhaseResult]) -> int:
    return 1 if any(r.status == FAIL for r in results) else 0


def _tail(data: bytes | None, limit: int = 400) -> str:
    if not data:
        return ""
    text = data.decode("utf-8", "replace").strip()
    return text[-limit:]


def _out(completed) -> str:
    data = completed.stdout or b""
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    return data.strip()


def child_env(target, base_env: dict[str, str] | None) -> dict[str, str]:
    """Environment for subprocesses that must behave like the target's login."""

    base = dict(base_env or {})
    for key in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR"):
        base.pop(key, None)
    local_bin = str(target.home / ".local" / "bin")
    path = base.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    if local_bin not in path.split(":"):
        path = f"{local_bin}:{path}"
    base.update({
        "HOME": str(target.home),
        "USER": target.username,
        "LOGNAME": target.username,
        "PATH": path,
        "XDG_DATA_HOME": str(target.data_home),
        "XDG_STATE_HOME": str(target.state_home),
        "XDG_CONFIG_HOME": str(target.config_home),
        "XDG_CACHE_HOME": str(target.cache_home),
    })
    base.setdefault("TERM", "xterm-256color")
    return base


# --- systemd ---------------------------------------------------------------

def systemd_user_supported(runner) -> bool:
    """Whether this machine boots with systemd and so has user managers."""

    return runner.which("systemctl") is not None and Path("/run/systemd/system").is_dir()


def systemd_user_reachable(runner, env: dict[str, str] | None = None) -> bool:
    if runner.which("systemctl") is None:
        return False
    try:
        completed = runner.run(["systemctl", "--user", "show-environment"],
                               timeout=15, check=False, env=env, read_only=True)
    except Exception:
        return False
    return completed.returncode == 0


# --- tmux plugins -----------------------------------------------------------

def load_tmux_plugins(repo_root: Path) -> list[dict] | None:
    path = repo_root / "manifests" / "tmux-plugins.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError("tmux-plugins.json: unsupported schema")
    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        raise ValueError("tmux-plugins.json: plugins must be a list")
    seen = set()
    for plugin in plugins:
        name = plugin.get("name") if isinstance(plugin, dict) else None
        if not isinstance(name, str) or not PLUGIN_NAME_RE.match(name) or name in seen:
            raise ValueError(f"tmux-plugins.json: bad or duplicate name {name!r}")
        if not isinstance(plugin.get("url"), str) or not plugin["url"]:
            raise ValueError(f"tmux-plugins.json: {name} has no url")
        if not isinstance(plugin.get("commit"), str) or not SHA_RE.match(plugin["commit"]):
            raise ValueError(f"tmux-plugins.json: {name} commit must be a full sha")
        seen.add(name)
    return plugins


def _git_head(runner, repo: Path) -> str | None:
    if not (repo / ".git").exists():
        return None
    completed = runner.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                           timeout=30, check=False, read_only=True)
    return _out(completed) if completed.returncode == 0 else None


def install_tmux_plugins(target, runner, repo_root: Path, run_id: str) -> tuple[str, list[str], dict]:
    plugins = load_tmux_plugins(repo_root)
    if plugins is None:
        return SKIPPED, ["tmux-plugins-manifest-absent"], {}
    root = target.data_home / "tmux" / "plugins"
    root.mkdir(parents=True, exist_ok=True)
    resurrect = target.data_home / "tmux" / "resurrect"
    resurrect.mkdir(parents=True, exist_ok=True)
    os.chmod(resurrect, 0o700)
    details: dict[str, str] = {}
    for plugin in plugins:
        name, url, commit = plugin["name"], plugin["url"], plugin["commit"]
        dest = root / name
        if not dest.is_symlink() and _git_head(runner, dest) == commit:
            details[name] = "at-pin"
            continue
        tmp = root / f".{name}.{run_id}.tmp"
        if os.path.lexists(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        try:
            runner.run(["git", "clone", "--quiet", "--", url, str(tmp)],
                       timeout=GIT_TIMEOUT)
            runner.run(["git", "-C", str(tmp), "checkout", "--quiet", "--detach",
                        commit], timeout=60)
            if (tmp / ".gitmodules").is_file():
                runner.run(["git", "-C", str(tmp), "submodule", "update", "--init",
                            "--recursive", "--quiet"], timeout=GIT_TIMEOUT)
            if _git_head(runner, tmp) != commit:
                raise RuntimeError(f"{name} is not at {commit}")
            if os.path.lexists(dest):
                backup = target.state_root / "backups" / "tmux-plugins" / run_id / name
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.chmod(backup.parent, 0o700)
                shutil.move(str(dest), str(backup))
                details[name] = "replaced"
            else:
                details[name] = "installed"
            os.replace(tmp, dest)
        except Exception as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            return FAIL, [f"tmux-plugin-{name}: {exc}"], details
    return PASS, [], details


def _plugin_step(runner, name: str, argv_fn, env, cwd, *, skip: bool,
                 flag: str) -> tuple[str, str]:
    """Run one plugin prefill/update command; ``(status, detail)``."""

    if skip:
        return SKIPPED, flag
    binary = runner.which(name)
    if binary is None:
        return SKIPPED, f"{name}-not-installed"
    completed = runner.run(argv_fn(binary), timeout=PLUGIN_TIMEOUT, check=False,
                           env=env, input=b"", cwd=cwd)
    if completed.returncode != 0:
        return FAIL, (f"{name} plugins failed ({completed.returncode}): "
                      + _tail(completed.stderr))
    return PASS, "updated"


# Upstream's zsh (antidote) and neovim (lazy.nvim) plugin updates.
ZSH_PLUGIN_SCRIPT = (
    "DOTFILES_UPDATE=1 __p9k_instant_prompt_disabled=1 source ${HOME}/.zshrc; "
    "if ! whence antidote >/dev/null; then "
    "echo 'antidote not found; check the zsh/antidote submodule' >&2; exit 1; fi; "
    "antidote update && antidote reset"
)
NVIM_PLUGIN_ARGS = [
    "--headless",
    "-c", "lua require('lazy').update { wait = true }",
    "-c", "lua require('config.plugins').report_errors { exit = true }",
]


def plugins_step(target, runner, *, env, skip_zplug: bool, skip_vimplug: bool):
    statuses, reasons, details = [], [], {}
    steps = (
        ("zsh", lambda zsh: [zsh, "-c", ZSH_PLUGIN_SCRIPT], skip_zplug, "--skip-zplug"),
        ("nvim", lambda nvim: [nvim, *NVIM_PLUGIN_ARGS], skip_vimplug, "--skip-vimplug"),
    )
    for name, argv_fn, skip, flag in steps:
        label = "zsh_plugins" if name == "zsh" else "vim_plugins"
        try:
            status, detail = _plugin_step(runner, name, argv_fn, env, target.home,
                                          skip=skip, flag=flag)
        except Exception as exc:
            status, detail = FAIL, f"{name} plugins: {exc}"
        details[label] = detail if status != FAIL else "failed"
        if status == FAIL:
            statuses.append(FAIL)
            reasons.append(detail)
    return statuses, reasons, details


def start_autosave_timer(target, runner, env) -> tuple[str, list[str], str]:
    """Start the autosave timer now; daemon-reload does not start new wants.

    Enabling stays the manifest's ``timers.target.wants`` link, so the timer
    also starts with every later user manager. ``start`` is a no-op for a
    timer that is already active.
    """

    wants = target.config_home / "systemd" / "user" / "timers.target.wants" / AUTOSAVE_TIMER
    if not os.path.lexists(wants):
        return PASS, [], "not-enabled"
    completed = runner.run(["systemctl", "--user", "start", AUTOSAVE_TIMER],
                           timeout=60, check=False, env=env)
    if completed.returncode != 0:
        return FAIL, [f"systemctl --user start {AUTOSAVE_TIMER} failed: "
                      + _tail(completed.stderr)], "start-failed"
    return PASS, [], "started"


# --- the running tmux server ---------------------------------------------------

# Global user options that hold a server's runtime state rather than its
# configuration; converging keeps them. tmux/resurrect-save only lets a save
# replace "last" on a server marked restored, and continuum records when it
# last saved. A fresh server sets these at run time, not from the config.
RUNTIME_USER_OPTIONS = frozenset({"@tmux-restore-complete",
                                  "@continuum-save-last-timestamp"})
# tmux-continuum (manifests/tmux-plugins.json) auto-restores only when it is
# loaded into a server younger than @continuum-restore-max-delay seconds
# (default 10; continuum.tmux just_started_tmux_server). Converging sources
# the new config only into an older server, with this margin.
CONTINUUM_RESTORE_MAX_DELAY = 10
CONTINUUM_MARGIN = 2.0
TMUX_CONVERGE_TIMEOUT = 300.0

# (show-options flags, set-option flags) per global option scope.
_OPTION_SCOPES = (("-g", "-g"), ("-gw", "-gw"), ("-s", "-s"))
_HOOK_SCOPES = ("-g", "-gw")
_OPTION_NAME_RE = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9_.-]*$")
_PLAIN_WORD_RE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
_PTS_RE = re.compile(r"^/dev/pts/[0-9]+$")
_PANE_FORMAT = "\t".join(("#{pane_id}", "#{pane_pid}", "#{pane_dead}", "#{pane_in_mode}",
                          "#{alternate_on}", "#{pane_height}", "#{pane_tty}",
                          "#{pane_current_command}", "#{window_index}",
                          "#{pane_index}", "#{session_name}"))  # the name last: any text
_PANE_FIELDS = 11
_sleep = time.sleep  # patched by tests


@dataclasses.dataclass
class TmuxDefaults:
    """What a server started with ``-f /dev/null`` has: the tmux defaults."""

    keys: list[str]                         # list-keys: re-sourceable bind-key lines
    notes: list[tuple[str, str, str]]       # (table, key, note) of the default keys
    options: dict[str, dict[str, tuple]]    # show-options flag -> name -> lines
    hooks: dict[str, dict[str, tuple]]      # show-hooks flag -> name -> lines


def _lines(completed) -> list[str]:
    return [line for line in _text(completed.stdout).splitlines() if line.strip()]


def _option_table(lines: list[str]) -> dict[str, tuple]:
    """``show-options``/``show-hooks`` lines by option name (arrays grouped)."""

    table: dict[str, list[str]] = {}
    for line in lines:
        name = line.split(" ", 1)[0].split("[", 1)[0]
        table.setdefault(name, []).append(line)
    return {name: tuple(values) for name, values in table.items()}


def _key_tables(keys: list[str]) -> list[str]:
    tables = set()
    for line in keys:
        words = line.split()
        for i, word in enumerate(words[:4]):
            if word == "-T" and i + 1 < len(words):
                tables.add(words[i + 1])
    return sorted(tables)


def tmux_quote(text: str) -> str:
    """One tmux config token holding ``text`` literally."""

    if "'" not in text:
        return f"'{text}'"
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def capture_tmux_defaults(binary: str, runner, env: dict[str, str],
                          cwd: Path | None = None) -> TmuxDefaults:
    """Start a throwaway ``-f /dev/null`` server, read its defaults, kill it.

    The server has its own short ``-L`` name inside a private TMUX_TMPDIR, so
    nothing else can be addressed; exactly that server is killed afterwards.
    """

    name = f"pdfd{os.getpid()}{secrets.token_hex(2)}"
    socket_dir = _short_socket_dir(name, env)
    if socket_dir is None:
        raise RuntimeError("no temporary directory is short enough for a tmux socket "
                           f"(limit {SOCKET_PATH_MAX} characters); set TMPDIR=/tmp")
    penv = dict(env)
    for key in ("TMUX", "TMUX_PANE"):
        penv.pop(key, None)
    penv["TMUX_TMPDIR"] = socket_dir
    base = [binary, "-L", name]

    def query(*args, required=True):
        done = runner.run([*base, *args], timeout=30, check=False, env=penv,
                          read_only=True)
        if done.returncode != 0:
            if not required:
                return None
            raise RuntimeError(f"tmux {' '.join(args)} failed: {_tail(done.stderr)}")
        return _lines(done)

    try:
        # Two arguments: tmux runs sleep itself, no shell and no rc files.
        started = runner.run([*base, "-f", "/dev/null", "new-session", "-d", "-s",
                              "defaults", "sleep", "600"], timeout=30, check=False,
                             env=penv, cwd=cwd, read_only=True)
        if started.returncode != 0:
            raise RuntimeError("could not start a tmux server to read its defaults: "
                               + _tail(started.stderr))
        keys = query("list-keys")
        notes = []
        for table in _key_tables(keys):
            for line in query("list-keys", "-N", "-P", "", "-T", table) or []:
                parts = line.split(None, 1)
                if len(parts) == 2:
                    notes.append((table, parts[0], parts[1].strip()))
        options = {flag: _option_table(query("show-options", flag))
                   for flag, _ in _OPTION_SCOPES}
        hooks = {}
        for flag in _HOOK_SCOPES:
            listed = query("show-hooks", flag, required=False)
            if listed is not None:
                hooks[flag] = _option_table(listed)
        return TmuxDefaults(keys, notes, options, hooks)
    finally:
        try:
            runner.run([*base, "kill-server"], timeout=30, check=False, env=penv,
                       read_only=True)
        finally:
            shutil.rmtree(socket_dir, ignore_errors=True)


def _server_state(tmux, runner, tenv) -> tuple[list[str], dict, dict]:
    def query(*args):
        done = runner.run([tmux, *args], timeout=30, check=False, env=tenv,
                          read_only=True)
        return _lines(done) if done.returncode == 0 else None

    keys = query("list-keys") or []
    options = {flag: _option_table(query("show-options", flag) or [])
               for flag, _ in _OPTION_SCOPES}
    hooks = {flag: _option_table(query("show-hooks", flag) or []) for flag in _HOOK_SCOPES}
    return keys, options, hooks


def _reset_lines(defaults: TmuxDefaults, keys: list[str], options: dict,
                 hooks: dict) -> list[str]:
    """tmux commands that put a server's keys, options and hooks at the defaults.

    An option is set straight to the fresh server's value, not unset: tmux
    takes default-shell, editor, status-keys and mode-keys from the
    environment when it starts, so ``-u`` would give the built-in values
    instead. Array options are unset (their defaults do not depend on the
    environment). Options that already have the fresh value are not touched.
    """

    lines = [f"unbind-key -a -T {tmux_quote(table)}" for table in _key_tables(keys)]
    lines += defaults.keys
    lines += [f"bind-key -N {tmux_quote(note)} -T {tmux_quote(table)} {tmux_quote(key)}"
              for table, key, note in defaults.notes]
    for show_flag, set_flag in _OPTION_SCOPES:
        fresh = defaults.options.get(show_flag, {})
        for name, value in sorted(options.get(show_flag, {}).items()):
            if not _OPTION_NAME_RE.match(name) or value == fresh.get(name):
                continue
            if name.startswith("@"):
                if name not in RUNTIME_USER_OPTIONS:
                    lines.append(f"set-option {set_flag}u {name}")
            elif name not in fresh:  # an option the defaults' tmux does not know
                continue
            elif any("[" in line.split(" ", 1)[0] for line in value + fresh[name]):
                lines.append(f"set-option {set_flag}u {name}")
            elif " " in fresh[name][0]:
                # show-options escapes the value so that it parses back.
                lines.append(f"set-option {set_flag} {fresh[name][0]}")
            else:
                lines.append(f"set-option {set_flag}u {name}")
    for flag in _HOOK_SCOPES:
        if flag not in defaults.hooks:
            continue
        fresh = defaults.hooks[flag]
        for name, value in sorted(hooks.get(flag, {}).items()):
            if _OPTION_NAME_RE.match(name) and value != fresh.get(name, (name,)):
                lines.append(f"set-hook {flag}u {name}")
    return lines


def _source_lines(tmux, runner, tenv, workdir: Path, label: str,
                  lines: list[str]) -> str | None:
    """Source ``lines`` into the server; the error text, or None.

    Any stderr output is an error, whatever the exit status: tmux 3.4 exits
    0 when a command error comes before a later run-shell or if-shell of the
    same file and only prints the error.
    """

    if not lines:
        return None
    path = workdir / f"{label}.conf"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    done = runner.run([tmux, "source-file", str(path)], timeout=TMUX_CONVERGE_TIMEOUT,
                      check=False, env=tenv)
    errors = _tail(done.stderr)
    if done.returncode != 0:
        return errors or _tail(done.stdout) or f"exit status {done.returncode}"
    return errors or None


def default_config_files(target) -> list[Path]:
    """The files a tmux server started without -f loads, as tmux resolves them."""

    out, seen = [], set()
    for path in (Path("/etc/tmux.conf"), target.home / ".tmux.conf",
                 target.config_home / "tmux" / "tmux.conf",
                 target.home / ".config" / "tmux" / "tmux.conf"):
        if path.is_file() and os.path.realpath(path) not in seen:
            seen.add(os.path.realpath(path))
            out.append(path)
    return out


def _config_lines(paths: list[Path]) -> list[str]:
    """The config files' text, to follow the reset in the same file.

    Not ``source-file``: tmux reads a sourced file asynchronously and runs its
    event loop meanwhile, so a reset default the config overrides (for
    example automatic-rename, which renames windows at once) would take
    effect for a moment. In one file the whole reset and the config up to its
    first run-shell or if-shell run in one go. A parse error anywhere in the
    file means nothing of it runs. (#{config_files} is not changed by either.)
    """

    lines = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            lines.append(f"source-file {tmux_quote(str(path))}")
            continue
        lines += [f"# {path}", *text.splitlines(), ""]
    return lines


def _proc_stat(pid: int) -> tuple[str, int, int, int] | None:
    """``(comm, ppid, pgrp, tpgid)`` from /proc/<pid>/stat."""

    fields = _proc_stat_fields(pid)
    if fields is None:
        return None
    comm, rest = fields
    try:
        return comm, int(rest[1]), int(rest[2]), int(rest[5])
    except (ValueError, IndexError):
        return None


def _proc_stat_fields(pid: int) -> tuple[str, list[str]] | None:
    """``(comm, fields 3...)`` of /proc/<pid>/stat (``fields[0]`` is field 3)."""

    try:
        data = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return data[data.index("(") + 1:data.rindex(")")], data[data.rindex(")") + 2:].split()
    except (OSError, ValueError):
        return None


def _has_children(pid: int) -> bool:
    for entry in os.listdir("/proc"):
        if entry.isdigit() and entry != str(pid):
            stat = _proc_stat(int(entry))
            if stat is not None and stat[1] == pid:
                return True
    return False


# --- the reload hook of zsh/zsh.d/dotfiles-reload.zsh ---------------------------
#
# Every interactive zsh started with this repository's config keeps one file
# descriptor open, read-only and close-on-exec, on the static file
# {state}/personal-dotfiles/shell-hook. Such a "hooked" shell reloads itself
# (at Enter on its primary prompt, or before its next prompt) when the
# generation changes, keeping its exported environment; nothing outside
# ever restarts it. A process is hooked iff one of its /proc/<pid>/fd links
# resolves to that file. There is no per-prompt state to go stale: the
# descriptor lives exactly as long as the zsh process image (it closes on
# 'exec bash' and on exit, and a reused pid never has it), and it does not
# depend on /run/user/<uid> or a login session.

SHELL_HOOK_FILE = "shell-hook"
# The last two components of a hook file wherever its state directory is
# (another XDG_STATE_HOME, a moved home, a bind mount): also hooked, so such
# a shell is left alone rather than taken for one without the hook.
_HOOK_SUFFIX = "/personal-dotfiles/" + SHELL_HOOK_FILE
_DELETED = " (deleted)"
RESPAWN_SHELL = "zsh"  # the only shell converge ever replaces (with a zsh)


def shell_hook_path(target) -> Path:
    """``{state}/personal-dotfiles/shell-hook`` of ``target``."""

    return target.state_root / SHELL_HOOK_FILE


def _hook_names(hook_path: Path) -> set[str]:
    names = {str(hook_path), os.path.realpath(hook_path)}
    return names | {name + _DELETED for name in names}


def shell_hooked(pid: int, hook_path: Path) -> bool | None:
    """Whether process ``pid`` holds a descriptor on the shell-hook file.

    True: one of its /proc/<pid>/fd links is ``hook_path`` (also when that
    file was removed or replaced since: " (deleted)", or the same inode under
    another name) or any ``.../personal-dotfiles/shell-hook``. False: none
    is. None: cannot tell (the process is gone or its fds cannot be read),
    which callers treat as hooked: nothing is restarted on a guess.
    """

    names = _hook_names(hook_path)
    try:
        st = os.stat(hook_path)
        identity = (st.st_dev, st.st_ino)
    except OSError:
        identity = None
    base = f"/proc/{pid}/fd"
    try:
        fds = os.listdir(base)
    except OSError:
        return None
    for fd in fds:
        link_path = f"{base}/{fd}"
        try:
            link = os.readlink(link_path)
        except FileNotFoundError:
            continue  # closed since the listing
        except OSError:
            return None
        if link in names:
            return True
        name = link[:-len(_DELETED)] if link.endswith(_DELETED) else link
        if name.endswith(_HOOK_SUFFIX):
            return True
        if identity is not None and name.startswith("/") and \
                os.path.basename(name) == SHELL_HOOK_FILE:
            try:
                fst = os.stat(link_path)  # the open file itself, not the name
            except OSError:
                continue
            if (fst.st_dev, fst.st_ino) == identity:
                return True
    if not os.path.isdir(f"/proc/{pid}"):
        return None  # exited while being looked at
    return False


# --- which pane shells are respawned ---------------------------------------------

# Options that make a shell run commands instead of reading the terminal.
_COMMAND_OPTIONS = ("c",)
_LONG_COMMAND_OPTIONS = ("--command", "--init-command")


def _cmdline(pid: int) -> list[str] | None:
    try:
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    if not data:
        return None
    return data.rstrip(b"\0").decode("utf-8", "replace").split("\0")


def interactive_invocation(argv: list[str]) -> bool:
    """Whether a shell's argv is an interactive invocation.

    Every argument after argv[0] must be an option word (``-l``, ``-il``,
    ``+x``, ``--login``): no ``-c``, no non-option operand such as a script
    file (``bash script.sh``) and no option argument (``--rcfile FILE``,
    ``-o OPT``), which is refused as an operand too. argv[0] starting with
    '-' is how login and tmux start a login shell; that alone is not enough
    (``-zsh -c ...`` and ``-bash -c ...``, as su - and sudo -i run a
    command, are still refused). bin/dotfiles applies the same rule to its
    parent shell (tests/unit/test_install_cli.py keeps the two in parity).
    """

    if not argv or not argv[0]:
        return False
    for word in argv[1:]:
        if word in ("-", "--", "+", "++") or not word[:1] in ("-", "+"):
            return False
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            if "=" in word or name in _LONG_COMMAND_OPTIONS or name == "--rcfile":
                return False
            continue
        letters = word[1:]
        if not letters.isalpha() or any(c in letters for c in _COMMAND_OPTIONS):
            return False
        if "o" in letters:  # -o takes the next word
            return False
    return True


def _std_fds_on(pid: int, tty: str) -> bool:
    try:
        return all(os.readlink(f"/proc/{pid}/fd/{fd}") == tty for fd in (0, 1, 2))
    except OSError:
        return False


def _exe_name(pid: int) -> str | None:
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None
    if exe.endswith(_DELETED):  # the binary was upgraded since the shell started
        exe = exe[:-len(_DELETED)]
    return os.path.basename(exe)


def shell_restart_check(pid: int, tty: str, *, hook_path: Path) -> tuple[str | None, str]:
    """``(cwd, "restart")`` when the shell of a pane may be respawned, else
    ``(None, why)``. The caller adds the conditions only it knows: the
    generation changed, and the pane is live, not in a mode and not on the
    alternate screen.

    All of these must hold:
    - ``pid`` is a zsh: /proc comm and the exe's basename are both zsh, so
      a bash, sh, dash or fish pane is never replaced by a zsh ("not-zsh");
    - it is its own process group and its terminal's foreground group, with
      no child process at all: no foreground command, no background or
      suspended job ("busy");
    - its argv is an interactive invocation (:func:`interactive_invocation`):
      no ``-c``, no script operand ("script");
    - its stdin, stdout and stderr are the pane's terminal ("redirected");
    - it is not hooked (:func:`shell_hooked`): a shell of this repository
      reloads itself, keeping its exported environment, so it is never
      respawned ("hooked"; "hook-unknown" when its fds cannot be read);
    - its working directory still exists ("no-directory").

    What is left is a zsh from before this repository's hook (such as an
    upstream wookayin/dotfiles one) that looks idle at its prompt.
    Residual risk, accepted and inherent: from outside, such an unhooked
    zsh cannot be told apart from one at its prompt while it sits inside a
    builtin that reads the terminal (``read``, ``vared``, ``select``, a
    ``source``d script waiting at ``read``), at a continuation (PS2) or
    heredoc prompt, at a spelling-correction query, or with a typed but
    unsubmitted command line; and variables it exported after it started
    (an activated venv, a sourced ROS overlay) are not visible in
    /proc/<pid>/environ. Respawning such a shell loses that. Every shell
    started with this repository's config is hooked, so this applies only
    to shells that predate it, and only when the generation changes.
    """

    stat = _proc_stat(pid)
    if stat is None:
        return None, "gone"
    comm, _, pgrp, tpgid = stat
    if comm != RESPAWN_SHELL or _exe_name(pid) != RESPAWN_SHELL:
        return None, "not-zsh"
    if pgrp != pid or tpgid != pid or _has_children(pid):
        return None, "busy"
    argv = _cmdline(pid)
    if argv is None or not interactive_invocation(argv):
        return None, "script"
    if not _std_fds_on(pid, tty):
        return None, "redirected"
    hooked = shell_hooked(pid, hook_path)
    if hooked is None:
        return None, "hook-unknown"
    if hooked:
        return None, "hooked"
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None, "gone"
    if not os.path.isdir(cwd):  # a removed cwd reads "... (deleted)"
        return None, "no-directory"
    return cwd, "restart"

def _respawn_argv(command: str, shell: str, path: str) -> list[str] | None:
    """How a new pane of this server would start, as respawn-pane arguments."""

    command = command.strip()
    if command:
        words = command.split()
        if len(words) > 1 and all(_PLAIN_WORD_RE.match(w) for w in words):
            # Plain words: tmux executes them directly, so the pane's pid is
            # the shell itself (as with a default-shell that execs).
            return words if shutil.which(words[0], path=path) else None
        return [command]
    if shell.startswith("/") and os.access(shell, os.X_OK):
        return [shell, "-l"]  # what tmux starts without default-command: a login shell
    return None


def _scroll_into_history(tmux, runner, tenv, pane_id: str, tty: str, height: int) -> None:
    """Keep what an idle pane shows: scroll it into the history first.

    respawn-pane keeps a pane's history but clears its screen. Newlines
    written to the pane's terminal scroll every shown line into the history;
    then the screen is blank.
    """

    if not _PTS_RE.match(tty) or height <= 0:
        return
    try:
        fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        return
    try:
        os.write(fd, b"\r\n" * height)
    except OSError:
        return
    finally:
        os.close(fd)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        shown = runner.run([tmux, "capture-pane", "-p", "-t", pane_id], timeout=10,
                           check=False, env=tenv, read_only=True)
        if shown.returncode != 0 or not _text(shown.stdout).strip():
            return
        _sleep(0.05)


def _pane_state(tmux, runner, tenv, pane_id: str) -> list[str] | None:
    """The pane's _PANE_FORMAT fields now, or None."""

    shown = runner.run([tmux, "display-message", "-p", "-t", pane_id, _PANE_FORMAT],
                       timeout=10, check=False, env=tenv, read_only=True)
    if shown.returncode != 0:
        return None
    fields = _text(shown.stdout).rstrip("\n").split("\t", _PANE_FIELDS - 1)
    return fields if len(fields) == _PANE_FIELDS else None


def _pane_check(fields: list[str], hook_path: Path) -> tuple[str | None, str]:
    """:func:`shell_restart_check` plus what tmux knows of the pane."""

    (_, pid, dead, in_mode, alternate, _, tty, current, *_rest) = fields
    if dead == "1" or in_mode == "1" or alternate == "1" or not pid.isdigit():
        return None, "busy"
    cwd, why = shell_restart_check(int(pid), tty, hook_path=hook_path)
    if cwd is not None and os.path.basename(current).lstrip("-") != RESPAWN_SHELL:
        return None, "busy"  # tmux sees another foreground program
    return cwd, why


def respawn_idle_panes(tmux, runner, tenv, *, hook_path: Path
                       ) -> tuple[list[dict], dict[str, int], list[str]]:
    """Respawn every pane shell :func:`_pane_check` allows (the caller has
    established that the generation changed).

    Returns ``(restarted, kept, problems)``: the respawned panes as
    ``{"pane": "session:window.pane", "pane_id", "directory"}``, and the
    number of panes left as they are, by reason. A pane that runs anything
    else (a command, a job, a script, codex, an editor, ssh, the installer
    itself, a bash), a hooked zsh, a pane in a mode, on the alternate screen
    or dead is left exactly as it is. ``list-panes -a`` names a pane once
    per session that shows it (grouped sessions, linked windows); each pane
    counts once.

    Everything is checked again, from tmux and /proc, right before the
    pane's screen is scrolled into its history, and once more right before
    respawn-pane; a pane that fails either check is left alone (after the
    first one, it is not even scrolled). What remains is the moment between
    the last check and respawn-pane -k itself.
    """

    listed = runner.run([tmux, "list-panes", "-a", "-F", _PANE_FORMAT], timeout=30,
                        check=False, env=tenv, read_only=True)
    if listed.returncode != 0:
        return [], {}, ["could not list the tmux panes: " + _tail(listed.stderr)]
    env_path = runner.run([tmux, "show-environment", "-g", "PATH"], timeout=10,
                          check=False, env=tenv, read_only=True)
    path = _text(env_path.stdout).strip()
    path = path[len("PATH="):] if path.startswith("PATH=") else tenv.get("PATH", "")
    restarted: list[dict] = []
    kept: dict[str, int] = {}
    problems: list[str] = []
    seen: set[str] = set()

    def keep(why: str) -> None:
        kept[why] = kept.get(why, 0) + 1

    def recheck(pane_id: str, pid: str, cwd: str) -> str | None:
        """None when the pane is still exactly as when it was chosen."""
        now = _pane_state(tmux, runner, tenv, pane_id)
        if now is None:
            return "gone"
        if now[1] != pid:
            return "moved"
        again, why = _pane_check(now, hook_path)
        if again is None:
            return why
        return None if again == cwd else "moved"

    for line in _lines(listed):
        fields = line.split("\t", _PANE_FIELDS - 1)
        if len(fields) != _PANE_FIELDS:
            continue
        (pane_id, pid, _dead, _mode, _alt, height, tty, _current,
         window, index, session) = fields
        if pane_id in seen:
            continue
        seen.add(pane_id)
        cwd, why = _pane_check(fields, hook_path)
        if cwd is None:
            keep(why)
            continue
        start = runner.run([tmux, "display-message", "-p", "-t", pane_id,
                            "#{default-command}\t#{default-shell}"], timeout=10,
                           check=False, env=tenv, read_only=True)
        command, _, shell = _text(start.stdout).rstrip("\n").partition("\t")
        argv = _respawn_argv(command, shell, path) if start.returncode == 0 else None
        if argv is None:
            keep("no-command")
            problems.append(f"no command to restart tmux pane {pane_id} with "
                            f"(default-command {command!r}, default-shell {shell!r})")
            continue
        # Look again right before touching the pane at all: the shell may
        # have started something since the listing.
        why = recheck(pane_id, pid, cwd)
        if why is not None:
            keep(why)
            continue
        _scroll_into_history(tmux, runner, tenv, pane_id, tty,
                             int(height) if height.isdigit() else 0)
        # And once more right before the respawn.
        why = recheck(pane_id, pid, cwd)
        if why is not None:
            keep(why)
            continue
        done = runner.run([tmux, "respawn-pane", "-k", "-t", pane_id, "-c",
                           cwd.replace("#", "##"), *argv], timeout=30, check=False,
                          env=tenv)
        if done.returncode != 0:
            keep("respawn-failed")
            problems.append(f"could not restart tmux pane {pane_id}: {_tail(done.stderr)}")
            continue
        restarted.append({"pane": f"{session}:{window}.{index}", "pane_id": pane_id,
                          "directory": cwd})
    return restarted, kept, problems

def _server_binary(tmux, runner, version: str, pid: str) -> tuple[str, str | None]:
    """A tmux binary of the running server's version, for reading its defaults."""

    def version_of(binary):
        try:
            done = runner.run([binary, "-V"], timeout=10, check=False, read_only=True)
        except Exception:
            return None
        return _text(done.stdout).strip() if done.returncode == 0 else None

    wanted = f"tmux {version}"
    if version_of(tmux) == wanted:
        return tmux, None
    try:
        exe = os.readlink(f"/proc/{int(pid)}/exe")
    except (OSError, ValueError):
        exe = None
    if exe and os.access(exe, os.X_OK) and version_of(exe) == wanted:
        return exe, None
    return tmux, (f"the running server is {wanted} but {tmux} is "
                  f"{version_of(tmux) or 'unknown'}; its defaults were read from {tmux}")


def _wait_out_continuum_restore(tmux, runner, tenv, start_time: str) -> float:
    """Wait until continuum would no longer auto-restore into this server."""

    delay = CONTINUUM_RESTORE_MAX_DELAY
    done = runner.run([tmux, "show-options", "-gqv", "@continuum-restore-max-delay"],
                      timeout=10, check=False, env=tenv, read_only=True)
    value = _text(done.stdout).strip()
    if done.returncode == 0 and value.isdigit():
        delay = max(delay, int(value))
    try:
        remaining = int(start_time) + delay + CONTINUUM_MARGIN - time.time()
    except ValueError:
        return 0.0
    if remaining > 0:
        _sleep(remaining)
        return remaining
    return 0.0


def converge_summary(details: dict) -> str | None:
    """The one neutral line printed for a converged server."""

    if details.get("outcome") != "converged":
        return None
    if details.get("respawn") == "generation-unchanged":
        return "tmux: applied the new config"
    count = details.get("respawned_panes", 0)
    return (f"tmux: applied the new config; restarted {count} idle "
            f"shell{'' if count == 1 else 's'}")


def generation_changed(generation: str | None, previous: str | None) -> bool:
    """Whether a run changed the generation: one was written and it differs
    from the one recorded before (None: there was none, as on a first
    install over another setup)."""

    return generation is not None and generation != previous


def converge_running_tmux(target, runner, env: dict[str, str] | None = None, *,
                          generation: str | None = None,
                          previous_generation: str | None = None,
                          hook_path: Path | None = None) -> tuple[list[str], dict]:
    """Make the running tmux server what a fresh one with the new config is.

    Only the user's default server (TMUX and TMUX_PANE removed, so never a
    caller's) is considered, and only when it loaded a config inside this
    home. Its key bindings, global options and global hooks are first put
    back at the tmux defaults, read from a throwaway ``-f /dev/null`` server,
    keeping only the runtime state in RUNTIME_USER_OPTIONS; then the files a
    fresh server loads (~/.tmux.conf) are sourced, so TPM loads the new
    plugins. This is idempotent and runs on every converge.

    Last, and only when the config loaded without any error and the
    generation changed (``generation``, the one just written, differs from
    ``previous_generation``, the one recorded before it), the pane shells
    :func:`respawn_idle_panes` allows are respawned in the same directory
    with the server's default command, so they run the new shell setup:
    idle, interactive zsh shells without the reload hook (``hook_path``,
    default :func:`shell_hook_path`). A hooked zsh reloads itself and a bash,
    sh or fish pane is never replaced. Every other pane, its sessions and
    windows are left untouched. A failure is reported, never fatal.

    Returns ``(reasons, details)``: ``reasons`` lists only problems;
    ``details["outcome"]`` is one of converged, no-running-server,
    running-server-uses-another-config, failed; ``details["respawn"]`` is
    done or generation-unchanged; ``details["restarted"]`` names each
    respawned pane (session:window.pane) and its directory,
    ``details["kept"]`` counts the panes left as they are, by reason.
    """

    details: dict = {"outcome": "no-running-server", "respawned_panes": 0,
                     "busy_panes": 0}
    tmux = runner.which("tmux")
    if tmux is None:
        return [], details
    tenv = dict(os.environ if env is None else env)
    for key in ("TMUX", "TMUX_PANE"):  # the user's default server, not a caller's
        tenv.pop(key, None)
    probe = runner.run([tmux, "display-message", "-p",
                        "#{pid}\t#{start_time}\t#{version}\t#{config_files}"],
                       timeout=10, check=False, env=tenv, read_only=True)
    if probe.returncode != 0:
        return [], details
    fields = _text(probe.stdout).strip("\n").split("\t", 3)
    if len(fields) != 4:
        fields = ["", "", "", ""]
    pid, start_time, version, config_files = fields
    loaded = [part.strip() for part in config_files.split(",") if part.strip()]
    # tmux records the resolved path of each file it loaded. A config inside
    # this home (an old ~/.tmux.conf, an upstream checkout's tmux.conf, ...)
    # is what a restart would replace with ~/.tmux.conf anyway; a server
    # running another user's or another home's config is left alone.
    home = str(target.home).rstrip("/") + "/"
    conf = str(target.home / ".tmux.conf")
    if not any(path == conf or path.startswith(home) for path in loaded):
        details["outcome"] = "running-server-uses-another-config"
        return [], details

    problems: list[str] = []
    waited = _wait_out_continuum_restore(tmux, runner, tenv, start_time)
    if waited:
        details["waited_for_continuum_seconds"] = round(waited, 1)
    binary, note = _server_binary(tmux, runner, version, pid)
    if note:
        details["defaults_from"] = note
    defaults = capture_tmux_defaults(binary, runner, child_env(target, tenv),
                                     cwd=target.home)
    keys, options, hooks = _server_state(tmux, runner, tenv)
    lines = _reset_lines(defaults, keys, options, hooks)
    lines += _config_lines(default_config_files(target))
    workdir = Path(tempfile.mkdtemp(prefix="pdfc-"))
    try:
        config_error = _source_lines(tmux, runner, tenv, workdir, "converge", lines)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if config_error:
        # No shell is restarted into a config that did not load cleanly.
        problems.append(f"loading the new config: {config_error}")
    elif not generation_changed(generation, previous_generation):
        # Nothing changed (or no generation was written): no shell has
        # anything new to load, so none is touched.
        details["respawn"] = "generation-unchanged"
        details["restarted"] = []
        details["kept"] = {}
    else:
        # Only with the new config in place: its default-command starts them.
        restarted, kept, why = respawn_idle_panes(
            tmux, runner, tenv,
            hook_path=shell_hook_path(target) if hook_path is None else Path(hook_path))
        details["respawn"] = "done"
        details["respawned_panes"] = len(restarted)
        details["busy_panes"] = sum(kept.values())
        details["restarted"] = restarted
        details["kept"] = dict(sorted(kept.items()))
        problems += why
    if problems:
        details["outcome"] = "failed"
        return (["could not fully apply the new config to the running tmux server: "
                 + "; ".join(problems)], details)
    details["outcome"] = "converged"
    return [], details


def post_install_phase(target, runner, *, repo_root: Path, run_id: str,
                       systemd_units_applied: bool,
                       env: dict[str, str] | None = None,
                       skip_zplug: bool = False,
                       skip_vimplug: bool = False,
                       tmux_env: dict[str, str] | None = None,
                       record_generation=None,
                       hook_path: Path | None = None) -> PhaseResult:
    """The post actions, in this order:

    1. the pinned tmux plugins, the systemd user units and the zsh/nvim
       plugin step;
    2. ``record_generation()`` (install.py: compute the checkout's
       generation, write it, return ``(previous, generation)``), so every
       shell started from here on loads the final generation and every
       hooked shell sees the change;
    3. the running tmux server is converged: config reset and source, then,
       only when the generation changed, the idle unhooked zsh panes are
       respawned.

    ``tmux_env`` addresses the user's tmux server (it keeps TMUX_TMPDIR,
    which ``env`` from :func:`child_env` drops); it defaults to ``env``.
    """

    statuses: list[str] = []
    reasons: list[str] = []
    details: dict = {}

    try:
        status, why, plugin_details = install_tmux_plugins(target, runner, repo_root, run_id)
    except Exception as exc:  # malformed manifest or unexpected I/O
        status, why, plugin_details = FAIL, [f"tmux-plugins: {exc}"], {}
    statuses.append(PASS if status == SKIPPED else status)
    reasons += why
    details["tmux_plugins"] = plugin_details

    if not systemd_units_applied:
        details["systemd"] = "units-skipped"
    elif systemd_user_reachable(runner, env):
        completed = runner.run(["systemctl", "--user", "daemon-reload"],
                               timeout=60, check=False, env=env)
        if completed.returncode == 0:
            details["systemd"] = "daemon-reloaded"
            status, why, timer = start_autosave_timer(target, runner, env)
            statuses.append(status)
            reasons += why
            details["autosave_timer"] = timer
        else:
            statuses.append(FAIL)
            reasons.append("systemctl --user daemon-reload failed: "
                           + _tail(completed.stderr))
    else:
        statuses.append(RELOGIN_REQUIRED)
        reasons.append("systemd-user-manager-unavailable")
        details["systemd"] = "pending-next-login"

    plugin_statuses, plugin_reasons, plugin_details = plugins_step(
        target, runner, env=env, skip_zplug=skip_zplug, skip_vimplug=skip_vimplug)
    statuses += plugin_statuses
    reasons += plugin_reasons
    details.update(plugin_details)

    generation = previous = None
    if record_generation is not None:
        try:
            previous, generation = record_generation()
        except Exception as exc:  # the files are installed; shells keep theirs
            reasons.append(f"could not write the generation file: {exc}")
        else:
            details["generation"] = generation
            details["generation_changed"] = generation_changed(generation, previous)

    # Last: the converged server loads the plugins above, and the shells it
    # restarts start on the generation just written.
    try:
        why, details["tmux_converge"] = converge_running_tmux(
            target, runner, env if tmux_env is None else tmux_env,
            generation=generation, previous_generation=previous, hook_path=hook_path)
    except Exception as exc:  # never let a running server break the install
        why = [f"could not apply the new config to the running tmux server: {exc}"]
        details["tmux_converge"] = {"outcome": "failed", "respawned_panes": 0,
                                    "busy_panes": 0}
    reasons += why
    summary = converge_summary(details["tmux_converge"])
    if summary:
        ui.log(summary)

    return PhaseResult("post-install", worst(statuses or [PASS]), reasons, details)


# --- smoke checks -----------------------------------------------------------

def smoke_zsh(target, runner, env: dict[str, str]) -> tuple[str, str]:
    zsh = runner.which("zsh")
    if zsh is None:
        return FAIL, "zsh-not-installed"
    completed = runner.run([zsh, "-i", "-c", "exit"], timeout=SMOKE_TIMEOUT,
                           check=False, env=env, input=b"", cwd=target.home)
    if completed.returncode != 0:
        return FAIL, f"zsh -i -c exit returned {completed.returncode}: {_tail(completed.stderr)}"
    return PASS, ""


def _socket_path(socket_dir: str, name: str) -> str:
    return os.path.join(socket_dir, f"tmux-{os.getuid()}", name)


def _short_socket_dir(name: str, env: dict[str, str]) -> str | None:
    """A private ``mkdtemp`` directory whose ``-L`` socket path fits sun_path."""

    candidates = []
    for root in (tempfile.gettempdir(), "/tmp", env.get("XDG_RUNTIME_DIR")):
        if root and root not in candidates:
            candidates.append(root)
    for root in candidates:
        # mkdtemp adds '/' + prefix + 8 random characters.
        projected = _socket_path(os.path.join(root, "pdfs-" + "x" * 8), name)
        if len(projected) > SOCKET_PATH_MAX or not os.path.isdir(root):
            continue
        try:
            socket_dir = tempfile.mkdtemp(prefix="pdfs-", dir=root)
        except OSError:
            continue
        if len(_socket_path(socket_dir, name)) <= SOCKET_PATH_MAX:
            return socket_dir
        shutil.rmtree(socket_dir, ignore_errors=True)
    return None


def smoke_tmux(target, runner, env: dict[str, str]) -> tuple[str, str]:
    """Load ~/.tmux.conf into a throwaway server on its own socket."""

    tmux = runner.which("tmux")
    if tmux is None:
        return FAIL, "tmux-not-installed"
    conf = target.home / ".tmux.conf"
    # A short, unique -L name inside a private TMUX_TMPDIR: isolated from
    # every other server, and short enough for sun_path even when TMPDIR
    # is a long per-session directory.
    name = f"pdfs{os.getpid()}{secrets.token_hex(2)}"
    socket_dir = _short_socket_dir(name, env)
    if socket_dir is None:
        return FAIL, ("no temporary directory is short enough for a tmux socket "
                      f"(limit {SOCKET_PATH_MAX} characters); set TMPDIR=/tmp")
    tenv = dict(env)
    for key in ("TMUX", "TMUX_PANE"):
        tenv.pop(key, None)
    # TMUX_TMPDIR puts the -L socket inside our own directory.
    tenv["TMUX_TMPDIR"] = socket_dir
    try:
        started = runner.run([tmux, "-L", name, "-f", "/dev/null", "new-session",
                              "-d", "-s", "smoke"], timeout=30, check=False,
                             env=tenv, cwd=target.home)
        if started.returncode != 0:
            return FAIL, f"tmux server did not start: {_tail(started.stderr)}"
        loaded = runner.run([tmux, "-L", name, "source-file", str(conf)],
                            timeout=60, check=False, env=tenv, cwd=target.home)
        if loaded.returncode != 0:
            return FAIL, f"tmux config failed to load: {_tail(loaded.stderr or loaded.stdout)}"
        return PASS, ""
    finally:
        try:
            runner.run([tmux, "-L", name, "kill-server"], timeout=30, check=False,
                       env=tenv)
        finally:
            shutil.rmtree(socket_dir, ignore_errors=True)


def smoke_phase(target, runner, env: dict[str, str] | None = None) -> PhaseResult:
    cenv = child_env(target, env)
    statuses, reasons, details = [], [], {}
    for label, check in (("zsh", smoke_zsh), ("tmux", smoke_tmux)):
        try:
            status, reason = check(target, runner, cenv)
        except Exception as exc:
            status, reason = FAIL, f"{label}: {exc}"
        statuses.append(status)
        details[label] = status
        if reason:
            reasons.append(reason)
    return PhaseResult("smoke", worst(statuses), reasons, details)


# --- login shell ------------------------------------------------------------

def _preferred_zsh(runner) -> str | None:
    try:
        shells = Path("/etc/shells").read_text(encoding="utf-8").split()
    except OSError:
        shells = []
    for candidate in ("/usr/bin/zsh", "/bin/zsh"):
        if candidate in shells and os.access(candidate, os.X_OK):
            return candidate
    return runner.which("zsh")


def password_status(runner, username: str) -> str | None:
    """``P``, ``L`` or ``NP`` from ``passwd -S`` for the own account, or None.

    ``passwd -S`` reports only whether the account has a usable (P), locked
    (L) or empty (NP) password; it never prints the hash.
    """

    try:
        completed = runner.run(["passwd", "-S"], timeout=15, check=False,
                               input=b"", read_only=True)
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    fields = _out(completed).split()
    if len(fields) < 2 or (username and fields[0] != username):
        return None
    return fields[1] if fields[1] in ("P", "L", "NP") else None


def _sudo_without_password(runner) -> bool:
    if runner.which("sudo") is None:
        return False
    try:
        completed = runner.run(["sudo", "-n", "true"], timeout=15, check=False,
                               input=b"", read_only=True)
    except Exception:
        return False
    return completed.returncode == 0


def login_shell_phase(target, runner, *, current_shell: str,
                      allow_change: bool, interactive=None,
                      checks_passed: bool = True) -> PhaseResult:
    """Make zsh the passwd login shell.

    ``interactive`` is a callable ``(argv) -> returncode`` that runs attached
    to the terminal, because chsh prompts for a password; without it the call
    goes through ``runner`` (tests, non-interactive use). ``checks_passed``
    is false when the zsh/tmux smoke checks failed: the login shell is then
    left alone, so a broken zshrc never becomes the next login's shell.

    Accounts without a usable password (cloud-init's default user, locked
    with NOPASSWD sudo) cannot pass chsh's PAM check; they are changed with
    ``sudo -n chsh`` when sudo needs no password, and otherwise get the sudo
    command to run instead of a chsh that cannot succeed.
    """

    if Path(current_shell or "").name == "zsh":
        ui.log(ui.GREEN("$SHELL is already zsh.") + f" ({current_shell})")
        return PhaseResult("login-shell", PASS, [], {"shell": current_shell})
    if not allow_change:
        return PhaseResult("login-shell", SKIPPED, ["--no-shell-change"],
                           {"shell": current_shell})
    zsh = _preferred_zsh(runner)
    if zsh is None:
        return PhaseResult("login-shell", FAIL, ["zsh-not-installed"],
                           {"shell": current_shell})
    user = target.username
    plain = f"chsh -s {zsh}"
    with_sudo = f"sudo chsh -s {zsh} {user}"
    if not checks_passed:
        ui.log(ui.YELLOW("The zsh/tmux smoke checks failed; the login shell was "
                         f"not changed. Fix them, then run: {plain}"))
        return PhaseResult("login-shell", SKIPPED,
                           [f"smoke-failed; after fixing it run: {plain}"],
                           {"shell": current_shell, "command": plain})

    password = password_status(runner, user)
    if password in ("L", "NP") and _sudo_without_password(runner):
        ui.log(ui.YELLOW("This account has no usable password; changing the "
                         "default shell to ZSH with sudo"))
        completed = runner.run(["sudo", "-n", "chsh", "-s", zsh, user],
                               timeout=60, check=False, input=b"")
        if completed.returncode != 0:
            return PhaseResult("login-shell", FAIL,
                               [f"sudo chsh failed ({completed.returncode}): "
                                f"{_tail(completed.stderr)}; run: {with_sudo}"],
                               {"shell": current_shell, "command": with_sudo})
    elif password == "L":
        # chsh authenticates the caller with PAM, which a locked password
        # can never pass; do not run a prompt that cannot succeed.
        ui.log(ui.YELLOW("This account has a locked password, so chsh cannot "
                         f"authenticate it. Run: {with_sudo}"))
        return PhaseResult("login-shell", SKIPPED,
                           [f"account has no usable password; run: {with_sudo}"],
                           {"shell": current_shell, "command": with_sudo})
    else:
        ui.log(ui.YELLOW("Please type your password if you wish to change the "
                         "default shell to ZSH"))
        if interactive is not None:
            returncode = interactive(["chsh", "-s", zsh])
        else:
            returncode = runner.run(["chsh", "-s", zsh], timeout=300,
                                    check=False).returncode
        if returncode != 0:
            return PhaseResult("login-shell", FAIL,
                               [f"chsh failed ({returncode}); run: {plain} "
                                f"(an account without a password: {with_sudo})"],
                               {"shell": current_shell, "command": plain})
    ui.log("Successfully changed the default shell, please re-login")
    return PhaseResult("login-shell", RELOGIN_REQUIRED,
                       ["log out and back in for zsh to become the login shell"],
                       {"shell": zsh})


# --- AI CLI sign-in ------------------------------------------------------------

def _tool_binary(target, runner, name: str) -> str | None:
    """The installed CLI: ~/.local/bin, the installer bin dir, then PATH."""

    for candidate in (target.home / ".local" / "bin" / name,
                      target.data_home / "personal-dotfiles" / "bin" / name):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return str(candidate)
    return runner.which(name)


def _text(data) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data or ""


def _lists_subcommand(runner, argv: list[str], name: str, env) -> bool:
    """Whether ``argv`` (a ``--help`` call) lists the ``name`` subcommand."""

    helped = runner.run(argv, timeout=AUTH_TIMEOUT, check=False, env=env,
                        input=b"", read_only=True)
    listed = [line.split()[0] for line in _text(helped.stdout).splitlines()
              if line.startswith("  ") and line.split()]
    return helped.returncode == 0 and name in listed


def _codex_signed_in(runner, binary: str, env) -> bool | None:
    """``codex login status``: 0 when signed in, 'Not logged in' otherwise."""

    if not _lists_subcommand(runner, [binary, "login", "--help"], "status", env):
        return None
    completed = runner.run([binary, "login", "status"], timeout=AUTH_TIMEOUT,
                           check=False, env=env, input=b"", read_only=True)
    if completed.returncode == 0:
        return True
    said = (_text(completed.stdout) + _text(completed.stderr)).lower()
    return False if "not logged in" in said else None


def _claude_signed_in(runner, binary: str, env) -> bool | None:
    """``claude auth status`` (JSON ``loggedIn``) when this build has it."""

    if not _lists_subcommand(runner, [binary, "auth", "--help"], "status", env):
        return None
    completed = runner.run([binary, "auth", "status", "--json"],
                           timeout=AUTH_TIMEOUT, check=False, env=env, input=b"",
                           read_only=True)
    try:
        # Only the boolean is read; the account details are discarded.
        logged_in = json.loads(_text(completed.stdout)).get("loggedIn")
    except (ValueError, AttributeError):
        return None
    if not isinstance(logged_in, bool):
        return None
    return logged_in


AUTH_CHECKS = (
    ("codex", _codex_signed_in, "run `codex login`"),
    ("claude", _claude_signed_in, "run `claude auth login` (or `claude`, then /login)"),
)


def auth_phase(target, runner, env: dict[str, str] | None = None) -> PhaseResult:
    """Whether the installed AI CLIs are signed in, via their own status commands.

    Each check is the tool's documented non-interactive status command, run
    as the target user; credential files are never opened here. A tool that
    is not installed or whose answer cannot be read is ``unverified`` and does
    not count either way.
    """

    cenv = child_env(target, env)
    statuses: list[str] = []
    reasons: list[str] = []
    details: dict[str, str] = {}
    for name, check, remedy in AUTH_CHECKS:
        binary = _tool_binary(target, runner, name)
        if binary is None:
            details[name] = "not-installed"
            continue
        try:
            signed_in = check(runner, binary, cenv)
        except Exception:
            signed_in = None
        if signed_in is None:
            details[name] = "unverified"
        elif signed_in:
            details[name] = "signed-in"
            statuses.append(PASS)
        else:
            details[name] = "not-signed-in"
            statuses.append(AUTH_REQUIRED)
            reasons.append(f"{name} is not signed in; {remedy}")
    if not statuses:
        return PhaseResult("auth", SKIPPED, ["sign-in-not-verifiable"], details)
    return PhaseResult("auth", worst(statuses), reasons, details)


# --- git identity -------------------------------------------------------------

SECRET_HEADER = "# vim: set ft=gitconfig:\n"


def _git_config_get(runner, secret: Path, key: str) -> str | None:
    completed = runner.run(["git", "config", "--file", str(secret), key],
                           timeout=30, check=False, read_only=True)
    value = _out(completed) if completed.returncode == 0 else ""
    return value or None


def git_identity_phase(target, runner, *, prompt=None, dry_run: bool = False) -> PhaseResult:
    """Upstream's ~/.gitconfig.secret check: identity lives outside the repo.

    ``prompt`` is ``callable(question) -> str | None`` reading the terminal;
    ``None`` (no terminal) prints the commands to run instead.
    """

    secret = target.home / ".gitconfig.secret"
    commands = [
        f'git config --file {secret} user.name "(YOUR NAME)"',
        f'git config --file {secret} user.email "(YOUR EMAIL)"',
    ]
    if runner.which("git") is None:
        return PhaseResult("git-identity", FAIL, ["git-not-installed"])
    if not dry_run and not os.path.lexists(secret):
        fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(SECRET_HEADER)
    name = _git_config_get(runner, secret, "user.name") if secret.exists() else None
    email = _git_config_get(runner, secret, "user.email") if secret.exists() else None
    if name and email:
        ui.log(ui.GREEN(f"user.name  : {name}"))
        ui.log(ui.GREEN(f"user.email : {email}"))
        return PhaseResult("git-identity", PASS, [], {"file": str(secret)})

    ui.log(ui.YELLOW("[!!!] Please configure git user name and email:"))
    for command in commands:
        ui.log("    " + ui.YELLOW(command))
    if prompt is None or dry_run:
        return PhaseResult("git-identity", SKIPPED, ["git-identity-not-configured"],
                           {"file": str(secret), "commands": commands})
    name = name or (prompt("(git config user.name) Please input your name  : ") or "").strip()
    email = email or (prompt("(git config user.email) Please input your email : ") or "").strip()
    if not (name and email):
        return PhaseResult("git-identity", SKIPPED, ["git-identity-not-configured"],
                           {"file": str(secret), "commands": commands})
    for key, value in (("user.name", name), ("user.email", email)):
        completed = runner.run(["git", "config", "--file", str(secret), key, value],
                               timeout=30, check=False)
        if completed.returncode != 0:
            return PhaseResult("git-identity", FAIL,
                               [f"git config --file {secret} {key} failed"])
    ui.log(ui.GREEN(f"user.name  : {name}"))
    ui.log(ui.GREEN(f"user.email : {email}"))
    return PhaseResult("git-identity", PASS, [], {"file": str(secret)})


# --- status -----------------------------------------------------------------

def write_status(target, payload: dict) -> Path:
    root = target.state_root
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    path = root / "status.json"
    tmp = root / f".status.json.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(jsonable(payload), handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def read_status(target) -> dict | None:
    try:
        return json.loads((target.state_root / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def format_summary(results: list[PhaseResult]) -> str:
    lines = ["", "Summary:"]
    for result in results:
        line = f"  {result.phase:<14} {result.status}"
        if result.reasons:
            line += "  (" + "; ".join(result.reasons) + ")"
        lines.append(line)
    lines.append(f"  {'overall':<14} {overall_status(results)}")
    follow = {
        RELOGIN_REQUIRED: "Log out and back in to finish (login shell / user services).",
        PENDING_GUI: "Desktop settings are deferred; run 'dotfiles gui-apply' inside the desktop session.",
        AUTH_REQUIRED: "Some tools need sign-in; see the reasons above.",
        FAIL: "Some phases failed; fix the reasons above and rerun 'dotfiles repair'.",
    }
    seen = {r.status for r in results}
    pending_reasons = [reason for r in results if r.status == PENDING_GUI
                       for reason in r.reasons]
    if pending_reasons and all(r == REMAPPER_UNSUPPORTED_REASON for r in pending_reasons):
        follow[PENDING_GUI] = ("Ctrl+Super+Left/Right tab switching is unavailable on "
                               "Ubuntu 22.04 (input-remapper 1.4); nothing to rerun.")
    for status in _SEVERITY:
        if status in seen and status in follow:
            lines.append("  -> " + follow[status])
    return "\n".join(lines)


def completion_lines(results: list[PhaseResult]) -> list[str]:
    """Upstream's closing box, then YELLOW follow-ups for this run."""

    failed = [r for r in results if r.status == FAIL]
    lines = [""]
    if failed:
        lines.append(ui.boxed("You have %3d warnings or errors -- check the logs!"
                              % len(failed), ui.YELLOW, use_bold=True))
        lines += ["   " + ui.YELLOW(f"{r.phase}: " + "; ".join(r.reasons or ["failed"]))
                  for r in failed]
    else:
        lines.append(ui.boxed("\u2714  You are all set! ", ui.GREEN, use_bold=True))

    by_phase = {r.phase: r for r in results}
    follow: list[str] = []
    packages = by_phase.get("packages")
    auth = by_phase.get("auth")
    if auth is not None:
        unverified = [name for name, _, _ in AUTH_CHECKS
                      if auth.details.get(name) == "unverified"]
        hints = {"codex": "`codex login`", "claude": "`claude` (then /login)"}
        if unverified:
            follow.append("Sign in if you have not yet (not verifiable here): "
                          + ", ".join(hints[n] for n in unverified) + ".")
    elif packages is not None and packages.status != SKIPPED:
        selected = packages.details.get("tools_selected") or []
        signins = [hint for tool, hint in (("codex", "`codex login`"),
                                           ("claude-code", "`claude` (then /login)"))
                   if tool in selected]
        if signins:
            follow.append("Sign in to the AI CLIs if you have not yet: "
                          + ", and ".join(signins) + ".")
    for result in results:
        if result.status == AUTH_REQUIRED:
            follow.append(f"{result.phase} needs sign-in: " + "; ".join(result.reasons))
    shell = by_phase.get("login-shell")
    if shell is not None and shell.status == RELOGIN_REQUIRED:
        follow.append("Log out and back in so zsh becomes your login shell.")
    elif (shell is not None and shell.status == SKIPPED
          and shell.details.get("command")):
        follow.append("zsh is not your login shell yet: "
                      + "; ".join(shell.reasons))
    post = by_phase.get("post-install")
    if post is not None and "systemd-user-manager-unavailable" in post.reasons:
        follow.append("User services (tmux) start at the next graphical login.")
    gui = by_phase.get("gui")
    if gui is not None and gui.status == PENDING_GUI:
        # 22.04's input-remapper 1.4 can never express the tab chord, so
        # rerunning gui-apply would not help; say so instead of suggesting it.
        permanent = [r for r in gui.reasons if r == REMAPPER_UNSUPPORTED_REASON]
        retryable = [r for r in gui.reasons if r != REMAPPER_UNSUPPORTED_REASON]
        if permanent:
            follow.append("Ctrl+Super+Left/Right tab switching is not available on "
                          "Ubuntu 22.04: its input-remapper 1.4 cannot express it. "
                          "The other desktop settings are unaffected.")
        if retryable:
            follow.append("Pending desktop settings (run `dotfiles gui-apply` in the "
                          "desktop session): " + "; ".join(retryable))
    identity = by_phase.get("git-identity")
    if identity is not None and identity.status == SKIPPED:
        for command in identity.details.get("commands", []):
            follow.append(command)
    lines += ["- " + ui.YELLOW(item) for item in follow]
    # No "restart your shell" line: open shells pick up the new setup by
    # themselves (idle tmux panes are restarted, zsh reloads at its prompt).
    lines += [
        "- To install some packages locally (e.g. neovim, fzf), try "
        + ui.CYAN("`dotfiles install <package>`"),
        "- If you want to update dotfiles (or have any errors), try "
        + ui.CYAN("`dotfiles update`"),
    ]
    return lines
