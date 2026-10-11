# 🛟 Recovery

Every install and update changes your configs in one transaction (the
*Creating symbolic links* step): each managed path is backed up before it is
replaced, and if that step fails, or the run is killed during it, every path
it touched is put back automatically (after a kill, by the next run). This
page is about the other direction — deliberately putting things back.


## What a failed run leaves behind

Only the config transaction is rolled back, and only when **it** fails.

| Step | On failure |
| ---- | ---------- |
| *Installing packages* (apt packages, or Homebrew formulae on macOS; pinned tools under `~/.local/share/personal-dotfiles/tools`) | Runs before the transaction and is **not** undone: whatever was installed stays installed, and the run stops before touching your configs. |
| *Creating symbolic links* (every managed path) | **Rolled back**: all paths return to their state before the run. |
| *Post actions* (tmux plugins, systemd user units and the autosave timer or, on macOS, the two LaunchAgents, zsh/tmux smoke checks, login shell, git identity, sign-in check) | **Not** undone: the new configs stay in place. The login shell is only changed after the smoke checks passed; replaced tmux plugin checkouts are kept under `backups/tmux-plugins/<RUN_ID>/`. |
| *GNOME settings* (not on macOS) | **Not** undone automatically; previous values are recorded (see [GNOME settings](#gnome-settings)) and returned by `restore --baseline`. |

So after a failure in a later step, either fix the reported cause and run
the install again, or undo that run's config changes yourself with
`restore --run <RUN_ID>` (below). No restore uninstalls apt packages,
Homebrew formulae or pinned tools; remove those with `apt` or
`brew uninstall`, or by deleting `~/.local/share/personal-dotfiles/tools`
and the links in `~/.local/bin`.

All commands run as your normal user (no `sudo`). `dotfiles status`,
`dotfiles restore ...` and `dotfiles repair` are the same commands as
`python3 ~/.dotfiles/install.py ...` below. On macOS, `python3` is usually
the Command Line Tools' 3.9, which `install.py` refuses: use the `dotfiles`
commands there, or `"$(brew --prefix)/bin/python3" ~/.dotfiles/install.py ...`.


## Where things live

| What | Path |
| ---- | ---- |
| The checkout (a normal git clone) | `~/.dotfiles` |
| State root | `~/.local/state/personal-dotfiles/` |
| Installed state | `~/.local/state/personal-dotfiles/state.json` |
| Last status | `~/.local/state/personal-dotfiles/status.json` |
| Per-run journal | `~/.local/state/personal-dotfiles/journal/<RUN_ID>.json` |
| Baseline backups | `~/.local/state/personal-dotfiles/backups/baseline/<ID>/` |
| Per-run backups | `~/.local/state/personal-dotfiles/backups/runs/<RUN_ID>/<ID>/` |
| A previous `~/.dotfiles` | `~/.local/state/personal-dotfiles/backups/pre-install-<TS>/dotfiles` |
| GNOME settings state and backups | `~/.local/state/personal-dotfiles/gui/` |
| LaunchAgents (macOS) | `~/Library/LaunchAgents/com.github.leekyungmoon.dotfiles.tmux.plist` and `…tmux-resurrect-autosave.plist` |
| Their logs (macOS) | `~/.local/state/personal-dotfiles/launchd/<label>.log` (and one older `.log.1`) |
| tmux-continuum's own launcher, set aside (macOS) | `~/.local/state/personal-dotfiles/backups/launchd/<RUN_ID>/Tmux.Start.plist` |
| `tmux-256color` compiled by the installer (macOS) | `~/.terminfo/` (not a managed path; no restore removes it) |

If you set `XDG_STATE_HOME`, substitute it for `~/.local/state`.

- `<ID>` is a managed entry id from
  [`manifests/managed-paths.json`](../manifests/managed-paths.json)
  (for example `zshrc`).
- `<RUN_ID>` looks like `20260928T031500Z-1a2b3c4d` (UTC time + random).
- `<TS>` is the UTC time the one-liner moved the old `~/.dotfiles` aside;
  it prints the exact path when it does.
- Directories are `0700` and files `0600`. On macOS, launchd itself
  creates the LaunchAgents' log files, inside their `0700` directory.

> ⚠️ **Backups may contain sensitive originals** — whatever was at a
> managed path before (an old `~/.zshrc` with tokens in it, for example).
> They stay on this machine. Do not commit, upload or share the
> `backups/` directory.


## Two kinds of backup

- **Baseline** — the very first state of each path, captured the first
  time the installer ever manages it. This includes *absence*: if
  `~/.tmux.conf` did not exist, the baseline records that, and restoring
  it deletes the file again. A baseline entry is written once and never
  rewritten, even by later installs.
- **Per-run** — before each run replaces a path, the previous content is
  saved under that run's id. This lets you step back one update at a time.

Backups keep the exact type, bytes, permission bits and symlink text
(dangling links included).

When the installer replaces something, it prints
`backed up to <path>, replaced` next to that target, so you can see right
away where the original went.


## See what is installed

```bash
python3 ~/.dotfiles/install.py status
python3 ~/.dotfiles/install.py status --json
```

This shows the last run, the installed commit, and for every managed id
whether the live file still matches what was installed. An id that no
longer matches has **drifted** — you (or another program) edited it after
the install.


## Copies you changed are kept

`~/.gitconfig`, `~/.config/pudb/pudb.cfg` and the systemd user units (on
macOS, the two LaunchAgent plists, written for your home) are
**copies**, not links. When one of them has
drifted — `git config --global ...`, `gh auth setup-git`, an application
saving its preferences, a hand edit — `dotfiles update`, `dotfiles repair`
and `python3 ~/.dotfiles/install.py` leave it as it is and report
`kept your local changes (use -f to overwrite)`. A deleted copy is simply
written again.

To take the shipped version anyway (your version is backed up first, under
that run's `backups/runs/<RUN_ID>/`):

```bash
python3 ~/.dotfiles/install.py -f
```

Keep your git identity and other private settings in
`~/.gitconfig.secret`, which is never managed or overwritten.

Linked configs (`~/.zshrc`, `~/.vim`, ...) point into `~/.dotfiles`, so
edits to them are edits to the checkout; see
[Local edits in `~/.dotfiles`](#local-edits-in-dotfiles).

To list the runs you can restore to:

```bash
ls ~/.local/state/personal-dotfiles/backups/runs
```


## Restore everything to before the first install

```bash
python3 ~/.dotfiles/install.py restore --baseline
```

On macOS (`dotfiles restore --baseline`) this also removes the two
LaunchAgent plists and unloads them from launchd; a running tmux server
keeps running.


## Restore to before a specific run

```bash
python3 ~/.dotfiles/install.py restore --run <RUN_ID>
```

Use a `<RUN_ID>` from the listing above; it restores every path to the
state it had just before that run changed it.


## Restore a single config

Limit any restore to one or more managed ids (repeat `--id`):

```bash
python3 ~/.dotfiles/install.py restore --baseline --id zshrc
python3 ~/.dotfiles/install.py restore --run <RUN_ID> --id zshrc --id vimrc
```

The ids are the `"id"` fields in
[`manifests/managed-paths.json`](../manifests/managed-paths.json).


## Drift and `--force`

A restore first snapshots the *current* state as a new run, so a restore
can itself be undone with `restore --run <that RUN_ID>`.

If a target has drifted from what the installer last wrote, the restore
**refuses** to touch it and tells you which ids drifted — your edits are
never discarded silently. When you really want the backup back anyway:

```bash
python3 ~/.dotfiles/install.py restore --baseline --force
```

Even with `--force`, the drifted content is saved in the new run's
backup before being replaced.


## A previous `~/.dotfiles`

The one-liner only reuses an existing `~/.dotfiles` when it is a clean
checkout of this repository (it then runs `git pull --ff-only`). Anything
else — another dotfiles repo, a checkout with local changes, a plain
directory or a symlink — is **moved, never deleted**, to
`~/.local/state/personal-dotfiles/backups/pre-install-<TS>/dotfiles`,
but only after the new clone (made first in a temporary
`~/.dotfiles.new-<TS>-<PID>` next to it) has succeeded. If the clone fails,
the old `~/.dotfiles` is left exactly where it was.

To go back to it, first restore your configs (this uses the new checkout's
`install.py`), then swap the directories:

```bash
python3 ~/.dotfiles/install.py restore --baseline
mv ~/.dotfiles ~/.dotfiles.replaced
mv ~/.local/state/personal-dotfiles/backups/pre-install-<TS>/dotfiles ~/.dotfiles
```

Delete `~/.dotfiles.replaced` yourself once you no longer need it.


## Local edits in `~/.dotfiles`

`~/.dotfiles` is an ordinary git checkout, so `git -C ~/.dotfiles status`
and `git diff` show what you changed — including lines that tools such as
`conda init` or `p10k configure` appended to the linked rc files (move those
to `~/.zshrc.local`, `~/.zshenv.local` or `~/.bashrc.local`).

`dotfiles update` stashes local edits to tracked files (a stash entry named
`DOTFILES_UPDATE`) before fast-forwarding and re-applies them afterwards.
Only the entry this update created is used — it is identified by its commit
id, so older stashes of yours are never applied or dropped. If the edits no
longer apply cleanly:

- the checkout is reset to the new commit when it was otherwise clean, so no
  conflict markers are left in your files (otherwise check `git status`);
- your edits stay, unchanged, in that stash entry;
- the update prints the entry's id and the commands to get them back, and
  exits with status 3 (or the installer's status, if the install failed):

```bash
cd ~/.dotfiles
git stash apply --index <STASH_ID>   # resolve the conflicts
git stash drop <STASH_REF>           # once you are done
```


## Interrupted runs

If a run was killed half-way (power loss, closed terminal), the next
install, update or restore finds its journal, rolls the incomplete run
back, and only then continues.

If you changed one of those paths between the crash and the next run, your
version is not thrown away by the rollback: it is saved first to

```text
~/.local/state/personal-dotfiles/backups/recovery/<RECOVERY_RUN>/<INTERRUPTED_RUN>/<ID>/object
```

and the run prints a "changed after an interrupted run; your version was
saved to …" line. That is the one case where you merge something back by
hand; otherwise there is nothing to do.

Only one run can hold `~/.local/state/personal-dotfiles/install.lock` at a
time; a second run exits with a "concurrent run" error instead of
waiting or interleaving.


## GNOME settings

GNOME settings and the input-remapper preset are not files, so they are not
managed ids. Before a GSettings key is first written, its previous value (or
the fact that it was unset) is recorded under
`~/.local/state/personal-dotfiles/gui/backups/`: `baseline/` keeps the first
value ever seen, and each run keeps the values it replaced.


## macOS: the LaunchAgents

On a Mac, two LaunchAgents take the systemd units' place:
`com.github.leekyungmoon.dotfiles.tmux` starts tmux at login and
`com.github.leekyungmoon.dotfiles.tmux-resurrect-autosave` saves it every
minute ([how they work](tmux-auto-restore.ko.md#macos), in Korean). Like
everything on macOS here, these steps are pending verification on a real
Mac.

What launchd has loaded, each agent's log, and the age of the last save:

```bash
dotfiles services
```

| The install or repair says | What to do |
| -------------------------- | ---------- |
| post-install `RELOGIN_REQUIRED` (`launchd-gui-domain-unavailable`) | It ran over SSH, or without a desktop login. The plists are in place and load at your next desktop login; to load them now, run the `launchctl bootstrap gui/$(id -u) ...` commands it printed in a Terminal of that desktop session. |
| `launchctl bootstrap failed (5: Input/output error)` | System Settings → General → Login Items & Extensions → *Allow in the Background*: turn it on for the agent (it may be listed as `sh` or by its script's name). Then run `dotfiles repair` in Terminal, not over SSH. |
| `tmux: disabled (enable: dotfiles services enable tmux)` | You turned it off (`dotfiles services disable`); repairs leave it off. `dotfiles services enable tmux` turns it back on. |
| `tmux started but did not restore the last snapshot (see <log>)` | The server runs without your snapshot; the log says why. Press prefix + C-r in tmux to restore it, or keep the current sessions and let saves go on: `tmux set-option -g @tmux-restore-complete on`. `tmux show-option -gqv @tmux-restore-status` shows the outcome. |
| `` the tmux LaunchAgent could not start tmux (exit status 1: tmux not found, HOME unusable, or `tmux new-session` failed); see <log> `` | The log's last lines say which: `tmux not found on PATH; run: brew install tmux && dotfiles repair`, `HOME is not an absolute path: ...`, the shell's own `cd: ...` error (HOME names a directory that cannot be entered), or `tmux new-session failed (exit status 1); nothing was restored`, usually with tmux's own error just above it. Once the cause is fixed, a tmux started in Terminal restores the last snapshot. |
| `the tmux LaunchAgent failed (exit status <N>)` | 126 or 127: tmux was found but could not be executed (the log says why); `brew reinstall tmux`, then `dotfiles repair`. Above 128: tmux was killed by a signal (128 + the signal's number); the log has the details. 78 (EX_CONFIG): launchd could not start the agent's script at all (its log directory or the script is missing), so there may be no log; run `dotfiles repair`. |
| `the tmux LaunchAgent was stopped (<signal>)` | The agent's script was killed by a signal; the log has the details. |
| `<label> was still loaded 10 s after launchctl bootout; run: launchctl bootout gui/$(id -u)/<label>` | launchd had not let the agent go yet (also from `dotfiles services disable`, whose disable is recorded anyway, and from `restore`). Run the printed command; `launchctl print gui/$(id -u)/<label>` fails once the agent is gone. |
| `process inspection unavailable: Codex/Claude resume commands will not be saved ...` | Saves still keep layouts, directories and pane contents; Codex and Claude panes come back as shells. `~/.dotfiles/tmux/resurrect-ai-session.py --self-test` says why. |
| `could not compile the tmux-256color terminal description: ...; run: /usr/bin/tic ...` | Run the printed command. |
| any other `launchctl ... failed (<code>: ...)` | The message ends with the command to run; `launchctl error <code>` explains the code. |

Turning them off, or removing them:

- `dotfiles services disable [tmux|autosave]` unloads an agent and marks
  it disabled in launchd: installs, updates and repairs leave it off until
  `dotfiles services enable [tmux|autosave]`. Without a name it acts on
  both. A running tmux server is not stopped. An agent in the middle of a
  run (the autosave saving) is given up to 10 s to leave launchd; one still
  loaded then is reported with the `launchctl bootout` command to run.
- `dotfiles restore --baseline --id launchd-tmux --id
  launchd-tmux-resurrect-autosave` removes both plists and unloads both
  jobs, and nothing else; `dotfiles restore --baseline` does it together
  with everything else. The next `dotfiles repair` installs them again.
- When the installer cannot run at all: `launchctl bootout
  gui/$(id -u)/com.github.leekyungmoon.dotfiles.tmux` (and the same for
  `...tmux-resurrect-autosave`), then delete the two plists.

A `restore --run <RUN_ID>` that puts back an older plist leaves launchd
running the definition it had loaded until your next login or
`dotfiles repair`.

tmux-continuum's own launcher, `~/Library/LaunchAgents/Tmux.Start.plist`
(left by an earlier `@continuum-boot on`; it opens a Terminal window at
login and types `tmux`), is unloaded and moved to
`~/.local/state/personal-dotfiles/backups/launchd/<RUN_ID>/` by every
install and repair on macOS, never deleted.

A failed Homebrew step stops the run before your configs are touched:
run `brew doctor`, fix what it reports, then `dotfiles repair`. A formula
that is installed but not linked fails with the `brew link <formula>`
command to run.


## Credentials

The installer never copies or restores SSH keys, GPG keys, git identity
(`~/.gitconfig.secret`), Codex/Claude logins or browser data — they are
not managed paths, so none of the commands above ever touch them. On
macOS, Apple's and Homebrew's git keep HTTPS credentials in your login
Keychain (`osxkeychain`); no restore touches it either.
