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


def post_install_phase(target, runner, *, repo_root: Path, run_id: str,
                       systemd_units_applied: bool,
                       env: dict[str, str] | None = None,
                       skip_zplug: bool = False,
                       skip_vimplug: bool = False) -> PhaseResult:
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
        follow.append("Sign in to the AI CLIs if you have not yet: `codex login`, "
                      "and `claude` (then /login).")
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
    lines += [
        "- Please restart shell (e.g. " + ui.CYAN("`exec zsh`") + ") if necessary.",
        "- To install some packages locally (e.g. neovim, fzf), try "
        + ui.CYAN("`dotfiles install <package>`"),
        "- If you want to update dotfiles (or have any errors), try "
        + ui.CYAN("`dotfiles update`"),
    ]
    return lines
