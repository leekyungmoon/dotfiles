"""Process inspection on macOS (Darwin), which has no /proc.

The Linux code in tmux/resurrect-ai-session.py, installer/phases.py and
bin/dotfiles reads what it needs to know about other processes from /proc:
their parent, start time, argv, executable, directory and open files. macOS
keeps the same facts in the kernel and hands them out through libproc
(proc_listpids, proc_pidinfo, proc_pidfdinfo, proc_pidpath) and sysctl(3)
(KERN_PROCARGS2). This module calls those through ctypes and is the macOS
backend of those files; on Linux they keep their /proc code and never call
it. ps(1) and lsof(8) are no substitute: 'ps -o args' joins argv with spaces,
so the argument boundaries the callers depend on are lost, and lsof is slow
and prints text meant for people.

Rules for every function here:
- Standard library only, Python 3.9 compatible: the tmux hook may run under
  the Xcode Command Line Tools python3, which is 3.9.
- Importable anywhere. ctypes and libSystem load on first use; off macOS
  available() is False and every call returns None.
- Nothing raises because a process is gone or is not ours: errors are None.
  bsd_info, procargs (argv, environ, exec_path), cwd and open_vnodes
  describe only the caller's own processes (every process for root) and
  return None for any other pid and for one that has exited; all_pids and
  children list every process.
- Strings are decoded as UTF-8 with 'surrogateescape': nothing is lost
  (os.fsencode() gives the kernel's bytes back), and a byte that is not
  UTF-8 becomes a lone surrogate, which str.isprintable() rejects.

API
  available() -> bool
      True on macOS once libSystem has loaded and its answers about this
      very process check out: its pid, parent, user and process group from
      PROC_PIDTBSDINFO, its argv from KERN_PROCARGS2, and, through
      PROC_PIDLISTFDS and PROC_PIDFDVNODEPATHINFO, a descriptor it holds on
      os.devnull with the device, inode and file type os.fstat() reports. A
      sandbox that refuses the calls, or a changed struct layout, shows here.
      Decided once per process, on first use of any call.
  unavailable_reason() -> str | None
      Why available() is False; None while it is True.
  all_pids() -> list[int] | None
      Every process id: proc_listpids(PROC_ALL_PIDS), which proc_listallpids
      wraps. None when the process table cannot be read.
  children(pid) -> list[int] | None
      The processes whose parent is pid: proc_listpids(PROC_PPID_ONLY). []
      when there are none or pid is gone. None when the process table cannot
      be read, which callers must take as "cannot tell" (busy), never as "no
      children".
  bsd_info(pid) -> BsdInfo | None
      proc_pidinfo(PROC_PIDTBSDINFO). BsdInfo fields: pid, ppid, uid (the
      effective one), pgid, tdev (the controlling terminal's device number,
      NODEV without one), tpgid (that terminal's foreground process group),
      comm (the kernel's short name: the executable's file name, at most 15
      bytes), name (a longer name of at most 31 bytes, or ""), status (SIDL,
      SRUN, SSLEEP, SSTOP or SZOMB) and start (the start time in seconds
      since the epoch, a float with microsecond resolution).
  procargs(pid) -> ProcArgs | None
      sysctl KERN_PROCARGS2 parsed by parse_procargs2 into ProcArgs(exec_path,
      argv, environ); argv(), environ() and exec_path() return one part each.
  argv(pid) -> list[str] | None
      The exact argv, empty arguments included, as 'ps' prints them (the
      /proc readers of this repository drop them). The kernel's format sets
      one limit: empty arguments at the very start (argv[0] == "") cannot be
      told from the padding after the exec path, so they are skipped, just
      as ps skips them.
  environ(pid) -> dict[str, str] | None
      The environment the process started with (later setenv() calls are
      not seen). macOS may withhold it for processes other than the caller
      (an empty dict), and when it ends exactly on an 8-byte boundary the
      loader's own "apple" strings (ptr_munge=, main_stack=, ...) follow it
      without a separator. Look variables up by name; the dict is never
      known to be complete.
  exec_path(pid) -> str | None
      The path the process was started with: execve()'s argument as the
      shell found it on PATH, symlinks not resolved (~/.local/bin/claude,
      not the versioned file that link points to). It can be relative.
  exe(pid) -> str | None
      proc_pidpath: the executable file itself, symlinks resolved.
  cwd(pid) -> str | None
      proc_pidinfo(PROC_PIDVNODEPATHINFO): the current directory.
  open_vnodes(pid) -> list[VnodeFd] | None
      Every descriptor of pid that is a vnode (a file, directory, terminal
      or other device; not a pipe, socket or kqueue), from PROC_PIDLISTFDS and
      PROC_PIDFDVNODEPATHINFO. VnodeFd fields: fd, path, dev, ino, nlink,
      mode, size, cloexec, uid, openflags. path is "" when the kernel has no
      name for the file; an unlinked file may read that way or keep its last
      name, but never gets a " (deleted)" suffix, so tell it by nlink == 0.
      dev is the unsigned 32-bit device number (compare it with
      os.stat().st_dev & 0xffffffff). mode includes the file type bits.
      cloexec is the descriptor's close-on-exec flag (PROC_FP_CLEXEC).
      openflags are the kernel's file flags: FREAD and FWRITE give the
      access mode, O_APPEND and the like follow. A descriptor that changed
      while the table was read is left out: closed (EBADF), its number reused
      for a pipe or socket (ENOTSUP), or its vnode reclaimed (ENOENT, as for
      a revoked terminal). Any other error makes the answer None, as does a
      table that cannot be read (pid is gone or not ours): a file the process
      holds must never look absent.
  tty_path(tdev) -> str | None
      The terminal of a BsdInfo.tdev, such as /dev/ttys003: devname(3),
      checked against the device node, whose rdev must be tdev. None for
      NODEV or a device without a node.

Pure parsers, kept apart from the ctypes calls so that tests on any system
can feed them synthetic buffers. Each raises ValueError for a buffer that is
too short or malformed.
  parse_bsdinfo(buf) -> BsdInfo                      struct proc_bsdinfo
  parse_procargs2(buf) -> ProcArgs                   KERN_PROCARGS2 data
  parse_vnode_fdinfowithpath(buf, fd=-1) -> VnodeFd  struct vnode_fdinfowithpath
  parse_vnodepathinfo(buf) -> str                    struct proc_vnodepathinfo

The layouts are those of xnu's <sys/proc_info.h>, the same on arm64 and
x86_64: proc_bsdinfo is 136 bytes, proc_fdinfo 8, vnode_fdinfowithpath 1200
with the path at offset 176, and proc_vnodepathinfo 2352. The kernel refuses
a buffer smaller than its struct and every call checks the size it returns,
so a changed layout makes the calls fail (and available() False) instead of
returning misread fields.
"""

