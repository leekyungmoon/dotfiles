"""macOS LaunchAgents: tmux at login and the one-minute autosave.

The counterpart of systemd/user/tmux.service and
tmux-resurrect-autosave.timer on Linux. Two agents live in the user's GUI
login domain (``gui/<uid>``):

- ``com.github.leekyungmoon.dotfiles.tmux`` runs tmux/tmux-launchd-start once
  when it is loaded (RunAtLoad): at every GUI login, and when install or
  repair loads it. That script starts the detached login server only when no
  server runs, so loading it again never touches a running tmux.
- ``com.github.leekyungmoon.dotfiles.tmux-resurrect-autosave`` runs
  tmux/resurrect-autosave every 60 seconds (StartInterval), with or without a
  client attached.

Their plists are rendered here per Target, with absolute paths only (launchd
expands neither ``~`` nor ``$HOME``), and installed by the transaction as
ordinary managed files (install.extra_desired_entries), so backups, drift,
``restore`` and idempotency work as for every other file: the same Target
renders the same bytes, and an unchanged plist means nothing to reload.

Registration goes through ``launchctl`` and the injected runner, and every
answer is mapped to a phase status. ``launchctl print`` output "is NOT API"
(launchctl(1)): it is read only for evidence (loaded, state, pid, last exit
code, run interval) and anything it does not say counts as "cannot tell",
never as a failure or a reason to reload. Nothing is reported PASS without a
successful ``launchctl print`` of the service.
"""

from __future__ import annotations

import dataclasses
import os
import plistlib
import re
import shlex
import shutil
import time
from pathlib import Path

from installer.phases import (FAIL, LAUNCHD_GUI_DOMAIN_UNAVAILABLE, PASS,
                              RELOGIN_REQUIRED, SKIPPED)
from installer.platform import APP_NAME
from installer.transaction import DesiredEntry

LABEL_TMUX = "com.github.leekyungmoon.dotfiles.tmux"
LABEL_AUTOSAVE = "com.github.leekyungmoon.dotfiles.tmux-resurrect-autosave"


@dataclasses.dataclass(frozen=True)
class Agent:
    name: str      # what `dotfiles services ... <name>` calls it
    label: str
    entry_id: str  # its plist's id in the transaction
    program: str   # relative to the checkout


TMUX = Agent("tmux", LABEL_TMUX, "launchd-tmux", "tmux/tmux-launchd-start")
AUTOSAVE = Agent("autosave", LABEL_AUTOSAVE, "launchd-tmux-resurrect-autosave",
                 "tmux/resurrect-autosave")
AGENTS = (TMUX, AUTOSAVE)
ENTRY_IDS = tuple(agent.entry_id for agent in AGENTS)

# tmux-continuum's own launcher (written by its @continuum-boot 'on'; its
# Label is the file name). It opens a terminal window and types tmux into
# it, a second launcher next to ours; never one of ours.
CONTINUUM_PLIST = "Tmux.Start.plist"
CONTINUUM_LABEL = "Tmux.Start.plist"

LAUNCHCTL = "/bin/launchctl"
LOCALE = "/usr/bin/locale"
DEFAULT_LANG = "en_US.UTF-8"
AUTOSAVE_INTERVAL = 60
# The agents' fallback PATH after the tools, ~/.local/bin and Homebrew: the
# directories of a login shell (path_helper) in their usual order.
SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
QUERY_TIMEOUT = 30.0
CHANGE_TIMEOUT = 60.0
TMUX_QUERY_TIMEOUT = 10.0
# launchd finishes a bootout asynchronously; a bootstrap of the same label
# before it is gone fails with 5 (or 37).
BOOTOUT_WAIT = 10.0
# tmux-launchd-start leaves at once when a server runs and when there is no
# snapshot; restoring one keeps it running for up to about 25 s, plus up to
# 180 s while the restore itself runs.
TMUX_START_WAIT = 15.0
POLL_INTERVAL = 0.5
_sleep = time.sleep       # patched by tests
_monotonic = time.monotonic

