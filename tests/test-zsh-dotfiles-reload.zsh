#!/usr/bin/env zsh
# zsh/zsh.d/dotfiles-reload.zsh in real interactive shells on a pseudo-terminal.
#
# Two tiers, both driven by the embedded python3 script, each shell with a
# temporary HOME, ZDOTDIR and XDG_{STATE,DATA,CACHE,CONFIG}_HOME:
#
# fixture  A small ~/.zshrc with the history options this repository ends up
#          with (prezto's history module, zsh/zsh.d/envs.zsh) that sources
#          only the reload module (and, for login shells, the repository's
#          zsh/zprofile). No network.
# real     The repository's own zshenv, zprofile, zshrc, zpreztorc and zsh/
#          directory (~/.zsh -> the repository's zsh/*, ~/.zshrc -> zsh/zshrc,
#          ...) with the whole plugin stack: antidote and fasd at the commits
#          the repository pins, and the antidote bundle (prezto, zsh-vi-mode,
#          powerlevel10k, F-Sy-H, zsh-autosuggestions, ...) cloned by antidote
#          itself into the temporary XDG data dir. That needs network reads
#          (git clone), unless RELOAD_TEST_PLUGIN_CACHE names a directory with
#          antidote/, fasd/ and antidote-home/ from an earlier run. Without
#          them the tier fails, or is skipped with RELOAD_TEST_ALLOW_SKIP=1.
#          RELOAD_TEST_TIERS="fixture" (or "real") runs one tier only.
#
# An "update" in a test writes a new generation and changes what the setup
# defines: the new setup has an alias 'td' that the old one lacks.
#
# Checked: the first command typed after a generation change runs in the new
# shell, exactly once, and enters the history once (insert mode, vi normal
# mode, an accepted autosuggestion, a bracketed multi-line paste); during a
# continuation (PS2), heredoc, select, vared or spelling-correction prompt
# nothing happens until the command ran, then the reload comes before the
# next primary prompt; an aborted (^C) line never runs; exported variables
# (venv, ROS overlay, PATH), cwd, SHLVL, user fds, login-ness, a login shell's
# umask and conda environment survive; jobs defer the reload; no reload loop;
# a broken zsh on PATH keeps the shell, silently, without retries; missing,
# FIFO and empty generation files are harmless; the hook mark (one fd on
# {state}/personal-dotfiles/shell-hook) is held from startup on, also across
# the reload, is close-on-exec (commands and 'exec bash' do not have it);
# a missing state directory is created 0700 with the file 0600; a symlinked,
# group-writable or unwritable one is refused without a message; strace sees
# no fork and no write to the state directory over prompts (fixture tier).
#
# The shells never see TMUX/TMUX_PANE/DBUS/XDG_RUNTIME_DIR; tmux and
# systemctl on their PATH are fakes, and TMUX_TMPDIR is private.
emulate -L zsh
setopt err_return no_unset

repo=${0:A:h:h}
work=$(mktemp -d "${TMPDIR:-/tmp}/zsh-reload-test.XXXXXX")
chmod 700 "$work"
trap 'command chmod -R u+rwx -- "$work" 2>/dev/null; command rm -rf -- "$work"' EXIT

env -u TMUX -u TMUX_PANE -u DBUS_SESSION_BUS_ADDRESS -u XDG_RUNTIME_DIR \
  TMUX_TMPDIR="$work" PYTHONDONTWRITEBYTECODE=1 \
  python3 -B - "$repo" "$work" <<'PY'
import os, pty, re, select, shutil, signal, stat, struct, subprocess, sys, time
import fcntl, termios

repo, work = sys.argv[1], sys.argv[2]
failures, skips, passes = [], [], []
UID = os.getuid()


def check(name, cond, detail=""):
    (passes if cond else failures).append(name if cond else f"{name}: {detail}")
    if not cond:
        print(f"FAIL {name}: {detail}", flush=True)


def strip(text):
    text = re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-9;?<=>]*[ -/]*[@-~]", "", text)
    return re.sub(r"\x1b[78=>()][0-9AB]?", "", text)


def tail(text, n=400):
    """The end of a screen, for a failure message: runs of blanks squeezed."""
    return re.sub(r"[ \t]{3,}", "  ", text)[-n:]