from __future__ import annotations

import collections
import errno
import os
import stat
import struct
import sys

LIBSYSTEM = "/usr/lib/libSystem.B.dylib"

# <sys/sysctl.h>
CTL_KERN = 1
KERN_ARGMAX = 8
KERN_PROCARGS2 = 49

# <sys/proc_info.h>
PROC_ALL_PIDS = 1
PROC_PPID_ONLY = 6
PROC_PIDLISTFDS = 1
PROC_PIDTBSDINFO = 3
PROC_PIDVNODEPATHINFO = 9
PROC_PIDFDVNODEPATHINFO = 2
PROC_PIDPATHINFO_MAXSIZE = 4 * 1024
PROX_FDTYPE_VNODE = 1
PROC_FP_CLEXEC = 2
MAXPATHLEN = 1024

# <sys/fcntl.h> kernel file flags, as VnodeFd.openflags reports them
FREAD = 0x1
FWRITE = 0x2

# <sys/proc.h> process states (BsdInfo.status)
SIDL, SRUN, SSLEEP, SSTOP, SZOMB = 1, 2, 3, 4, 5

# BsdInfo.tdev of a process without a controlling terminal ((dev_t)-1)
NODEV = 0xFFFFFFFF

# proc_pidfdinfo errors that mean the descriptor changed after the listing
# (see open_vnodes in the module docstring): it is skipped, not a failure.
FD_CHANGED_ERRORS = (errno.EBADF, errno.ENOTSUP, errno.ENOENT)