# Exit statuses of `launchctl bootstrap` / `bootout` this module tells apart.
# 5 is what current macOS returns for nearly every refusal (already loaded,
# disabled, blocked under Login Items, a bootout still in progress); 37 and
# 17 are how older releases say "already loaded".
NOT_LOADED = (3, 113)        # bootout or print: no such service
NO_GUI_DOMAIN = (112, 125)   # no GUI login session for this user here
ALREADY_LOADED = (5, 17, 37)
DISABLED = 119
IN_PROGRESS = 36
# `launchctl error <code>` for the codes these commands return in practice.
ERROR_TEXT = {
    1: "Operation not permitted",
    2: "No such file or directory",
    3: "No such process",
    5: "Input/output error",
    17: "File exists",
    22: "Invalid argument",
    36: "Operation now in progress",
    37: "Operation already in progress",
    107: "Malformed bundle",
    108: "Invalid path",
    109: "Invalid property list",
    110: "Invalid or missing service identifier",
    111: "Invalid or missing Program/ProgramArguments",
    112: "Could not find specified domain",
    113: "Could not find specified service",
    119: "Service is disabled",
    122: "Path had bad ownership/permissions",
    124: "Domain is tearing down",
    125: "Domain does not support specified action",
    134: "Service cannot load in requested session",
    150: "Operation not permitted while System Integrity Protection is engaged",
    155: "Refusing to execute/trust quarantined program/file",
}
BACKGROUND_HINT = (
    "check System Settings > General > Login Items & Extensions, where "
    "'Allow in the Background' must be on for it (it may be listed as 'sh' or by "
    "its script's name), and run the install from Terminal in your desktop "
    "session, not over SSH or inside a sandbox")


def agent_named(name: str) -> Agent:
    for agent in AGENTS:
        if agent.name == name:
            return agent
    raise KeyError(name)


# --- paths and plists ---------------------------------------------------------

def plist_path(target, agent: Agent) -> Path:
    return target.home / "Library" / "LaunchAgents" / f"{agent.label}.plist"


def log_dir(target) -> Path:
    """``{state}/personal-dotfiles/launchd``: the agents' logs (0700)."""

    return target.state_root / "launchd"


def log_path(target, agent: Agent) -> Path:
    return log_dir(target) / f"{agent.label}.log"


def program_path(target, agent: Agent) -> Path:
    # The checkout as every managed link names it (target.repo_root), not
    # wherever install.py happens to run from.
    return target.repo_root / agent.program


def prepare_log_dir(target) -> Path:
    """Create the log directory: launchd opens StandardOutPath itself and
    does not create missing directories."""

    path = log_dir(target)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def launch_path(target, brew_prefix) -> str:
    """The agents' PATH: the pinned tools, ~/.local/bin, Homebrew, then the
    system directories, each once (first wins).

    Neither script adds Homebrew itself, and launchd's default PATH has none
    of these, so tmux, the hook's python3 and everything a pane or run-shell
    needs come from here. A directory whose name holds ':' cannot be a PATH
    element and is left out.
    """

    entries = [target.data_home / APP_NAME / "bin", target.home / ".local" / "bin"]
    if brew_prefix is not None:
        entries += [Path(brew_prefix) / "bin", Path(brew_prefix) / "sbin"]
    out: list[str] = []
    for entry in [str(e) for e in entries] + list(SYSTEM_PATH):
        if ":" not in entry and entry not in out:
            out.append(entry)
    return ":".join(out)


# A complete UTF-8 locale name (C.UTF-8, en_US.UTF-8, sr_RS.UTF-8@latin);
# macOS also accepts a bare "UTF-8" for LC_CTYPE, which is no LANG.
_UTF8_LOCALE = re.compile(r"^[A-Za-z]{1,8}(?:_[A-Za-z0-9]{2,8})?\.utf-?8(?:@[A-Za-z0-9_]+)?$",
                          re.IGNORECASE)


def _locale_key(name: str) -> str:
    return name.strip().lower().replace("utf-8", "utf8")


def _text(data) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data or ""


def _tail(data, limit: int = 400) -> str:
    return _text(data).strip()[-limit:]


