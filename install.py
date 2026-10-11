#!/usr/bin/env python3
# -*- coding: utf-8 -*-

'''
   @leekyungmoon's          ███████╗██╗██╗     ███████╗███████╗
   ██████╗  █████╗ ████████╗██╔════╝██║██║     ██╔════╝██╔════╝
   ██╔══██╗██╔══██╗╚══██╔══╝█████╗  ██║██║     █████╗  ███████╗
   ██║  ██║██║  ██║   ██║   ██╔══╝  ██║██║     ██╔══╝  ╚════██║
   ██████╔╝╚█████╔╝   ██║   ██║     ██║███████╗███████╗███████║
   ╚═════╝  ╚════╝    ╚═╝   ╚═╝     ╚═╝╚══════╝╚══════╝╚══════╝

   https://github.com/leekyungmoon/dotfiles
'''

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # keep the checkout free of __pycache__

import argparse  # noqa: E402
import dataclasses  # noqa: E402
import hashlib  # noqa: E402
import importlib  # noqa: E402
import inspect  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pwd  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from installer import phases  # noqa: E402
from installer import platform as plat  # noqa: E402
from installer import repo  # noqa: E402
from installer import ui  # noqa: E402
from installer.phases import FAIL, PASS, SKIPPED, PhaseResult  # noqa: E402

MIN_PYTHON = (3, 10)

USAGE = """\
Install the dotfiles from this checkout, which must be ~/.dotfiles:

    git clone --recursive https://github.com/leekyungmoon/dotfiles.git ~/.dotfiles && ~/.dotfiles/install

    python3 install.py [-f] [--skip-vimplug] [--skip-zplug] [--no-packages]
                       [--no-gui] [--no-shell-change] [--dry-run]
    python3 install.py status [--json]
    python3 install.py restore (--baseline | --run RUN_ID) [--force] [--id ID ...]
    python3 install.py repair
    python3 install.py gui-apply [--autostart]
    python3 install.py services [status|enable|disable] [tmux|autosave ...]
"""

SUBCOMMANDS = ("install", "status", "restore", "repair", "gui-apply", "packages",
               "services")
# Commands that act on the checkout and so must run from ~/.dotfiles.
LOCATED_COMMANDS = ("install", "repair", "gui-apply", "packages")
# Commands that work from anywhere on any host: no platform is detected.
UNLOCATED_COMMANDS = ("status", "restore", "services")
# `services`: the LaunchAgents on macOS (installer/launchd.py names them the
# same), the systemd units on Linux.
SERVICE_ACTIONS = ("status", "enable", "disable")
SERVICE_NAMES = ("tmux", "autosave")
SYSTEMD_UNITS = {"tmux": "tmux.service", "autosave": "tmux-resurrect-autosave.timer"}
# Entries managed by the pre-release layout that promoted a staged checkout.
LEGACY_IDS = ("repo", "dotfiles-compat")
# ~/.local/bin links requested by the packages phase (installer/packages.py).
TOOL_LINK_PREFIX = "tool-link-"
# macOS LaunchAgent plists, rendered by installer/launchd.py (no source file).
LAUNCH_AGENT_PREFIX = "launchd-"


@dataclasses.dataclass
class Options:
    force: bool = False
    skip_vimplug: bool = False
    skip_zplug: bool = False
    no_packages: bool = False
    no_gui: bool = False
    no_shell_change: bool = False
    dry_run: bool = False
    # Tri-state: True/False from --[no-]claude-code / --[no-]codex, None = ask.
    claude_code: bool | None = None
    codex: bool | None = None


@dataclasses.dataclass
class Context:
    target: plat.Target
    platform: plat.Platform | None
    runner: object
    env: dict
    run_id: str
    current_shell: str = ""
    interactive: object = None  # callable(argv) -> returncode, for chsh
    repo_root: Path | None = None  # the checkout; defaults to target.repo_root
    prompt: object = None  # callable(question) -> str | None, for git identity
    conditions: dict | None = None  # condition_facts(), computed once per run

    def __post_init__(self) -> None:
        if self.repo_root is None:
            self.repo_root = self.target.repo_root


# --- platform -------------------------------------------------------------------

# The GNOME settings, input-remapper and their login autostart entry exist only
# on Linux; installer/gui.py reports the same reason.
GUI_NOT_APPLICABLE = "not-applicable-on-macos"


def platform_system(ctx: Context) -> str:
    """``linux`` or ``darwin``: the detected platform's, or, for status and
    restore (which detect none), the kernel's."""

    if ctx.platform is not None:
        return ctx.platform.system
    return plat.current_system()


def condition_facts(ctx: Context) -> dict[str, bool]:
    """Whether each manifest ``condition`` holds for this run.

    Computed once per run (kept in ``ctx.conditions``), so the transaction,
    --dry-run and the tool links never disagree. systemd user managers are
    only looked for on Linux (on macOS launchd plays their part).
    """

    if ctx.conditions is None:
        system = platform_system(ctx)
        linux = system == "linux"
        ctx.conditions = {
            "always": True,
            "linux": linux,
            "darwin": system == "darwin",
            "systemd-user": linux and phases.systemd_user_supported(ctx.runner),
        }
    return dict(ctx.conditions)


def _condition_skips(resolved: list, facts: dict) -> tuple[list[str], list[str]]:
    """``(skipped, not_applicable)`` ids of the entries left out by ``facts``.

    ``skipped`` are the systemd-user entries of a Linux machine without a
    systemd user manager: a degraded install, reported entry by entry as it
    always was. ``not_applicable`` are entries for another platform.
    """

    from installer import transaction

    left_out = set(transaction.skipped_by_condition(resolved, conditions=facts))
    skipped, not_applicable = [], []
    for entry in resolved:
        if entry.id not in left_out:
            continue
        if entry.condition == "systemd-user" and facts.get("linux"):
            skipped.append(entry.id)
        else:
            not_applicable.append(entry.id)
    return skipped, not_applicable


def _report_not_applicable(ctx: Context, resolved: list, not_applicable: list[str],
                           details: dict) -> None:
    """Name the other platform's entries once per kind, never entry by entry.

    Only macOS has any today (the systemd units and the Linux desktop files);
    a macOS-only entry on Linux is left out silently, so the Linux output and
    status.json stay what they were.
    """

    if not not_applicable or platform_system(ctx) != "darwin":
        return
    details["not_applicable_ids"] = sorted(not_applicable)
    by_condition: dict[str, list[str]] = {}
    for entry in resolved:
        if entry.id in not_applicable:
            by_condition.setdefault(entry.condition, []).append(entry.id)
    for condition, ids in sorted(by_condition.items()):
        if condition == "systemd-user":
            ui.log_target("systemd user units",
                          ui.GRAY("not applicable on macOS, which uses launchd"))
        elif condition == "linux":
            ui.log_target("Linux-only files",
                          ui.GRAY(f"not applicable on macOS ({', '.join(sorted(ids))})"))
        else:
            ui.log_target(f"{condition} entries",
                          ui.GRAY(f"not applicable here ({', '.join(sorted(ids))})"))


def _accepts(function, name: str) -> bool:
    """Whether ``function`` takes a ``name`` keyword (seams written before
    that keyword existed, or test doubles, may not)."""

    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    keyword = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    return any((p.name == name and p.kind in keyword)
               or p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters)


# --- argument parsing ---------------------------------------------------------