# struct proc_bsdinfo: twelve uint32 (flags, status, xstatus, pid, ppid, uid,
# gid, ruid, rgid, svuid, svgid, reserved), comm[16], name[32], five uint32
# (nfiles, pgid, pjobc, e_tdev, e_tpgid), int32 nice, then the start time as
# two uint64 (seconds, microseconds).
BSDINFO = struct.Struct("=12I16s32s5Ii2Q")
# struct proc_fdinfo {int32 proc_fd; uint32 proc_fdtype;}
FDINFO = struct.Struct("=iI")
# struct proc_fileinfo: fi_openflags, fi_status, fi_offset, fi_type,
# fi_guardflags
FILEINFO = struct.Struct("=IIqiI")
# The head of struct vinfo_stat: vst_dev, vst_mode, vst_nlink, vst_ino,
# vst_uid, vst_gid. vst_size (off_t) is at VINFO_STAT_SIZE_OFFSET.
VINFO_STAT_HEAD = struct.Struct("=IHHQII")
VINFO_STAT_SIZE_OFFSET = 88
# struct vnode_info (vinfo_stat, vi_type, vi_pad, vi_fsid) is 152 bytes, and
# struct vnode_info_path adds the path: char vip_path[MAXPATHLEN].
VNODE_INFO_SIZE = 152
VNODE_INFO_PATH_SIZE = VNODE_INFO_SIZE + MAXPATHLEN
# struct vnode_fdinfowithpath {proc_fileinfo pfi; vnode_info_path pvip;}
VNODE_FDINFOWITHPATH_SIZE = FILEINFO.size + VNODE_INFO_PATH_SIZE
VNODE_FDINFOWITHPATH_PATH_OFFSET = FILEINFO.size + VNODE_INFO_SIZE
# struct proc_vnodepathinfo {vnode_info_path pvi_cdir, pvi_rdir;}
VNODEPATHINFO_SIZE = 2 * VNODE_INFO_PATH_SIZE

BsdInfo = collections.namedtuple(
    "BsdInfo", "pid ppid uid pgid tdev tpgid comm name status start"
)
VnodeFd = collections.namedtuple(
    "VnodeFd", "fd path dev ino nlink mode size cloexec uid openflags"
)
ProcArgs = collections.namedtuple("ProcArgs", "exec_path argv environ")


# --- pure parsers -------------------------------------------------------------

def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _c_string(raw: bytes) -> str:
    """A NUL-terminated char array (the whole array when it has no NUL)."""

    return _decode(raw.split(b"\0", 1)[0])


def parse_bsdinfo(buf) -> BsdInfo:
    """BsdInfo from a struct proc_bsdinfo."""

    data = bytes(buf)
    if len(data) < BSDINFO.size:
        raise ValueError(f"proc_bsdinfo needs {BSDINFO.size} bytes, got {len(data)}")
    (_flags, status, _xstatus, pid, ppid, uid, _gid, _ruid, _rgid, _svuid,
     _svgid, _reserved, comm, name, _nfiles, pgid, _pjobc, tdev, tpgid, _nice,
     start_sec, start_usec) = BSDINFO.unpack_from(data)
    return BsdInfo(
        pid=pid, ppid=ppid, uid=uid, pgid=pgid, tdev=tdev, tpgid=tpgid,
        comm=_c_string(comm), name=_c_string(name), status=status,
        start=start_sec + start_usec / 1000000,
    )


