"""Standalone receiver, passed to python3 over SSH with a source archive on stdin.

Never invokes the normal installer. All owned files are below one named profile.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid


LAUNCHER = '''#!/bin/sh
set -eu
release=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
profile=$(dirname -- "$(dirname -- "$release")")
export SSH_ZSH_HOME="$profile"
export SSH_ZSH_ROOT="$release/src"
export ZDOTDIR="$release/zdot"
export XDG_CONFIG_HOME="$profile/config"
export XDG_DATA_HOME="$profile/data"
export XDG_CACHE_HOME="$release/cache"
export XDG_STATE_HOME="$profile/state"
if [ -z "${SSH_ZSH_TMUX+x}" ]; then
    export SSH_ZSH_TMUX="$(command -v tmux || :)"
fi
export SHELL="$(command -v zsh)"
export PATH="$release/bin:$release/src/bin:$PATH"
unset ZSH_PROFILE_LOG
exec zsh -il
'''

ENTRYPOINT = '''#!/bin/sh
set -eu
profile=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec "$profile/current/launch"
'''

TMUX_WRAPPER = '''#!/bin/sh
set -eu
if [ -z "${SSH_ZSH_TMUX:-}" ]; then
    echo "tmux is not installed on this server." >&2
    exit 127
fi
exec "$SSH_ZSH_TMUX" -L "ssh-dotfiles-${SSH_ZSH_HOME##*/}" "$@"
'''


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, timeout=120, **kwargs)


def prerequisites():
    if sys.version_info < (3, 8):
        raise ValueError("Python 3.8 or newer is required on the server")
    for tool in ("zsh", "git", "infocmp", "fzf"):
        if not shutil.which(tool):
            raise ValueError(f"server prerequisite missing: {tool}; install it separately, then retry")
    try:
        run(["zsh", "-dfc", "autoload -Uz is-at-least; is-at-least 5.4.2"],
            stdout=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        raise ValueError("server needs zsh 5.4.2 or newer; upgrade it separately, then retry")
    try:
        run(["fzf", "--zsh"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError:
        raise ValueError("server needs fzf 0.48 or newer (fzf --zsh); no local native binary is copied")


def directory(path):
    if path.is_symlink():
        raise ValueError(f"refusing symlink directory: {path}")
    path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"not a directory: {path}")


def extract(stream, release):
    """Extract source files without following archive links or special files."""
    seen = set()
    links = []
    total = 0
    with tarfile.open(fileobj=stream, mode="r|gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if (name.is_absolute() or ".." in name.parts or not name.parts
                    or name.parts[0] not in ("src", "plugins", "bundle.json") or member.name in seen):
                raise ValueError(f"invalid or duplicate archive member: {member.name}")
            if name.parts[0] == "bundle.json" and (len(name.parts) != 1 or not member.isfile()):
                raise ValueError("invalid bundle metadata")
            seen.add(member.name)
            total += member.size
            if total > 512 * 1024 * 1024 or len(seen) > 100000:
                raise ValueError("source archive exceeds the 512 MiB / 100000 entry limit")
            dest = release.joinpath(*name.parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                dest.mkdir(exist_ok=True)
            elif member.isfile():
                with archive.extractfile(member) as source, dest.open("xb") as target:
                    shutil.copyfileobj(source, target)
                dest.chmod(0o755 if member.mode & 0o111 else 0o644)
            elif member.issym():
                link = PurePosixPath(member.linkname)
                if link.is_absolute():
                    raise ValueError(f"absolute archive symlink: {member.name}")
                target = (dest.parent / member.linkname).resolve()
                if release.resolve() not in target.parents:
                    raise ValueError(f"escaping archive symlink: {member.name}")
                links.append((dest, member.linkname))
            else:
                raise ValueError(f"special archive member: {member.name}")
    # Creating links last prevents a later entry from writing through one.
    for dest, link in links:
        dest.symlink_to(link)
    for dest, _ in links:
        if release.resolve() not in dest.resolve().parents:
            raise ValueError(f"escaping chained archive symlink: {dest}")
    metadata = release / "bundle.json"
    if metadata.is_symlink() or not metadata.is_file():
        raise ValueError("source archive is incomplete (bundle.json missing)")
    if json.loads(metadata.read_text()).get("schema") != 1:
        raise ValueError("unsupported source bundle schema")


def receive(stream, target, expected_size, expected_digest):
    """Read to EOF and validate the complete compressed transfer before unpacking."""
    if not 0 < expected_size <= 128 * 1024 * 1024:
        raise ValueError("compressed source archive must be at most 128 MiB")
    if not re.fullmatch("[a-f0-9]{64}", expected_digest):
        raise ValueError("invalid source archive digest")
    digest = hashlib.sha256()
    size = 0
    while True:
        data = stream.read(min(65536, expected_size - size + 1))
        if not data:
            break
        size += len(data)
        if size > expected_size:
            raise ValueError("source archive exceeds the declared size")
        digest.update(data)
        target.write(data)
    if size != expected_size or digest.hexdigest() != expected_digest:
        raise ValueError("source archive transfer is truncated or corrupt; active profile unchanged")
    target.seek(0)


def environment(profile, release):
    env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "LC_ALL", "TERM")
           if key in os.environ}
    env.update(SSH_ZSH_HOME=str(profile), SSH_ZSH_ROOT=str(release / "src"),
               ZDOTDIR=str(release / "zdot"), TERM="xterm-256color",
               POWERLEVEL9K_DISABLE_CONFIGURATION_WIZARD="true",
               GITSTATUS_AUTO_INSTALL="0")
    for kind in ("CONFIG", "DATA", "CACHE", "STATE"):
        env[f"XDG_{kind}_HOME"] = str(profile / kind.lower())
    env["XDG_CACHE_HOME"] = str(release / "cache")
    env["PATH"] = str(release / "bin") + ":" + str(release / "src/bin") + ":" + env["PATH"]
    return env


def prepare(profile, release):
    src = release / "src"
    for file in ("zsh/zshrc", "zsh/zshenv", "zsh/antidote/antidote.zsh", "zsh/fasd/fasd"):
        if not (src / file).is_file():
            raise ValueError(f"incomplete source archive: missing {file}")
    zdot = release / "zdot"
    zdot.mkdir()
    (release / "cache").mkdir()
    (zdot / ".zsh").symlink_to("../src/zsh")
    for name in ("zshenv", "zprofile", "zlogin", "zlogout", "zpreztorc"):
        (zdot / ("." + name)).symlink_to("../src/zsh/" + name)
    # A failed .zshrc normally leaves an interactive zsh running. Do not
    # present a broken personal profile as a successful connect.
    (zdot / ".zshrc").write_text(
        'unset _ssh_zsh_ready\n'
        'source "$SSH_ZSH_ROOT/zsh/zshrc"\n'
        '[[ ${_ssh_zsh_ready:-0} == 1 ]] || {\n'
        '  print -u2 -- "ssh-dotfiles: profile startup failed; reinstall from the client."\n'
        '  exit 1\n}\n')
    (release / "bin").mkdir()
    (release / "bin/fasd").symlink_to("../src/zsh/fasd/fasd")
    (release / "bin/tmux").write_text(TMUX_WRAPPER)
    (release / "bin/tmux").chmod(0o755)
    (release / "launch").write_text(LAUNCHER)
    (release / "launch").chmod(0o755)
    env = environment(profile, release)
    specs = run(["zsh", "-df", str(src / "zsh/plugins.zsh")], env=env,
                capture_output=True, text=True).stdout
    bundle = []
    for line in specs.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        tokens = line.split()
        plugin = tokens[0]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", plugin):
            raise ValueError(f"unsupported plugin specification: {line}")
        plugin_dir = release / "plugins" / plugin
        if not plugin_dir.is_dir():
            raise ValueError(f"local bundle lacks server plugin {plugin}; prepare it locally and retry")
        if tokens[1:] == ["kind:clone"]:
            continue
        if tokens[1:]:
            raise ValueError(f"unsupported offline plugin options: {line}")
        # Reuse the pinned Antidote entrypoint selection, but quote the
        # resulting paths ourselves: its bundle printer splits paths with
        # spaces. Unlike `antidote bundle`, this helper cannot clone anything.
        script = 'source "$1"; __antidote_initfiles "$2"'
        result = run(["zsh", "-dfc", script, "entrypoints",
                      str(src / "zsh/antidote/antidote.zsh"), str(plugin_dir)],
                     env=env, capture_output=True, text=True)
        bundle.append("fpath+=( " + shlex.quote(str(plugin_dir)) + " )")
        for entry in result.stdout.splitlines():
            if not Path(entry).is_file():
                raise ValueError(f"missing plugin entrypoint: {entry}")
            bundle.append("source " + shlex.quote(entry) + " || return 1")
    content = "\n".join(bundle) + "\n"
    if "prezto/init.zsh" not in content:
        raise ValueError("incomplete plugin bundle (prezto missing)")
    (release / "antidote.bundled.zsh").write_text(content)
    smoke = ('[[ ${_ssh_zsh_ready:-0} == 1 ]] && '
             '(( $+functions[antidote] && $+functions[p10k] )) && '
             '[[ $HISTFILE == "$SSH_ZSH_HOME/state/zsh/history" ]] && '
             '[[ ${(t)HISTFILE} != *export* ]]')
    # -l exercises the same login startup files as connect. ZLE needs a real
    # terminal; disable it for this non-terminal smoke, not for user sessions.
    result = run(["zsh", "+Z", "-lic", smoke], env=env, cwd=release,
                 stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if result.stderr:
        # Successful zsh can still have diagnostics; keep these visible.
        print(result.stderr, end="", file=sys.stderr)


def install(stream, name, expected_size, expected_digest):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
        raise ValueError("invalid profile name")
    prerequisites()
    home = Path.home()
    parent = home
    for part in (".local", "share", "ssh-dotfiles", "profiles"):
        parent = parent / part
        directory(parent)
    lock = parent / ("." + name + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "w") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("profile lock is not a regular file")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another install is in progress for this profile; retry after it finishes")
        profile = parent / name
        marker = {"format": 1, "profile": name}
        if profile.exists() or profile.is_symlink():
            if profile.is_symlink() or not profile.is_dir():
                raise ValueError(f"profile path is occupied: {profile}")
            owner = profile / "profile.json"
            if owner.is_symlink() or not owner.is_file() or json.loads(owner.read_text()) != marker:
                raise ValueError(f"refusing existing unmanaged profile directory: {profile}")
        else:
            staging = Path(tempfile.mkdtemp(prefix=".new-", dir=parent))
            try:
                (staging / "profile.json").write_text(json.dumps(marker) + "\n")
                staging.rename(profile)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        for name in ("releases", "config", "data", "cache", "state"):
            directory(profile / name)
        directory(profile / "state/zsh")
        current = profile / "current"
        if current.exists() and not current.is_symlink():
            raise ValueError("current is occupied by a non-symlink")
        shell = profile / "shell"
        if shell.exists() or shell.is_symlink():
            if shell.is_symlink() or not shell.is_file() or shell.read_text() != ENTRYPOINT:
                raise ValueError("profile launcher has local changes; preserve/move it before reinstalling")
        release = profile / "releases" / uuid.uuid4().hex
        release.mkdir()
        published = False
        pending = profile / (".current-" + release.name)
        try:
            with tempfile.TemporaryFile(dir=profile) as payload:
                receive(stream, payload, expected_size, expected_digest)
                extract(payload, release)
            prepare(profile, release)
            # The stable entrypoint only dispatches through current. All
            # versioned launch logic moves atomically with the release.
            if not shell.exists():
                temporary = profile / (".shell-" + release.name)
                temporary.write_text(ENTRYPOINT)
                temporary.chmod(0o755)
                temporary.replace(shell)
            pending.symlink_to("releases/" + release.name)
            pending.replace(current)
            published = True
            print(f"Installed profile {marker['profile']}. Connect using ssh-dotfiles connect --profile {marker['profile']} HOST.")
        finally:
            if pending.is_symlink():
                pending.unlink()
            if not published:
                shutil.rmtree(release)


def main():
    try:
        install(sys.stdin.buffer, sys.argv[1], int(sys.argv[2]), sys.argv[3])
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print(f"ssh-dotfiles: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