def _install_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("-f", "--force", action="store_true", default=False,
                   help="also overwrite managed copied files you changed "
                        "locally (every replaced target is backed up first)")
    p.add_argument("--skip-vimplug", action="store_true",
                   help="do not install or update neovim plugins")
    p.add_argument("--skip-zplug", action="store_true",
                   help="do not install or update zsh plugins")
    p.add_argument("--no-packages", action="store_true",
                   help="skip apt packages and pinned tools")
    p.add_argument("--no-gui", action="store_true",
                   help="skip desktop (GNOME) settings")
    p.add_argument("--no-shell-change", action="store_true",
                   help="do not run chsh to make zsh the login shell")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would change without changing anything")
    p.add_argument("--claude-code", action=argparse.BooleanOptionalAction, default=None,
                   help="install Claude Code without asking (--no-claude-code: skip it)")
    p.add_argument("--codex", action=argparse.BooleanOptionalAction, default=None,
                   help="install the Codex CLI and oh-my-codex without asking "
                        "(--no-codex: skip them)")
    p.add_argument("--allow-any-location", action="store_true",
                   help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install.py", description=USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("install", help="install (the default command)")
    _install_flags(p)

    p = sub.add_parser("repair", help="reapply this checkout (same as install)")
    _install_flags(p)

    p = sub.add_parser("status", help="show the installed generation and drift")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("restore", help="restore managed paths from backups")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--baseline", action="store_true",
                       help="restore the state before the first install")
    which.add_argument("--run", metavar="RUN_ID",
                       help="restore the state before the given run")
    p.add_argument("-f", "--force", action="store_true",
                   help="also restore paths that drifted since the last install")
    p.add_argument("--id", dest="ids", action="append", metavar="ID",
                   help="restrict to this managed entry id (repeatable)")

    p = sub.add_parser("gui-apply", help="apply desktop settings in this session")
    p.add_argument("--autostart", action="store_true",
                   help="invoked from the login autostart entry")
    p.add_argument("--allow-any-location", action="store_true",
                   help=argparse.SUPPRESS)

    p = sub.add_parser("services", help="show, enable or disable starting tmux at "
                                        "login and the one-minute autosave")
    p.add_argument("words", nargs="*", metavar="[ACTION] [NAME ...]",
                   help=f"ACTION: {', '.join(SERVICE_ACTIONS)} (default status); "
                        f"NAME: {', '.join(SERVICE_NAMES)} (default both)")

    # Internal: used by 'dotfiles install <name>'.
    p = sub.add_parser("packages")  # no help=: not listed in --help
    p.add_argument("--only", action="append", required=True, metavar="NAME")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-f", "--force", action="store_true")
    p.add_argument("--allow-any-location", action="store_true",
                   help=argparse.SUPPRESS)
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """``install.py [flags]`` means ``install.py install [flags]``."""

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in SUBCOMMANDS and argv[0].startswith("-")
                    and argv[0] not in ("-h", "--help")):
        argv = ["install", *argv]
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "services":
        _service_words(parser, args)
    return args


