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

    git clone --recursive https://github.com/leekyungmoon/dotfiles.git ~/.dotfiles
    cd ~/.dotfiles && python3 install.py

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


@dataclasses.dataclass
class Options:
    force: bool = False
    skip_vimplug: bool = False
    skip_zplug: bool = False
    no_packages: bool = False
    no_gui: bool = False
    no_shell_change: bool = False
    dry_run: bool = False


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
                   help="accepted like upstream; managed targets are always "
                        "replaced after an exact backup")
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


def options_from(args: argparse.Namespace) -> Options:
    return Options(**{f.name: bool(getattr(args, f.name, False))
                      for f in dataclasses.fields(Options)})


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


def packages_phase(ctx: Context, *, disabled: bool, dry_run: bool) -> PhaseResult:
    if disabled:
        return PhaseResult("packages", SKIPPED, ["--no-packages"])
    run = load_seam("packages", "run_packages_phase")
    if run is None:
        return PhaseResult("packages", SKIPPED, ["packages-phase-not-available"])
    try:
        return PhaseResult.coerce(
            run(ctx.target, ctx.platform, ctx.runner, dry_run=dry_run), "packages")
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
                          *, no_gui: bool) -> tuple[list, list[str]]:
    """Transaction entries contributed by the packages and gui modules.

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


# --- location -------------------------------------------------------------------

def location_error(here: Path, target: plat.Target) -> str | None:
    """Why ``here`` is not the ``~/.dotfiles`` checkout, or None."""

    expected = target.repo_root
    if Path(os.path.realpath(here)) == Path(os.path.realpath(expected)):
        return None
    return (
        f"install.py must run from the checkout at {expected}, not {here}.\n"
        "Clone the repository into ~/.dotfiles and run it from there:\n\n"
        f"    git clone --recursive {repo.DEFAULT_REPO_URL} ~/.dotfiles\n"
        "    cd ~/.dotfiles && python3 install.py\n\n"
        "or use the one-line installer:\n\n"
        "    curl -fsSL https://raw.githubusercontent.com/leekyungmoon/dotfiles"
        "/HEAD/etc/install | bash"
    )


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


def report_entries(ctx: Context, desired: list, befores: dict, applied,
                   sources: dict) -> None:
    changed = set(applied.changed)
    for entry in sorted(desired, key=lambda d: str(d.dest)):
        dest = entry.dest
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
                ui.log_target(dest, ui.YELLOW(f"backed up to {where}, replaced")
                              + " " + ui.GREEN(f"({what})"))
        elif entry.kind == "absent":
            ui.log_target(dest, ui.GRAY("already absent"))
        else:
            ui.log_target(dest, ui.GREEN(what))
    for entry_id in applied.retired:
        ui.log_target(entry_id, ui.YELLOW("no longer managed; restored from the baseline"))
    for entry_id in applied.drifted:
        ui.log_target(entry_id, ui.YELLOW("no longer managed but changed since; left as is"))


def transaction_phase(ctx: Context, *, extra_entries: list | None = None
                      ) -> tuple[PhaseResult, bool]:
    """Apply the managed paths from the checkout.

    Returns the result and whether systemd-user entries were applied.
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
    sources = {e.id: str(e.source) for e in resolved if e.kind == "copy"}
    try:
        info = repo.checkout_info(ctx.runner, checkout)
        commit, origin, branch = info.commit, info.origin_url, info.branch
    except Exception:
        commit, origin, branch = None, None, None
    generation = {
        "commit": commit,
        "manifest_sha256": _manifest_sha(manifest_path),
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
            applied = tx.apply(desired, generation=generation)
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
    packages_result = _report(results, packages_phase(
        ctx, disabled=opts.no_packages, dry_run=False))
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
    tx_result, systemd_applied = transaction_phase(ctx, extra_entries=extra)
    tx_result.reasons.extend(warnings)
    if _report(results, tx_result).status == FAIL:
        return finish(ctx, command, results, closing=True)

    ui.section("Post actions")
    child_env = phases.child_env(ctx.target, ctx.env)
    _report(results, phases.post_install_phase(
        ctx.target, ctx.runner, repo_root=Path(ctx.repo_root), run_id=ctx.run_id,
        systemd_units_applied=systemd_applied, env=child_env,
        skip_zplug=opts.skip_zplug, skip_vimplug=opts.skip_vimplug))
    _report(results, phases.smoke_phase(ctx.target, ctx.runner, ctx.env))
    _report(results, phases.login_shell_phase(
        ctx.target, ctx.runner, current_shell=ctx.current_shell,
        allow_change=not opts.no_shell_change, interactive=ctx.interactive))
    _report(results, phases.git_identity_phase(ctx.target, ctx.runner,
                                               prompt=ctx.prompt))

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
    results.append(packages_phase(ctx, disabled=opts.no_packages, dry_run=True))
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
    result = PhaseResult.coerce(
        run(ctx.target, ctx.platform, ctx.runner, **kwargs), "packages")
    return finish(ctx, "packages", [result], merge=True)


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