def parse_procargs2(buf) -> ProcArgs:
    """ProcArgs from the data of sysctl {CTL_KERN, KERN_PROCARGS2, pid}.

    The layout: argc as a native int32, the exec path, NUL padding up to a
    pointer boundary, then argc NUL-terminated arguments, then the
    environment strings, ended by an empty string or by the end of the data
    (see environ() in the module docstring for what may follow it).

    Every argument is kept, empty ones too; only arguments that run out
    before argc are an error, because a cut argv must never be taken for a
    whole one. An unterminated last environment string is dropped.
    """

    data = bytes(buf)
    if len(data) < 4:
        raise ValueError("KERN_PROCARGS2 data is shorter than its argc")
    (argc,) = struct.unpack_from("=i", data, 0)
    if argc < 0:
        raise ValueError(f"KERN_PROCARGS2 reports argc {argc}")
    end = data.find(b"\0", 4)
    if end < 0:
        raise ValueError("KERN_PROCARGS2 exec path is not terminated")
    exec_path = _decode(data[4:end])
    # The padding after the exec path; like ps, skip every NUL up to the
    # first argument.
    position = end
    while position < len(data) and data[position] == 0:
        position += 1
    arguments = []
    for _ in range(argc):
        end = data.find(b"\0", position)
        if end < 0:
            raise ValueError(
                f"KERN_PROCARGS2 data ends after {len(arguments)} of {argc} arguments"
            )
        arguments.append(_decode(data[position:end]))
        position = end + 1
    environment = {}
    while position < len(data):
        end = data.find(b"\0", position)
        if end <= position:  # an empty string ends it; an unterminated one is cut
            break
        key, sep, value = data[position:end].partition(b"=")
        if sep and key:
            environment.setdefault(_decode(key), _decode(value))
        position = end + 1
    return ProcArgs(exec_path, arguments, environment)


def _vnode_info_path(data: bytes, offset: int):
    """(dev, mode, nlink, ino, uid, size, path) of the struct vnode_info_path
    at ``offset``."""

    dev, mode, nlink, ino, uid, _gid = VINFO_STAT_HEAD.unpack_from(data, offset)
    (size,) = struct.unpack_from("=q", data, offset + VINFO_STAT_SIZE_OFFSET)
    start = offset + VNODE_INFO_SIZE
    return dev, mode, nlink, ino, uid, size, _c_string(data[start:start + MAXPATHLEN])


def parse_vnode_fdinfowithpath(buf, fd: int = -1) -> VnodeFd:
    """VnodeFd from a struct vnode_fdinfowithpath (descriptor ``fd``)."""

    data = bytes(buf)
    if len(data) < VNODE_FDINFOWITHPATH_SIZE:
        raise ValueError(
            f"vnode_fdinfowithpath needs {VNODE_FDINFOWITHPATH_SIZE} bytes, "
            f"got {len(data)}"
        )
    openflags, status, _offset, _type, _guardflags = FILEINFO.unpack_from(data, 0)
    dev, mode, nlink, ino, uid, size, path = _vnode_info_path(data, FILEINFO.size)
    return VnodeFd(
        fd=fd, path=path, dev=dev, ino=ino, nlink=nlink, mode=mode, size=size,
        cloexec=bool(status & PROC_FP_CLEXEC), uid=uid, openflags=openflags,
    )


def parse_vnodepathinfo(buf) -> str:
    """The current directory from a struct proc_vnodepathinfo ("" when the
    kernel has no name for it)."""

    data = bytes(buf)
    if len(data) < VNODEPATHINFO_SIZE:
        raise ValueError(
            f"proc_vnodepathinfo needs {VNODEPATHINFO_SIZE} bytes, got {len(data)}"
        )
    return _vnode_info_path(data, 0)[-1]


# --- libSystem through ctypes -------------------------------------------------

# Loaded on first use: the configured libSystem handle, kern.argmax, and why
# the backend cannot be used (once that is known, it is never retried).
_LIB = None
_ARGMAX = 0
_PROBLEM = None