def _service_words(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """``services [ACTION] [NAME ...]`` into ``args.action`` and ``args.names``:
    the action defaults to status (so ``services tmux`` shows tmux), the names
    to both agents."""

    words = list(args.words)
    action = words.pop(0) if words and words[0] in SERVICE_ACTIONS else "status"
    names: list[str] = []
    for word in words:
        if word not in SERVICE_NAMES:
            parser.error(f"services: unknown name {word!r} (choose from "
                         f"{', '.join(SERVICE_NAMES)}; the action comes first)")
        if word not in names:
            names.append(word)
    args.action, args.names = action, names or list(SERVICE_NAMES)


_TRISTATE = ("claude_code", "codex")


def options_from(args: argparse.Namespace) -> Options:
    values = {}
    for field in dataclasses.fields(Options):
        value = getattr(args, field.name, None if field.name in _TRISTATE else False)
        values[field.name] = value if field.name in _TRISTATE and value is None else bool(value)
    return Options(**values)


# --- optional AI CLIs ----------------------------------------------------------

# (option name, tool id in manifests/tools.json, label, tools that come with it)
AI_CLIS = (
    ("claude_code", "claude-code", "Claude Code", ()),
    ("codex", "codex", "Codex CLI", ("oh-my-codex",)),
)


def _choices_path(target) -> Path:
    return target.state_root / "choices.json"


def _load_choices(target) -> dict:
    try:
        data = json.loads(_choices_path(target).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_choices(target, choices: dict) -> None:
    path = _choices_path(target)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(choices, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def _yes(answer: str | None, default: bool = True) -> bool:
    answer = (answer or "").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def ai_cli_exclusions(ctx: Context, opts: Options, *, record: bool = True) -> list[str]:
    """Ask once per AI CLI whether to install it; return the tool ids to skip.

    A --[no-]claude-code / --[no-]codex flag wins, then the answer given on an
    earlier run (so `dotfiles update` does not ask again), then a Y/n question
    on the terminal. Without a terminal and without an earlier answer the CLI
    is installed, as before. Declining never uninstalls anything.
    """

    choices = _load_choices(ctx.target)
    changed = False
    exclude: list[str] = []
    for option, tool_id, label, companions in AI_CLIS:
        wanted = getattr(opts, option, None)
        if wanted is not None:
            if choices.get(tool_id) != wanted:
                choices[tool_id], changed = wanted, True
        elif isinstance(choices.get(tool_id), bool):
            wanted = choices[tool_id]
        elif ctx.prompt is not None:
            wanted = _yes(ctx.prompt(f"Install {label}? [Y/n] "))
            choices[tool_id], changed = wanted, True
        else:
            wanted = True
        if not wanted:
            exclude += [tool_id, *companions]
            if platform_system(ctx) == "darwin":
                # python3 there is usually the Command Line Tools' 3.9, which
                # install.py refuses; the wrapper runs it with Homebrew's.
                later = f"`~/.dotfiles/install --{option.replace('_', '-')}` "
            else:
                later = (
                    f"`python3 ~/.dotfiles/install.py --{option.replace('_', '-')}` "
                )
            ui.log_target(label, ui.GRAY("skipped (your choice; " + later
                                         + "installs it later)"))
    if changed and record:
        try:
            _save_choices(ctx.target, choices)
        except OSError as exc:
            ui.log(ui.YELLOW(f"could not remember the AI CLI choices: {exc}"))
    return exclude


# --- seams --------------------------------------------------------------------

def load_seam(module_name: str, function_name: str):
    """Return ``installer.<module>.<function>`` or None when not provided."""

    try:
        module = importlib.import_module(f"installer.{module_name}")
    except ModuleNotFoundError as exc:
        if exc.name in (f"installer.{module_name}", "installer"):
            return None
        raise
    function = getattr(module, function_name, None)
    return function if callable(function) else None


def packages_phase(ctx: Context, *, disabled: bool, dry_run: bool,
                   exclude: list[str] | None = None) -> PhaseResult:
    if disabled:
        return PhaseResult("packages", SKIPPED, ["--no-packages"])
    run = load_seam("packages", "run_packages_phase")
    if run is None:
        return PhaseResult("packages", SKIPPED, ["packages-phase-not-available"])
    kwargs = {"dry_run": dry_run}
    if exclude:
        try:
            if "exclude" in inspect.signature(run).parameters:
                kwargs["exclude"] = list(exclude)
        except (TypeError, ValueError):
            pass
    # The environment the run was started with (HOMEBREW_PREFIX, proxies),
    # not this process's: tests and callers hand in their own.
    if _accepts(run, "env"):
        kwargs["env"] = ctx.env
    try:
        return PhaseResult.coerce(
            run(ctx.target, ctx.platform, ctx.runner, **kwargs), "packages")
    except Exception as exc:
        return PhaseResult("packages", FAIL, [f"{type(exc).__name__}: {exc}"])


def gui_phase(ctx: Context, *, disabled: bool, autostart: bool = False) -> PhaseResult:
    # GNOME settings and input-remapper have no macOS counterpart; the module
    # is not even loaded there.
    if platform_system(ctx) == "darwin":
        return PhaseResult("gui", SKIPPED, [GUI_NOT_APPLICABLE])
    if disabled:
        return PhaseResult("gui", SKIPPED, ["--no-gui"])
    apply = load_seam("gui", "apply_or_defer")
    if apply is None:
        return PhaseResult("gui", SKIPPED, ["gui-phase-not-available"])
    env = dict(ctx.env)
    if autostart:
        env["PERSONAL_DOTFILES_GUI_AUTOSTART"] = "1"
    # The platform detected once in main(), so the module does not detect it
    # again from /etc/os-release.
    kwargs = {}
    if ctx.platform is not None and _accepts(apply, "platform"):
        kwargs["platform"] = ctx.platform
    try:
        return PhaseResult.coerce(apply(ctx.target, ctx.runner, env, **kwargs), "gui")
    except Exception as exc:
        return PhaseResult("gui", FAIL, [f"{type(exc).__name__}: {exc}"])


def extra_desired_entries(ctx: Context, packages_result: PhaseResult | None,
                          *, no_gui: bool, packages_complete: bool | None = None
                          ) -> tuple[list, list[str]]:
    """Transaction entries contributed by the packages and gui modules.

    Only a packages run over every tool (``packages_complete``; by default a
    run that neither was skipped nor failed) decides which ~/.local/bin tool
    links exist. After a skipped (--no-packages), failed or partial (--only)
    run the links installed before are kept instead of being retired.

    Returns ``(entries, warnings)``; a gui autostart entry that cannot be
    built is a warning (the GNOME section reports the gui state), while a
    broken package link request fails the transaction.
    """

    extra = []
    warnings: list[str] = []
    if packages_result is not None and packages_result.details.get("links"):
        link_requests = load_seam("packages", "link_requests")
        if link_requests is not None:
            extra += list(link_requests(packages_result.details))
    if packages_complete is None:
        packages_complete = (packages_result is not None
                             and packages_result.status not in (SKIPPED, FAIL))
    if not packages_complete:
        fresh = {e.id for e in extra}
        extra += [e for e in previous_tool_links(ctx) if e.id not in fresh]
    # The autostart entry runs 'install.py gui-apply' at a GNOME login, which
    # macOS never has: it is not installed there, and one managed anyway is
    # retired like any entry that is no longer wanted. macOS has the
    # LaunchAgents instead; every caller (also 'dotfiles install <tool>')
    # wants them, or the transaction would retire them.
    if platform_system(ctx) == "darwin":
        extra += launch_agent_entries(ctx)
        return extra, warnings
    # --no-gui skips applying desktop settings, but an autostart entry that an
    # earlier run installed stays managed instead of being retired.
    if no_gui:
        autostart_id = _gui_autostart_id()
        no_gui = not (autostart_id is not None and autostart_id in _managed_entries(ctx))
    if not no_gui:
        autostart = load_seam("gui", "autostart_desired_entry")
        if autostart is not None:
            try:
                entry = autostart(ctx.target)
            except Exception as exc:
                warnings.append(f"gui autostart entry unavailable: "
                                f"{type(exc).__name__}: {exc}")
            else:
                if entry is not None:
                    extra.append(entry)
    return extra, warnings


def _managed_entries(ctx: Context) -> dict:
    from installer import transaction

    try:
        entries = transaction.load_status(ctx.target).get("entries") or {}
    except Exception:
        return {}
    return entries if isinstance(entries, dict) else {}


def previous_tool_links(ctx: Context) -> list:
    """The ~/.local/bin tool links the last transaction installed, as entries."""

    from installer.transaction import DesiredEntry

    kept = []
    for entry_id, entry in sorted(_managed_entries(ctx).items()):
        if not entry_id.startswith(TOOL_LINK_PREFIX) or not isinstance(entry, dict):
            continue
        installed = entry.get("installed") or {}
        link_text = installed.get("link_text")
        if installed.get("kind") != "symlink" or not link_text or not entry.get("dest"):
            continue
        kept.append(DesiredEntry(entry_id, Path(entry["dest"]), "symlink",
                                 link_text=link_text))
    return kept


# --- macOS LaunchAgents ----------------------------------------------------------

def _homebrew_prefix(ctx: Context) -> Path | None:
    """Homebrew's prefix for this run (HOMEBREW_PREFIX from its environment,
    else the architecture's); None off macOS."""

    return plat.homebrew_prefix(ctx.platform, ctx.env) if ctx.platform is not None else None


def homebrew_dirs(ctx: Context) -> list[Path]:
    """Homebrew's bin and sbin on macOS (none elsewhere), for child PATHs."""

    prefix = _homebrew_prefix(ctx)
    return [prefix / "bin", prefix / "sbin"] if prefix is not None else []


def launch_agent_entries(ctx: Context) -> list:
    """The two LaunchAgent plists (installer/launchd.py) for this Target:
    Homebrew's prefix and a UTF-8 LANG from the environment of this run."""

    from installer import launchd

    return launchd.desired_entries(ctx.target, brew_prefix=_homebrew_prefix(ctx),
                                   lang=launchd.pick_lang(ctx.env, ctx.runner))


def launch_agent_changes(tx_result: PhaseResult) -> dict:
    """What the transaction did with the plists, for post-install: their ids
    it ``changed`` (rewrote), ``kept`` (the user edited them; -f overwrites)
    and left ``unchanged``, and whether it ``applied`` both."""

    from installer import launchd

    result = tx_result.details.get("result") or {}
    ids = set(launchd.ENTRY_IDS)
    seen = {key: sorted(ids & set(result.get(key) or ()))
            for key in ("changed", "kept", "unchanged")}
    applied = tx_result.status != FAIL and ids <= {i for v in seen.values() for i in v}
    return {**seen, "applied": applied}


# --- location -------------------------------------------------------------------

def location_error(here: Path, target: plat.Target) -> str | None:
    """Why ``here`` is not the ``~/.dotfiles`` checkout, or None; with what to
    run instead, which depends on what ``~/.dotfiles`` is."""

    expected = target.repo_root
    if Path(os.path.realpath(here)) == Path(os.path.realpath(expected)):
        return None
    head = f"install.py must run from the checkout at {expected}, not {here}.\n"
    one_liner = ("    curl -fsSL https://raw.githubusercontent.com/leekyungmoon/dotfiles"
                 "/HEAD/etc/install | bash")
    if not os.path.lexists(expected):
        return head + (
            "Clone the repository into ~/.dotfiles and install it from there:\n\n"
            f"    git clone --recursive {repo.DEFAULT_REPO_URL} ~/.dotfiles && ~/.dotfiles/install\n\n"
            "or use the one-line installer:\n\n" + one_liner)
    if all((expected / name).exists() for name in ("install", "etc/install", "installer")):
        return head + "Install that checkout:\n\n    ~/.dotfiles/install"
    return head + ("~/.dotfiles is another checkout. The one-line installer moves it aside "
                   "(nothing is deleted) and installs:\n\n" + one_liner)


# --- phases -------------------------------------------------------------------

# /usr/bin/git on macOS is a shim that runs the Command Line Tools' git, and
# without the tools only offers to install them and fails; xcode-select -p
# names the active tools and fails when there are none.
MACOS_GIT_SHIM = "/usr/bin/git"
XCODE_SELECT = "/usr/bin/xcode-select"
XCODE_SELECT_TIMEOUT = 30.0


def _macos_python_remedy() -> str:
    version = ".".join(str(part) for part in sys.version_info[:3])
    return (f"python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required, but this is {version} "
            f"({sys.executable}): run 'brew install python3', then rerun "
            "~/.dotfiles/install, which runs install.py with Homebrew's python3")


def _clt_git_stub(runner, git: str) -> str | None:
    """Why ``git`` cannot run on this Mac, or None.

    Only the shim at /usr/bin/git depends on the Command Line Tools (a
    Homebrew git does not); it is not run here, because without the tools
    running it opens the installation dialog.
    """

    if git != MACOS_GIT_SHIM and os.path.realpath(git) != MACOS_GIT_SHIM:
        return None
    try:
        done = runner.run([XCODE_SELECT, "-p"], timeout=XCODE_SELECT_TIMEOUT,
                          check=False, read_only=True)
    except Exception:  # xcode-select itself missing or hanging
        developer_dir = None
    else:
        out = done.stdout or b""
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        developer_dir = text.strip() if done.returncode == 0 else None
    if developer_dir and os.path.isdir(developer_dir):
        return None
    return ("git is the Xcode Command Line Tools stub (/usr/bin/git) and the tools are "
            "not installed: run 'xcode-select --install' (or 'brew install git' with "
            "Homebrew's bin first on PATH), then rerun")


def _ssl_ca_problem(verify_paths=None) -> str | None:
    """Why this Python cannot verify HTTPS certificates, or None.

    A python.org build on macOS has no CA certificates until its 'Install
    Certificates.command' ran; every pinned download would then fail.
    ``ssl.get_default_verify_paths()`` reports cafile/capath as None when
    they do not exist; an empty bundle or directory verifies nothing either.
    """

    import ssl

    from installer import downloads

    paths = verify_paths if verify_paths is not None else ssl.get_default_verify_paths()
    try:
        if paths.cafile and os.path.getsize(paths.cafile) > 0:
            return None
        if paths.capath and any(True for _ in os.scandir(paths.capath)):
            return None
    except OSError:
        pass
    # SSL_CERT_FILE, when set, replaces the bundle the build was made with.
    looked = os.environ.get(paths.openssl_cafile_env or "") or paths.openssl_cafile
    return (f"this Python has no CA certificates (it looks for {looked}), so the "
            f"pinned downloads would fail: {downloads.certificate_remedy('darwin')}")


def _lzma_problem() -> str | None:
    """Why this Python cannot unpack .tar.xz archives (node's), or None."""

    try:
        importlib.import_module("lzma")
    except ImportError:
        return ("this Python was built without lzma (xz), which the pinned node "
                "archive needs: run 'brew install python3', then rerun ~/.dotfiles/install")
    return None


def _macos_preflight(ctx: Context, *, packages: bool) -> list[str]:
    """macOS-only reasons the install cannot start."""

    reasons = []
    git = ctx.runner.which("git")
    if git is None:
        reasons.append("git is not installed (run: xcode-select --install, or: "
                       "brew install git)")
    else:
        stub = _clt_git_stub(ctx.runner, git)
        if stub:
            reasons.append(stub)
    # Only the packages phase downloads anything with this Python.
    if packages:
        for problem in (_ssl_ca_problem(), _lzma_problem()):
            if problem:
                reasons.append(problem)
    return reasons


def preflight_phase(ctx: Context, *, packages: bool = True) -> PhaseResult:
    """Platform, Python, git, home and checkout checks before any change.

    ``packages`` says whether the packages phase will run (not
    --no-packages); on macOS it adds the checks only downloads need.
    """

    reasons = []
    details: dict = {}
    darwin = platform_system(ctx) == "darwin"
    if ctx.platform is not None:
        p = ctx.platform
        ui.log_target("platform", ui.GREEN(p.describe()))
        if p.best_effort:
            details["best_effort"] = True
            ui.log(ui.YELLOW("Intel Macs are best effort: Homebrew supports them at "
                             "Tier 3 only, so some formulae may build from source."))
    if sys.version_info < MIN_PYTHON:
        reasons.append(_macos_python_remedy() if darwin else "python 3.10+ is required")
    if darwin:
        reasons += _macos_preflight(ctx, packages=packages)
    elif ctx.runner.which("git") is None:
        reasons.append("git is not installed (run etc/install, or: sudo apt-get "
                       "install -y git)")
    if not ctx.target.home.is_dir():
        reasons.append(f"home {ctx.target.home} does not exist")
    elif not os.access(ctx.target.home, os.W_OK):
        reasons.append(f"home {ctx.target.home} is not writable")
    checkout = Path(ctx.repo_root)
    if not repo.is_checkout(checkout):
        reasons.append(f"{checkout} is not a git checkout")
    if reasons:
        for reason in reasons:
            ui.log(ui.RED(reason))
        return PhaseResult("preflight", FAIL, reasons)

    ui.log_target(checkout, ui.GREEN("git checkout"))
    try:
        issues = repo.submodule_issues(ctx.runner, checkout)
        if issues:
            for path, flag in issues:
                ui.log(ui.RED("git submodule {name} : {status}".format(
                    name=path, status=repo.SUBMODULE_STATUS.get(flag, "(Unknown)"))))
            ui.log(ui.YELLOW("Git submodules are not initialized.\n"))
            ui.log("Running: %s" % ui.CYAN(
                "git submodule update --init --recursive --jobs 8"))
            repo.update_submodules(ctx.runner, checkout)
            repo.verify_submodules(ctx.runner, checkout)
            details["submodules"] = "updated"
        else:
            details["submodules"] = "ok"
    except Exception as exc:
        ui.log(ui.RED(f"submodules: {exc}"))
        return PhaseResult("preflight", FAIL, [f"submodules: {exc}"], details)
    return PhaseResult("preflight", PASS, [], details)


def _manifest_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _backup_location(target: plat.Target, run_id: str, entry_id: str) -> Path | None:
    """Where the transaction kept the object it replaced for ``entry_id``."""

    folder = target.state_root / "backups" / "runs" / run_id / entry_id
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    payload = meta.get("payload")
    if payload == "object":
        return folder / "object"
    if payload == "baseline":
        return target.state_root / "backups" / "baseline" / entry_id / "object"
    return None


def kept_local_changes(applied, desired: list) -> list[tuple[str, str]]:
    """``(id, dest)`` of copies the transaction kept because the user changed them.

    ``ApplyResult.kept`` lists their ids. Read defensively: items may also be
    dests, ``(id, dest)`` pairs or ``{"id", "dest"}`` dicts, and older
    transactions have no such field.
    """

    raw = getattr(applied, "kept", None)
    if raw is None:
        raw = getattr(applied, "drifted_kept", None)
    dests = {d.id: str(d.dest) for d in desired}
    by_dest = {str(d.dest): d.id for d in desired}
    kept = []
    for item in raw or []:
        if isinstance(item, dict):
            entry_id, dest = item.get("id"), item.get("dest")
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            entry_id, dest = item
        else:
            entry_id, dest = str(item), None
        if entry_id not in dests and str(entry_id or dest) in by_dest:
            entry_id = by_dest[str(entry_id or dest)]
        dest = dest if dest is not None else dests.get(entry_id)
        kept.append((str(entry_id or dest), str(dest or entry_id)))
    return kept


def report_entries(ctx: Context, desired: list, befores: dict, applied,
                   sources: dict) -> None:
    changed = set(applied.changed)
    kept = dict(kept_local_changes(applied, desired))
    forced = set(getattr(applied, "forced", None) or [])
    for entry in sorted(desired, key=lambda d: str(d.dest)):
        dest = entry.dest
        if entry.id in kept:
            ui.log_target(dest, ui.YELLOW("kept your local changes (use -f to overwrite)"))
            continue
        if entry.id not in changed:
            ui.log_target(dest, ui.GREEN("already up-to-date"))
            continue
        if entry.kind == "symlink":
            what = f"symlink created from '{entry.link_text}'"
        elif entry.kind == "file" and entry.id.startswith(LAUNCH_AGENT_PREFIX):
            what = "LaunchAgent written for this home"
        elif entry.kind == "file":
            what = f"copied from '{sources.get(entry.id) or 'the repository'}'"
        elif entry.kind == "absent":
            what = "removed"
        else:
            what = "installed"
        before = befores.get(entry.id)
        if before is not None and before.kind != "absent":
            backup = _backup_location(ctx.target, ctx.run_id, entry.id)
            where = str(backup) if backup else str(applied.backup_dir)
            if entry.kind == "absent":
                ui.log_target(dest, ui.YELLOW(f"backed up to {where}, removed"))
            else:
                note = " (your local changes were overwritten: -f)" \
                    if entry.id in forced else ""
                ui.log_target(dest, ui.YELLOW(f"backed up to {where}, replaced{note}")
                              + " " + ui.GREEN(f"({what})"))
        elif entry.kind == "absent":
            ui.log_target(dest, ui.GRAY("already absent"))
        else:
            ui.log_target(dest, ui.GREEN(what))
    for entry_id in applied.retired:
        ui.log_target(entry_id, ui.YELLOW("no longer managed; restored from the baseline"))
    for entry_id in applied.drifted:
        ui.log_target(entry_id, ui.YELLOW("no longer managed but changed since; left as is"))


def transaction_phase(ctx: Context, *, extra_entries: list | None = None,
                      force: bool = False) -> tuple[PhaseResult, bool]:
    """Apply the managed paths from the checkout.

    Returns the result and whether systemd-user entries were applied.
    ``force`` (``-f``) lets the transaction overwrite copied files the user
    changed, when the transaction supports keeping them. Only entries whose
    condition holds (:func:`condition_facts`) are applied.
    """

    from installer import manifest as manifest_mod
    from installer import transaction

    checkout = Path(ctx.repo_root)
    manifest_path = checkout / "manifests" / "managed-paths.json"
    try:
        loaded = manifest_mod.load_manifest(manifest_path)
        resolved = manifest_mod.resolve(loaded, ctx.target, checkout)
    except manifest_mod.ManifestError as exc:
        ui.log(ui.RED(f"manifest: {exc}"))
        return PhaseResult("transaction", FAIL, [f"manifest: {exc}"]), False

    try:
        previous = transaction.load_status(ctx.target)
    except Exception:
        previous = {}
    legacy = [i for i in LEGACY_IDS if i in (previous.get("entries") or {})]
    if legacy:
        reason = ("this home was installed by the pre-release layout that managed "
                  f"{', '.join(legacy)}; run 'python3 install.py restore --baseline' "
                  "with that installer first")
        ui.log(ui.RED(reason))
        return PhaseResult("transaction", FAIL, [reason]), False

    facts = condition_facts(ctx)
    systemd_ok = facts["systemd-user"]
    skipped, not_applicable = _condition_skips(resolved, facts)
    desired = transaction.entries_from_manifest(resolved, ctx.target,
                                                systemd_user=systemd_ok,
                                                conditions=facts)
    desired += list(extra_entries or [])
    return _apply_entries(ctx, desired, resolved=resolved, skipped=skipped,
                          systemd_ok=systemd_ok, manifest_path=manifest_path,
                          force=force, not_applicable=not_applicable)


def _apply_kwargs(tx, generation: dict, force: bool) -> dict:
    kwargs = {"generation": generation}
    try:
        parameters = inspect.signature(tx.apply).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "force" in parameters:
        kwargs["force"] = bool(force)
    return kwargs


def _apply_entries(ctx: Context, desired: list, *, resolved: list, skipped: list,
                   systemd_ok: bool, manifest_path: Path | None,
                   force: bool = False, not_applicable: list | None = None
                   ) -> tuple[PhaseResult, bool]:
    from installer import transaction

    checkout = Path(ctx.repo_root)
    sources = {e.id: str(e.source) for e in resolved if e.kind == "copy"}
    try:
        info = repo.checkout_info(ctx.runner, checkout)
        commit, origin, branch = info.commit, info.origin_url, info.branch
    except Exception:
        commit, origin, branch = None, None, None
    generation = {
        "commit": commit,
        "manifest_sha256": _manifest_sha(manifest_path) if manifest_path else None,
        "origin": origin,
        "ref": branch,
        "checkout": str(checkout),
    }
    befores = {}
    for entry in desired:
        try:
            befores[entry.id] = transaction.snapshot(Path(entry.dest))
        except Exception:
            befores[entry.id] = None
    try:
        with transaction.Transaction(ctx.target, ctx.run_id) as tx:
            applied = tx.apply(desired, **_apply_kwargs(tx, generation, force))
    except transaction.ConcurrentRunError as exc:
        ui.log(ui.RED(f"another install is running: {exc}"))
        return PhaseResult("transaction", FAIL,
                           [f"another install is running: {exc}"]), False
    except Exception as exc:
        ui.log(ui.RED(f"rolled back: {type(exc).__name__}: {exc}"))
        return PhaseResult("transaction", FAIL,
                           [f"rolled back: {type(exc).__name__}: {exc}"]), False

    report_entries(ctx, desired, befores, applied, sources)
    details = {"commit": commit, "result": phases.jsonable(applied)}
    reasons = []
    # An interrupted earlier run was rolled back first; anything the user had
    # changed at those paths in the meantime was saved, not discarded.
    for item in getattr(applied, "recovery_saved", None) or []:
        if isinstance(item, dict) and item.get("dest"):
            ui.log_target(item["dest"], ui.YELLOW(
                "changed after an interrupted run; your version was saved to "
                f"{item.get('saved_to')} before rolling back"))
            reasons.append(f"recovered interrupted run {item.get('run_id')}: saved "
                           f"{item['dest']} to {item.get('saved_to')}")
    kept = kept_local_changes(applied, desired)
    if kept:
        details["kept_local_changes"] = [entry_id for entry_id, _ in kept]
        reasons.append("kept your local changes to "
                       + ", ".join(entry_id for entry_id, _ in kept)
                       + " (use -f to overwrite)")
    _report_condition_skips(ctx, resolved, skipped, list(not_applicable or []),
                            details, reasons)
    return PhaseResult("transaction", PASS, reasons, details), bool(systemd_ok)


def _report_condition_skips(ctx: Context, resolved: list, skipped: list,
                            not_applicable: list, details: dict, reasons: list) -> None:
    """Report what :func:`_condition_skips` left out, the same way for the
    transaction and for --dry-run."""

    if skipped:
        details["skipped_ids"] = skipped
        reasons.append("systemd-user entries skipped: no systemd user manager")
        for entry in resolved:
            if entry.id in skipped:
                ui.log_target(entry.dest, ui.GRAY("skipped (no systemd user manager)"))
    # Another platform's entries are not a degradation: no reason, one line.
    _report_not_applicable(ctx, resolved, not_applicable, details)


def finish(ctx: Context, command: str, results: list[PhaseResult], *,
           merge: bool = False, closing: bool = False) -> int:
    """Write status.json and the summary; ``merge`` updates only these phases."""

    recorded = [r.to_dict() for r in results]
    if merge:
        previous = phases.read_status(ctx.target) or {}
        names = {r.phase for r in results}
        recorded = [p for p in previous.get("phases", [])
                    if isinstance(p, dict) and p.get("phase") not in names] + recorded
    payload = {
        "schema": phases.STATUS_SCHEMA,
        "command": command,
        "run_id": ctx.run_id,
        "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "overall": phases.worst(p.get("status") for p in recorded),
        "phases": recorded,
    }
    try:
        phases.write_status(ctx.target, payload)
    except OSError as exc:
        print(f"warning: cannot write status.json: {exc}", file=sys.stderr)
    print(phases.format_summary(results))
    if closing:
        for line in phases.completion_lines(results):
            ui.log(line)
    return phases.exit_code(results)


GENERATION_FILE = "generation"
GENERATION_GIT_TIMEOUT = 120.0
GENERATION_MAX_BYTES = 4096


def _git_bytes(runner, repo: Path, env: dict, *args: str) -> bytes | None:
    """stdout of a read-only git command in ``repo``, or None on failure."""

    try:
        done = runner.run(["git", "--no-optional-locks", "-C", str(repo), *args],
                          timeout=GENERATION_GIT_TIMEOUT, check=False, env=env,
                          read_only=True)
    except Exception:
        return None
    if done.returncode != 0:
        return None
    out = done.stdout or b""
    return out if isinstance(out, bytes) else str(out).encode("utf-8")


def _tree_digest(root: Path) -> str:
    """Content digest of a checkout git cannot read (paths, link targets and
    file bytes; .git left out)."""

    digest = hashlib.sha256()
    for top, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files + [d for d in dirs if os.path.islink(os.path.join(top, d))]):
            path = os.path.join(top, name)
            rel = os.path.relpath(path, root)
            digest.update(rel.encode("utf-8", "surrogateescape") + b"\0")
            try:
                if os.path.islink(path):
                    digest.update(b"l" + os.readlink(path).encode("utf-8", "surrogateescape"))
                else:
                    with open(path, "rb") as handle:
                        digest.update(b"f" + hashlib.sha256(handle.read()).digest())
            except OSError:
                digest.update(b"?")
            digest.update(b"\0")
    return digest.hexdigest()


def _untracked_digest(repo: Path, listed: bytes) -> str:
    """Digest of the untracked, non-ignored files ``git ls-files -o
    --exclude-standard -z`` listed: each path with its content (a link with
    its target), so editing such a file is a new generation too."""

    digest = hashlib.sha256()
    for name in sorted(set(filter(None, listed.split(b"\0")))):
        path = os.path.join(os.fsencode(repo), name)
        digest.update(name + b"\0")
        try:
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                digest.update(b"l" + os.readlink(path))
            elif stat.S_ISREG(st.st_mode):
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                             | os.O_CLOEXEC)
                file_digest = hashlib.sha256()
                with os.fdopen(fd, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        file_digest.update(chunk)
                digest.update(b"f" + file_digest.digest())
            else:  # a nested repository's directory, a FIFO, ...
                digest.update(b"o%o" % stat.S_IFMT(st.st_mode))
        except OSError:
            digest.update(b"?")
        digest.update(b"\0")
    return digest.hexdigest()


def checkout_generation(runner, repo: Path, env: dict | None = None) -> str:
    """The generation of the checkout, from its content, not from the run.

    sha256 over the HEAD commit, the digest of ``git status --porcelain=v1
    -z``, the digest of ``git diff-index -p HEAD`` and the content digest of
    the untracked, non-ignored files (``git ls-files -o --exclude-standard
    -z``), so every uncommitted local edit counts, and an update or repair
    that changes nothing gives the same generation (and so respawns and
    reloads nothing). None of these writes the index: diff-index is
    plumbing that never refreshes it (``git diff`` would, despite
    GIT_OPTIONAL_LOCKS=0, whenever a file is only stat-dirty), so no run
    takes index.lock in the checkout. A checkout git cannot read is hashed
    by its files instead.
    """

    git_env = {k: v for k, v in (os.environ if env is None else env).items()
               if not k.startswith("GIT_")}  # no GIT_DIR, GIT_INDEX_FILE, ...
    git_env["GIT_OPTIONAL_LOCKS"] = "0"
    head = _git_bytes(runner, repo, git_env, "rev-parse", "--verify", "-q", "HEAD")
    status = _git_bytes(runner, repo, git_env, "status", "--porcelain=v1", "-z")
    diff = _git_bytes(runner, repo, git_env, "diff-index", "-p", "--no-ext-diff",
                      "--no-textconv", "--no-color", "--binary", "HEAD", "--")
    untracked = _git_bytes(runner, repo, git_env, "ls-files", "-o", "--exclude-standard",
                           "-z")
    digest = hashlib.sha256(b"personal-dotfiles generation 2\0")
    if head is None or status is None or diff is None or untracked is None:
        digest.update(b"tree\0" + _tree_digest(Path(repo)).encode("ascii"))
    else:
        digest.update(b"git\0" + head.strip() + b"\0")
        digest.update(hashlib.sha256(status).hexdigest().encode("ascii") + b"\0")
        digest.update(hashlib.sha256(diff).hexdigest().encode("ascii") + b"\0")
        digest.update(_untracked_digest(Path(repo), untracked).encode("ascii"))
    return digest.hexdigest()


def read_generation(target: plat.Target) -> str | None:
    """The generation recorded now, or None (none yet, or unreadable)."""

    path = target.state_root / GENERATION_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, GENERATION_MAX_BYTES)
    except OSError:
        return None
    finally:
        os.close(fd)
    text = data.decode("utf-8", "replace").split("\n", 1)[0]
    return text or None


def write_generation(target: plat.Target, generation: str) -> Path:
    """Record the installed generation for long-running shells.

    ``{state}/personal-dotfiles/generation`` holds ``"<generation>\n"``
    (:func:`checkout_generation`). It is rewritten at the end of the post
    actions of every successful install, repair and update (which runs the
    install), atomically and private (0600). A hooked zsh
    (zsh/zsh.d/dotfiles-reload.zsh) compares it when Enter is pressed on its
    primary prompt and before each prompt, and reloads itself when it
    changed; the tmux converge respawns idle unhooked zsh panes only when
    :func:`record_generation` saw it change.
    """

    if not generation or "\n" in generation or "\t" in generation:
        raise ValueError(f"bad generation {generation!r}")
    root = target.state_root
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    path = root / GENERATION_FILE
    tmp = root / f".{GENERATION_FILE}.{os.getpid()}.tmp"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{generation}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
    return path


def record_generation(ctx: Context) -> tuple[str | None, str]:
    """Compute the checkout's generation and write it; returns
    ``(previous, generation)``, ``previous`` being the one recorded before
    (None: none), so the tmux converge respawns shells only on a change."""

    generation = checkout_generation(ctx.runner, Path(ctx.repo_root), ctx.env)
    previous = read_generation(ctx.target)
    write_generation(ctx.target, generation)
    return previous, generation


def _report(results: list[PhaseResult], result: PhaseResult) -> PhaseResult:
    results.append(result)
    status = result.status
    color = {PASS: ui.GREEN, FAIL: ui.RED}.get(status, ui.YELLOW)
    ui.log(f"[{result.phase}] " + color(status)
           + (f": {'; '.join(result.reasons)}" if result.reasons else ""))
    return result


def run_pipeline(ctx: Context, command: str, opts: Options) -> int:
    """Checking platform -> packages -> links -> post actions -> GNOME settings."""

    results: list[PhaseResult] = []

    ui.section("Checking platform")
    if _report(results, preflight_phase(ctx, packages=not opts.no_packages)).status == FAIL:
        return finish(ctx, command, results, closing=True)

    ui.section("Installing packages")
    exclude = [] if opts.no_packages else ai_cli_exclusions(ctx, opts)
    packages_result = _report(results, packages_phase(
        ctx, disabled=opts.no_packages, dry_run=False, exclude=exclude))
    if packages_result.status == FAIL:
        return finish(ctx, command, results, closing=True)

    ui.section("Creating symbolic links")
    try:
        extra, warnings = extra_desired_entries(ctx, packages_result, no_gui=opts.no_gui)
    except Exception as exc:
        _report(results, PhaseResult("transaction", FAIL,
                                     [f"extra entries: {type(exc).__name__}: {exc}"]))
        return finish(ctx, command, results, closing=True)
    for warning in warnings:
        ui.log(ui.YELLOW(warning))
    tx_result, systemd_applied = transaction_phase(ctx, extra_entries=extra,
                                                   force=opts.force)
    tx_result.reasons.extend(warnings)
    if _report(results, tx_result).status == FAIL:
        return finish(ctx, command, results, closing=True)

    ui.section("Post actions")
    # macOS: launchd instead of systemd, decided by the platform (not by
    # whether systemd units were applied), Homebrew on the children's PATH,
    # and chsh only with a terminal to ask for the password on.
    path_kwargs: dict = {}
    post_kwargs: dict = {}
    shell_kwargs: dict = {"interactive": ctx.interactive}
    darwin = platform_system(ctx) == "darwin"
    if darwin:
        path_kwargs = {"extra_path": homebrew_dirs(ctx)}
        post_kwargs = {"launch_agents": launch_agent_changes(tx_result),
                       "platform": ctx.platform}
        shell_kwargs = {"interactive": ctx.interactive if ctx.prompt is not None else None,
                        "platform": ctx.platform}
    child_env = phases.child_env(ctx.target, ctx.env, **path_kwargs)
    _report(results, phases.post_install_phase(
        ctx.target, ctx.runner, repo_root=Path(ctx.repo_root), run_id=ctx.run_id,
        systemd_units_applied=systemd_applied, env=child_env,
        skip_zplug=opts.skip_zplug, skip_vimplug=opts.skip_vimplug,
        tmux_env=dict(ctx.env),
        # At the end of the post actions, before the running tmux server is
        # converged: respawned shells and every hooked zsh (at its next
        # Enter or prompt) pick up this generation.
        record_generation=lambda: record_generation(ctx), **post_kwargs))
    smoke = _report(results, phases.smoke_phase(ctx.target, ctx.runner, ctx.env,
                                                **path_kwargs))
    # The login shell changes only after the smoke checks passed.
    _report(results, phases.login_shell_phase(
        ctx.target, ctx.runner, current_shell=ctx.current_shell,
        allow_change=not opts.no_shell_change, checks_passed=smoke.status == PASS,
        **shell_kwargs))
    _report(results, phases.git_identity_phase(ctx.target, ctx.runner,
                                               prompt=ctx.prompt))
    _report(results, phases.auth_phase(ctx.target, ctx.runner, ctx.env))

    if darwin:
        # GNOME settings and input-remapper have no macOS counterpart: the
        # phase is left out, not run as SKIPPED, so it neither shows a
        # section nor makes every Mac's overall status SKIPPED.
        return finish(ctx, command, results, closing=True)
    ui.section("GNOME settings")
    _report(results, gui_phase(ctx, disabled=opts.no_gui))
    return finish(ctx, command, results, closing=True)


# --- dry run ------------------------------------------------------------------

def dry_run_install(ctx: Context, opts: Options | None = None) -> int:
    from installer import manifest as manifest_mod

    opts = opts or Options(dry_run=True)
    ui.section("Checking platform")
    results = [preflight_phase(ctx, packages=not opts.no_packages)]
    ui.section("Installing packages")
    results.append(packages_phase(ctx, disabled=opts.no_packages, dry_run=True,
                                  exclude=ai_cli_exclusions(ctx, opts, record=False)))
    ui.section("Creating symbolic links")
    checkout = Path(ctx.repo_root)
    try:
        loaded = manifest_mod.load_manifest(checkout / "manifests" / "managed-paths.json")
        resolved = manifest_mod.resolve(loaded, ctx.target, checkout)
    except manifest_mod.ManifestError as exc:
        results.append(PhaseResult("plan", FAIL, [f"manifest: {exc}"]))
    else:
        # The real run's conditions: what it would leave out is not planned.
        skipped, not_applicable = _condition_skips(resolved, condition_facts(ctx))
        left_out = set(skipped) | set(not_applicable)
        rows = [(entry.dest, entry.id, _planned_action(entry))
                for entry in resolved if entry.id not in left_out]
        reasons: list[str] = []
        status = PASS
        if platform_system(ctx) == "darwin":
            # The LaunchAgent plists the real run adds (only reading here).
            try:
                rows += [(entry.dest, entry.id, _planned_file_action(entry))
                         for entry in launch_agent_entries(ctx)]
            except Exception as exc:
                status = FAIL
                reasons.append(f"extra entries: {type(exc).__name__}: {exc}")
        plan = {}
        for dest, entry_id, action in sorted(rows, key=lambda row: str(row[0])):
            plan[entry_id] = f"{action} {dest}"
            color = ui.GREEN if action in ("unchanged", "keep-absent") else ui.YELLOW
            ui.log_target(dest, color(f"would {action}"
                                      if action not in ("unchanged", "keep-absent")
                                      else "already up-to-date"))
            print(f"  {plan[entry_id]}")
        details: dict = {"entries": plan}
        _report_condition_skips(ctx, resolved, skipped, not_applicable, details, reasons)
        results.append(PhaseResult("plan", status, reasons, details))
    for result in results:
        print(f"[{result.phase}] {result.status}"
              + (f": {'; '.join(result.reasons)}" if result.reasons else ""))
    print("dry run: nothing was changed")
    return phases.exit_code(results)


def _planned_action(entry) -> str:
    dest = entry.dest
    exists = os.path.lexists(dest)
    if entry.kind == "remove":
        return "remove" if exists else "keep-absent"
    if entry.kind in ("symlink", "link"):
        if dest.is_symlink() and os.readlink(dest) == entry.link_text:
            return "unchanged"
    elif entry.kind == "copy" and dest.is_file() and not dest.is_symlink():
        if entry.source is not None and dest.read_bytes() == entry.source.read_bytes():
            return "unchanged"
    return "replace" if exists else "create"


def _planned_file_action(entry) -> str:
    """The plan for a rendered ``kind="file"`` transaction entry: unchanged
    when the same bytes and mode are already there."""

    dest = Path(entry.dest)
    if dest.is_file() and not dest.is_symlink():
        mode = entry.mode if entry.mode is not None else 0o644
        if (stat.S_IMODE(dest.stat().st_mode) == mode
                and dest.read_bytes() == (entry.content or b"")):
            return "unchanged"
    return "replace" if os.path.lexists(dest) else "create"


# --- other commands -----------------------------------------------------------

def cmd_status(ctx: Context, as_json: bool) -> int:
    from installer import transaction

    try:
        state = transaction.load_status(ctx.target)
    except Exception as exc:
        state = {"error": f"{type(exc).__name__}: {exc}"}
    payload = {"state": phases.jsonable(state), "last_run": phases.read_status(ctx.target)}
    services = None
    if platform_system(ctx) == "darwin":
        from installer import launchd

        # What launchd says now (read only), not what the last run saw.
        services = launchd.status(ctx.target, ctx.runner)
        payload["services"] = services
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    last = payload["last_run"] or {}
    print(f"repo:      {ctx.target.repo_root}")
    print(f"last run:  {last.get('run_id', '-')} {last.get('command', '')} "
          f"-> {last.get('overall', 'never installed')}")
    for phase in last.get("phases", []):
        reasons = "; ".join(phase.get("reasons") or [])
        print(f"  {phase.get('phase', '?'):<14} {phase.get('status')}"
              + (f"  ({reasons})" if reasons else ""))
    entries = state.get("entries") if isinstance(state, dict) else None
    if isinstance(entries, dict):
        drifted = [k for k, v in entries.items()
                   if isinstance(v, dict) and v.get("matches") is False]
        print(f"managed:   {len(entries)} entries, "
              f"{len(drifted)} drifted{': ' + ', '.join(drifted) if drifted else ''}")
    elif isinstance(state, dict) and state.get("error"):
        print(f"state:     {state['error']}")
    if services is not None:
        from installer import launchd

        for line in launchd.describe_status(services):
            print(line)
    return 0


def cmd_restore(ctx: Context, args) -> int:
    from installer import transaction

    which = "baseline" if args.baseline else args.run
    if which != "baseline" and not plat.is_generation_id(which):
        print(ui.RED(f"error: {which!r} is not a run id"), file=sys.stderr)
        return 2
    try:
        result = transaction.restore(ctx.target, which=which, force=args.force,
                                     ids=args.ids)
    except Exception as exc:
        print(ui.RED(f"restore failed: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 1
    data = phases.jsonable(result)
    for entry_id in data.get("restored", []):
        ui.log_target(entry_id, ui.GREEN(f"restored ({which})"))
    for entry_id in data.get("forced", []):
        ui.log_target(entry_id, ui.YELLOW("restored over local changes (--force)"))
    for entry_id in data.get("unchanged", []):
        ui.log_target(entry_id, ui.GRAY("already restored"))
    if data.get("backup_dir"):
        ui.log(ui.YELLOW(f"the replaced objects were backed up to {data['backup_dir']}"))

    # A full baseline restore also returns the GNOME keys and input-remapper
    # presets to their pre-install values; they are not files, so the file
    # transaction above cannot. Restoring a single id or run stays file-only,
    # and macOS has no such settings at all.
    gui_status = None
    if which == "baseline" and not args.ids and platform_system(ctx) != "darwin":
        restore_gui = load_seam("gui", "restore_gui")
        if restore_gui is not None:
            kwargs = {}
            if ctx.platform is not None and _accepts(restore_gui, "platform"):
                kwargs["platform"] = ctx.platform
            try:
                gui_result = PhaseResult.coerce(restore_gui(ctx.target, ctx.runner, ctx.env,
                                                            **kwargs), "gui-restore")
            except Exception as exc:  # the file restore already succeeded
                gui_result = PhaseResult("gui-restore", phases.FAIL,
                                         [f"{type(exc).__name__}: {exc}"])
            gui_status = gui_result.status
            data["gui"] = phases.jsonable(gui_result)
            color = {phases.PASS: ui.GREEN, phases.SKIPPED: ui.GRAY,
                     phases.PENDING_GUI: ui.YELLOW}.get(gui_status, ui.RED)
            ui.log_target("GNOME settings", color(f"{gui_status.lower()}"
                          + (f" ({'; '.join(gui_result.reasons)})" if gui_result.reasons else "")))
            if gui_status == phases.PENDING_GUI:
                ui.log(ui.YELLOW("GNOME settings are restored from a desktop session: run "
                                 "'python3 ~/.dotfiles/install.py restore --baseline' again "
                                 "inside GNOME (files already restored stay as they are)."))
    unload_failed = False
    if platform_system(ctx) == "darwin":
        unload_failed = _unload_restored_agents(ctx, data)
    print(json.dumps(data, indent=2, sort_keys=True))
    return 1 if gui_status == phases.FAIL or unload_failed else 0


def _unload_restored_agents(ctx: Context, data: dict) -> bool:
    """After a restore on macOS: unload each agent the restore covered whose
    plist is gone, since launchd keeps a loaded job (and the autosave keeps
    firing) until logout; whether any unload failed.

    A plist the restore put back (an earlier version) is loaded from disk at
    the next login; ``dotfiles repair`` makes the installer's own again.
    """

    from installer import launchd

    covered = {entry_id for key in ("restored", "forced", "unchanged")
               for entry_id in data.get(key) or []}
    gone = [agent for agent in launchd.AGENTS if agent.entry_id in covered
            and not os.path.lexists(launchd.plist_path(ctx.target, agent))]
    if not gone:
        return False
    outcomes: dict[str, str] = {}
    problems = launchd.unload(ctx.target, ctx.runner, [a.label for a in gone],
                              outcomes=outcomes)
    data["launchd"] = {agent.name: outcomes.get(agent.label, "failed") for agent in gone}
    words = {"unloaded": "unloaded (LaunchAgent removed)", "not-loaded": "was not loaded",
             "no-gui-domain": "not loaded (no desktop login session here)"}
    for agent in gone:
        outcome = outcomes.get(agent.label, "failed")
        color = ui.RED if outcome == "failed" else ui.GREEN
        ui.log_target(agent.label, color(words.get(outcome, "could not be unloaded")))
    for problem in problems:
        print(ui.RED(problem), file=sys.stderr)
    return bool(problems)


def _package_names(repo_root: Path, platform=None) -> set[str]:
    names: set[str] = set()
    # The platform's system package profile: Homebrew's groups on macOS.
    profile = load_seam("packages", "package_manifest_name")
    name = profile(platform) if profile is not None else "packages.json"
    try:
        data = json.loads((repo_root / "manifests" / name).read_text("utf-8"))
        names.update(g["id"] for g in data.get("groups", []) if isinstance(g, dict))
    except (OSError, ValueError, KeyError):
        pass
    try:
        data = json.loads((repo_root / "manifests" / "tools.json").read_text("utf-8"))
        tools = data.get("tools", {})
        names.update(tools if isinstance(tools, dict)
                     else (t.get("name") for t in tools if isinstance(t, dict)))
    except (OSError, ValueError):
        pass
    names.discard(None)
    return names


def cmd_packages(ctx: Context, only: list[str], dry_run: bool,
                 force: bool = False) -> int:
    allowed = _package_names(Path(ctx.repo_root), ctx.platform)
    unknown = [name for name in only if name not in allowed]
    if unknown:
        print(ui.RED(f"error: not in the package manifests: {', '.join(unknown)}; "
                     f"allowed: {', '.join(sorted(allowed)) or '(none)'}"),
              file=sys.stderr)
        return 2
    run = load_seam("packages", "run_packages_phase")
    if run is None:
        print("error: the packages phase is not available in this checkout",
              file=sys.stderr)
        return 1
    parameters = inspect.signature(run).parameters
    if "only" not in parameters:
        print("error: the packages phase does not support selecting packages; "
              "run 'dotfiles repair' instead", file=sys.stderr)
        return 1
    kwargs = {"dry_run": dry_run, "only": list(only)}
    if force and "force" in parameters:
        kwargs["force"] = True
    if _accepts(run, "env"):
        kwargs["env"] = ctx.env
    try:
        result = PhaseResult.coerce(
            run(ctx.target, ctx.platform, ctx.runner, **kwargs), "packages")
    except Exception as exc:
        result = PhaseResult("packages", FAIL, [f"{type(exc).__name__}: {exc}"])
    results = [result]
    if not dry_run and result.details.get("links"):
        results.append(tool_links_phase(ctx, result, force=force))
    return finish(ctx, "packages", results, merge=True)


def tool_links_phase(ctx: Context, packages_result: PhaseResult, *,
                     force: bool = False) -> PhaseResult:
    """Own the ~/.local/bin links of ``dotfiles install <tool>``.

    The links go through the transaction together with everything it already
    manages, so nothing else is retired: an installed home reapplies its
    managed paths plus the previous tool links (and the gui autostart entry
    when it was managed); a home that was never installed gets the links only.
    """

    ui.section("Creating symbolic links")
    managed = _managed_entries(ctx)
    try:
        # The gui autostart entry is kept only when it is already managed.
        autostart_id = _gui_autostart_id()
        gui_managed = autostart_id is not None and autostart_id in managed
        extra, warnings = extra_desired_entries(ctx, packages_result,
                                                no_gui=not gui_managed,
                                                packages_complete=False)
    except Exception as exc:
        return PhaseResult("transaction", FAIL,
                           [f"extra entries: {type(exc).__name__}: {exc}"])
    for warning in warnings:
        ui.log(ui.YELLOW(warning))
    # --force here means "reinstall the tool"; it never forces unrelated
    # copied configs the user changed, so the transaction runs unforced.
    if managed:
        result, _ = transaction_phase(ctx, extra_entries=extra, force=False)
    else:
        # Never installed: the links only (on macOS no LaunchAgents either).
        links = [e for e in extra if e.id.startswith(TOOL_LINK_PREFIX)]
        result, _ = _apply_entries(ctx, links, resolved=[], skipped=[],
                                   systemd_ok=False, manifest_path=None, force=False)
    result.reasons.extend(warnings)
    return result


def cmd_services(ctx: Context, action: str, names: list[str]) -> int:
    """``services [status|enable|disable] [tmux|autosave ...]``.

    macOS: the LaunchAgents. disable unloads each one (bootout) and marks it
    disabled in launchd (persistent: later installs and repairs leave it off
    and say so); a running tmux server is not stopped. enable clears that
    mark, then loads and verifies it as a repair would. status shows what
    launchd says. Linux: systemd manages these units; this prints its
    commands and changes nothing.
    """

    if platform_system(ctx) != "darwin":
        return _systemd_services(action, names)
    from installer import launchd

    agents = [launchd.agent_named(name) for name in names]
    if action == "status":
        view = launchd.status(ctx.target, ctx.runner)
        view["agents"] = {a.name: view["agents"][a.name] for a in agents}
        for line in launchd.describe_status(view):
            print(line)
        return 0
    failed = False
    uid = ctx.target.uid
    for agent in agents:
        plist = launchd.plist_path(ctx.target, agent)
        if action == "disable":
            outcomes: dict[str, str] = {}
            problems = launchd.unload(ctx.target, ctx.runner, [agent.label], outcomes=outcomes)
            try:
                code = launchd.disable(ctx.runner, uid, agent.label).returncode
            except Exception as exc:  # launchctl could not run, or timed out
                problems.append(f"launchctl disable: {exc}")
            else:
                if code != 0:
                    problems.append(f"launchctl disable "
                                    f"{launchd.service_target(uid, agent.label)} failed "
                                    f"({launchd.error_text(code)})")
            if problems:
                failed = True
                ui.log_target(plist, ui.RED("could not disable: " + "; ".join(problems)))
            else:
                ui.log_target(plist, ui.GREEN(
                    f"disabled ({outcomes.get(agent.label)}); stays off until: "
                    f"dotfiles services enable {agent.name}"))
            continue
        if not plist.is_file():
            failed = True
            ui.log_target(plist, ui.RED("not installed; run: dotfiles repair"))
            continue
        try:
            code = launchd.enable(ctx.runner, uid, agent.label).returncode
        except Exception as exc:  # launchctl could not run, or timed out
            failed = True
            ui.log_target(plist, ui.RED(f"launchctl enable: {exc}"))
            continue
        if code != 0:
            failed = True
            ui.log_target(plist, ui.RED(
                "no desktop login session here; run this in a Terminal of your desktop "
                "session" if code in launchd.NO_GUI_DOMAIN else
                f"launchctl enable failed ({launchd.error_text(code)})"))
            continue
        result = launchd.register(ctx.target, ctx.runner, agent, changed=False, env=ctx.env)
        if result.status == FAIL:
            failed = True
            ui.log_target(plist, ui.RED("; ".join(result.reasons)))
        elif result.status == phases.RELOGIN_REQUIRED:
            ui.log_target(plist, ui.YELLOW(
                "enabled; loads at your next desktop login (or now, in a Terminal of "
                f"that session: {result.details.get('command')})"))
        elif result.status == SKIPPED:
            failed = True  # still disabled: launchctl enable did not take
            ui.log_target(plist, ui.RED("; ".join(result.reasons)))
        else:
            ui.log_target(plist, ui.GREEN("enabled and loaded")
                          + ("" if not result.reasons else
                             " " + ui.YELLOW("(" + "; ".join(result.reasons) + ")")))
    return 1 if failed else 0


def _systemd_services(action: str, names: list[str]) -> int:
    """Linux: the systemctl commands for ``services`` (nothing is run).

    The autosave timer is stopped and started with --now; tmux.service is
    not (stopping it saves and ends the running server); it is only no
    longer (or again) started at the next graphical login.
    """

    units = [SYSTEMD_UNITS[name] for name in names]
    print("On Linux, systemd user units start tmux and save it every minute; "
          "nothing was changed. Run:")
    if action == "status":
        print(f"    systemctl --user status {' '.join(units)}")
        return 0
    for name in names:
        now = " --now" if name == "autosave" else ""
        print(f"    systemctl --user {action}{now} {SYSTEMD_UNITS[name]}")
    return 0


def _gui_autostart_id() -> str | None:
    try:
        from installer import gui
    except Exception:
        return None
    return getattr(gui, "AUTOSTART_ENTRY_ID", None)


def _open_tty(mode: str):
    try:
        return open("/dev/tty", mode)
    except OSError:
        return None


def _interactive(argv: list[str]) -> int:
    """Run a command attached to the terminal (chsh needs to prompt)."""

    stdin = None
    if not sys.stdin.isatty():
        stdin = _open_tty("rb") or subprocess.DEVNULL
    try:
        return subprocess.call(argv, stdin=stdin)
    finally:
        if stdin not in (None, subprocess.DEVNULL):
            stdin.close()


def terminal_prompt():
    """A ``prompt(question)`` reading the terminal, or None without one."""

    if sys.stdin.isatty():
        def prompt(question: str) -> str | None:
            try:
                return input(ui.YELLOW(question))
            except EOFError:
                return None
        return prompt
    probe = _open_tty("r")
    if probe is None:
        return None
    probe.close()

    def prompt_tty(question: str) -> str | None:
        handle = _open_tty("r+")
        if handle is None:
            return None
        with handle:
            handle.write(ui.YELLOW(question))
            handle.flush()
            line = handle.readline()
        return line.rstrip("\n") if line else None
    return prompt_tty


def main(argv: list[str] | None = None, *, env: dict | None = None,
         euid: int | None = None, getpwuid=pwd.getpwuid, runner=None,
         detect=plat.detect_platform, here: Path | None = None,
         prompt=None, interactive=None, run_id: str | None = None) -> int:
    args = parse_args(argv)
    env = dict(os.environ if env is None else env)
    ui.configure(env=env)
    here = Path(here) if here is not None else HERE
    command = args.command

    if command in ("install", "repair"):
        ui.log(__doc__)  # print logo.

    try:
        target = plat.resolve_target(env, euid=euid, getpwuid=getpwuid)
        platform = None
        if command not in UNLOCATED_COMMANDS:
            platform = detect()
            plat.require_supported_platform(platform)
    except plat.PlatformError as exc:
        print(ui.RED(f"error: {exc}"), file=sys.stderr)
        return 2

    # Before the location gate: no checkout location would make it apply.
    if command == "gui-apply" and platform is not None and platform.is_darwin:
        print(ui.RED("error: gui-apply applies GNOME settings and input-remapper "
                     "presets, which macOS does not have; there is nothing to apply "
                     "on this Mac"), file=sys.stderr)
        return 2

    if command in LOCATED_COMMANDS and not getattr(args, "allow_any_location", False):
        problem = location_error(here, target)
        if problem is not None:
            print(ui.RED("error: " + problem), file=sys.stderr)
            return 2

    from installer.runner import DryRunRunner, Runner
    from installer.transaction import new_run_id

    dry_run = bool(getattr(args, "dry_run", False))
    if runner is None:
        runner = DryRunRunner() if dry_run else Runner()
    try:
        current_shell = getpwuid(target.uid).pw_shell
    except KeyError:
        current_shell = ""
    # prompt=False (tests) means "no terminal": print the commands instead.
    if prompt is None and command in ("install", "repair") and not dry_run:
        prompt = terminal_prompt()
    ctx = Context(target=target, platform=platform, runner=runner, env=env,
                  run_id=run_id or new_run_id(), current_shell=current_shell,
                  interactive=interactive or _interactive, repo_root=here,
                  prompt=prompt if callable(prompt) else None)

    if command in ("install", "repair"):
        opts = options_from(args)
        if opts.dry_run:
            return dry_run_install(ctx, opts)
        return run_pipeline(ctx, command, opts)
    if command == "status":
        return cmd_status(ctx, args.json)
    if command == "restore":
        return cmd_restore(ctx, args)
    if command == "gui-apply":
        result = gui_phase(ctx, disabled=False, autostart=args.autostart)
        return finish(ctx, "gui-apply", [result], merge=True)
    if command == "packages":
        return cmd_packages(ctx, args.only, args.dry_run, args.force)
    if command == "services":
        return cmd_services(ctx, args.action, args.names)
    return 2


if __name__ == "__main__":
    sys.exit(main())
