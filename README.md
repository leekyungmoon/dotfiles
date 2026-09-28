
# Dotfiles

🏠 Personal dotfiles for **Ubuntu 22.04 / 24.04** (amd64 and arm64),
derived from [wookayin/dotfiles][upstream].

zsh, tmux, neovim, fzf, clipboard helpers, GNOME navigation shortcuts and
the Codex / Claude Code CLIs, installed the way upstream does it: one
command clones this repository into `~/.dotfiles` and links your configs
to it.


## Installation

### 👉 One-liner (if you trust me):

```bash
curl -fsSL https://raw.githubusercontent.com/leekyungmoon/dotfiles/HEAD/etc/install | bash
```

No `curl` yet? `wget` works just as well:

```bash
wget -qO- https://raw.githubusercontent.com/leekyungmoon/dotfiles/HEAD/etc/install | bash
```

<details>
<summary>
💡 (Tip) What exactly does the one-liner do? (Click to expand)
</summary>
<p>

[`etc/install`](etc/install) is a small bash script; everything is inside a
`main` function called on its last line, so a truncated download never runs
half a script. It echoes each command as it goes and stops at the first
error.

1. Refuses to run as **root**, and refuses anything other than
   **Ubuntu 22.04 / 24.04 on amd64 / arm64**.
2. If `git`, `curl` (or `wget`), `python3` or `ca-certificates` is missing,
   says so and runs
   `sudo apt-get update && sudo apt-get install -y git curl ca-certificates python3`.
   If sudo is refused it stops with a clear error.
3. Gets `~/.dotfiles`:
   - already a clean checkout of this repository → `git pull --ff-only`
     plus a submodule update;
   - otherwise it first runs `git clone --recursive -j8` into a temporary
     sibling (`~/.dotfiles.new-<TS>-<PID>`). Only when that clone (and the
     `DOTFILES_REF` checkout, if any) has succeeded is an existing
     `~/.dotfiles` **moved aside** (never deleted) to
     `~/.local/state/personal-dotfiles/backups/pre-install-<TS>/dotfiles`
     (the path is printed) and the new clone moved into its place. If the
     clone fails, the temporary sibling is removed, an existing
     `~/.dotfiles` is left exactly as it was, and nothing else runs.
4. Runs `cd ~/.dotfiles && python3 install.py` (reading from your terminal,
   so prompts work even under `curl | bash`), then prints `All Done!`.

Pass options through to `install.py` with `bash -s --`:

```bash
curl -fsSL https://raw.githubusercontent.com/leekyungmoon/dotfiles/HEAD/etc/install | bash -s -- --no-gui
```

`DOTFILES_REPO_URL` and `DOTFILES_REF` (branch, tag or commit) override the
clone source.

</p>
</details>

<details>
<summary>
🤔 Want to manually clone and install? (Click to expand)
</summary>

<p>

```bash
$ git clone --recursive https://github.com/leekyungmoon/dotfiles.git ~/.dotfiles
$ cd ~/.dotfiles && python3 install.py
```

`install.py` must run from `~/.dotfiles`; from anywhere else it refuses and
tells you to clone there. It also refuses root and unsupported platforms.

</p>
</details>

<br>


The installation script will clone the repository into `~/.dotfiles` and
create symbolic links (e.g., `~/.zshrc` → `~/.dotfiles/zsh/zshrc`) for you.
Because the rc files are links, anything that appends to them (`conda init`,
an nvm/rustup installer, `p10k configure`) edits the checkout; keep
machine-local lines in `~/.zshrc.local`, `~/.zshenv.local` or
`~/.bashrc.local`, which are sourced when they exist.