def pick_lang(env, runner) -> str:
    """LANG for the agents: the first UTF-8 locale among the installing
    shell's LC_ALL, LC_CTYPE and LANG that ``locale -a`` lists (compared
    without case and hyphen, so en_US.utf8 is en_US.UTF-8), spelled as listed;
    otherwise en_US.UTF-8, which every macOS has.

    A UTF-8 LANG matters beyond zsh: macOS ps vis-encodes the bytes it takes
    for unprintable, so under the C locale non-ASCII pane arguments would not
    match their live process and the AI hook would clear them.
    """

    wanted = [value for value in ((env or {}).get(name, "").strip()
                                  for name in ("LC_ALL", "LC_CTYPE", "LANG"))
              if _UTF8_LOCALE.match(value)]
    if not wanted:
        return DEFAULT_LANG
    try:
        done = runner.run([LOCALE, "-a"], timeout=QUERY_TIMEOUT, check=False,
                          read_only=True)
    except Exception:
        return DEFAULT_LANG
    if done.returncode != 0:
        return DEFAULT_LANG
    listed = {}
    for line in _text(done.stdout).splitlines():
        if line.strip():
            listed.setdefault(_locale_key(line), line.strip())
    for value in wanted:
        if _locale_key(value) in listed:
            return listed[_locale_key(value)]
    return DEFAULT_LANG


def environment(target, agent: Agent, *, brew_prefix, lang: str) -> dict[str, str]:
    """The plist's EnvironmentVariables.

    XDG_* only when the Target's value is not the default under the home, so
    the agents agree with the shell that installed them. Never TMUX or
    TMUX_TMPDIR: the agents' tmux must be the user's default server, the one
    a terminal's tmux finds. Never the scripts' test seams either.
    """

    env = {"PATH": launch_path(target, brew_prefix), "LANG": lang}
    home = target.home
    for name, value, default in (
            ("XDG_DATA_HOME", target.data_home, home / ".local" / "share"),
            ("XDG_STATE_HOME", target.state_home, home / ".local" / "state"),
            ("XDG_CONFIG_HOME", target.config_home, home / ".config"),
            ("XDG_CACHE_HOME", target.cache_home, home / ".cache")):
        if Path(value) != default:
            env[name] = str(value)
    # The scripts rotate their own log (launchd has no rotation).
    env["PERSONAL_DOTFILES_LAUNCHD_LOG"] = str(log_path(target, agent))
    return env


def _agent_keys(agent: Agent) -> dict:
    if agent.name == TMUX.name:
        return {
            # Once per load, no KeepAlive: the script leaves once the server
            # runs; relaunching it would fight an intentional kill-server.
            "RunAtLoad": True,
            # Unspecified would throttle CPU and I/O for the server and, by
            # inheritance, for every pane.
            "ProcessType": "Interactive",
            # tmux daemonizes; launchd must not kill what stays behind.
            "AbandonProcessGroup": True,
        }
    return {
        "StartInterval": AUTOSAVE_INTERVAL,
        "RunAtLoad": False,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "Umask": 0o077,
    }