def _open_libsystem():
    """libSystem with the argument and result types of the calls used here."""

    import ctypes

    lib = ctypes.CDLL(LIBSYSTEM, use_errno=True)
    c_int, c_uint32, c_void_p = ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p
    signatures = {
        # int proc_listpids(uint32_t type, uint32_t typeinfo, void *buffer,
        #                   int buffersize)
        "proc_listpids": (c_int, [c_uint32, c_uint32, c_void_p, c_int]),
        # int proc_pidinfo(int pid, int flavor, uint64_t arg, void *buffer,
        #                  int buffersize)
        "proc_pidinfo": (c_int, [c_int, c_int, ctypes.c_uint64, c_void_p, c_int]),
        # int proc_pidfdinfo(int pid, int fd, int flavor, void *buffer,
        #                    int buffersize)
        "proc_pidfdinfo": (c_int, [c_int, c_int, c_int, c_void_p, c_int]),
        # int proc_pidpath(int pid, void *buffer, uint32_t buffersize)
        "proc_pidpath": (c_int, [c_int, c_void_p, c_uint32]),
        # int sysctl(int *name, u_int namelen, void *oldp, size_t *oldlenp,
        #            void *newp, size_t newlen)
        "sysctl": (c_int, [ctypes.POINTER(c_int), ctypes.c_uint, c_void_p,
                           ctypes.POINTER(ctypes.c_size_t), c_void_p,
                           ctypes.c_size_t]),
        # char *devname(dev_t dev, mode_t type); dev_t is int32, mode_t uint16
        "devname": (ctypes.c_char_p, [ctypes.c_int32, ctypes.c_uint16]),
    }
    for name, (restype, argtypes) in signatures.items():
        function = getattr(lib, name)
        function.restype = restype
        function.argtypes = argtypes
    return lib


def _call(function, *args):
    """``(function(*args), errno)``, with errno cleared first: libproc
    returns 0 both for an empty answer and for an error."""

    import ctypes

    ctypes.set_errno(0)
    result = function(*args)
    return result, ctypes.get_errno()


def _sysctl_int(lib, *names: int) -> int:
    """A numeric sysctl value (an int or a long, as the kernel declares it)."""

    import ctypes

    mib = (ctypes.c_int * len(names))(*names)
    buf = ctypes.create_string_buffer(8)
    size = ctypes.c_size_t(8)
    result, error = _call(lib.sysctl, mib, len(names), buf, ctypes.pointer(size), None, 0)
    if result != 0 or size.value not in (4, 8):
        raise OSError(error, f"sysctl {list(names)} failed")
    return struct.unpack_from("=i" if size.value == 4 else "=q", buf.raw)[0]


def _load_library():
    """``(lib, argmax, None)`` when libproc can inspect this process, else
    ``(None, 0, why)``."""

    if sys.platform != "darwin":
        return None, 0, f"sys.platform is {sys.platform!r}, not 'darwin'"
    try:
        lib = _open_libsystem()
        argmax = _sysctl_int(lib, CTL_KERN, KERN_ARGMAX)
        if argmax < 4096:
            return None, 0, f"sysctl kern.argmax reports {argmax}"
        problem = _self_check(lib, argmax)
    except Exception as error:  # an unknown macOS: fail closed with the reason
        return None, 0, f"cannot use libproc from {LIBSYSTEM}: {error}"
    if problem:
        return None, 0, problem
    return lib, argmax, None


def _self_check(lib, argmax: int) -> str | None:
    """Why libproc's answers about this very process are wrong, or None.

    A refused call (a sandbox) or a changed struct layout shows here, before
    any caller trusts the answers. Every field compared sits at another
    place of its struct.
    """

    pid = os.getpid()
    parent = os.getppid()
    info = _bsd_info(lib, pid)
    # The parent may exit meanwhile, which hands this process to launchd.
    parents = (parent, os.getppid())
    if (
        info is None or info.pid != pid or info.ppid not in parents
        or info.uid != os.geteuid() or info.pgid != os.getpgrp()
    ):
        return "proc_pidinfo(PROC_PIDTBSDINFO) does not describe this process"
    args = _procargs(lib, argmax, pid)
    if args is None or not args.argv:
        return "sysctl KERN_PROCARGS2 does not return this process's argv"
    probe = os.open(os.devnull, os.O_RDONLY)
    try:
        expected = os.fstat(probe)
        listed = _list_fds(lib, pid)
        entry, _ = _vnode_fd(lib, pid, probe)
    finally:
        os.close(probe)
    if listed is None or (probe, PROX_FDTYPE_VNODE) not in listed:
        return "proc_pidinfo(PROC_PIDLISTFDS) does not list this process's files"
    if entry is None or (entry.dev, entry.ino, stat.S_IFMT(entry.mode)) != (
        expected.st_dev & 0xFFFFFFFF, expected.st_ino, stat.S_IFMT(expected.st_mode)
    ):
        return (
            "proc_pidfdinfo(PROC_PIDFDVNODEPATHINFO) does not describe this "
            "process's files"
        )
    return None


