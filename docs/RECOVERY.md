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
| *Installing packages* (apt packages, pinned tools under `~/.local/share/personal-dotfiles/tools`) | Runs before the transaction and is **not** undone: whatever was installed stays installed, and the run stops before touching your configs. |
| *Creating symbolic links* (every managed path) | **Rolled back**: all paths return to their state before the run. |
| *Post actions* (tmux plugins, systemd user units and the autosave timer, zsh/tmux smoke checks, login shell, git identity, sign-in check) | **Not** undone: the new configs stay in place. The login shell is only changed after the smoke checks passed; replaced tmux plugin checkouts are kept under `backups/tmux-plugins/<RUN_ID>/`. |
| *GNOME settings* | **Not** undone automatically; previous values are recorded (see [GNOME settings](#gnome-settings)) and returned by `restore --baseline`. |

So after a failure in a later step, either fix the reported cause and run
the install again, or undo that run's config changes yourself with
`restore --run <RUN_ID>` (below). No restore uninstalls apt packages or
pinned tools; remove those with `apt` or by deleting
`~/.local/share/personal-dotfiles/tools` and the links in `~/.local/bin`.

All commands run as your normal user (no `sudo`). `dotfiles status`,
`dotfiles restore ...` and `dotfiles repair` are the same commands as
`python3 ~/.dotfiles/install.py ...` below.


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

If you set `XDG_STATE_HOME`, substitute it for `~/.local/state`.

- `<ID>` is a managed entry id from
  [`manifests/managed-paths.json`](../manifests/managed-paths.json)
  (for example `zshrc`).
- `<RUN_ID>` looks like `20260928T031500Z-1a2b3c4d` (UTC time + random).
- `<TS>` is the UTC time the one-liner moved the old `~/.dotfiles` aside;
  it prints the exact path when it does.
- Directories are `0700` and files `0600`.

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

`~/.gitconfig`, `~/.config/pudb/pudb.cfg` and the systemd user units are
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


## Credentials

The installer never copies or restores SSH keys, GPG keys, git identity
(`~/.gitconfig.secret`), Codex/Claude logins or browser data — they are
not managed paths, so none of the commands above ever touch them.