def render(agent: Agent, target, *, brew_prefix, lang: str) -> bytes:
    """The agent's plist: XML, keys sorted, so a Target always gives the
    same bytes. plistlib escapes '&', '<' and '>' and writes UTF-8."""

    log = str(log_path(target, agent))
    payload = {
        "Label": agent.label,
        "ProgramArguments": [str(program_path(target, agent))],
        "EnvironmentVariables": environment(target, agent, brew_prefix=brew_prefix,
                                            lang=lang),
        # Only in a GUI (Aqua) login: the pasteboard, the Keychain and the
        # ssh-agent live there.
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "WorkingDirectory": str(target.home),
        **_agent_keys(agent),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def desired_entries(target, *, brew_prefix, lang: str) -> list:
    """Both plists as transaction entries: real files (launchd refuses a
    plist others can write; a symlink is no plist it owns), mode 0644."""

    return [DesiredEntry(agent.entry_id, plist_path(target, agent), "file",
                         content=render(agent, target, brew_prefix=brew_prefix, lang=lang),
                         mode=0o644)
            for agent in AGENTS]


def agent_env(target, agent: Agent = TMUX, base_env=None) -> dict[str, str]:
    """What the agent's programs run with, for running things as they do
    (tmux has-session, the AI hook self-test): launchd's login variables
    from ``base_env`` and the installed plist's EnvironmentVariables. Never
    TMUX, TMUX_PANE or TMUX_TMPDIR, as for the agent."""

    base = os.environ if base_env is None else base_env
    env = {name: base[name] for name in ("SHELL", "TMPDIR") if base.get(name)}
    env.update(HOME=str(target.home), USER=target.username, LOGNAME=target.username,
               PATH=launch_path(target, None))
    try:
        with open(plist_path(target, agent), "rb") as handle:
            declared = plistlib.load(handle).get("EnvironmentVariables")
    except Exception:  # missing, unreadable, or edited into something else
        declared = None
    if isinstance(declared, dict):
        env.update({str(k): str(v) for k, v in declared.items()})
    env.pop("PERSONAL_DOTFILES_LAUNCHD_LOG", None)
    return env


# --- launchctl ------------------------------------------------------------------

def domain(uid: int) -> str:
    return f"gui/{uid}"


def service_target(uid: int, label: str) -> str:
    return f"gui/{uid}/{label}"


def error_text(code: int) -> str:
    known = ERROR_TEXT.get(code)
    return f"{code}: {known}" if known else f"{code}; see: launchctl error {code}"


def _launchctl(runner, *args: str, read_only: bool, timeout: float):
    return runner.run([LAUNCHCTL, *args], timeout=timeout, check=False,
                      read_only=read_only)


def gui_domain_available(runner, uid: int) -> bool:
    """Whether this user has a GUI login domain here: not over SSH without
    a desktop login, not in a sandbox without one."""

    try:
        done = _launchctl(runner, "print", domain(uid), read_only=True,
                          timeout=QUERY_TIMEOUT)
    except Exception:
        return False
    return done.returncode == 0


_DISABLED_LINE = re.compile(r'^\s*"(?P<label>[^"]+)"\s*=>\s*(?P<value>[A-Za-z]+)\s*$')


def parse_disabled(text: str) -> dict[str, bool]:
    """``print-disabled`` lines ``"label" => disabled|enabled`` (older
    releases: ``=> true|false``, true meaning disabled)."""

    states: dict[str, bool] = {}
    for line in text.splitlines():
        match = _DISABLED_LINE.match(line)
        if match is None:
            continue
        value = match.group("value").lower()
        if value in ("disabled", "true"):
            states[match.group("label")] = True
        elif value in ("enabled", "false"):
            states[match.group("label")] = False
    return states


def disabled_labels(runner, uid: int) -> dict[str, bool] | None:
    """label -> disabled, from ``launchctl print-disabled gui/<uid>``; None
    when it cannot be read. A label it does not list is enabled."""

    try:
        done = _launchctl(runner, "print-disabled", domain(uid), read_only=True,
                          timeout=QUERY_TIMEOUT)
    except Exception:
        return None
    if done.returncode != 0:
        return None
    return parse_disabled(_text(done.stdout))


@dataclasses.dataclass
class ServiceInfo:
    """What ``launchctl print gui/<uid>/<label>`` says; None for anything
    it does not say (its format is not an interface)."""

    state: str | None = None
    pid: int | None = None
    last_exit_code: int | None = None  # None: never exited (or not said)
    last_signal: str | None = None
    run_interval: int | None = None
    path: str | None = None
    program: str | None = None
    arguments: list[str] = dataclasses.field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.state == "running" or self.pid is not None

    @property
    def exited(self) -> bool:
        """Not running, and a finished run is on record."""

        return not self.running and (self.last_exit_code is not None
                                     or self.last_signal is not None)


_PROPERTY = re.compile(r"^(?P<key>[a-z][a-z ]*?) = (?P<value>.*)$")


def parse_service(text: str) -> ServiceInfo:
    """The service's own properties: the lines directly inside its outer
    braces (nested blocks such as environment or endpoints are skipped),
    plus the ``arguments`` block."""

    props: dict[str, str] = {}
    arguments: list[str] = []
    depth = 0
    in_arguments = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line in ("}", "};"):
            depth = max(0, depth - 1)
            if depth < 2:
                in_arguments = False
            continue
        opens = line.endswith("{")
        if depth == 1:
            match = _PROPERTY.match(line)
            if match is not None:
                if opens:
                    in_arguments = match.group("key") == "arguments"
                else:
                    props.setdefault(match.group("key"), match.group("value").strip())
        elif depth == 2 and in_arguments and not opens:
            arguments.append(line)
        if opens:
            depth += 1
    info = ServiceInfo(arguments=arguments)
    info.state = props.get("state") or None
    pid = props.get("pid", "")
    info.pid = int(pid) if pid.isdigit() else None
    # "0", "78: EX_CONFIG", "(never exited)"
    code = re.match(r"^(-?[0-9]+)\b", props.get("last exit code", ""))
    info.last_exit_code = int(code.group(1)) if code else None
    info.last_signal = props.get("last terminating signal") or None
    interval = re.match(r"^([0-9]+) seconds?$", props.get("run interval", ""))
    info.run_interval = int(interval.group(1)) if interval else None
    info.path = props.get("path") or None
    info.program = props.get("program") or None
    return info


def service_info(runner, uid: int, label: str) -> ServiceInfo | None:
    """The loaded service, or None when it is not loaded (print fails)."""

    try:
        done = _launchctl(runner, "print", service_target(uid, label), read_only=True,
                          timeout=QUERY_TIMEOUT)
    except Exception:
        return None
    if done.returncode != 0:
        return None
    return parse_service(_text(done.stdout))


def bootstrap(runner, uid: int, plist: Path):
    return _launchctl(runner, "bootstrap", domain(uid), str(plist), read_only=False,
                      timeout=CHANGE_TIMEOUT)


def bootout(runner, uid: int, label: str):
    return _launchctl(runner, "bootout", service_target(uid, label), read_only=False,
                      timeout=CHANGE_TIMEOUT)


def enable(runner, uid: int, label: str):
    return _launchctl(runner, "enable", service_target(uid, label), read_only=False,
                      timeout=CHANGE_TIMEOUT)


def disable(runner, uid: int, label: str):
    return _launchctl(runner, "disable", service_target(uid, label), read_only=False,
                      timeout=CHANGE_TIMEOUT)


def kickstart(runner, uid: int, label: str, *, kill: bool = False):
    args = ["kickstart", *(["-k"] if kill else []), service_target(uid, label)]
    return _launchctl(runner, *args, read_only=False, timeout=CHANGE_TIMEOUT)


# --- registration -----------------------------------------------------------------

@dataclasses.dataclass
class AgentResult:
    status: str
    reasons: list[str]
    details: dict


def bootstrap_command(plist: Path) -> str:
    """The command a user runs in a Terminal of the desktop session."""

    return f"launchctl bootstrap gui/$(id -u) {shlex.quote(str(plist))}"


def bootout_command(label: str) -> str:
    return f"launchctl bootout gui/$(id -u)/{label}"


def _same_path(a: str, b: str) -> bool:
    return a == b or os.path.realpath(a) == os.path.realpath(b)


def _stale(info: ServiceInfo, target, agent: Agent, plist: Path) -> str | None:
    """Why the loaded service is not this plist's, or None. Only a definite
    difference counts; what print does not say is not a difference."""

    if info.path and not _same_path(info.path, str(plist)):
        return f"loaded from {info.path}"
    program = info.program or (info.arguments[0] if info.arguments else None)
    expected = str(program_path(target, agent))
    if program and not _same_path(program, expected):
        return f"runs {program}"
    if (agent.name == AUTOSAVE.name and info.run_interval is not None
            and info.run_interval != AUTOSAVE_INTERVAL):
        return f"runs every {info.run_interval} s"
    return None


def _describe(info: ServiceInfo, details: dict) -> None:
    details.update(state=info.state, pid=info.pid, last_exit_code=info.last_exit_code,
                   run_interval=info.run_interval)
    if info.last_signal:
        details["last_signal"] = info.last_signal


def _bootout_and_wait(runner, uid: int, agent: Agent) -> str | None:
    """Unload the agent and wait until launchd has let it go; the problem,
    or None."""

    done = bootout(runner, uid, agent.label)
    code = done.returncode
    if code not in (0, IN_PROGRESS, *NOT_LOADED):
        return (f"launchctl bootout {service_target(uid, agent.label)} failed "
                f"({error_text(code)}){': ' + _tail(done.stderr) if _tail(done.stderr) else ''}; "
                f"run: {bootout_command(agent.label)}")
    deadline = _monotonic() + BOOTOUT_WAIT
    while service_info(runner, uid, agent.label) is not None:
        if _monotonic() >= deadline:
            return (f"{agent.label} was still loaded {BOOTOUT_WAIT:g} s after launchctl "
                    f"bootout; run: {bootout_command(agent.label)}")
        _sleep(POLL_INTERVAL)
    return None


def _bootstrap_failure(code: int, stderr, plist: Path) -> str:
    text = f"launchctl bootstrap failed ({error_text(code)})"
    tail = _tail(stderr)
    if tail:
        text += f": {tail}"
    if code in ALREADY_LOADED:
        text += f"; {BACKGROUND_HINT}"
    elif code == 122:
        text += f"; {plist} must belong to you and be writable only by you (chmod 644)"
    elif code == 134:
        text += "; run the install from Terminal in your desktop session"
    return f"{text}; then run: {bootstrap_command(plist)}"


def _await_tmux_agent(runner, uid: int, agent: Agent, env: dict,
                      details: dict) -> tuple[str, list[str]]:
    """Read how the tmux agent's run ended, or that it still runs.

    tmux-launchd-start: 0 restored, nothing to restore or a server was
    already running; 3 a snapshot exists but was not restored (the server
    runs; the log says why and how to recover); 1 tmux missing; anything
    else is new-session's failure. It may keep running for minutes while a
    restore runs; that is not a failure.
    """

    log = details["log"]
    deadline = _monotonic() + TMUX_START_WAIT
    while True:
        info = service_info(runner, uid, agent.label)
        if info is None:
            return FAIL, [f"{agent.label} is no longer loaded after it started; see {log}"]
        if info.exited or _monotonic() >= deadline:
            break
        _sleep(POLL_INTERVAL)
    _describe(info, details)
    if info.exited:
        code = info.last_exit_code
        if code == 0:
            details["outcome"] = "started"
            return PASS, []
        if code == 3:
            details["outcome"] = "not-restored"
            return PASS, [f"tmux started but did not restore the last snapshot (see {log}); "
                          "press prefix + C-r in tmux to restore it"]
        details["outcome"] = "failed"
        if code is None:
            return FAIL, [f"the tmux LaunchAgent was stopped ({info.last_signal}); see {log}"]
        if code == 1:
            return FAIL, [f"the tmux LaunchAgent could not start tmux (exit status 1: tmux "
                          f"not found, or HOME unusable); see {log}"]
        return FAIL, [f"the tmux LaunchAgent failed (exit status {code}); see {log}"]
    # Still running: restoring, or slow. Its server answering is the start.
    tmux = runner.which("tmux", path=env.get("PATH"))
    if tmux is not None:
        try:
            answered = runner.run([tmux, "has-session"], timeout=TMUX_QUERY_TIMEOUT,
                                  check=False, env=env, read_only=True).returncode == 0
        except Exception:
            answered = False
        if answered:
            details["outcome"] = "running"
            return PASS, []
    details["outcome"] = "starting"
    return PASS, [f"the tmux LaunchAgent is still starting after {TMUX_START_WAIT:g} s; "
                  f"see {log}"]


def register(target, runner, agent: Agent, *, changed: bool,
             env: dict | None = None) -> AgentResult:
    """Load ``agent`` into ``gui/<uid>`` and verify it, honestly.

    ``changed``: the transaction rewrote its plist in this run, so a loaded
    copy is stale and is reloaded (bootout, wait until gone, bootstrap). An
    unchanged plist whose service is loaded is left alone: a repair that
    changes nothing runs no bootout and no bootstrap (unless print proves
    the loaded service is another definition). An agent the user disabled
    (`dotfiles services disable`) stays off: SKIPPED. Without a GUI login
    domain (SSH, a sandbox) nothing can load now; the plist loads at the
    next desktop login: RELOGIN_REQUIRED. Every other refusal is FAIL with
    the code, launchd's words and the command to run.

    ``env`` is the installer's environment, for running tmux as the agent
    does (its own PATH and LANG come from the installed plist). A launchctl
    that cannot run or hangs is FAIL too, never an exception.
    """

    details = {"label": agent.label, "plist": str(plist_path(target, agent)),
               "log": str(log_path(target, agent))}
    try:
        return _register(target, runner, agent, changed, env, details)
    except Exception as exc:  # launchctl could not run, or timed out
        details["action"] = "failed"
        return AgentResult(FAIL, [f"{agent.name}: {type(exc).__name__}: {exc}; run: "
                                  f"{bootstrap_command(plist_path(target, agent))}"],
                           details)


def _register(target, runner, agent: Agent, changed: bool, env, details: dict
              ) -> AgentResult:
    uid = target.uid
    plist = plist_path(target, agent)
    if not plist.is_file():
        details["action"] = "missing"
        return AgentResult(FAIL, [f"{plist} is missing; run: dotfiles repair"], details)
    if not gui_domain_available(runner, uid):
        details.update(action="pending-login", command=bootstrap_command(plist))
        return AgentResult(RELOGIN_REQUIRED, [LAUNCHD_GUI_DOMAIN_UNAVAILABLE], details)
    disabled = disabled_labels(runner, uid)
    if disabled and disabled.get(agent.label):
        details["action"] = "disabled"
        return AgentResult(SKIPPED, [f"{agent.name}: disabled (enable: dotfiles services "
                                     f"enable {agent.name})"], details)

    info = service_info(runner, uid, agent.label)
    if info is not None:
        why = "changed" if changed else _stale(info, target, agent, plist)
        if why is None:
            details["action"] = "unchanged"
            _describe(info, details)
            return AgentResult(PASS, [], details)
        details["reloaded"] = why
        problem = _bootout_and_wait(runner, uid, agent)
        if problem:
            details["action"] = "failed"
            return AgentResult(FAIL, [problem], details)
        details["action"] = "reloaded"
    else:
        details["action"] = "bootstrapped"

    done = bootstrap(runner, uid, plist)
    code = done.returncode
    if code in NO_GUI_DOMAIN:
        details.update(action="pending-login", command=bootstrap_command(plist))
        return AgentResult(RELOGIN_REQUIRED, [LAUNCHD_GUI_DOMAIN_UNAVAILABLE], details)
    if code == DISABLED:
        details["action"] = "disabled"
        return AgentResult(SKIPPED, [f"{agent.name}: disabled (enable: dotfiles services "
                                     f"enable {agent.name})"], details)
    info = service_info(runner, uid, agent.label)
    if code != 0 and not (code in ALREADY_LOADED and info is not None):
        details.update(action="failed", code=code)
        return AgentResult(FAIL, [f"{agent.name}: "
                                  + _bootstrap_failure(code, done.stderr, plist)], details)
    if info is None:
        details["action"] = "failed"
        return AgentResult(FAIL, [f"{agent.name}: launchctl bootstrap returned {code} but "
                                  f"launchctl print does not find {agent.label}; "
                                  f"{BACKGROUND_HINT}; then run: {bootstrap_command(plist)}"],
                           details)

    if agent.name == TMUX.name:
        status, reasons = _await_tmux_agent(runner, uid, agent, agent_env(target, agent, env),
                                            details)
        return AgentResult(status, reasons, details)
    _describe(info, details)
    if info.run_interval is not None and info.run_interval != AUTOSAVE_INTERVAL:
        return AgentResult(FAIL, [f"{agent.name}: launchd runs {agent.label} every "
                                  f"{info.run_interval} s, not {AUTOSAVE_INTERVAL} s; run: "
                                  f"{bootout_command(agent.label)} && "
                                  f"{bootstrap_command(plist)}"], details)
    return AgentResult(PASS, [], details)


def unload(target, runner, labels, *, outcomes: dict | None = None) -> list[str]:
    """``launchctl bootout`` each label; the problems (an empty list when
    all are gone). ``outcomes`` gets label -> unloaded | not-loaded |
    no-gui-domain | failed."""

    uid = target.uid
    problems: list[str] = []
    for label in labels:
        try:
            done = bootout(runner, uid, label)
        except Exception as exc:
            problems.append(f"launchctl bootout {service_target(uid, label)}: {exc}; "
                            f"run: {bootout_command(label)}")
            outcome = "failed"
        else:
            code = done.returncode
            if code == 0:
                outcome = "unloaded"
            elif code in NOT_LOADED:
                outcome = "not-loaded"
            elif code in NO_GUI_DOMAIN:
                outcome = "no-gui-domain"  # nothing is loaded without one
            else:
                outcome = "failed"
                tail = _tail(done.stderr)
                problems.append(f"launchctl bootout {service_target(uid, label)} failed "
                                f"({error_text(code)}){': ' + tail if tail else ''}; "
                                f"run: {bootout_command(label)}")
        if outcomes is not None:
            outcomes[label] = outcome
    return problems


def last_save_path(target) -> Path:
    """Touched by tmux/resurrect-save after every verified save."""

    return target.data_home / "tmux" / "resurrect" / "last-successful-save"


def status(target, runner, *, now=None) -> dict:
    """Read-only view of both agents and of the last verified save."""

    uid = target.uid
    gui = gui_domain_available(runner, uid)
    disabled = disabled_labels(runner, uid)
    agents = {}
    for agent in AGENTS:
        plist = plist_path(target, agent)
        info = service_info(runner, uid, agent.label) if gui else None
        entry = {
            "label": agent.label,
            "plist": str(plist),
            "plist_present": plist.is_file(),
            # Without a GUI domain nothing can be loaded, so it cannot tell.
            "loaded": (info is not None) if gui else None,
            "disabled": None if disabled is None else bool(disabled.get(agent.label)),
            "state": None, "pid": None, "last_exit_code": None, "run_interval": None,
            "log": str(log_path(target, agent)),
        }
        if info is not None:
            _describe(info, entry)
        agents[agent.name] = entry
    saved = last_save_path(target)
    try:
        age = max(0, int((time.time() if now is None else now) - saved.stat().st_mtime))
    except OSError:
        age = None
    return {"manager": "launchd", "domain": domain(uid), "gui_domain": gui,
            "agents": agents, "last_successful_save": {"path": str(saved),
                                                       "age_seconds": age}}


def _agent_line(name: str, entry: dict) -> str:
    if not entry["plist_present"]:
        what = "not installed (no plist; run: dotfiles repair)"
    elif entry["disabled"]:
        what = f"disabled (enable: dotfiles services enable {name})"
    elif entry["loaded"] is None:
        what = "installed; loads at the next desktop login"
    elif not entry["loaded"]:
        what = f"not loaded (load: dotfiles services enable {name})"
    else:
        parts = ["loaded"]
        if entry["pid"] is not None:
            parts.append(f"running (pid {entry['pid']})")
        elif entry["state"]:
            parts.append(entry["state"])
        if entry["run_interval"] is not None:
            parts.append(f"every {entry['run_interval']} s")
        if entry["last_exit_code"] is not None:
            parts.append(f"last exit {entry['last_exit_code']}")
        what = ", ".join(parts)
    return what


def describe_status(view: dict) -> list[str]:
    """Text lines for ``status`` and ``services status``."""

    head = f"services:  launchd {view['domain']}"
    if not view["gui_domain"]:
        head += " (no GUI login session here; agents load at the next desktop login)"
    lines = [head]
    for name, entry in view["agents"].items():
        lines.append(f"  {name:<10} {_agent_line(name, entry)}")
        lines.append(f"  {'':<10} log: {entry['log']}")
    age = view["last_successful_save"]["age_seconds"]
    lines.append("  last save: " + ("never" if age is None else f"{age} s ago"))
    return lines


# --- tmux-continuum's own launcher --------------------------------------------

def retire_continuum_launcher(target, runner, run_id: str) -> dict | None:
    """Unload and set aside ~/Library/LaunchAgents/Tmux.Start.plist.

    tmux-continuum writes it for @continuum-boot 'on' (forced 'off' here on
    macOS) and only ever deletes it, without launchctl; one left from an
    earlier setup would open a terminal and type tmux at the next login, a
    second launcher next to the tmux agent. It is moved, not deleted, to
    {state}/personal-dotfiles/backups/launchd/<run id>/ (0700). None when
    there is none; otherwise what happened, with ``error`` when it could not
    be moved.
    """

    path = target.home / "Library" / "LaunchAgents" / CONTINUUM_PLIST
    if not os.path.lexists(path):
        return None
    outcomes: dict[str, str] = {}
    problems = unload(target, runner, [CONTINUUM_LABEL], outcomes=outcomes)
    report = {"plist": str(path), "bootout": outcomes.get(CONTINUUM_LABEL, "failed")}
    if problems:
        report["bootout_error"] = problems[0]
    folder = target.state_root / "backups" / "launchd" / run_id
    try:
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder.parent, 0o700)
        os.chmod(folder, 0o700)
        dest = folder / CONTINUUM_PLIST
        shutil.move(str(path), str(dest))
    except OSError as exc:
        report["error"] = f"could not move {path} to {folder}: {exc}"
    else:
        report["moved_to"] = str(dest)
    return report


__all__ = [
    "AGENTS", "AUTOSAVE", "Agent", "AgentResult", "ENTRY_IDS", "LABEL_AUTOSAVE",
    "LABEL_TMUX", "ServiceInfo", "TMUX", "agent_env", "agent_named", "bootout",
    "bootstrap", "describe_status", "desired_entries", "disable", "disabled_labels",
    "enable", "environment", "gui_domain_available", "kickstart", "launch_path",
    "log_dir", "log_path", "parse_disabled", "parse_service", "pick_lang", "plist_path",
    "prepare_log_dir", "register", "render", "retire_continuum_launcher",
    "service_info", "status", "unload",
]
