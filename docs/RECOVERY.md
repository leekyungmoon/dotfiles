# 🛟 Recovery

Every install and update is a transaction: each managed path is backed up
before it is replaced, and a run that fails part-way is rolled back
automatically. This page is about the other direction — deliberately
putting things back.

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
`~/.local/state/personal-dotfiles/backups/pre-install-<TS>/dotfiles`.

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
and `git diff` show what you changed. `dotfiles update` stashes local edits
(`DOTFILES_UPDATE`) before fast-forwarding and pops them back afterwards;
if the pop conflicts, your edits are still in `git stash list`.


## Interrupted runs

If a run was killed half-way (power loss, closed terminal), the next
install, update or restore finds its journal, rolls the incomplete run
back, and only then continues. You do not need to do anything by hand.

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
