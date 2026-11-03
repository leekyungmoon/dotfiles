# Personal zsh over SSH

`ssh-dotfiles` lets one person select this repository's zsh configuration while
sharing a remote Unix account. It preserves the remote `HOME` and the account's
ordinary shell. It is **not a security boundary**: people with the same UID can
access each other's profile files and processes.

The usual **local** dotfiles installer exposes the command in `~/.local/bin`.
Existing installations receive it through the normal `dotfiles update`. There
is no separate client configuration. This command does not install the full
desktop setup on the server.

## Install and connect

Run on your local computer, using a normal SSH destination or configured Host
alias. OpenSSH handles your identity files, password prompt, host-key checking,
port and any configured jump host; the tool does not store credentials.

```sh
ssh-dotfiles install user@server
ssh-dotfiles connect user@server
```

The default profile name is your **local** login name. Use an explicit name
when moving between client computers with different login names, or keeping
multiple profiles. Names contain 1–64 letters/digits/dots/dashes/underscores
and start with a letter or digit.
People whose client login names are the same (for example `ubuntu`) must choose
different `--profile` names: a name selects a shared account directory, not an
authenticated person, and installing that name replaces its active release.

```sh
ssh-dotfiles install my-server --profile alice
ssh-dotfiles connect my-server --profile alice
```

Ordinary `ssh`, SCP/SFTP and automation continue to use the ordinary account.
`connect` requests a terminal and launches an interactive login zsh. It never
installs or updates anything; an absent profile produces an installation hint.
`exit` closes that SSH shell. If you run tmux inside it, the profile's wrapper
selects the separate `ssh-dotfiles-<profile>` tmux server; this also covers
aliases and helper scripts. Its new panes use zsh. No tmux server starts until
you explicitly run tmux, and shared tmux configuration is not edited. Use an
ordinary SSH login for the account's common tmux server. Explicitly overriding
the socket or bypassing the wrapper can share the environment again; this is
convenience isolation, not a sandbox.

## Offline preparation and prerequisites

**Every install transfers local files**; there is no remote clone/download
fallback. The client needs Python 3.8+, Git, OpenSSH and this repository with
initialized `zsh/antidote` and `zsh/fasd` submodules. Prepare the ordinary local
zsh installation and its plugins while the client has network access.
The client needs OpenSSH 8.7 or newer (`StdinNull` support). The remote account's
login shell must accept POSIX shell command syntax, such as bash or zsh;
fish/csh login shells are not supported by this command protocol.

Plugins are found under `${XDG_DATA_HOME:-$HOME/.local/share}/antidote`, or the
legacy `zsh/antidote-plugins` directory in the local checkout. A custom cache
can be selected explicitly:

```sh
ssh-dotfiles install my-server \
  --plugins /absolute/path/to/antidote
```

The server must already provide Python 3.8+, zsh 5.4.2+, Git, `infocmp`
(ncurses), and fzf 0.48+ with `fzf --zsh` support. Missing prerequisites cause
a clear failure; arrange their installation separately, including on an
offline server. Native programs from the client are **not** copied, so a local
x86-64 fzf cannot accidentally replace an ARM server's executable.

The payload contains Git-tracked zsh configuration, shell helpers, theme data,
Antidote/fasd and the selected cached plugins, including initialized nested
submodules. It excludes Git metadata, untracked files, machine-local overrides,
bytecode, repository tests and the client-only `ssh-dotfiles` command. Modified
tracked configuration is included: inspect your local changes before sharing
them. Never put credentials in tracked configuration. Arbitrary home directories,
SSH keys, authentication stores and session transcripts are not packaged.

All common plugins are required; a missing plugin fails locally instead of
silently omitting it. The macOS-only plugin is included when cached, and a
macOS receiver refuses installation if it needs that missing plugin. Linux was
tested; macOS execution is not yet verified. Powerlevel10k remains enabled;
its optional native gitstatus daemon is not transferred or downloaded, so it
uses its zsh Git-status fallback. Terminal fonts remain a client setting.

## Files and failures

One profile lives under
`~/.local/share/ssh-dotfiles/profiles/<name>/`. Releases contain the zsh source,
plugin source, completion cache and generated plugin bundle. History and other
state live below that profile; optional overrides are `<profile>/.zshenv.local`,
`.zprofile.local` and `.zshrc.local`. Existing shared home startup files and
global Git/tmux settings are not replaced or sourced as personal overrides.
The server's SSH login shell and system-wide startup files still run according
to its existing policy before/around the selected zsh; this is not a sandbox.

Re-running `install` prepares a new release, verifies real zsh initialization,
then atomically switches `current`. A failed install retains the previous
release. Concurrent installs of the same profile are refused. Old releases
are kept so existing shells can continue using them; space is not reclaimed
automatically. Abrupt power/process loss can leave an inactive release or
temporary file, but not publish a half-installed release. A later install
uses a fresh release and the kernel releases the install lock when its process
exits. Unmanaged profile directories, symlinked installation directories and
locally edited launchers are refused rather than overwritten.

The complete compressed transfer is checked against its client-provided size
and SHA-256 before extraction (128 MiB compressed / 512 MiB expanded limits).
Truncation or corruption cannot publish a release. The stable entrypoint only
dispatches through `current`; versioned launch logic changes with the release,
so future launcher updates do not require deleting a previous installation.
If the client disconnects or is interrupted, remote completion is unknown:
the active pointer will select either a previous release or a fully prepared
new one. Reconnect to check before retrying.

To update, update/prepare the client and run `ssh-dotfiles install` again.
Connect again to select the new release. Mutating `dotfiles` commands are
refused within the personal remote shell; they would target the shared home.

## Verification

Unit tests cover CLI argument boundaries, source selection, prerequisite and
conflict failures, malformed archives, locks, reinstalls and failed activation.
The zsh regression test exercises both ordinary and shared-profile branches.

```sh
python3 -m unittest discover -s tests/unit
zsh tests/test-ssh-zsh-profile.zsh
```

The Linux integration suite uses the real cached plugins and real zsh, a
disposable home containing spaces, and a local SSH transport double. Linux
seccomp blocks creation of IPv4/IPv6 sockets in receiver subprocesses. This
tests archive transport, offline install/connect/reinstall, shared-file
preservation and loaded prompt/keybinding functions, not SSH authentication or
an actual remote server deployment. It needs the system libseccomp runtime.

```sh
export SSH_ZSH_TEST_PLUGINS=/path/to/antidote
python3 -m unittest discover \
  -s tests/integration -p test_ssh_dotfiles.py -v
```