class Shell:
    def __init__(self, env, argv, cwd):
        self.env, self.log_dir = env, env["ZDOTDIR"]
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(cwd)
                os.execvpe(argv[0], argv, env)
            finally:
                os._exit(127)
        self.child, self.fd = pid, fd
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
        self.buf = b""
        self.pid = None

    def pump(self, seconds=0.05):
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            ready, _, _ = select.select([self.fd], [], [], max(0.0, left))
            if ready:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    return False
                self.buf += data
            if time.monotonic() >= end:
                return True

    def wait(self, cond, what, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            if not self.pump(0.05):
                time.sleep(0.05)
        raise TimeoutError(f"{what}; screen: {strip(self.text())[-1200:]!r}")

    def lines(self, name):
        try:
            with open(os.path.join(self.log_dir, name)) as handle:
                rows = [line.split() for line in handle.read().splitlines()]
        except OSError:
            return []
        return [r for r in rows if len(r) >= 2 and (self.pid is None or r[1] == str(self.pid))]

    def prompts(self):
        return self.lines("prompts.log")

    def starts(self):
        return self.lines("starts.log")

    def loaded(self):
        rows = self.prompts()
        return rows[-1][2] if rows else None

    def ready(self, timeout=60):
        # Several shells may share one log: this one is the child (or, under
        # strace, the child's child).
        def mine():
            for row in self.lines("prompts.log"):
                if row[1].isdigit() and (int(row[1]) == self.child
                                         or _ppid(row[1]) == self.child):
                    return int(row[1])
            return None
        self.wait(lambda: mine() is not None, "first prompt", timeout)
        self.pid = mine()
        self.pump(0.2)

    def send(self, text):
        os.write(self.fd, text.encode() if isinstance(text, str) else text)

    def mark(self):
        return len(self.buf), len(self.prompts())

    def run(self, line, timeout=30):
        n = len(self.prompts())
        self.send(line + "\r")
        self.wait(lambda: len(self.prompts()) > n, f"prompt after {line!r}", timeout)
        self.pump(0.15)

    def wait_prompts(self, n, timeout=30):
        self.wait(lambda: len(self.prompts()) >= n, f"prompt #{n}", timeout)
        self.pump(0.15)

    def wait_text(self, rx, since=0, timeout=30):
        pattern = re.compile(rx)
        self.wait(lambda: pattern.search(strip(self.text(since))), f"text {rx!r}", timeout)

    def text(self, since=0):
        return self.buf[since:].decode("utf-8", "replace")

    def close(self):
        for pid in (self.child,):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            os.waitpid(self.child, 0)
        except OSError:
            pass
        os.close(self.fd)


def hook_links(pid):
    out = []
    try:
        names = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return out
    for name in names:
        try:
            target = os.readlink(f"/proc/{pid}/fd/{name}")
        except OSError:
            continue
        if target.replace(" (deleted)", "").endswith("/personal-dotfiles/shell-hook"):
            out.append((int(name), target))
    return out


def write_gen(state, value):
    folder = os.path.join(state, "personal-dotfiles")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    tmp = os.path.join(folder, ".generation.tmp")
    with open(tmp, "w") as handle:
        handle.write(value + "\n")
    os.replace(tmp, os.path.join(folder, "generation"))


def history(home, name=".zsh_history"):
    try:
        with open(os.path.join(home, name), "rb") as handle:
            return [l.decode("utf-8", "replace").split(";", 1)[-1]
                    for l in handle.read().splitlines()]
    except OSError:
        return []


def fakebin(root):
    folder = os.path.join(root, "fakebin")
    os.makedirs(folder, exist_ok=True)
    for tool in ("tmux", "systemctl"):
        path = os.path.join(folder, tool)
        with open(path, "w") as handle:
            handle.write(f"#!/bin/sh\necho 'fake {tool} must not run' >&2\nexit 1\n")
        os.chmod(path, 0o755)
    return folder


def base_env(root, home, zdotdir):
    xdg = {"XDG_STATE_HOME": "state", "XDG_DATA_HOME": "data",
           "XDG_CACHE_HOME": "cache", "XDG_CONFIG_HOME": "config"}
    env = {"HOME": home, "ZDOTDIR": zdotdir, "TERM": "xterm-256color", "LANG": "C.UTF-8",
           "USER": os.environ.get("USER", "user"), "SHELL": shutil.which("zsh") or "zsh",
           "PATH": fakebin(root) + ":/usr/local/bin:/usr/bin:/bin",
           "TMUX_TMPDIR": os.path.join(root, "tmux"), "RELOAD_TEST_REPO": repo}
    os.makedirs(env["TMUX_TMPDIR"], mode=0o700, exist_ok=True)
    for name, sub in xdg.items():
        env[name] = os.path.join(root, sub)
        os.makedirs(env[name], exist_ok=True)
    return env


# ---------------------------------------------------------------- fixture tier

FIXTURE_ZSHRC = r'''
# prezto history module options, then zsh/zsh.d/envs.zsh overrides
setopt BANG_HIST EXTENDED_HISTORY HIST_EXPIRE_DUPS_FIRST HIST_IGNORE_DUPS \
  HIST_IGNORE_ALL_DUPS HIST_FIND_NO_DUPS HIST_IGNORE_SPACE HIST_SAVE_NO_DUPS \
  HIST_VERIFY INC_APPEND_HISTORY
unsetopt SHARE_HISTORY
HISTFILE=$ZDOTDIR/.zsh_history
HISTSIZE=10000 SAVEHIST=10000
# the hook mark before the module ran: held across a reload
() {
  local f links=
  for f in /proc/$$/fd/*(N); do links+=" ${f:t}=${f:A}"; done
  print -r -- "start $$ ${options[login]}$links" >> $ZDOTDIR/starts.log
}
PS1='@@P@@ '
PS2='@@PS2@@ '
PROMPT_EOL_MARK=''
# as zsh/zshenv: the tools dir always first, even when PATH carries it
typeset -gU path
[[ -d $ZDOTDIR/tools ]] && path=($ZDOTDIR/tools ${path:#$ZDOTDIR/tools})
export SETUP_KEPT=from-setup SETUP_OVERRIDE=from-setup
if [[ -e $ZDOTDIR/old-setup ]]; then
  export SETUP_VALUE=old-default
else
  alias td='print -r -- TD-NEW-$$-${_pd_reload_loaded:-none}'
  export SETUP_VALUE=new-default
  [[ -d $ZDOTDIR/added ]] && path=($ZDOTDIR/added $path)
fi
source $RELOAD_TEST_REPO/zsh/zsh.d/dotfiles-reload.zsh
_t_precmd() { print -r -- "prompt $$ ${_pd_reload_loaded:-none}" >> $ZDOTDIR/prompts.log }
precmd_functions+=(_t_precmd)
# last, as zsh/zshrc does
[[ -e $ZDOTDIR/no-startup-call ]] || _pd_reload_startup
'''


class Fixture:
    def __init__(self, name, login=False, gen="g1", state_setup=None, extra_path=None,
                 strace_log=None, unset_state=False, tools=False, runtime=False,
                 startup_call=True):
        self.root = os.path.join(work, "fx-" + name)
        self.home = os.path.join(self.root, "home")
        self.zdot = os.path.join(self.root, "zdot")
        os.makedirs(self.home)
        os.makedirs(self.zdot)
        self.env = base_env(self.root, self.home, self.zdot)
        if tools:
            for sub in ("tools", "added"):
                os.makedirs(os.path.join(self.zdot, sub))
        if runtime:
            self.runtime = os.path.join(self.root, "run")
            os.makedirs(self.runtime, mode=0o700)
            self.env["XDG_RUNTIME_DIR"] = self.runtime
        if not startup_call:
            open(os.path.join(self.zdot, "no-startup-call"), "w").close()
        if unset_state:
            del self.env["XDG_STATE_HOME"]
            self.state = os.path.join(self.home, ".local", "state")
        else:
            self.state = self.env["XDG_STATE_HOME"]
        if extra_path:
            self.env["PATH"] = extra_path + ":" + self.env["PATH"]
        with open(os.path.join(self.zdot, ".zshenv"), "w") as handle:
            handle.write("unsetopt GLOBAL_RCS\n")
        with open(os.path.join(self.zdot, ".zshrc"), "w") as handle:
            handle.write(FIXTURE_ZSHRC)
        with open(os.path.join(self.zdot, ".zprofile"), "w") as handle:
            handle.write(f"source {repo}/zsh/zprofile\n")
        self.old_setup()
        if state_setup:
            state_setup(self)
        elif gen:
            write_gen(self.state, gen)
        self.cwd = os.path.join(self.root, "cwd", "sub dir")
        os.makedirs(self.cwd)
        self.argv = ["zsh", "-l"] if login else ["zsh", "-i"]
        argv = self.argv
        if strace_log:
            argv = ["strace", "-f", "-qq", "-o", strace_log, "-e",
                    "trace=fork,vfork,clone,clone3,execve,openat"] + argv
        self.sh = Shell(self.env, argv, self.cwd)
        self.sh.ready()
        self.others = []

    def another(self):
        """One more shell on the same home, setup and state."""
        sh = Shell(self.env, self.argv, self.cwd)
        sh.ready()
        self.others.append(sh)
        return sh

    def old_setup(self):
        open(os.path.join(self.zdot, "old-setup"), "w").close()

    def update(self, gen):
        try:
            os.unlink(os.path.join(self.zdot, "old-setup"))
        except FileNotFoundError:
            pass
        folder = os.path.join(self.state, "personal-dotfiles")
        mode = os.lstat(folder).st_mode if os.path.lexists(folder) else None
        if mode is not None and stat.S_ISDIR(mode) and not mode & 0o200:
            os.chmod(folder, 0o700)  # as the installer, for this test only
            write_gen(self.state, gen)
            os.chmod(folder, stat.S_IMODE(mode))
        else:
            write_gen(self.state, gen)

    def close(self):
        for sh in [self.sh] + self.others:
            sh.close()


def case(name):
    def wrap(fn):
        CASES.append((name, fn))
        return fn
    return wrap


CASES = []


@case("fixture: unchanged generation never reloads")
def _():
    fx = Fixture("unchanged")
    sh = fx.sh
    for _ in range(3):
        sh.run("print -r -- same-$_pd_reload_loaded")
    sh.run("")
    check("unchanged: one start", len(sh.starts()) == 1, sh.starts())
    check("unchanged: one hook fd", len(hook_links(sh.pid)) == 1, hook_links(sh.pid))
    fx.close()


@case("fixture: sourcing the module again keeps its state")
def _():
    fx = Fixture("resource")
    sh = fx.sh
    sh.run("source $RELOAD_TEST_REPO/zsh/zsh.d/dotfiles-reload.zsh; "
           "source $RELOAD_TEST_REPO/zsh/zsh.d/dotfiles-reload.zsh")
    check("resource: one hook fd", len(hook_links(sh.pid)) == 1, hook_links(sh.pid))
    check("resource: hooks not doubled",
          sh.loaded() == "g1" and len(sh.starts()) == 1, sh.starts())
    since = len(sh.buf)
    sh.run("print -r -- PC-${#${(M)precmd_functions:#_pd_reload_precmd}}; "
           "zstyle -L zle-line-finish; zstyle -L zle-line-init")
    out = strip(sh.text(since))
    check("resource: one precmd and one line hook each",
          "PC-1" in out and out.count(":_pd_reload_line_finish") == 1
          and out.count(":_pd_reload_line_init") == 1, out[-300:])
    fx.update("g2")
    since = len(sh.buf)
    sh.send("td\r")
    sh.wait_text(r"TD-NEW-\d+-g2", since)
    sh.pump(0.3)
    check("resource: still reloads, once", strip(sh.text(since)).count("TD-NEW-") == 1
          and len(sh.starts()) == 2, sh.starts())
    fx.close()


@case("fixture: first command after an update runs once, in the new shell")
def _():
    fx = Fixture("first-command")
    sh = fx.sh
    sh.run("print -r -- before")
    fx.update("g2")
    since = len(sh.buf)
    sh.send("td\r")
    sh.wait_text(r"TD-NEW-\d+-g2", since)
    sh.wait(lambda: len(sh.prompts()) >= 3 and sh.loaded() == "g2", "prompt after td")
    sh.pump(0.3)
    out = strip(sh.text(since))
    check("first: td ran once", out.count("TD-NEW-") == 1, out[-500:])
    check("first: same pid", f"TD-NEW-{sh.pid}-g2" in out, out[-300:])
    check("first: never in the old shell", "command not found" not in out, out[-300:])
    check("first: two starts", len(sh.starts()) == 2, sh.starts())
    check("first: history once", history(fx.zdot).count("td") == 1, history(fx.zdot))
    # no reload loop
    for _ in range(3):
        sh.run("print -r -- after")
    sh.run("")
    check("first: no loop", len(sh.starts()) == 2, sh.starts())
    check("first: one hook fd after the reload", len(hook_links(sh.pid)) == 1,
          hook_links(sh.pid))
    rows = sh.starts()
    check("first: mark held while the new shell started",
          len(rows) == 2 and not any("shell-hook" in f for f in rows[0][3:])
          and sum("shell-hook" in f for f in rows[1][3:]) == 1, rows)
    since = len(sh.buf)
    sh.run("print -r -- LEAK-${#${(M)${(f)\"$(env)\"}:#_PD_RELOAD*}}")
    check("first: no _PD_RELOAD_* in the environment", "LEAK-0" in strip(sh.text(since)),
          strip(sh.text(since))[-200:])
    fx.close()


@case("fixture: the kept line: large, private, never in the environment")
def _():
    fx = Fixture("kept-line")
    sh = fx.sh
    fx.update("g2")
    big = "x" * 150000
    since = len(sh.buf)
    sh.send("\x1b[200~: " + big + "; print -r -- BIG-${#${:-" + big[:10] + "}}-$_pd_reload_loaded\x1b[201~")
    sh.pump(1.0)
    sh.send("\r")
    sh.wait_text(r"BIG-10-g2", since, timeout=60)
    check("kept: a 150 kB line ran in the new shell", len(sh.starts()) == 2, sh.starts())
    fx.update("g3")
    since = len(sh.buf)
    sh.send(" print -r -- HIDDEN-${:-SECRET}-$_pd_reload_loaded\r")
    sh.wait_text(r"HIDDEN-SECRET-g3", since)
    sh.pump(0.3)
    with open(f"/proc/{sh.pid}/environ", "rb") as handle:
        environ = handle.read()
    check("kept: not in /proc/<pid>/environ", b"HIDDEN" not in environ and b"xxxxxxxx" not in environ,
          [e for e in environ.split(b"\0") if b"PD_RELOAD" in e])
    check("kept: a leading-space line stays out of the history",
          not any("HIDDEN" in l for l in history(fx.zdot)), history(fx.zdot)[-3:])
    left = os.listdir(os.path.join(fx.state, "personal-dotfiles"))
    check("kept: no file left behind", sorted(left) == ["generation", "shell-hook"], left)
    check("kept: one hook fd, no other inherited copy",
          len(hook_links(sh.pid)) == 1 and not [l for _, l in _fd_links(sh.pid) if ".line." in l],
          _fd_links(sh.pid))
    fx.close()


def _fd_links(pid):
    out = []
    for name in os.listdir(f"/proc/{pid}/fd"):
        try:
            out.append((int(name), os.readlink(f"/proc/{pid}/fd/{name}")))
        except OSError:
            pass
    return out


@case("fixture: PS2 and heredoc wait for the command, then reload")
def _():
    for kind, first, middle, last, expect in (
            ("ps2", "for i in 1 2; do", "print -r -- LOOP-$i-$_pd_reload_loaded", "done",
             ["LOOP-1-g1", "LOOP-2-g1"]),
            ("heredoc", "cat <<EOF", "HD-$_pd_reload_loaded", "EOF", ["HD-g1"])):
        fx = Fixture(kind)
        sh = fx.sh
        since = len(sh.buf)
        sh.send(first + "\r")
        sh.wait_text(r"@@PS2@@|heredoc>", since)
        fx.update("g2")
        sh.send(middle + "\r")
        sh.pump(1.0)
        check(f"{kind}: no exec in the middle", len(sh.starts()) == 1, sh.starts())
        since = len(sh.buf)
        sh.send(last + "\r")
        sh.wait(lambda: sh.loaded() == "g2", f"{kind}: reload after the command")
        sh.pump(0.3)
        out = re.findall(r"(?:LOOP-\d|HD)-g\d+", strip(sh.text(since)))
        check(f"{kind}: ran in the old shell, once", out == expect, out)
        check(f"{kind}: reloaded before the next prompt", len(sh.starts()) == 2, sh.starts())
        fx.close()


@case("fixture: select, vared and spelling correction are left alone")
def _():
    for kind, start, waitfor, answer, expect in (
            ("select", "select x in aa bb; do print -r -- SEL-$x-$_pd_reload_loaded; break; done",
             r"\?# ", "1", "SEL-aa-g1"),
            ("vared", "v=; vared -p \"VA${:-RED}> \" v; print -r -- VAR-$v-$_pd_reload_loaded",
             r"VARED> ", "xy", "VAR-xy-g1"),
            ("correct", "ehco CORR-$_pd_reload_loaded", r"\[nyae\]\? ", "y",
             "CORR-g1")):
        fx = Fixture(kind)
        sh = fx.sh
        if kind == "correct":
            sh.run("setopt correct")
        since = len(sh.buf)
        sh.send(start + "\r")
        sh.wait_text(waitfor, since)
        fx.update("g2")
        sh.pump(0.5)
        check(f"{kind}: no exec while it waits", len(sh.starts()) == 1, sh.starts())
        since = len(sh.buf)
        sh.send(answer + ("" if kind == "correct" else "\r"))
        sh.wait(lambda: sh.loaded() == "g2", f"{kind}: reload after it ran")
        sh.pump(0.3)
        out = strip(sh.text(since))
        check(f"{kind}: ran in the old shell", expect in out, out[-300:])
        fx.close()


@case("fixture: an aborted line never runs")
def _():
    fx = Fixture("abort")
    sh = fx.sh
    sh.send("print -r -- ABORT${:-ED}-RAN")
    sh.pump(0.3)
    fx.update("g2")
    since = len(sh.buf)
    sh.send("\x03")
    sh.pump(1.0)
    sh.send("print -r -- AFTER-$_pd_reload_loaded\r")
    sh.wait_text(r"AFTER-g2", since)
    sh.pump(0.3)
    out = strip(sh.text(since))
    check("abort: never ran", "ABORTED-RAN" not in out, out[-300:])
    check("abort: not in history", not any("ABORT" in l for l in history(fx.zdot)),
          history(fx.zdot))
    fx.close()


@case("fixture: a line aborted earlier, typed again, runs in the new shell")
def _():
    # ZLE_LINE_ABORTED keeps an earlier ^C'd line; it must not count as an
    # abort of the line accepted now.
    fx = Fixture("abort-again")
    sh = fx.sh
    sh.send("td")
    sh.pump(0.3)
    sh.send("\x03")
    sh.pump(0.5)
    sh.run("")
    fx.update("g2")
    since = len(sh.buf)
    sh.send("td\r")
    sh.wait_text(r"TD-NEW-\d+-g2", since)
    sh.wait(lambda: sh.loaded() == "g2", "abort-again: new prompt")
    out = strip(sh.text(since))
    check("abort-again: td ran once, in the new shell",
          out.count("TD-NEW-") == 1 and f"TD-NEW-{sh.pid}-g2" in out
          and "command not found" not in out, out[-300:])
    check("abort-again: one reload", len(sh.starts()) == 2, sh.starts())
    fx.close()


@case("fixture: the history list stays this shell's own")
def _():
    fx = Fixture("history-own")
    a = fx.sh
    b = fx.another()
    a.run("print -r -- EARLIER-${:-A}")
    a.run("fc -ln 1 >| $ZDOTDIR/a-before")
    a.run("print -r -- FROM-${:-A}")
    b.run("print -r -- FROM-${:-B}")
    fx.update("g2")
    since = len(a.buf)
    a.send("r\r")   # re-runs the previous command at once, without asking
    a.wait(lambda: a.loaded() == "g2", "history-own: reload")
    a.pump(0.3)
    out = strip(a.text(since))
    check("history-own: r re-ran this shell's command",
          re.findall(r"^FROM-\w+", out, re.M) == ["FROM-A"], out[-300:])
    a.run("fc -ln 1 >| $ZDOTDIR/a-after")
    with open(os.path.join(fx.zdot, "a-before")) as h1, \
            open(os.path.join(fx.zdot, "a-after")) as h2:
        before, after = h1.read().splitlines(), h2.read().splitlines()
    # (HIST_IGNORE_ALL_DUPS moves a re-run command to the end)
    check("history-own: this shell's list, not the other shell's",
          set(before) <= set(after) and not any("FROM-${:-B}" in l for l in after),
          (before, after))
    since = len(a.buf)
    a.send("\x1b[A")   # up-history
    a.pump(0.5)
    check("history-own: Up shows this shell's last command",
          "fc -ln 1 >| $ZDOTDIR/a-after" in strip(a.text(since)), strip(a.text(since))[-200:])
    a.send("\x03")
    a.pump(0.3)
    a.run("print -r -- AFTER-RELOAD")
    check("history-own: new commands still reach HISTFILE",
          "print -r -- AFTER-RELOAD" in history(fx.zdot), history(fx.zdot)[-4:])
    left = [n for n in os.listdir(os.path.join(fx.state, "personal-dotfiles"))
            if n.startswith((".hist", ".handover"))]
    check("history-own: no hand-over file left", left == [], left)
    fx.close()


@case("fixture: an unset HISTFILE or SAVEHIST=0 stays so")
def _():
    for kind, setup, probe in (
            ("unset", "unset HISTFILE", "print -r -- HF-${+HISTFILE}-$_pd_reload_loaded"),
            ("savehist", "SAVEHIST=0", "print -r -- HF-$SAVEHIST-$_pd_reload_loaded")):
        fx = Fixture("private-" + kind)
        sh = fx.sh
        sh.run(setup)
        fx.update("g2")
        since = len(sh.buf)
        sh.send("print -r -- SECRET-${:-ONE}-$_pd_reload_loaded\r")
        sh.wait_text(r"SECRET-ONE-g2", since)
        sh.wait(lambda: sh.loaded() == "g2", f"private {kind}: new prompt")
        sh.run("print -r -- SECRET-${:-TWO}")
        sh.run(probe)
        out = strip(sh.text(since))
        check(f"private {kind}: kept in the new shell", "HF-0-g2" in out, out[-300:])
        check(f"private {kind}: nothing written to the history file",
              not any("SECRET" in l for l in history(fx.zdot)), history(fx.zdot)[-4:])
        fx.close()


@case("fixture: PATH keeps its order; the new setup's entries are added")
def _():
    fx = Fixture("path-order", tools=True)
    sh = fx.sh
    tools, added = os.path.join(fx.zdot, "tools"), os.path.join(fx.zdot, "added")
    sh.run("export PATH=/opt/venv/bin:$PATH")
    since = len(sh.buf)
    sh.run("print -r -- OLD:$PATH")
    old = re.findall(r"OLD:(\S+)", strip(sh.text(since)))[-1].split(":")
    check("path: the setup puts its tools after the venv here",
          old[:2] == ["/opt/venv/bin", tools], old)
    fx.update("g2")
    since = len(sh.buf)
    sh.send("print -r -- NEW:$_pd_reload_loaded:$PATH\r")
    sh.wait_text(r"NEW:g2:", since)
    sh.pump(0.3)
    new = re.findall(r"NEW:g2:(\S+)", strip(sh.text(since)))[-1].split(":")
    check("path: old order, the added entry in front of the one it precedes",
          new == [old[0], added] + old[1:], (old, new))
    fx.close()


@case("fixture: the buffer stack (push-line) comes back after the reload")
def _():
    # Enter hands the typed line over: it runs first, the pushed line then
    # comes back at the next prompt.
    fx = Fixture("stack")
    sh = fx.sh
    sh.send("print -r -- PUSHED-${:-ONE}-$_pd_reload_loaded")
    sh.pump(0.3)
    sh.send("\x1bq")   # push-line
    sh.pump(0.3)
    fx.update("g2")
    since = len(sh.buf)
    sh.send("td\r")
    sh.wait_text(r"TD-NEW-\d+-g2", since)
    sh.wait(lambda: sh.loaded() == "g2", "stack: new prompt")
    sh.pump(0.5)
    sh.send("\r")
    sh.wait_text(r"PUSHED-ONE-g2", since)
    sh.pump(0.3)
    out = strip(sh.text(since))
    check("stack: td first, then the pushed line, once each",
          re.findall(r"TD-NEW-\d+-g2|PUSHED-ONE-g2", out)
          == [f"TD-NEW-{sh.pid}-g2", "PUSHED-ONE-g2"], out[-400:])
    # A reload at the prompt, after a command: the pushed line is at the
    # first prompt of the new shell.
    sh.send("print -r -- PUSHED-${:-TWO}-$_pd_reload_loaded")
    sh.pump(0.3)
    sh.send("\x1bq")
    sh.pump(0.3)
    sh.send("sleep 1\r")
    sh.pump(0.3)
    fx.update("g3")
    sh.wait(lambda: sh.loaded() == "g3", "stack: reload after sleep")
    sh.pump(0.5)
    since = len(sh.buf)
    sh.send("\r")
    sh.wait_text(r"PUSHED-TWO-g3", since)
    check("stack: back at the first prompt after a reload from precmd",
          len(sh.starts()) == 3, sh.starts())
    fx.close()


@case("fixture: exported changes stay; untouched values follow the new setup")
def _():
    fx = Fixture("env-delta")
    sh = fx.sh
    sh.run("export SETUP_OVERRIDE=mine MULTI=$'line 1\\nline \\'2\\'' SESSION_ONLY=1; "
           "unset SETUP_KEPT")
    fx.update("g2")
    since = len(sh.buf)
    sh.send("print -r -- DELTA:$SETUP_VALUE:$SETUP_OVERRIDE:${SETUP_KEPT-unset}:"
            "$SESSION_ONLY:${(q)MULTI}:$_pd_reload_loaded\r")
    sh.wait_text(r"DELTA:.*:g2", since)
    sh.pump(0.3)
    found = re.findall(r"DELTA:\S.*", strip(sh.text(since)))
    want = "DELTA:new-default:mine:unset:1:line\\ 1$'\\n'line\\ \\'2\\':g2"
    check("env-delta: session changes on top of the new setup",
          any(l.strip() == want for l in found), (found, want))
    since = len(sh.buf)
    sh.run("print -r -- EXP:${parameters[SETUP_OVERRIDE]}")
    check("env-delta: still exported", "EXP:scalar-export" in strip(sh.text(since)),
          strip(sh.text(since))[-200:])
    fx.close()


@case("fixture: a venv's deactivate still works after the reload")
def _():
    fx = Fixture("venv")
    sh = fx.sh
    activate = os.path.join(fx.root, "activate")
    with open(activate, "w") as handle:
        handle.write(
            'deactivate () {\n'
            '    if [ -n "${_OLD_VIRTUAL_PATH:-}" ] ; then\n'
            '        PATH="${_OLD_VIRTUAL_PATH:-}"; export PATH; unset _OLD_VIRTUAL_PATH\n'
            '    fi\n'
            '    unset VIRTUAL_ENV\n'
            '    if [ ! "${1:-}" = "nondestructive" ] ; then unset -f deactivate; fi\n'
            '}\n'
            'deactivate nondestructive\n'
            'VIRTUAL_ENV=/opt/fakevenv; export VIRTUAL_ENV\n'
            '_OLD_VIRTUAL_PATH="$PATH"\n'
            'PATH="$VIRTUAL_ENV/bin:$PATH"; export PATH\n')
    since = len(sh.buf)
    sh.run("print -r -- PRE:${PATH%%:*}")
    first = re.findall(r"PRE:(\S+)", strip(sh.text(since)))[-1]
    sh.run(f"source {activate}")
    fx.update("g2")
    since = len(sh.buf)
    sh.send("deactivate; print -r -- DEACT:${VIRTUAL_ENV:-none}:${PATH%%:*}:"
            "${+functions[deactivate]}:$_pd_reload_loaded\r")
    sh.wait_text(r"DEACT:.*:g2", since)
    sh.pump(0.3)
    out = strip(sh.text(since))
    check("venv: deactivate ran in the new shell and restored PATH",
          f"DEACT:none:{first}:0:g2" in out and "command not found" not in out, out[-300:])
    fx.close()


@case("fixture: the directory stack and OLDPWD come back")
def _():
    fx = Fixture("dirs")
    sh = fx.sh
    sh.run("pushd -q /tmp; pushd -q /")
    since = len(sh.buf)
    probe = "print -r -- DIRS:${(j:|:)${(@q)dirstack}}:${(q)OLDPWD}:${(q)PWD}"
    sh.run(probe)
    before = re.findall(r"DIRS:\S+", strip(sh.text(since)))[-1]
    fx.update("g2")
    since = len(sh.buf)
    sh.send(probe + "; print -r -- AT-$_pd_reload_loaded\r")
    sh.wait_text(r"AT-g2", since)
    after = re.findall(r"DIRS:\S+", strip(sh.text(since)))[-1]
    check("dirs: same stack, OLDPWD and cwd", before == after, (before, after))
    fx.close()


@case("fixture: without the zshrc's call the first prompt does the hand-over")
def _():
    fx = Fixture("no-startup-call", startup_call=False, tools=True)
    sh = fx.sh
    sh.run("export PATH=/opt/venv/bin:$PATH SETUP_OVERRIDE=mine")
    fx.update("g2")
    since = len(sh.buf)
    sh.send("td; print -r -- LATE:${PATH%%:*}:$SETUP_OVERRIDE:$_pd_reload_loaded\r")
    sh.wait_text(r"LATE:.*:g2", since)
    sh.pump(0.3)
    out = strip(sh.text(since))
    check("late: td once in the new shell, the session on top",
          out.count("TD-NEW-") == 1 and "LATE:/opt/venv/bin:mine:g2" in out, out[-300:])
    left = [n for n in os.listdir(os.path.join(fx.state, "personal-dotfiles"))
            if n.startswith((".hist", ".handover"))]
    check("late: no hand-over file left", left == [], left)
    fx.close()


@case("fixture: hand-over leftovers of dead shells are swept at startup")
def _():
    child = os.fork()
    if child == 0:
        os._exit(0)
    os.waitpid(child, 0)   # a pid that is gone
    live = os.getpid()

    def leftovers(fx):
        write_gen(fx.state, "g1")
        folder = os.path.join(fx.state, "personal-dotfiles")
        for name in (f".hist.{child}", f".hist.{child}.LOCK", f".hist.{child}.new",
                     f".handover.{child}", f".hist.{live}", "keep.txt"):
            open(os.path.join(folder, name), "w").close()
    fx = Fixture("sweep", state_setup=leftovers)
    names = sorted(os.listdir(os.path.join(fx.state, "personal-dotfiles")))
    check("sweep: a dead shell's files go, the rest stays",
          names == sorted([f".hist.{live}", "keep.txt", "generation", "shell-hook"]), names)
    fx.close()


@case("fixture: jobs defer the reload")
def _():
    fx = Fixture("jobs")
    sh = fx.sh
    sh.run("sleep 60 &")
    fx.update("g2")
    since = len(sh.buf)
    sh.run("print -r -- JOB-$_pd_reload_loaded")
    check("jobs: ran in the old shell", "JOB-g1" in strip(sh.text(since)), strip(sh.text(since)))
    check("jobs: no reload", len(sh.starts()) == 1, sh.starts())
    sh.send("kill %1; wait\r")
    sh.wait(lambda: sh.loaded() == "g2", "reload once the job ended")
    check("jobs: reloaded after", len(sh.starts()) == 2, sh.starts())
    fx.close()


@case("fixture: a broken zsh keeps the shell, silently, without retries")
def _():
    bad = os.path.join(work, "badbin")
    os.makedirs(bad, exist_ok=True)
    with open(os.path.join(bad, "zsh"), "wb") as handle:
        handle.write(b"\x7fELF not a real binary")
    os.chmod(os.path.join(bad, "zsh"), 0o755)
    fx = Fixture("broken", extra_path=bad)
    sh = fx.sh
    fx.update("g2")
    since = len(sh.buf)
    sh.run("print -r -- BROKEN-$_pd_reload_loaded")
    for _ in range(3):
        sh.run("")
    out = strip(sh.text(since))
    check("broken: command ran in the shell", "BROKEN-g1" in out, out[-300:])
    check("broken: shell kept", len(sh.starts()) == 1 and os.path.exists(f"/proc/{sh.pid}"),
          sh.starts())
    check("broken: no message",
          not re.search(r"(?im)^zsh:|exec format|error|not found|permission", out), out[-300:])
    sh.run("path=(${path:#%s}); hash -r" % bad)
    fx.update("g3")
    sh.run("")
    sh.wait(lambda: sh.loaded() == "g3", "reload once zsh works again")
    check("broken: next generation reloads", len(sh.starts()) == 2, sh.starts())
    fx.close()


@case("fixture: missing, FIFO and empty generation files are harmless")
def _():
    fx = Fixture("genfile", gen=None)
    sh = fx.sh
    gen = os.path.join(fx.state, "personal-dotfiles", "generation")
    sh.run("print -r -- none")
    os.mkfifo(gen)
    sh.run("print -r -- fifo", timeout=10)
    os.unlink(gen)
    open(gen, "w").close()
    sh.run("print -r -- empty")
    check("genfile: no reload", len(sh.starts()) == 1, sh.starts())
    write_gen(fx.state, "g1")
    sh.run("")
    sh.wait(lambda: sh.loaded() == "g1", "the first generation reloads")
    check("genfile: first generation reloads once", len(sh.starts()) == 2, sh.starts())
    fx.close()


@case("fixture: environment, cwd, SHLVL and user fds survive")
def _():
    fx = Fixture("env")
    sh = fx.sh
    note = os.path.join(fx.root, "note.txt")
    sh.run("export VIRTUAL_ENV=/opt/venv AMENT_PREFIX_PATH=/opt/ws/install:/opt/ros/humble "
           "COLCON_PREFIX_PATH=/opt/ws/install ROS_DISTRO=humble "
           "LD_LIBRARY_PATH=/opt/ws/install/lib PATH=/opt/ws/install/bin:$PATH; "
           f"exec 3>{note}; LOCAL_ONLY=1")
    fx.update("g2")
    since = len(sh.buf)
    sh.send("print -r -- ENV:$VIRTUAL_ENV:$AMENT_PREFIX_PATH:$COLCON_PREFIX_PATH:$ROS_DISTRO:"
            "$LD_LIBRARY_PATH:${PATH%%:*}:$SHLVL:${LOCAL_ONLY:-unset}:${options[login]}:"
            "$_pd_reload_loaded:$PWD\r")
    sh.wait_text(r"ENV:.*:g2:", since)
    sh.pump(0.3)
    found = re.findall(r"ENV:\S.*", strip(sh.text(since)))
    want = ("ENV:/opt/venv:/opt/ws/install:/opt/ros/humble:/opt/ws/install:humble:"
            "/opt/ws/install/lib:/opt/ws/install/bin:1:unset:off:g2:" + fx.cwd)
    check("env: kept", any(l.strip() == want for l in found), found)
    try:
        link = os.readlink(f"/proc/{sh.pid}/fd/3")
    except OSError as exc:
        link = str(exc)
    check("env: user fd 3 kept", link == note, link)
    fx.close()


@case("fixture: a login shell keeps login-ness, umask and conda")
def _():
    fx = Fixture("login", login=True)
    sh = fx.sh
    prefix = os.path.join(fx.root, "conda", "envs", "x")
    sh.run(f"umask 077; export CONDA_PREFIX={prefix} CONDA_DEFAULT_ENV=x CONDA_SHLVL=1 "
           f"CONDA_PYTHON_EXE={prefix}/bin/python PATH={prefix}/bin:$PATH")
    fx.update("g2")
    since = len(sh.buf)
    sh.send("print -r -- LOGIN:${options[login]}:$(umask):$CONDA_PREFIX:$CONDA_DEFAULT_ENV:"
            "$CONDA_SHLVL:$CONDA_PYTHON_EXE:${PATH%%:*}:$_pd_reload_loaded\r")
    sh.wait_text(r"LOGIN:.*:g2", since)
    sh.pump(0.3)
    found = re.findall(r"LOGIN:\S+", strip(sh.text(since)))
    want = f"LOGIN:on:077:{prefix}:x:1:{prefix}/bin/python:{prefix}/bin:g2"
    check("login: kept", want in found, found)
    check("login: reloaded", len(sh.starts()) == 2 and sh.starts()[1][2] == "on", sh.starts())
    fx.close()


@case("fixture: the hook mark is close-on-exec")
def _():
    fx = Fixture("cloexec")
    sh = fx.sh
    out = os.path.join(fx.root, "child-fds.txt")
    sh.run("for f in /proc/self/fd/*; do readlink $f; done > %s; "
           "zsh -fc 'for f in /proc/$$/fd/*; do readlink $f; done' >> %s; "
           "sleep 0.1 & wait" % (out, out))
    with open(out) as handle:
        lines = handle.read().splitlines()
    check("cloexec: commands do not get it",
          lines and not any("shell-hook" in l for l in lines), lines)
    links = hook_links(sh.pid)
    check("cloexec: the shell holds it, read-only", len(links) == 1, links)
    if links:
        with open(f"/proc/{sh.pid}/fdinfo/{links[0][0]}") as handle:
            info = dict(l.split(":", 1) for l in handle.read().splitlines() if ":" in l)
        flags = int(info.get("flags", "0").strip(), 8)
        check("cloexec: O_CLOEXEC and O_RDONLY", flags & 0o2000000 and flags & 3 == 0,
              oct(flags))
    sh.send("exec bash --norc --noprofile\r")
    sh.wait(lambda: open(f"/proc/{sh.pid}/comm").read().strip() == "bash", "exec bash")
    sh.pump(0.3)
    check("cloexec: exec bash drops it", hook_links(sh.pid) == [], hook_links(sh.pid))
    fx.close()


@case("fixture: the state directory is created, or refused safely")
def _():
    fx = Fixture("statedir", gen=None, unset_state=True)
    sh = fx.sh
    folder = os.path.join(fx.home, ".local", "state", "personal-dotfiles")
    hook = os.path.join(folder, "shell-hook")
    modes = [oct(os.lstat(p).st_mode & 0o777) if os.path.exists(p) else None
             for p in (os.path.join(fx.home, ".local", "state"), folder, hook)]
    check("statedir: created 0700/0700/0600", modes == ["0o700", "0o700", "0o600"], modes)
    check("statedir: mark held", len(hook_links(sh.pid)) == 1, hook_links(sh.pid))
    fx.close()

    def symlinked(fx):
        target = os.path.join(fx.root, "elsewhere")
        os.makedirs(target, mode=0o700)
        os.symlink(target, os.path.join(fx.state, "personal-dotfiles"))
        write_gen(target, "g1")

    def group_writable(fx):
        os.makedirs(os.path.join(fx.state, "personal-dotfiles"))
        os.chmod(os.path.join(fx.state, "personal-dotfiles"), 0o775)
        write_gen(fx.state, "g1")

    def unwritable(fx):
        write_gen(fx.state, "g1")
        os.chmod(os.path.join(fx.state, "personal-dotfiles"), 0o500)

    def hook_symlink(fx):
        write_gen(fx.state, "g1")
        os.symlink(os.path.join(fx.root, "planted"),
                   os.path.join(fx.state, "personal-dotfiles", "shell-hook"))

    def hook_fifo(fx):
        write_gen(fx.state, "g1")
        os.mkfifo(os.path.join(fx.state, "personal-dotfiles", "shell-hook"), 0o600)

    for name, setup in (("symlinked dir", symlinked), ("group-writable dir", group_writable),
                        ("unwritable dir", unwritable), ("symlinked hook", hook_symlink),
                        ("FIFO hook", hook_fifo)):
        fx = Fixture("state-" + name.replace(" ", "-"), state_setup=setup)
        sh = fx.sh
        out = strip(sh.text())
        check(f"statedir {name}: no mark", hook_links(sh.pid) == [], hook_links(sh.pid))
        check(f"statedir {name}: no message",
              not re.search(r"sysopen|zstat|mkdir|permission|denied", out, re.I), out[-300:])
        check(f"statedir {name}: nothing planted",
              not os.path.exists(os.path.join(fx.root, "planted"))
              and not os.path.exists(os.path.join(fx.root, "elsewhere", "shell-hook")), name)
        fx.update("g2")
        since = len(sh.buf)
        if name in ("symlinked hook", "FIFO hook"):
            # The directory itself is fine: the hand-over goes there.
            sh.send("td\r")
            sh.wait_text(r"TD-NEW-\d+-g2", since)
            check(f"statedir {name}: still reloads itself", len(sh.starts()) == 2, sh.starts())
        else:
            # No private place for the hand-over: the shell stays as it is,
            # and the typed line runs here.
            sh.run("td")
            sh.run("")
            out = strip(sh.text(since))
            check(f"statedir {name}: stays as it is", len(sh.starts()) == 1
                  and sh.loaded() != "g2" and "command not found: td" in out,
                  (sh.starts(), sh.loaded(), tail(out)))
            check(f"statedir {name}: still no message",
                  not re.search(r"sysopen|zstat|mkdir|permission|denied", out, re.I),
                  out[-300:])
        os.chmod(os.path.join(fx.state), 0o700)
        for top, dirs, _ in os.walk(fx.state):
            for d in dirs:
                p = os.path.join(top, d)
                if not os.path.islink(p):
                    os.chmod(p, 0o700)
        fx.close()
    skips.append("statedir: a directory owned by another user (needs root or user "
                 "namespaces, unavailable unprivileged here); the uid check is the "
                 "same zstat test as for the symlink/mode cases")

    # A refused state directory, but a private XDG_RUNTIME_DIR: the hand-over
    # goes there (memory, never the disk) and the shell reloads.
    fx = Fixture("state-runtime", state_setup=group_writable, runtime=True)
    sh = fx.sh
    fx.update("g2")
    since = len(sh.buf)
    sh.send("td\r")
    sh.wait_text(r"TD-NEW-\d+-g2", since)
    sh.wait(lambda: sh.loaded() == "g2", "runtime: new prompt")
    folder = os.path.join(fx.runtime, "personal-dotfiles")
    check("statedir runtime: reloads through XDG_RUNTIME_DIR", len(sh.starts()) == 2,
          sh.starts())
    check("statedir runtime: 0700, nothing left behind",
          oct(os.stat(folder).st_mode & 0o777) == "0o700" and os.listdir(folder) == [],
          (oct(os.stat(folder).st_mode), os.listdir(folder)))
    os.chmod(os.path.join(fx.state, "personal-dotfiles"), 0o700)
    fx.close()

    # XDG_RUNTIME_DIR open to others is not private: the state directory is.
    fx = Fixture("state-runtime-open", runtime=True)
    os.chmod(fx.runtime, 0o755)
    sh = fx.sh
    fx.update("g2")
    since = len(sh.buf)
    sh.send("td\r")
    sh.wait_text(r"TD-NEW-\d+-g2", since)
    sh.wait(lambda: sh.loaded() == "g2", "runtime-open: new prompt")
    check("statedir runtime-open: reloads, nothing in the open directory",
          len(sh.starts()) == 2 and os.listdir(fx.runtime) == [], os.listdir(fx.runtime))
    left = [n for n in os.listdir(os.path.join(fx.state, "personal-dotfiles"))
            if n.startswith((".hist", ".handover"))]
    check("statedir runtime-open: no hand-over file left", left == [], left)
    fx.close()


@case("fixture: no fork and no write to the state directory over prompts (strace)")
def _():
    if not shutil.which("strace"):
        skips.append("strace: not installed")
        return
    log = os.path.join(work, "strace.log")
    fx = Fixture("strace", strace_log=log)
    sh = fx.sh
    sh.run("")
    time.sleep(0.3)
    with open(log) as handle:
        before = len(handle.read().splitlines())
    for line in ("", ":", "print -r -- builtin-only", "", "x=1; (( x++ ))", "read -t 0.1 y", ""):
        sh.run(line)
    time.sleep(0.3)
    with open(log) as handle:
        new = handle.read().splitlines()[before:]
    forks = [l for l in new if re.search(r"\b(fork|vfork|clone3?|execve)\(", l)]
    state_dir = os.path.join(fx.state, "personal-dotfiles")
    writes = [l for l in new if state_dir in l and "openat(" in l
              and re.search(r"O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_APPEND", l)]
    reads = [l for l in new if state_dir + "/generation" in l]
    check("strace: traced the prompts", len(new) > 0 and reads, len(new))
    check("strace: no fork or exec", forks == [], forks[:5])
    check("strace: no write in the state directory", writes == [], writes[:5])
    fx.close()


# ---------------------------------------------------------------- real tier

REAL_LOCAL = r'''
# test fixture, sourced twice by zsh/zshrc (before and after zsh.d)
if [[ -e $HOME/old-setup ]]; then
  unalias td 2>/dev/null
else
  alias td='print -r -- TD-NEW-$$-${_pd_reload_loaded:-none}'
fi
typeset -gi _t_sourced=$(( ${_t_sourced:-0} + 1 ))
if (( _t_sourced == 1 )); then
  print -r -- "start $$ ${options[login]}" >>| $HOME/starts.log
else
  # after the reload module's precmd hook: the shell is ready for input
  _t_precmd() { print -r -- "prompt $$ ${_pd_reload_loaded:-none}" >>| $HOME/prompts.log }
  add-zsh-hook precmd _t_precmd
fi
'''


def plugin_sources(target):
    """antidote/, fasd/ (pinned commits) and antidote-home/ (the bundle)."""
    cache = os.environ.get("RELOAD_TEST_PLUGIN_CACHE")
    if cache and all(os.path.isdir(os.path.join(cache, d)) for d in ("antidote", "fasd")):
        return cache, os.path.join(cache, "antidote-home")
    os.makedirs(target, exist_ok=True)
    for name, url in (("antidote", "https://github.com/mattmc3/antidote"),
                      ("fasd", "https://github.com/clvv/fasd.git")):
        pin = subprocess.run(["git", "-C", repo, "ls-tree", "HEAD", f"zsh/{name}"],
                             capture_output=True, text=True, timeout=60).stdout.split()
        dest = os.path.join(target, name)
        subprocess.run(["git", "clone", "-q", url, dest], check=True, timeout=600,
                       env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        if len(pin) >= 3:
            subprocess.run(["git", "-C", dest, "checkout", "-q", pin[2]], check=True,
                           timeout=60)
    return target, os.path.join(target, "antidote-home")


class Real:
    sources = None

    def __init__(self, name, gen="g1", tools_bin=False):
        self.root = os.path.join(work, "real-" + name)
        self.home = os.path.join(self.root, "home")
        zdir = os.path.join(self.home, ".zsh")
        os.makedirs(zdir)
        self.env = base_env(self.root, self.home, self.home)
        self.state = self.env["XDG_STATE_HOME"]
        self.others = []
        if tools_bin:  # the installer's pinned tools, which zsh/zshenv puts first
            os.makedirs(os.path.join(self.env["XDG_DATA_HOME"], "personal-dotfiles", "bin"))
        src, bundle = Real.sources
        for entry in os.listdir(os.path.join(repo, "zsh")):
            if entry not in ("antidote", "fasd"):
                os.symlink(os.path.join(repo, "zsh", entry), os.path.join(zdir, entry))
        for entry in ("antidote", "fasd"):
            os.symlink(os.path.join(src, entry), os.path.join(zdir, entry))
        for entry in ("zshenv", "zprofile", "zshrc", "zlogin", "zlogout", "zpreztorc"):
            os.symlink(os.path.join(repo, "zsh", entry), os.path.join(self.home, "." + entry))
        if bundle and os.path.isdir(bundle):
            shutil.copytree(bundle, os.path.join(self.env["XDG_DATA_HOME"], "antidote"),
                            symlinks=True)
        with open(os.path.join(self.home, ".zshrc.local"), "w") as handle:
            handle.write(REAL_LOCAL)
        open(os.path.join(self.home, "old-setup"), "w").close()
        write_gen(self.state, gen)
        self.sh = Shell(self.env, ["zsh", "-l"], self.root)
        self.sh.ready(timeout=600)
        cloned = os.path.join(self.env["XDG_DATA_HOME"], "antidote")
        if bundle and not os.path.isdir(bundle) and os.path.isdir(cloned):
            shutil.copytree(cloned, bundle, symlinks=True)  # for the next shells

    def update(self, gen):
        try:
            os.unlink(os.path.join(self.home, "old-setup"))
        except FileNotFoundError:
            pass
        write_gen(self.state, gen)

    def another(self):
        """One more login shell on the same home, setup and state."""
        sh = Shell(self.env, ["zsh", "-l"], self.root)
        sh.ready(timeout=600)
        self.others.append(sh)
        return sh

    def td_once(self, name, since):
        sh = self.sh
        sh.wait_text(r"TD-NEW-\d+-g2", since)
        sh.wait(lambda: sh.loaded() == "g2", f"{name}: new prompt")
        sh.pump(0.5)
        out = strip(sh.text(since))
        check(f"{name}: td ran once in the new shell",
              out.count("TD-NEW-") == 1 and f"TD-NEW-{sh.pid}-g2" in out, out[-400:])
        check(f"{name}: not in the old shell", "correct 'td'" not in out
              and "command not found" not in out, out[-400:])
        check(f"{name}: one reload", len(sh.starts()) == 2, sh.starts())
        check(f"{name}: history once", history(self.home).count("td") == 1,
              history(self.home)[-5:])

    def close(self):
        for sh in [self.sh] + self.others:
            sh.close()


REAL_CASES = []


def real(name):
    def wrap(fn):
        REAL_CASES.append((name, fn))
        return fn
    return wrap


@real("real: the widgets compose with the plugin stack")
def _():
    r = Real("widgets")
    sh = r.sh
    since = len(sh.buf)
    sh.run("print -r -- W:${widgets[zle-line-init]}:${widgets[zle-line-finish]}; "
           "zstyle -L 'zle-line-(init|finish)'")
    out = strip(sh.text(since))
    check("real widgets: dispatcher kept",
          "W:user:azhw:zle-line-init:user:azhw:zle-line-finish" in out, out[-600:])
    check("real widgets: ours and the plugins' hooked",
          "_pd_reload_line_finish" in out and "_pd_reload_line_init" in out
          and "_p9k_widget_zle-line-finish" in out and "_fsh_" in out, out[-600:])
    check("real widgets: one hook fd", len(hook_links(sh.pid)) == 1, hook_links(sh.pid))
    r.close()


@real("real: td, typed (insert mode) as the first command after an update")
def _():
    r = Real("insert")
    r.update("g2")
    since = len(r.sh.buf)
    r.sh.send("td\r")
    r.td_once("real insert", since)
    for _ in range(3):
        r.sh.run("")
    check("real insert: no reload loop", len(r.sh.starts()) == 2, r.sh.starts())
    r.close()


@real("real: td, Enter in vi normal mode")
def _():
    r = Real("vicmd")
    r.update("g2")
    since = len(r.sh.buf)
    r.sh.send("td")
    r.sh.pump(0.3)
    r.sh.send("\x1b")
    r.sh.pump(0.8)
    r.sh.send("\r")
    r.td_once("real vicmd", since)
    r.close()


@real("real: td from an accepted autosuggestion")
def _():
    r = Real("autosuggest")
    with open(os.path.join(r.home, ".zsh_history"), "a") as handle:
        handle.write(": 1700000000:0;td\n")
    r.sh.run("fc -R")
    r.update("g2")
    since = len(r.sh.buf)
    r.sh.send("t")
    r.sh.pump(1.0)
    r.sh.send("\x1b[C")  # forward-char accepts the suggestion
    r.sh.pump(0.5)
    r.sh.send("\r")
    r.sh.wait_text(r"TD-NEW-\d+-g2", since)
    r.sh.pump(0.5)
    out = strip(r.sh.text(since))
    check("real autosuggest: ran once in the new shell", out.count("TD-NEW-") == 1, out[-300:])
    check("real autosuggest: one reload", len(r.sh.starts()) == 2, r.sh.starts())
    r.close()


@real("real: a bracketed multi-line paste")
def _():
    r = Real("paste")
    r.update("g2")
    since = len(r.sh.buf)
    r.sh.send("\x1b[200~print -r -- P1-$_pd_reload_loaded\rprint -r -- P2-$_pd_reload_loaded\x1b[201~")
    r.sh.pump(0.8)
    r.sh.send("\r")
    r.sh.wait_text(r"P2-g2\b", since)
    r.sh.pump(0.5)
    out = re.findall(r"\bP\d-g\d+\b", strip(r.sh.text(since)))
    check("real paste: both lines once, in the new shell", out == ["P1-g2", "P2-g2"], out)
    check("real paste: one reload", len(r.sh.starts()) == 2, r.sh.starts())
    r.close()


@real("real: a pasted shell fence, unwrapped by the smart-paste widget")
def _():
    r = Real("fence")
    r.update("g2")
    since = len(r.sh.buf)
    r.sh.send("\x1b[200~```sh\rprint -r -- FENCE-$_pd_reload_loaded\r```\x1b[201~")
    r.sh.pump(0.8)
    r.sh.send("\r")
    r.sh.wait_text(r"FENCE-g2\b", since)
    r.sh.pump(0.5)
    out = strip(r.sh.text(since))
    check("real fence: ran once in the new shell",
          re.findall(r"\bFENCE-g\d+\b", out) == ["FENCE-g2"], out[-300:])
    check("real fence: unwrapped (no fence in the history)",
          "print -r -- FENCE-$_pd_reload_loaded" in history(r.home)
          and not any("```" in l for l in history(r.home)), history(r.home)[-3:])
    r.close()


@real("real: PS2 and heredoc across an update")
def _():
    for kind, first, middle, last, expect in (
            ("ps2", "for i in 1 2; do", "print -r -- LOOP-$i-$_pd_reload_loaded", "done",
             ["LOOP-1-g1", "LOOP-2-g1"]),
            ("heredoc", "cat <<EOF", "HD-$_pd_reload_loaded", "EOF", ["HD-g1"])):
        r = Real(kind)
        sh = r.sh
        sh.send(first + "\r")
        sh.pump(0.6)
        r.update("g2")
        sh.send(middle + "\r")
        sh.pump(1.0)
        check(f"real {kind}: no exec in the middle", len(sh.starts()) == 1, sh.starts())
        since = len(sh.buf)
        sh.send(last + "\r")
        sh.wait(lambda: sh.loaded() == "g2", f"real {kind}: reload after")
        sh.pump(0.3)
        out = re.findall(r"\b(?:LOOP-\d|HD)-g\d+\b", strip(sh.text(since)))
        check(f"real {kind}: ran in the old shell, once", out == expect, out)
        check(f"real {kind}: reloaded before the next prompt", len(sh.starts()) == 2,
              sh.starts())
        r.close()


@real("real: venv/ROS exports, cwd and the hook mark survive; exec bash drops it")
def _():
    r = Real("env")
    sh = r.sh
    sh.run("export VIRTUAL_ENV=/opt/venv AMENT_PREFIX_PATH=/opt/ws/install "
           "COLCON_PREFIX_PATH=/opt/ws/install PATH=/opt/ws/install/bin:$PATH; cd /")
    r.update("g2")
    since = len(sh.buf)
    sh.send("print -r -- ENV:$VIRTUAL_ENV:$AMENT_PREFIX_PATH:$COLCON_PREFIX_PATH:"
            "${PATH%%:*}:$PWD:$SHLVL:$_pd_reload_loaded\r")
    sh.wait_text(r"ENV:.*:g2\b", since)
    found = re.findall(r"ENV:\S+", strip(sh.text(since)))
    check("real env: kept",
          "ENV:/opt/venv:/opt/ws/install:/opt/ws/install:/opt/ws/install/bin:/:1:g2" in found,
          found)
    check("real env: one hook fd", len(hook_links(sh.pid)) == 1, hook_links(sh.pid))
    out = os.path.join(r.root, "child-fds.txt")
    sh.run("for f in /proc/self/fd/*; do readlink $f; done >| %s" % out)
    with open(out) as handle:
        check("real env: commands do not get the mark", "shell-hook" not in handle.read(), out)
    gitstatusd = [p for p in os.listdir("/proc") if p.isdigit()
                  and _ppid(p) == sh.pid and _comm(p).startswith("gitstatus")]
    check("real env: no gitstatusd left behind by the reload", len(gitstatusd) <= 1,
          gitstatusd)
    sh.send("exec bash --norc --noprofile\r")
    sh.wait(lambda: _comm(str(sh.pid)) == "bash", "exec bash")
    sh.pump(0.3)
    check("real env: exec bash drops the mark", hook_links(sh.pid) == [], hook_links(sh.pid))
    r.close()


@real("real: select and vared across an update: nothing emptied, nothing run")
def _():
    for kind, start, waitfor, answer, expect in (
            ("select", "select x in aa bb; do print -r -- SEL-$x-$_pd_reload_loaded; break; done",
             r"\?# ", "1", "SEL-aa-g1"),
            ("vared", "v=; vared -p \"VA${:-RED}> \" v; print -r -- VAR-$v-$_pd_reload_loaded",
             r"VARED> ", "xy", "VAR-xy-g1")):
        r = Real(kind)
        sh = r.sh
        since = len(sh.buf)
        sh.send(start + "\r")
        sh.wait_text(waitfor, since)
        r.update("g2")
        sh.pump(0.5)
        check(f"real {kind}: no exec while it waits", len(sh.starts()) == 1, sh.starts())
        since = len(sh.buf)
        sh.send(answer + "\r")
        sh.wait(lambda: sh.loaded() == "g2", f"real {kind}: reload after it ran")
        sh.pump(0.5)
        out = strip(sh.text(since))
        check(f"real {kind}: the answer reached the command, in the old shell",
              expect in out, out[-300:])
        check(f"real {kind}: the answer never ran as a command",
              "command not found" not in out and f"correct '{answer}'" not in out, out[-300:])
        check(f"real {kind}: one reload", len(sh.starts()) == 2, sh.starts())
        r.close()


@real("real: several shells reload at once, each runs its own line once")
def _():
    r = Real("parallel")
    shells = [r.sh] + [r.another() for _ in range(3)]
    for i, sh in enumerate(shells):
        sh.send(f"print -r -- PAR-{i}-$$-$_pd_reload_loaded")
        sh.pump(0.2)
    r.update("g2")
    marks = [len(sh.buf) for sh in shells]
    for sh in shells:
        sh.send("\r")
    for i, sh in enumerate(shells):
        sh.wait_text(rf"PAR-{i}-{sh.pid}-g2", marks[i], timeout=60)
    for i, sh in enumerate(shells):
        sh.wait(lambda: sh.loaded() == "g2", f"real parallel {i}: new prompt")
        sh.pump(0.3)
        out = strip(sh.text(marks[i]))
        check(f"real parallel {i}: its line ran once, in its new shell",
              re.findall(r"\bPAR-\d+-\d+-g\d\b", out) == [f"PAR-{i}-{sh.pid}-g2"], out[-300:])
        check(f"real parallel {i}: one reload", len(sh.starts()) == 2, sh.starts())
    lines = history(r.home)
    check("real parallel: each line once in the history",
          all(sum(f"PAR-{i}-" in l for l in lines) == 1 for i in range(4)), lines[-8:])
    r.close()


@real("real: 'r' after an update re-runs this shell's own command")
def _():
    r = Real("rerun")
    a, b = r.sh, r.another()
    a.run("print -r -- RERUN-FROM-${:-A}")
    b.run("print -r -- RERUN-FROM-${:-B}")
    r.update("g2")
    since = len(a.buf)
    a.send("r\r")
    a.wait(lambda: a.loaded() == "g2", "real rerun: reload")
    a.pump(0.5)
    out = strip(a.text(since))
    check("real rerun: r re-ran this shell's command",
          re.findall(r"^RERUN-FROM-\w+", out, re.M) == ["RERUN-FROM-A"], out[-300:])
    r.close()


@real("real: PATH keeps the user's order with the tools bin present")
def _():
    r = Real("toolsbin", tools_bin=True)
    sh = r.sh
    tools = os.path.join(r.env["XDG_DATA_HOME"], "personal-dotfiles", "bin")
    since = len(sh.buf)
    sh.run("print -r -- FIRST:${PATH%%:*}")
    check("real toolsbin: zsh/zshenv puts the tools bin first",
          f"FIRST:{tools}" in strip(sh.text(since)), strip(sh.text(since))[-200:])
    sh.run("export PATH=/opt/venv/bin:/opt/ws/bin:$PATH")
    r.update("g2")
    since = len(sh.buf)
    sh.send("print -r -- ORDER:$_pd_reload_loaded:${(j:,:)${${(s.:.)PATH}[1,3]}}\r")
    sh.wait_text(r"ORDER:g2:", since)
    sh.pump(0.3)
    found = re.findall(r"ORDER:g2:(\S+)", strip(sh.text(since)))
    check("real toolsbin: the venv and the workspace stay in front",
          found[-1:] == [f"/opt/venv/bin,/opt/ws/bin,{tools}"], found)
    r.close()


@real("real: a line aborted earlier, typed again, runs in the new shell")
def _():
    r = Real("abortagain")
    sh = r.sh
    sh.send("td")
    sh.pump(0.5)
    sh.send("\x03")
    sh.pump(1.0)
    sh.run("")
    r.update("g2")
    since = len(sh.buf)
    sh.send("td\r")
    r.td_once("real abort-again", since)
    r.close()


@real("real: a push-line'd line comes back after the reload")
def _():
    r = Real("pushline")
    sh = r.sh
    sh.send("print -r -- HALF-${:-TYPED}-$_pd_reload_loaded")
    sh.pump(0.5)
    sh.send("\x1bq")   # push-line (bound in this config)
    sh.pump(1.0)
    sh.send("\x1b")
    sh.pump(0.3)
    sh.send("i")
    sh.pump(0.3)
    r.update("g2")
    since = len(sh.buf)
    sh.send("td\r")
    r.td_once("real pushline", since)
    sh.send("\r")
    sh.wait_text(r"HALF-TYPED-g2", since)
    sh.pump(0.3)
    check("real pushline: the pushed line ran once, in the new shell",
          strip(sh.text(since)).count("HALF-TYPED-g") == 1, strip(sh.text(since))[-300:])
    r.close()


@real("real: an unset HISTFILE stays unset")
def _():
    r = Real("histfile")
    sh = r.sh
    sh.run("unset HISTFILE")
    r.update("g2")
    since = len(sh.buf)
    sh.send("print -r -- SECRET-${:-TOKEN}-$_pd_reload_loaded\r")
    sh.wait_text(r"SECRET-TOKEN-g2", since)
    sh.wait(lambda: sh.loaded() == "g2", "real histfile: new prompt")
    sh.run("print -r -- HF-${+HISTFILE}")
    out = strip(sh.text(since))
    check("real histfile: still unset, nothing written",
          "HF-0" in out and not any("SECRET" in l for l in history(r.home)),
          (out[-200:], history(r.home)[-3:]))
    r.close()


def _ppid(pid):
    try:
        with open(f"/proc/{pid}/stat") as handle:
            data = handle.read()
        return int(data[data.rindex(")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _comm(pid):
    try:
        with open(f"/proc/{pid}/comm") as handle:
            return handle.read().strip()
    except OSError:
        return ""


# ---------------------------------------------------------------- run

def run(cases):
    only = os.environ.get("RELOAD_TEST_ONLY")  # a regex on the case names
    for name, fn in cases:
        if only and not re.search(only, name):
            continue
        print(f"-- {name}", flush=True)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - reported as a failure
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"FAIL {name}: {type(exc).__name__}: {str(exc)[:1500]}", flush=True)


tiers = os.environ.get("RELOAD_TEST_TIERS", "fixture real").split()
if "fixture" in tiers:
    run(CASES)
else:
    skips.append("fixture tier: not selected (RELOAD_TEST_TIERS)")
if "real" not in tiers:
    skips.append("real tier: not selected (RELOAD_TEST_TIERS)")
else:
  try:
    Real.sources = plugin_sources(os.path.join(work, "plugins"))
  except Exception as exc:  # noqa: BLE001
    message = f"real tier: cannot get the plugins ({type(exc).__name__}: {exc})"
    if os.environ.get("RELOAD_TEST_ALLOW_SKIP") == "1":
        skips.append(message)
    else:
        failures.append(message)
  else:
    run(REAL_CASES)

for line in skips:
    print(f"SKIP {line}")
print(f"{len(passes)} checks passed, {len(failures)} failed, {len(skips)} skipped")
sys.exit(1 if failures else 0)
PY
