"""Client for shared-account zsh profiles. SSH owns authentication and transport."""

import argparse
import getpass
import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

from installer.ssh_bundle import BundleError, build_bundle


def profile_name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise argparse.ArgumentTypeError("profile must be 1-64 letters, digits, dots, dashes or underscores")
    return value


def destination(value):
    if value.startswith("-") or not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise argparse.ArgumentTypeError("expected an SSH Host alias or user@host, not an option")
    return value


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="action", required=True)
    for action in ("install", "connect"):
        cmd = commands.add_parser(action)
        cmd.add_argument("host", type=destination, help="existing SSH Host alias or user@host")
        cmd.add_argument("--profile", type=profile_name, default=getpass.getuser(),
                         help="personal profile name (default: local login name)")
        if action == "install":
            cmd.add_argument("--plugins", type=Path,
                             help="installed Antidote plugin directory (no downloads)")
    return p


def plugin_directory(repo, explicit=None):
    if explicit is not None:
        return explicit.expanduser().resolve()
    data = Path(os.environ.get("XDG_DATA_HOME", ""))
    if not data.is_absolute():
        data = Path.home() / ".local/share"
    for candidate in (data / "antidote", repo / "zsh/antidote-plugins"):
        if candidate.is_dir():
            return candidate
    raise ValueError("local plugins are missing; prepare the normal local zsh installation first, "
                     "or pass --plugins PATH")


def ssh_command(host, remote, interactive=False):
    # Do not inherit a Host's RemoteCommand (it conflicts with this command),
    # TTY preference for uploads, or stdin redirection. All auth/host-key
    # settings, ProxyJump, identity and port configuration stay with OpenSSH.
    return ["ssh", "-t" if interactive else "-T", "-o", "RemoteCommand=none",
            "-o", "StdinNull=no", "--", host, remote]


def main(argv=None):
    args = parser().parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    try:
        profile_name(args.profile)
        if args.action == "connect":
            # Profile is restricted to a path component; HOME expands only
            # on the server, never in the local command shell.
            path = '"$HOME/.local/share/ssh-dotfiles/profiles/' + args.profile + '/shell"'
            remote = (f'if [ -x {path} ]; then exec {path}; else '
                      "printf '%s\\n' 'Profile not installed; run ssh-dotfiles install first.' >&2; "
                      "exit 1; fi")
            return subprocess.call(ssh_command(args.host, remote, interactive=True))

        plugins = plugin_directory(repo, args.plugins)
        print(f"Installing profile {args.profile!r} on {args.host}. "
              "An existing profile with this name will be replaced; "
              "use --profile NAME to distinguish people sharing a local login name.",
              file=sys.stderr)
        with tempfile.TemporaryDirectory(prefix="ssh-zsh-") as temporary:
            archive = Path(temporary) / "profile.tar.gz"
            build_bundle(repo, plugins, archive)
            script = (repo / "installer/ssh_remote.py").read_text()
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            remote = "exec python3 -c " + shlex.quote(script) + " " + shlex.join(
                [args.profile, str(archive.stat().st_size), digest])
            with archive.open("rb") as stream:
                return subprocess.call(ssh_command(args.host, remote), stdin=stream)
    except (OSError, ValueError, BundleError, subprocess.CalledProcessError) as exc:
        print(f"ssh-dotfiles: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("ssh-dotfiles: interrupted; remote completion is unknown. "
              "Reconnect to check the active profile before retrying.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