A few files that their programs rewrite (`~/.gitconfig`, the pudb and
terminator configs, the systemd user units) are **copied** instead, so those
programs never write into the checkout. **A copy that you or your apps
changed is kept**: `dotfiles update`, `dotfiles repair` and `install.py`
only rewrite a copy while it is still exactly what the installer wrote last
time, and otherwise print `kept your local changes (use -f to overwrite)`.
`python3 install.py -f` backs your version up and writes a fresh copy.
`git config --global ...` therefore stays in `~/.gitconfig`; your identity
belongs in `~/.gitconfig.secret` (see [After install](#-after-install)).

**The one intentional difference from upstream:** if target files already
exist (e.g. `~/.zshrc`, `~/.vim`), they are **backed up and replaced**
instead of being left for you to delete manually. The installer prints
`backed up to <path>, replaced` for each one, and you can put everything
back with

```bash
python3 ~/.dotfiles/install.py restore --baseline
```

See [🛟 Recovery](#-recovery) below.

`install.py` works through these sections, in order: *Checking platform*,
*Installing packages*, *Creating symbolic links*, *Post actions* (tmux
plugins, systemd user units — the tmux-resurrect autosave timer is enabled
**and started** right away, not only at your next login — zsh/tmux smoke
checks, login shell → zsh (only after the smoke checks passed), git
identity, AI CLI sign-in check) and *GNOME settings*, and finishes with the
follow-ups still left to you.

### `install.py` options

```bash
$ python3 install.py                   # install (the default)
$ python3 install.py --dry-run         # show what would change, change nothing
$ python3 install.py -f                # --force: also overwrite copies you changed (after a backup)
$ python3 install.py --skip-vimplug    # do not prefill/update vim plugins
$ python3 install.py --skip-zplug      # do not prefill/update zsh plugins
$ python3 install.py --no-packages     # skip apt packages and pinned tools
$ python3 install.py --no-gui          # skip GNOME settings
$ python3 install.py --no-shell-change # do not change the login shell
```

### 🔑 After install

The installer intentionally leaves these to you:

- **Sign in to the AI CLIs** yourself. Codex:

  ```bash
  codex login
  ```

  Claude Code: start it and type `/login` at its prompt (`claude --help`
  shows whether your build also has a non-interactive sign-in command):

  ```bash
  claude
  ```

  The installer only checks whether you are signed in (with each CLI's own
  status command) and never reads or copies credentials.

- **oh-my-codex** is installed from a pinned lockfile with
  `npm ci --ignore-scripts`, so its npm lifecycle scripts never run; the
  native helper its postinstall would download is fetched by the installer
  from pinned, checksummed inputs instead. Run `omx setup` yourself when you
  want it, and upgrade through `dotfiles update` rather than `omx update`.

- **Git identity.** Like upstream, `install.py` asks for your name and email
  when `~/.gitconfig.secret` has none and a terminal is available.
  Otherwise (or to change it later):

  ```bash
  git config --file ~/.gitconfig.secret user.name "Your Name"
  git config --file ~/.gitconfig.secret user.email "you@example.com"
  ```

  `~/.gitconfig.secret` is never part of the repository.

- **Log out and back in once**, so the new login shell, GNOME shortcuts and
  input-remapper preset are all picked up.


## `$ dotfiles`

**To update dotfiles** (pull changes from upstream and run
[`install.py`](install.py) again):

```bash
$ dotfiles update
$ dotfiles update --fast          # fast update mode: skip updating {vim,zsh} plugins
```

Like upstream, this runs in `~/.dotfiles`: `git fetch origin`, stashes local
edits to tracked files (only if there are any), `git merge --ff-only`,
`git submodule update --init --recursive`, `python3 install.py`, then puts
your edits back. It ends with `Update complete!` and the changelog, or
`dotfiles is up-to-date`, or `installer has failed. Check the log.`
A history that cannot fast-forward is not merged. `--skip-zplug` and
`--skip-vimplug` skip one kind of plugin update.

Only the stash entry that this update created is ever re-applied or
dropped (it is identified by its commit id, never as "the newest stash"),
so older stashes of yours are never touched. If your edits no longer apply
cleanly on top of the new commit, the update still finishes, the checkout is
reset to the new commit (when it was otherwise clean, so no conflict
markers are left behind), your edits **stay in the stash**, the exact
`git stash apply --index <id>` and `git stash drop` commands are printed,
and `dotfiles update` exits with status 3 (or with the installer's status,
if the install itself failed).

That is also how changes travel between machines: edit in `~/.dotfiles`,
commit and push to
[leekyungmoon/dotfiles](https://github.com/leekyungmoon/dotfiles), then run
`dotfiles update` on every other machine. Nothing updates on its own.

You can install the pinned tools locally (into
`~/.local/share/personal-dotfiles/tools`, linked from `~/.local/bin`)
*without sudo*:

```bash
$ dotfiles install                # list available packages
$ dotfiles install neovim         # -> ~/.local/bin/nvim
$ dotfiles install fzf            # -> ~/.local/bin/fzf
```

The list comes from [`manifests/tools.json`](manifests/tools.json)
(node, neovim, fzf, codex, claude-code, oh-my-codex, a Nerd Font); every
download is checked against a pinned SHA-256.

And a few more:

```bash
$ dotfiles github                 # open github.com/leekyungmoon/dotfiles
$ dotfiles status                 # = python3 ~/.dotfiles/install.py status
$ dotfiles restore --baseline     # = python3 ~/.dotfiles/install.py restore --baseline
$ dotfiles repair                 # = python3 ~/.dotfiles/install.py repair
```

The same entrypoints, directly:

```bash
$ python3 ~/.dotfiles/install.py status              # last run, drift (--json too)
$ python3 ~/.dotfiles/install.py restore --baseline  # undo everything
$ python3 ~/.dotfiles/install.py restore --run <RUN_ID>
$ python3 ~/.dotfiles/install.py restore --baseline --id zshrc
$ python3 ~/.dotfiles/install.py restore --baseline --force
$ python3 ~/.dotfiles/install.py repair              # reapply, e.g. tmux plugins
$ python3 ~/.dotfiles/install.py gui-apply           # apply pending GNOME settings now
```


## 🆘 Troubleshooting

*Please read carefully warning messages during installation !!*

- If something goes wrong, please run **`dotfiles update`** (or
  `python3 ~/.dotfiles/install.py`) to make everything up-to-date.
    - Please carefully READ the error/warning message printed by the
      installation script. `python3 ~/.dotfiles/install.py status` shows the
      last run again: each phase is `PASS`, `FAIL`, `SKIPPED`,
      `PENDING_GUI`, `RELOGIN_REQUIRED` or `AUTH_REQUIRED`, with reasons.
    - What a failure undoes: the *Creating symbolic links* step is one
      transaction, so if **it** fails (or the run is killed during it) every
      managed path it touched is put back as it was (after a kill, by the
      next run). Nothing else is rolled back: apt packages and pinned tools
      installed before it stay installed, and when a later step fails
      (post actions such as tmux plugins or the systemd units, the smoke
      checks, the login shell, git identity, GNOME settings) the new
      configs stay in place. Fix the reported cause and run it again, or
      undo that run's config changes with
      `python3 ~/.dotfiles/install.py restore --run <RUN_ID>`
      (see [docs/RECOVERY.md](docs/RECOVERY.md)).
    - If you had your own `~/.zshrc`, `~/.vimrc`, `~/.vim`, etc., they were
      **backed up and replaced**, not deleted. Want one back? See
      [docs/RECOVERY.md](docs/RECOVERY.md#restore-a-single-config).
    - Another install holding the lock? Wait for it to finish; two runs
      never overlap.

- Q: I see some weird icons like `⍰` in (neo)vim statusline, or in tmux
  statusbar.
  - A: Use a [Nerd font](https://github.com/ryanoasis/nerd-fonts) in your
    terminal. The installer ships one (`dotfiles install nerd-font`); pick
    it in your terminal's profile.

- If [**neovim**][neovim] emits any startup errors:
    - Use the pinned neovim: `dotfiles install neovim`.
    - Try `:checkhealth`, and `:Lazy update` (or `dotfiles update`).
    - If neovim + treesitter emits `query: invalid node type`, run
      `:TSUpdate`.

- `pbpaste` fails over SSH? **That is expected.**
  An SSH session has no local clipboard to read and OSC52 cannot be read
  back. Paste with your terminal (`Ctrl+Shift+V`), use `Ctrl+V` inside
  tmux, or read the tmux buffer with `pbpaste --tmux-buffer`.
  (`pbcopy` over SSH does work, via OSC52.)

- GNOME shortcuts did not change?
    - If you installed from SSH or a TTY, they are `PENDING_GUI` and are
      applied at your next graphical login.
      Check with `python3 ~/.dotfiles/install.py status`.
    - From a terminal inside the GNOME session you can apply them now with
      `python3 ~/.dotfiles/install.py gui-apply`.

- `Ctrl+Super+Left/Right` does not switch tabs?
    - It is **Ubuntu 24.04 only**: input-remapper 1.4 on 22.04 cannot
      express the chord, so on 22.04 the installer reports it as not
      available and leaves it out.
    - input-remapper only acts on a **physical keyboard**; it does nothing
      over SSH, VNC or in a VM without a passed-through keyboard.
    - It needs its service running and permission to read input devices.
      Log out and back in once after install, then check
      `python3 ~/.dotfiles/install.py status`.

- Does tmux look weird on **22.04**? Ubuntu 22.04 ships tmux 3.2a, which
  has no `allow-passthrough` and no status-bar drag reordering; the
  `prefix S` / `prefix @` pickers fall back to a plain popup.
  See [docs/SUPPORT.md](docs/SUPPORT.md#tmux-32a-on-2204).
  Glyphs rendering as boxes? Use the Nerd Font (above).

- If you are still lost, or you've found a bug, please raise an issue on
  [leekyungmoon/dotfiles](https://github.com/leekyungmoon/dotfiles/issues)
  (not upstream).


## License

[The MIT License (MIT)](LICENSE)

Copyright (c) 2012-2026 Jongwook Choi (@wookayin)

Modifications in this repository are released under the same MIT license.


## 📦 What gets installed on Ubuntu

- **apt** (with `sudo`, always `--no-install-recommends`; the plan is
  simulated first): zsh, tmux, git, curl, ca-certificates, python3 (+ pip,
  venv), ripgrep, fd, tree, wl-clipboard, xclip, locales, and for the
  desktop gnome-terminal, input-remapper and Google Chrome (from Google's
  signed apt repository).
- **Pinned tools** without sudo, into `~/.local/share/personal-dotfiles/tools`
  and linked from `~/.local/bin`: neovim, fzf, node, codex, claude-code,
  oh-my-codex (npm lifecycle scripts disabled) and a Nerd Font, each checked
  against a pinned SHA-256. vim and neovim use this fzf from `$PATH`;
  nothing is cloned into `~/.fzf`.
- **Configs** linked from `~/.dotfiles`: zsh, bash, vim/neovim, tmux, git,
  terminal emulators, python tools, `pbcopy` / `pbpaste`, `dotfiles`.
- **tmux plugins** at pinned commits, the tmux systemd user units, zsh as
  your login shell and the GNOME settings below.

## 🙅 What it deliberately does NOT install

- No Docker, ROS, `gh`, `glab`, nvm, conda, pyenv, apt Node.js, or a C/C++
  toolchain (`build-essential`, `gcc`, `make`, ...). The apt plan is refused
  if any of these would sneak in.
- No GNOME Shell, `ubuntu-desktop` or display manager — it configures the
  desktop you already have, it does not install one.
- **Never copies credentials**: no SSH keys, GPG keys, git identity,
  Codex/Claude logins, browser profiles or tokens. You bring those yourself.

## ✨ Highlights

- 📋 **`pbcopy` / `pbpaste` on Linux.**
  `pbcopy` writes through Wayland (`wl-copy`), then X11 (`xclip`), and
  falls back to OSC52 so copying works over SSH and inside tmux.
  `pbpaste` reads only from a verified local backend; over SSH it
  **fails loudly** instead of returning something stale
  (OSC52 is write-only). Use `pbpaste --tmux-buffer` to read the tmux
  paste buffer explicitly, and `--backend` on either to see which backend
  would be used.
- 🪟 **tmux** with a `Ctrl+A` prefix: `prefix s` / `prefix v` split
  below / beside, `prefix S` picks a session with fzf, and `Ctrl+V` is a
  smart paste that also lets Codex / Claude Code attach clipboard images.
  Shell aliases: `tl` (ls), `tn` (new), `ta` (attach), `tk` (kill),
  `td` (detach) and `trn` (rename-session; not `tr`, which stays coreutils).
- 💾 **tmux auto save / restore** with tmux-resurrect (Codex / Claude TUIs
  included): saved every minute (the timer starts at install), restored at
  desktop login; snapshots live
  in `~/.local/share/tmux/resurrect`.
  See [docs/tmux-auto-restore.ko.md](docs/tmux-auto-restore.ko.md).
- 🐚 **zsh widgets**: `Ctrl+E` fuzzy directory picker, `Ctrl+S` git status
  without losing your prompt, and paste repair — a pasted shell fence or a
  display-wrapped absolute path is fixed automatically, and `Alt+J` repairs
  other terminal-wrapped commands on demand.
- 🧭 **GNOME navigation** for windows, workspaces, monitors and
  applications, plus **`Ctrl+Super+Left` / `Ctrl+Super+Right`** to switch
  tabs in Chrome and GNOME Terminal (via an input-remapper preset;
  Ubuntu 24.04 only).
  Applied immediately inside a GNOME session, otherwise at your next
  graphical login.

Every key is listed in [docs/SHORTCUTS.md](docs/SHORTCUTS.md).

## 🛟 Recovery

Nothing is overwritten without a backup. To put your machine back exactly
as it was before the first install:

```bash
python3 ~/.dotfiles/install.py restore --baseline
```

You can also restore the state from before a specific run
(`restore --run <RUN_ID>`) or just one config (`--id <ID>`), and a restore
refuses to clobber files you edited after installing unless you add
`--force`. Restores cover the managed configs (and, for a full
`--baseline`, the GNOME settings); they do not uninstall apt packages or
pinned tools. Backups live in `~/.local/state/personal-dotfiles/backups/`:

- `baseline/` — the very first original of every managed path;
- `runs/<RUN_ID>/` — what each run replaced;
- `pre-install-<TS>/dotfiles` — a previous `~/.dotfiles` that the one-liner
  moved aside.

They may contain your **original, possibly sensitive** configs, so they are
private (`0700`), stay on this machine and are never uploaded.

Full details: [docs/RECOVERY.md](docs/RECOVERY.md).

## 🧪 Support

| Ubuntu | amd64 | arm64 |
| ------ | ----- | ----- |
| 24.04  | see matrix | see matrix |
| 22.04  | see matrix (tmux 3.2a, degraded) | see matrix (tmux 3.2a, degraded) |

The honest, per-cell status (install, update, restore, GNOME, input-remapper)
is in [docs/SUPPORT.md](docs/SUPPORT.md).


## Acknowledgements

This repository is derived from [wookayin/dotfiles][upstream] by
[Jongwook Choi (@wookayin)][wookayin], and keeps its full upstream commit
history and MIT license. The vim/neovim, zsh, tmux and helper-script
foundations here are his work — and so are the shape of this README, the
one-liner, `install.py` and `dotfiles`; the Ubuntu package profile,
backup-and-restore installer, desktop integration and clipboard helpers
are the changes made on top.

It is an independent repository rather than a GitHub fork, so that nothing in
it can be pushed back to upstream by accident. Upstream is tracked as a
fetch-only remote:

```sh
git remote add upstream \
  https://github.com/wookayin/dotfiles.git
git remote set-url --push upstream no-push
```

Please report issues with this repository here, not upstream.

[upstream]: https://github.com/wookayin/dotfiles
[wookayin]: https://github.com/wookayin
[neovim]: https://github.com/neovim/neovim
