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
"""

SUBCOMMANDS = ("install", "status", "restore", "repair", "gui-apply", "packages")
# Commands that act on the checkout and so must run from ~/.dotfiles.
LOCATED_COMMANDS = ("install", "repair", "gui-apply", "packages")
# Entries managed by the pre-release layout that promoted a staged checkout.
LEGACY_IDS = ("repo", "dotfiles-compat")
# ~/.local/bin links requested by the packages phase (installer/packages.py).
TOOL_LINK_PREFIX = "tool-link-"


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

    def __post_init__(self) -> None:
        if self.repo_root is None:
            self.repo_root = self.target.repo_root


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
    return build_parser().parse_args(argv)


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
            ui.log_target(label, ui.GRAY("skipped (your choice; "
                                         f"`python3 ~/.dotfiles/install.py --{option.replace('_', '-')}` "
                                         "installs it later)"))
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
    try:
        return PhaseResult.coerce(
            run(ctx.target, ctx.platform, ctx.runner, **kwargs), "packages")
    except Exception as exc:
        return PhaseResult("packages", FAIL, [f"{type(exc).__name__}: {exc}"])


def gui_phase(ctx: Context, *, disabled: bool, autostart: bool = False) -> PhaseResult:
    if disabled:
        return PhaseResult("gui", SKIPPED, ["--no-gui"])
    apply = load_seam("gui", "apply_or_defer")
    if apply is None:
        return PhaseResult("gui", SKIPPED, ["gui-phase-not-available"])
    env = dict(ctx.env)
    if autostart:
        env["PERSONAL_DOTFILES_GUI_AUTOSTART"] = "1"
    try:
        return PhaseResult.coerce(apply(ctx.target, ctx.runner, env), "gui")
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

def preflight_phase(ctx: Context) -> PhaseResult:
    reasons = []
    details: dict = {}
    if ctx.platform is not None:
        p = ctx.platform
        ui.log_target("platform", ui.GREEN(f"{p.distribution} {p.release} ({p.architecture})"))
    if sys.version_info < MIN_PYTHON:
        reasons.append("python 3.10+ is required")
    if ctx.runner.which("git") is None:
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
    changed, when the transaction supports keeping them.
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

    systemd_ok = phases.systemd_user_supported(ctx.runner)
    skipped = transaction.skipped_by_condition(resolved, systemd_user=systemd_ok)
    desired = transaction.entries_from_manifest(resolved, ctx.target,
                                                systemd_user=systemd_ok)
    desired += list(extra_entries or [])
    return _apply_entries(ctx, desired, resolved=resolved, skipped=skipped,
                          systemd_ok=systemd_ok, manifest_path=manifest_path,
                          force=force)


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
                   force: bool = False) -> tuple[PhaseResult, bool]:
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
    if skipped:
        details["skipped_ids"] = skipped
        reasons.append("systemd-user entries skipped: no systemd user manager")
        for entry in resolved:
            if entry.id in skipped:
                ui.log_target(entry.dest, ui.GRAY("skipped (no systemd user manager)"))
    return PhaseResult("transaction", PASS, reasons, details), bool(systemd_ok)


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
    if _report(results, preflight_phase(ctx)).status == FAIL:
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
    child_env = phases.child_env(ctx.target, ctx.env)
    _report(results, phases.post_install_phase(
        ctx.target, ctx.runner, repo_root=Path(ctx.repo_root), run_id=ctx.run_id,
        systemd_units_applied=systemd_applied, env=child_env,
        skip_zplug=opts.skip_zplug, skip_vimplug=opts.skip_vimplug,
        tmux_env=dict(ctx.env),
        # At the end of the post actions, before the running tmux server is
        # converged: respawned shells and every hooked zsh (at its next
        # Enter or prompt) pick up this generation.
        record_generation=lambda: record_generation(ctx)))
    smoke = _report(results, phases.smoke_phase(ctx.target, ctx.runner, ctx.env))
    # The login shell changes only after the smoke checks passed.
    _report(results, phases.login_shell_phase(
        ctx.target, ctx.runner, current_shell=ctx.current_shell,
        allow_change=not opts.no_shell_change, interactive=ctx.interactive,
        checks_passed=smoke.status == PASS))
    _report(results, phases.git_identity_phase(ctx.target, ctx.runner,
                                               prompt=ctx.prompt))
    _report(results, phases.auth_phase(ctx.target, ctx.runner, ctx.env))

    ui.section("GNOME settings")
    _report(results, gui_phase(ctx, disabled=opts.no_gui))
    return finish(ctx, command, results, closing=True)


# --- dry run ------------------------------------------------------------------

def dry_run_install(ctx: Context, opts: Options | None = None) -> int:
    from installer import manifest as manifest_mod

    opts = opts or Options(dry_run=True)
    ui.section("Checking platform")
    results = [preflight_phase(ctx)]
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
        plan = {}
        for entry in sorted(resolved, key=lambda e: str(e.dest)):
            action = _planned_action(entry)
            plan[entry.id] = f"{action} {entry.dest}"
            color = ui.GREEN if action in ("unchanged", "keep-absent") else ui.YELLOW
            ui.log_target(entry.dest, color(f"would {action}"
                                            if action not in ("unchanged", "keep-absent")
                                            else "already up-to-date"))
            print(f"  {plan[entry.id]}")
        results.append(PhaseResult("plan", PASS, [], {"entries": plan}))
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


# --- other commands -----------------------------------------------------------

def cmd_status(ctx: Context, as_json: bool) -> int:
    from installer import transaction

    try:
        state = transaction.load_status(ctx.target)
    except Exception as exc:
        state = {"error": f"{type(exc).__name__}: {exc}"}
    payload = {"state": phases.jsonable(state), "last_run": phases.read_status(ctx.target)}
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
    # transaction above cannot. Restoring a single id or run stays file-only.
    gui_status = None
    if which == "baseline" and not args.ids:
        restore_gui = load_seam("gui", "restore_gui")
        if restore_gui is not None:
            try:
                gui_result = PhaseResult.coerce(restore_gui(ctx.target, ctx.runner, ctx.env),
                                           "gui-restore")
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
    print(json.dumps(data, indent=2, sort_keys=True))
    return 1 if gui_status == phases.FAIL else 0


def _package_names(repo_root: Path) -> set[str]:
    names: set[str] = set()
    try:
        data = json.loads((repo_root / "manifests" / "packages.json").read_text("utf-8"))
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
    allowed = _package_names(Path(ctx.repo_root))
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
        result, _ = _apply_entries(ctx, extra, resolved=[], skipped=[],
                                   systemd_ok=False, manifest_path=None, force=False)
    result.reasons.extend(warnings)
    return result


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
        if command not in ("status", "restore"):
            platform = detect()
            plat.require_supported_platform(platform)
    except plat.PlatformError as exc:
        print(ui.RED(f"error: {exc}"), file=sys.stderr)
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
    return 2


if __name__ == "__main__":
    sys.exit(main())