def _library():
    global _LIB, _ARGMAX, _PROBLEM
    if _LIB is None and _PROBLEM is None:
        _LIB, _ARGMAX, _PROBLEM = _load_library()
    return _LIB


def _valid_pid(pid) -> bool:
    return isinstance(pid, int) and 0 <= pid <= 0x7FFFFFFF


def _list_pids(lib, kind: int, info: int) -> list[int] | None:
    import ctypes

    width = ctypes.sizeof(ctypes.c_int)
    need, _ = _call(lib.proc_listpids, kind, info, None, 0)
    if need <= 0:
        return None
    buf = (ctypes.c_int * (need // width + 64))()  # room for new processes
    got, error = _call(lib.proc_listpids, kind, info, buf, ctypes.sizeof(buf))
    if got < 0 or (got == 0 and error):
        return None
    return [pid for pid in buf[:got // width] if pid > 0]


def _bsd_info(lib, pid: int) -> BsdInfo | None:
    import ctypes

    buf = ctypes.create_string_buffer(BSDINFO.size)
    if lib.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, buf, BSDINFO.size) != BSDINFO.size:
        return None
    return parse_bsdinfo(buf.raw)


def _procargs(lib, argmax: int, pid: int) -> ProcArgs | None:
    import ctypes

    mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
    buf = ctypes.create_string_buffer(argmax)
    size = ctypes.c_size_t(argmax)
    if lib.sysctl(mib, 3, buf, ctypes.pointer(size), None, 0) != 0:
        return None
    try:
        return parse_procargs2(ctypes.string_at(buf, min(size.value, argmax)))
    except ValueError:
        return None


def _list_fds(lib, pid: int) -> list[tuple[int, int]] | None:
    """``[(fd, PROX_FDTYPE_*)]`` of pid's descriptor table (PROC_PIDLISTFDS),
    or None when it cannot be read."""

    import ctypes

    # A NULL buffer asks for the size, with room for 20 more descriptors.
    need, _ = _call(lib.proc_pidinfo, pid, PROC_PIDLISTFDS, 0, None, 0)
    if need <= 0:
        return None
    size = need + 32 * FDINFO.size  # and room for more opened meanwhile
    listing = ctypes.create_string_buffer(size)
    got, error = _call(lib.proc_pidinfo, pid, PROC_PIDLISTFDS, 0, listing, size)
    if got < 0 or (got == 0 and error):
        return None
    return list(FDINFO.iter_unpack(listing.raw[:got - got % FDINFO.size]))


def _vnode_fd(lib, pid: int, fd: int, buffer=None):
    """``(VnodeFd, 0)`` for descriptor fd of pid (PROC_PIDFDVNODEPATHINFO),
    or ``(None, errno)``. ``buffer``, when given, is reused for the answer."""

    import ctypes

    if buffer is None:
        buffer = ctypes.create_string_buffer(VNODE_FDINFOWITHPATH_SIZE)
    done, error = _call(
        lib.proc_pidfdinfo, pid, fd, PROC_PIDFDVNODEPATHINFO, buffer,
        VNODE_FDINFOWITHPATH_SIZE,
    )
    if done != VNODE_FDINFOWITHPATH_SIZE:
        # The kernel answers with its own struct size or fails (0 and
        # errno); any other size would be a layout this module misreads.
        return None, error or errno.EIO
    return parse_vnode_fdinfowithpath(buffer.raw, fd), 0


def _open_vnodes(lib, pid: int) -> list[VnodeFd] | None:
    import ctypes

    listing = _list_fds(lib, pid)
    if listing is None:
        return None
    buffer = ctypes.create_string_buffer(VNODE_FDINFOWITHPATH_SIZE)
    result = []
    for fd, kind in listing:
        if kind != PROX_FDTYPE_VNODE:
            continue
        entry, error = _vnode_fd(lib, pid, fd, buffer)
        if entry is not None:
            result.append(entry)
        elif error not in FD_CHANGED_ERRORS:
            # Anything but "changed since the listing" would make a held
            # file look absent.
            return None
    return result


def _character_device(path: str, tdev: int) -> bool:
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    return stat.S_ISCHR(st.st_mode) and (st.st_rdev & 0xFFFFFFFF) == tdev


# --- the API (see the module docstring) ---------------------------------------

def available() -> bool:
    return _library() is not None


def unavailable_reason() -> str | None:
    _library()
    return _PROBLEM


def all_pids() -> list[int] | None:
    lib = _library()
    return None if lib is None else _list_pids(lib, PROC_ALL_PIDS, 0)


def children(pid: int) -> list[int] | None:
    lib = _library()
    if lib is None or not _valid_pid(pid):
        return None
    return _list_pids(lib, PROC_PPID_ONLY, pid)


def bsd_info(pid: int) -> BsdInfo | None:
    lib = _library()
    if lib is None or not _valid_pid(pid):
        return None
    return _bsd_info(lib, pid)


def procargs(pid: int) -> ProcArgs | None:
    lib = _library()
    if lib is None or not _valid_pid(pid):
        return None
    return _procargs(lib, _ARGMAX, pid)


def argv(pid: int) -> list[str] | None:
    args = procargs(pid)
    return None if args is None else args.argv


def environ(pid: int) -> dict[str, str] | None:
    args = procargs(pid)
    return None if args is None else args.environ


def exec_path(pid: int) -> str | None:
    args = procargs(pid)
    return None if args is None or not args.exec_path else args.exec_path


def exe(pid: int) -> str | None:
    lib = _library()
    if lib is None or not _valid_pid(pid):
        return None
    import ctypes

    buf = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
    length = lib.proc_pidpath(pid, buf, PROC_PIDPATHINFO_MAXSIZE)
    if length <= 0:
        return None
    return _decode(buf.raw[:length])


def cwd(pid: int) -> str | None:
    lib = _library()
    if lib is None or not _valid_pid(pid):
        return None
    import ctypes

    buf = ctypes.create_string_buffer(VNODEPATHINFO_SIZE)
    if lib.proc_pidinfo(pid, PROC_PIDVNODEPATHINFO, 0, buf,
                        VNODEPATHINFO_SIZE) != VNODEPATHINFO_SIZE:
        return None
    return parse_vnodepathinfo(buf.raw) or None


def open_vnodes(pid: int) -> list[VnodeFd] | None:
    lib = _library()
    if lib is None or not _valid_pid(pid):
        return None
    return _open_vnodes(lib, pid)


def tty_path(tdev: int) -> str | None:
    lib = _library()
    if lib is None or not isinstance(tdev, int):
        return None
    tdev &= 0xFFFFFFFF
    if tdev == NODEV:
        return None
    # devname(3) takes the signed dev_t.
    name = lib.devname(tdev - (1 << 32) if tdev >= (1 << 31) else tdev, stat.S_IFCHR)
    if name:
        path = "/dev/" + _decode(name)
        if _character_device(path, tdev):
            return path
    # devname(3) found no name (or one that is not the node): look in /dev.
    # lstat, so that /dev/stdin and the like (symlinks) never stand in.
    try:
        with os.scandir("/dev") as entries:
            for entry in entries:
                if _character_device(entry.path, tdev):
                    return entry.path
    except OSError:
        pass
    return None
