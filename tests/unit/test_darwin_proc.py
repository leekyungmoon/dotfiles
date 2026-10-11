"""Unit tests for installer/darwin_proc.py, process inspection on macOS.

Three tiers, so that most of the module runs on any test machine:
- the pure parsers, fed buffers built from ctypes mirrors of xnu's
  <sys/proc_info.h> structures: an independent model of the C layout, not
  the module's own struct formats;
- the ctypes side, driven on Linux through LinuxLibSystem, a stand-in for
  libSystem that answers the libproc and sysctl calls from /proc in macOS's
  layouts and conventions;
- the same live-process scenarios against the real libproc, on macOS only
  (CI's macOS runners).
Plus guards that the files the Xcode Command Line Tools python3 (3.9) may
run keep to Python 3.9 grammar and the standard library.
"""

from __future__ import annotations

import ast
import ctypes
import errno
import os
import select
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import types
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from installer import darwin_proc  # noqa: E402

HANGUL = "\ud55c\uae00"
LINUX_PROC = sys.platform.startswith("linux") and os.path.isdir("/proc/self/fd")
DARWIN = sys.platform == "darwin"


# --- ctypes mirrors of xnu's <sys/proc_info.h> (LP64: arm64 and x86_64) -----

class ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class ProcFdInfo(ctypes.Structure):
    _fields_ = [("proc_fd", ctypes.c_int32), ("proc_fdtype", ctypes.c_uint32)]


class ProcFileInfo(ctypes.Structure):
    _fields_ = [
        ("fi_openflags", ctypes.c_uint32), ("fi_status", ctypes.c_uint32),
        ("fi_offset", ctypes.c_int64), ("fi_type", ctypes.c_int32),
        ("fi_guardflags", ctypes.c_uint32),
    ]


class VinfoStat(ctypes.Structure):
    _fields_ = [
        ("vst_dev", ctypes.c_uint32), ("vst_mode", ctypes.c_uint16),
        ("vst_nlink", ctypes.c_uint16), ("vst_ino", ctypes.c_uint64),
        ("vst_uid", ctypes.c_uint32), ("vst_gid", ctypes.c_uint32),
        ("vst_atime", ctypes.c_int64), ("vst_atimensec", ctypes.c_int64),
        ("vst_mtime", ctypes.c_int64), ("vst_mtimensec", ctypes.c_int64),
        ("vst_ctime", ctypes.c_int64), ("vst_ctimensec", ctypes.c_int64),
        ("vst_birthtime", ctypes.c_int64), ("vst_birthtimensec", ctypes.c_int64),
        ("vst_size", ctypes.c_int64), ("vst_blocks", ctypes.c_int64),
        ("vst_blksize", ctypes.c_int32), ("vst_flags", ctypes.c_uint32),
        ("vst_gen", ctypes.c_uint32), ("vst_rdev", ctypes.c_uint32),
        ("vst_qspare", ctypes.c_int64 * 2),
    ]


class VnodeInfo(ctypes.Structure):
    _fields_ = [
        ("vi_stat", VinfoStat), ("vi_type", ctypes.c_int),
        ("vi_pad", ctypes.c_int), ("vi_fsid", ctypes.c_int32 * 2),
    ]


class VnodeInfoPath(ctypes.Structure):
    _fields_ = [("vip_vi", VnodeInfo), ("vip_path", ctypes.c_char * 1024)]


class VnodeFdInfoWithPath(ctypes.Structure):
    _fields_ = [("pfi", ProcFileInfo), ("pvip", VnodeInfoPath)]


class ProcVnodePathInfo(ctypes.Structure):
    _fields_ = [("pvi_cdir", VnodeInfoPath), ("pvi_rdir", VnodeInfoPath)]


def strings_area(arguments, environment=()) -> bytes:
    return b"".join(item + b"\0" for item in [*arguments, *environment])


def procargs2_bytes(arguments, environment=(), exec_path=b"/bin/zsh",
                    apple=(b"ptr_munge=", b"main_stack=")) -> bytes:
    """KERN_PROCARGS2 data laid out as xnu's exec lays it out: argc, the exec
    path padded to a pointer boundary, argv and environment strings padded
    together to one, then the loader's "apple" strings. When argv and the
    environment end on that boundary, no padding separates the apple
    strings (see environ() in darwin_proc's docstring)."""

    path = exec_path + b"\0"
    path += b"\0" * (-len(path) % 8)
    strings = strings_area(arguments, environment)
    strings += b"\0" * (-len(strings) % 8)
    tail = b"".join(item + b"\0" for item in apple)
    return struct.pack("=i", len(arguments)) + path + strings + tail


def fill_vnode_info_path(target: VnodeInfoPath, path: str, st: os.stat_result) -> None:
    vst = target.vip_vi.vi_stat
    vst.vst_dev = st.st_dev & 0xFFFFFFFF
    vst.vst_mode = st.st_mode & 0xFFFF
    vst.vst_nlink = min(st.st_nlink, 0xFFFF)
    vst.vst_ino = st.st_ino
    vst.vst_uid = st.st_uid
    vst.vst_gid = st.st_gid
    vst.vst_size = st.st_size
    vst.vst_rdev = st.st_rdev & 0xFFFFFFFF
    target.vip_vi.vi_type = 1
    if path.endswith(" (deleted)"):
        path = ""  # macOS has no name for an unlinked file
    target.vip_path = os.fsencode(path)[:darwin_proc.MAXPATHLEN - 1]


def nfc(text):
    return None if text is None else unicodedata.normalize("NFC", text)


class LayoutTests(unittest.TestCase):
    def test_struct_sizes_and_offsets_match_the_headers(self):
        self.assertEqual(ctypes.sizeof(ProcBsdInfo), 136)
        self.assertEqual(darwin_proc.BSDINFO.size, ctypes.sizeof(ProcBsdInfo))
        self.assertEqual(darwin_proc.FDINFO.size, ctypes.sizeof(ProcFdInfo))
        self.assertEqual(darwin_proc.FDINFO.size, 8)
        self.assertEqual(darwin_proc.FILEINFO.size, ctypes.sizeof(ProcFileInfo))
        self.assertEqual(darwin_proc.VINFO_STAT_SIZE_OFFSET, VinfoStat.vst_size.offset)
        self.assertEqual(darwin_proc.VNODE_INFO_SIZE, ctypes.sizeof(VnodeInfo))
        self.assertEqual(ctypes.sizeof(VnodeFdInfoWithPath), 1200)
        self.assertEqual(darwin_proc.VNODE_FDINFOWITHPATH_SIZE, 1200)
        self.assertEqual(
            VnodeFdInfoWithPath.pvip.offset + VnodeInfoPath.vip_path.offset, 176
        )
        self.assertEqual(darwin_proc.VNODE_FDINFOWITHPATH_PATH_OFFSET, 176)
        self.assertEqual(ctypes.sizeof(ProcVnodePathInfo), 2352)
        self.assertEqual(darwin_proc.VNODEPATHINFO_SIZE, 2352)


class BsdInfoParserTests(unittest.TestCase):
    def test_every_field_comes_from_its_place(self):
        info = ProcBsdInfo(
            pbi_flags=0x4, pbi_status=darwin_proc.SRUN, pbi_xstatus=9,
            pbi_pid=4242, pbi_ppid=4000, pbi_uid=501, pbi_gid=20, pbi_ruid=502,
            pbi_rgid=21, pbi_svuid=503, pbi_svgid=22, rfu_1=7,
            pbi_comm=b"zsh", pbi_name=b"zsh-with-a-longer-name",
            pbi_nfiles=12, pbi_pgid=4243, pbi_pjobc=1, e_tdev=0x10000003,
            e_tpgid=4300, pbi_nice=-5, pbi_start_tvsec=1760000000,
            pbi_start_tvusec=250000,
        )
        self.assertEqual(darwin_proc.parse_bsdinfo(bytes(info)), darwin_proc.BsdInfo(
            pid=4242, ppid=4000, uid=501, pgid=4243, tdev=0x10000003, tpgid=4300,
            comm="zsh", name="zsh-with-a-longer-name", status=darwin_proc.SRUN,
            start=1760000000.25,
        ))

    def test_names_without_terminator_and_undecodable_bytes(self):
        info = darwin_proc.parse_bsdinfo(bytes(ProcBsdInfo(
            pbi_comm=b"c" * 16, pbi_name=b"caf\xe9", e_tdev=darwin_proc.NODEV,
        )))
        self.assertEqual(info.comm, "c" * 16)
        self.assertEqual(os.fsencode(info.name), b"caf\xe9")
        self.assertFalse(info.name.isprintable())
        self.assertEqual(info.tdev, darwin_proc.NODEV)

    def test_short_buffer_is_refused(self):
        with self.assertRaises(ValueError):
            darwin_proc.parse_bsdinfo(bytes(ProcBsdInfo())[:-1])


class ProcArgsParserTests(unittest.TestCase):
    def test_argv_is_exact_with_empty_arguments(self):
        arguments = [b"vim", b"", b"two words", f"{HANGUL}.txt".encode(), b"a\tb", b""]
        environment = [b"HOME=/tmp/h", b"LANG=en_US.UTF-8"]
        # Padding ends the environment (see test_apple_strings_may_follow...).
        self.assertNotEqual(len(strings_area(arguments, environment)) % 8, 0)
        args = darwin_proc.parse_procargs2(procargs2_bytes(arguments, environment))
        self.assertEqual(args.exec_path, "/bin/zsh")
        self.assertEqual(args.argv, ["vim", "", "two words", f"{HANGUL}.txt", "a\tb", ""])
        self.assertEqual(args.environ, {"HOME": "/tmp/h", "LANG": "en_US.UTF-8"})

    def test_exec_path_padding_of_any_length_is_skipped(self):
        for length in range(1, 20):
            exec_path = b"/" + b"x" * (length - 1)
            with self.subTest(length=length):
                args = darwin_proc.parse_procargs2(
                    procargs2_bytes([b"prog", b"", b"arg"], exec_path=exec_path)
                )
                self.assertEqual(args.exec_path, exec_path.decode())
                self.assertEqual(args.argv, ["prog", "", "arg"])

    def test_undecodable_bytes_round_trip(self):
        args = darwin_proc.parse_procargs2(
            procargs2_bytes([b"cat", b"caf\xe9"], [b"X=\xff"])
        )
        self.assertEqual(os.fsencode(args.argv[1]), b"caf\xe9")
        self.assertFalse(args.argv[1].isprintable())
        self.assertEqual(args.environ["X"].encode("utf-8", "surrogateescape"), b"\xff")

    def test_environment_rules(self):
        environment = [b"A=1", b"A=2", b"NOEQUALS", b"=no-key", b"BB="]
        # padding separates the apple strings
        self.assertNotEqual(len(strings_area([b"zsh"], environment)) % 8, 0)
        args = darwin_proc.parse_procargs2(procargs2_bytes([b"zsh"], environment))
        # The first of two wins (as getenv), entries without a name are
        # skipped, and the empty padding string ends the environment.
        self.assertEqual(args.environ, {"A": "1", "BB": ""})

    def test_apple_strings_may_follow_without_a_separator(self):
        # The documented limit: when argv and the environment end on an
        # 8-byte boundary, no padding separates the loader's strings.
        self.assertEqual(len(strings_area([b"zsh"], [b"V=x"])) % 8, 0)
        args = darwin_proc.parse_procargs2(procargs2_bytes([b"zsh"], [b"V=x"]))
        self.assertEqual(args.environ["V"], "x")
        self.assertIn("ptr_munge", args.environ)

    def test_withheld_environment_reads_empty(self):
        head = struct.pack("=i", 1) + b"/bin/zsh" + b"\0" * 8 + b"zsh\0"
        for data in (head, head + b"\0" * 12):
            with self.subTest(data=data):
                args = darwin_proc.parse_procargs2(data)
                self.assertEqual((args.argv, args.environ), (["zsh"], {}))

    def test_no_arguments(self):
        args = darwin_proc.parse_procargs2(procargs2_bytes([], [b"A=1"]))
        self.assertEqual((args.argv, args.environ), ([], {"A": "1"}))

    def test_unterminated_environment_tail_is_dropped(self):
        data = struct.pack("=i", 1) + b"/bin/zsh" + b"\0" * 8 + b"zsh\0A=1\0B=2"
        self.assertEqual(darwin_proc.parse_procargs2(data).environ, {"A": "1"})

    def test_malformed_data_is_refused(self):
        cut = struct.pack("=i", 3) + b"/bin/zsh" + b"\0" * 8 + b"zsh\0-l\0"
        for data in (
            b"", b"\x01\x00", struct.pack("=i", -1) + b"/x\0",
            struct.pack("=i", 1) + b"/no-terminator", cut,
        ):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    darwin_proc.parse_procargs2(data)


def vnode_fdinfo(path=b"", **fields) -> bytes:
    info = VnodeFdInfoWithPath()
    info.pfi.fi_openflags = fields.get("openflags", 0)
    info.pfi.fi_status = fields.get("status", 0)
    info.pfi.fi_offset = 77
    info.pfi.fi_type = 1
    vst = info.pvip.vip_vi.vi_stat
    vst.vst_dev = fields.get("dev", 0)
    vst.vst_mode = fields.get("mode", 0)
    vst.vst_nlink = fields.get("nlink", 0)
    vst.vst_ino = fields.get("ino", 0)
    vst.vst_uid = fields.get("uid", 0)
    vst.vst_gid = 20
    vst.vst_size = fields.get("size", 0)
    vst.vst_blksize = 4096
    info.pvip.vip_vi.vi_type = 1
    info.pvip.vip_path = path
    return bytes(info)


class VnodeParserTests(unittest.TestCase):
    def test_every_field_comes_from_its_place(self):
        path = f"/private/var/folders/x/T/two words {HANGUL}/file".encode()
        entry = darwin_proc.parse_vnode_fdinfowithpath(vnode_fdinfo(
            path, openflags=darwin_proc.FREAD | darwin_proc.FWRITE | 0x8,
            status=darwin_proc.PROC_FP_CLEXEC | 0x1, dev=0x1000011,
            mode=stat.S_IFREG | 0o600, nlink=1, ino=(1 << 40) + 5, uid=501,
            size=12345,
        ), 7)
        self.assertEqual(entry, darwin_proc.VnodeFd(
            fd=7, path=path.decode(), dev=0x1000011, ino=(1 << 40) + 5, nlink=1,
            mode=stat.S_IFREG | 0o600, size=12345, cloexec=True, uid=501,
            openflags=darwin_proc.FREAD | darwin_proc.FWRITE | 0x8,
        ))

    def test_flags_unnamed_files_and_long_paths(self):
        plain = darwin_proc.parse_vnode_fdinfowithpath(vnode_fdinfo(b"/tmp/x", status=0x1))
        self.assertFalse(plain.cloexec)
        self.assertEqual(plain.fd, -1)
        self.assertEqual(darwin_proc.parse_vnode_fdinfowithpath(vnode_fdinfo()).path, "")
        long_path = b"/" + b"p" * 1022
        self.assertEqual(
            darwin_proc.parse_vnode_fdinfowithpath(vnode_fdinfo(long_path)).path,
            long_path.decode(),
        )

    def test_short_buffer_is_refused(self):
        with self.assertRaises(ValueError):
            darwin_proc.parse_vnode_fdinfowithpath(vnode_fdinfo()[:-1])

    def test_current_directory(self):
        info = ProcVnodePathInfo()
        info.pvi_cdir.vip_path = f"/private/tmp/two words {HANGUL}".encode()
        info.pvi_rdir.vip_path = b"/"
        self.assertEqual(
            darwin_proc.parse_vnodepathinfo(bytes(info)),
            f"/private/tmp/two words {HANGUL}",
        )
        self.assertEqual(darwin_proc.parse_vnodepathinfo(bytes(ProcVnodePathInfo())), "")
        with self.assertRaises(ValueError):
            darwin_proc.parse_vnodepathinfo(bytes(info)[:-1])


# --- a stand-in for libSystem on Linux --------------------------------------

PROX_FDTYPE_SOCKET, PROX_FDTYPE_KQUEUE, PROX_FDTYPE_PIPE = 2, 5, 6
LINUX_O_CLOEXEC = 0o2000000
LINUX_STATES = {
    "R": darwin_proc.SRUN, "S": darwin_proc.SSLEEP, "D": darwin_proc.SSLEEP,
    "I": darwin_proc.SSLEEP, "T": darwin_proc.SSTOP, "t": darwin_proc.SSTOP,
    "Z": darwin_proc.SZOMB, "X": darwin_proc.SZOMB,
}


class LinuxLibSystem:
    """libSystem's libproc and sysctl calls, answered from Linux's /proc.

    The answers are laid out with the ctypes mirrors above, not with
    darwin_proc's own formats, and keep libproc's conventions: a byte count,
    or 0 with errno set (sysctl: -1); a NULL buffer asks for the size; only
    the caller's own processes are described (EPERM for others, ESRCH for a
    pid that is gone, most calls fail for a zombie); an unlinked file has no
    name. So darwin_proc's ctypes code runs here as it would on a Mac.
    """

    ARGMAX = 1 << 20

    def __init__(self):
        self.uid = os.geteuid()

    @staticmethod
    def fail(code, result=0):
        ctypes.set_errno(code)
        return result

    @staticmethod
    def put(buffer, size, data):
        count = min(len(data), size)
        ctypes.memmove(buffer, data, count)
        return count

    def answer(self, buffer, size, value):
        data = bytes(value)
        if buffer is None or size < len(data):
            return self.fail(errno.ENOMEM)
        return self.put(buffer, size, data)

    @staticmethod
    def stat_fields(pid):
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return text[text.index("(") + 1:text.rindex(")")], text[text.rindex(")") + 2:].split()

    @staticmethod
    def owner(pid):
        try:
            status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        for line in status.splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[2])  # real, effective, saved, fs
        return None

    def refusal(self, pid):
        """The errno libproc gives for ``pid``, or None when it may be read."""
        owner = self.owner(pid)
        if owner is None:
            return errno.ESRCH
        return None if self.uid in (0, owner) else errno.EPERM

    def zombie(self, pid):
        try:
            return self.stat_fields(pid)[1][0] in "ZX"
        except (OSError, ValueError, IndexError):
            return True

    # libproc

    def proc_listpids(self, kind, info, buffer, size):
        pids = [int(entry) for entry in os.listdir("/proc") if entry.isdigit()]
        if buffer is None:
            return (len(pids) + 20) * 4
        if kind == darwin_proc.PROC_PPID_ONLY:
            def parent(pid):
                try:
                    return int(self.stat_fields(pid)[1][1])
                except (OSError, ValueError, IndexError):
                    return None
            pids = [pid for pid in pids if parent(pid) == info]
        elif kind != darwin_proc.PROC_ALL_PIDS:
            return self.fail(errno.EINVAL)
        return self.put(buffer, size - size % 4, struct.pack(f"={len(pids)}i", *pids))

    def bsdinfo(self, pid):
        comm, fields = self.stat_fields(pid)
        with open("/proc/stat", encoding="utf-8") as handle:
            boot = next(int(line.split()[1]) for line in handle if line.startswith("btime "))
        start = boot + int(fields[19]) / os.sysconf("SC_CLK_TCK")
        tty = int(fields[4])
        info = ProcBsdInfo(
            pbi_status=LINUX_STATES.get(fields[0], darwin_proc.SSLEEP),
            pbi_pid=pid, pbi_ppid=int(fields[1]), pbi_uid=self.owner(pid) or 0,
            pbi_pgid=int(fields[2]), e_tdev=tty if tty else darwin_proc.NODEV,
            e_tpgid=max(int(fields[5]), 0),
            pbi_comm=os.fsencode(comm)[:15], pbi_name=os.fsencode(comm)[:31],
            pbi_start_tvsec=int(start),
            pbi_start_tvusec=int((start - int(start)) * 1000000),
        )
        return info

    def fds(self, pid):
        names = os.listdir(f"/proc/{pid}/fd")
        result = []
        for name in sorted(names, key=int):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{name}")
            except OSError:
                continue
            if target.startswith("/"):
                kind = darwin_proc.PROX_FDTYPE_VNODE
            elif target.startswith("pipe:"):
                kind = PROX_FDTYPE_PIPE
            elif target.startswith("socket:"):
                kind = PROX_FDTYPE_SOCKET
            else:
                kind = PROX_FDTYPE_KQUEUE
            result.append((int(name), kind))
        return result

    def proc_pidinfo(self, pid, flavor, arg, buffer, size):
        refusal = self.refusal(pid)
        if refusal:
            return self.fail(refusal)
        try:
            if flavor == darwin_proc.PROC_PIDTBSDINFO:  # zombies too
                return self.answer(buffer, size, self.bsdinfo(pid))
            if self.zombie(pid):
                return self.fail(errno.ESRCH)
            if flavor == darwin_proc.PROC_PIDLISTFDS:
                fds = self.fds(pid)
                if buffer is None:
                    return (len(fds) + 20) * ctypes.sizeof(ProcFdInfo)
                data = b"".join(bytes(ProcFdInfo(fd, kind)) for fd, kind in fds)
                return self.put(buffer, size - size % 8, data)
            if flavor == darwin_proc.PROC_PIDVNODEPATHINFO:
                info = ProcVnodePathInfo()
                cwd = f"/proc/{pid}/cwd"
                fill_vnode_info_path(info.pvi_cdir, os.readlink(cwd), os.stat(cwd))
                fill_vnode_info_path(info.pvi_rdir, "/", os.stat("/"))
                return self.answer(buffer, size, info)
        except (OSError, ValueError, IndexError):
            return self.fail(errno.ESRCH)
        return self.fail(errno.EINVAL)

    def proc_pidfdinfo(self, pid, fd, flavor, buffer, size):
        refusal = self.refusal(pid)
        if refusal:
            return self.fail(refusal)
        if flavor != darwin_proc.PROC_PIDFDVNODEPATHINFO:
            return self.fail(errno.EINVAL)
        link = f"/proc/{pid}/fd/{fd}"
        try:
            target = os.readlink(link)
            st = os.stat(link)
            with open(f"/proc/{pid}/fdinfo/{fd}", encoding="utf-8") as handle:
                flags = next(int(line.split()[1], 8) for line in handle
                             if line.startswith("flags:"))
        except (OSError, ValueError, StopIteration):
            return self.fail(errno.EBADF)
        if not target.startswith("/"):
            return self.fail(errno.EBADF)  # not a vnode
        info = VnodeFdInfoWithPath()
        info.pfi.fi_openflags = (flags & os.O_ACCMODE) + 1  # FFLAGS(): FREAD/FWRITE
        info.pfi.fi_status = darwin_proc.PROC_FP_CLEXEC if flags & LINUX_O_CLOEXEC else 0
        info.pfi.fi_type = 1
        fill_vnode_info_path(info.pvip, target, st)
        return self.answer(buffer, size, info)

    def proc_pidpath(self, pid, buffer, size):
        try:
            target = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return self.fail(errno.ESRCH)
        data = os.fsencode(target[:-len(" (deleted)")] if target.endswith(" (deleted)") else target)
        if len(data) >= size:
            return self.fail(errno.ENOMEM)
        self.put(buffer, size, data + b"\0")
        return len(data)

    # sysctl

    def procargs2(self, pid):
        if self.refusal(pid) or self.zombie(pid):
            return None
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            environment = Path(f"/proc/{pid}/environ").read_bytes()
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return None
        if not cmdline:
            return None
        arguments = (cmdline[:-1] if cmdline.endswith(b"\0") else cmdline).split(b"\0")
        path = os.fsencode(exe) + b"\0"
        path += b"\0" * (-len(path) % 8)
        strings = b"".join(item + b"\0" for item in arguments) + environment
        strings += b"\0" * (-len(strings) % 8)
        return struct.pack("=i", len(arguments)) + path + strings + b"ptr_munge=\0main_stack=\0"

    def sysctl(self, mib, namelen, oldp, oldlenp, newp, newlen):
        names = list(mib[:namelen])
        if names == [darwin_proc.CTL_KERN, darwin_proc.KERN_ARGMAX]:
            data = struct.pack("=i", self.ARGMAX)
        elif len(names) == 3 and names[:2] == [darwin_proc.CTL_KERN, darwin_proc.KERN_PROCARGS2]:
            data = self.procargs2(names[2])
            if data is None:
                return self.fail(errno.EINVAL, -1)
        else:
            return self.fail(errno.ENOENT, -1)
        if oldp is None:
            oldlenp.contents.value = len(data)
            return 0
        if len(data) > oldlenp.contents.value:
            return self.fail(errno.ENOMEM, -1)
        self.put(oldp, len(data), data)
        oldlenp.contents.value = len(data)
        return 0

    # libc

    def devname(self, dev, mode):
        dev &= 0xFFFFFFFF
        for directory in ("/dev/pts", "/dev"):
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                try:
                    st = os.lstat(entry.path)
                except OSError:
                    continue
                if stat.S_ISCHR(st.st_mode) and st.st_rdev & 0xFFFFFFFF == dev:
                    return os.fsencode(os.path.relpath(entry.path, "/dev"))
        return None


def use_library(test, lib):
    patcher = mock.patch.multiple(
        darwin_proc, _LIB=lib, _ARGMAX=LinuxLibSystem.ARGMAX, _PROBLEM=None
    )
    patcher.start()
    test.addCleanup(patcher.stop)


# --- live processes ------------------------------------------------------------

# The child holds three files: one opened read-only (close-on-exec, as Python
# opens every file), one for appending made inheritable, and one the test
# unlinks while it is held.
CHILD = (
    "import os, sys; "
    "held = open(os.environ['HELD'], 'rb'); "
    "shared = open(os.environ['SHARED'], 'ab'); "
    "os.set_inheritable(shared.fileno(), True); "
    "doomed = open(os.environ['DOOMED'], 'rb'); "
    "sys.stdout.write('ready\\n'); sys.stdout.flush(); "
    "sys.stdin.read()"
)
CHILD_TAIL = ["-c", CHILD, "", "two words", HANGUL, ""]
CHILD_VALUE = "\uac12 two"
# A child that says it is ready, then waits for the end of its input.
READY_CHILD = "import sys; sys.stdout.write('ready\\n'); sys.stdout.flush(); sys.stdin.read()"
# The child makes its pty its controlling terminal (TIOCSCTTY; the getattr
# fallbacks are macOS's and Linux's values).
TERMINAL_CHILD = (
    "import fcntl, os, sys, termios; "
    "request = getattr(termios, 'TIOCSCTTY', "
    "0x20007461 if sys.platform == 'darwin' else 0x540E); "
    "fcntl.ioctl(0, request, 0); os.write(1, b'ready\\n'); sys.stdin.read()"
)


class ProcessScenarios:
    """What the callers rely on, checked against live child processes."""

    # macOS may withhold another process's environment (an empty dict).
    environment_may_be_withheld = True

    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="darwin-proc-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.place = self.root / f"two words {HANGUL}"
        self.place.mkdir()
        self.held = self.place / "held file.txt"
        self.held.write_bytes(b"held\n")
        self.shared = self.place / "shared.txt"
        self.shared.write_bytes(b"")
        self.doomed = self.place / "doomed.txt"
        self.doomed.write_bytes(b"x")

    @staticmethod
    def reap(child):
        if child.poll() is None:
            child.kill()
        child.wait()
        for stream in (child.stdin, child.stdout):
            if stream is not None:
                stream.close()

    def spawn(self):
        env = dict(
            os.environ, HELD=str(self.held), SHARED=str(self.shared),
            DOOMED=str(self.doomed), DARWIN_PROC_VALUE=CHILD_VALUE,
        )
        child = subprocess.Popen(
            [sys.executable, *CHILD_TAIL], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, cwd=str(self.place), env=env,
        )
        self.addCleanup(self.reap, child)
        ready, _, _ = select.select([child.stdout], [], [], 30)
        self.assertTrue(ready and child.stdout.readline() == b"ready\n",
                        "the test child did not start")
        return child

    def test_argv_is_exact(self):
        child = self.spawn()
        argv = darwin_proc.argv(child.pid)
        self.assertIsNotNone(argv)
        # argv[0] is not compared: a framework Python (python.org, Homebrew,
        # Xcode) re-executes itself as Python.app, which replaces it.
        self.assertTrue(argv[0])
        self.assertEqual(argv[1:], CHILD_TAIL)
        args = darwin_proc.procargs(child.pid)
        self.assertEqual(args.argv, argv)
        self.assertTrue(os.path.isabs(args.exec_path), args.exec_path)
        self.assertEqual(darwin_proc.exec_path(child.pid), args.exec_path)

    def test_environment(self):
        child = self.spawn()
        environment = darwin_proc.environ(child.pid)
        self.assertIsInstance(environment, dict)
        if environment or not self.environment_may_be_withheld:
            self.assertEqual(environment.get("DARWIN_PROC_VALUE"), CHILD_VALUE)
        self.assertIsInstance(darwin_proc.environ(os.getpid()), dict)

    def test_bsd_info(self):
        before = time.time()
        child = self.spawn()
        after = time.time()
        info = darwin_proc.bsd_info(child.pid)
        self.assertIsNotNone(info)
        self.assertEqual(
            (info.pid, info.ppid, info.uid, info.pgid),
            (child.pid, os.getpid(), os.geteuid(), os.getpgid(child.pid)),
        )
        self.assertIn(info.status, (darwin_proc.SRUN, darwin_proc.SSLEEP))
        self.assertTrue(info.comm)
        self.assertIsInstance(info.name, str)
        self.assertLessEqual(before - 5, info.start)
        self.assertLessEqual(info.start, after + 5)
        # The controlling terminal (or none) is inherited.
        self.assertEqual(info.tdev, darwin_proc.bsd_info(os.getpid()).tdev)

    def test_process_table(self):
        child = self.spawn()
        self.assertIn(child.pid, darwin_proc.children(os.getpid()))
        self.assertEqual(darwin_proc.children(child.pid), [])
        pids = darwin_proc.all_pids()
        self.assertIn(os.getpid(), pids)
        self.assertIn(child.pid, pids)

    def test_open_files(self):
        child = self.spawn()
        doomed = os.stat(self.doomed)
        doomed_name = os.path.realpath(self.doomed)
        self.doomed.unlink()
        files = darwin_proc.open_vnodes(child.pid)
        self.assertIsNotNone(files)
        found = {(entry.dev, entry.ino): entry for entry in files}

        def entry(st):
            return found[(st.st_dev & 0xFFFFFFFF, st.st_ino)]

        held = entry(os.stat(self.held))
        self.assertEqual(nfc(held.path), nfc(os.path.realpath(self.held)))
        self.assertEqual((held.nlink, held.size, held.uid), (1, 5, os.geteuid()))
        self.assertTrue(stat.S_ISREG(held.mode))
        self.assertTrue(held.cloexec)
        self.assertEqual(held.openflags & (darwin_proc.FREAD | darwin_proc.FWRITE),
                         darwin_proc.FREAD)
        self.assertGreaterEqual(held.fd, 3)
        shared = entry(os.stat(self.shared))
        self.assertFalse(shared.cloexec)
        self.assertEqual(shared.openflags & (darwin_proc.FREAD | darwin_proc.FWRITE),
                         darwin_proc.FWRITE)
        gone = entry(doomed)
        self.assertEqual(gone.nlink, 0)
        self.assertIn(nfc(gone.path), ("", nfc(doomed_name)))

    def test_current_directory_and_executable(self):
        child = self.spawn()
        self.assertEqual(nfc(darwin_proc.cwd(child.pid)),
                         nfc(os.path.realpath(self.place)))
        executable = darwin_proc.exe(child.pid)
        self.assertTrue(executable and os.path.isabs(executable)
                        and os.path.exists(executable), executable)

    def test_controlling_terminal(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        try:
            rdev = os.fstat(slave).st_rdev & 0xFFFFFFFF
            name = os.path.realpath(os.ttyname(slave))
            child = subprocess.Popen(
                [sys.executable, "-c", TERMINAL_CHILD], stdin=slave, stdout=slave,
                stderr=slave, start_new_session=True,
            )
        finally:
            os.close(slave)
        self.addCleanup(self.reap, child)
        output = b""
        deadline = time.monotonic() + 30
        while b"ready" not in output and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 1)
            if ready:
                try:
                    output += os.read(master, 1024)
                except OSError:
                    break
        self.assertIn(b"ready", output)
        info = darwin_proc.bsd_info(child.pid)
        self.assertEqual((info.tdev, info.tpgid), (rdev, child.pid))
        self.assertEqual(darwin_proc.tty_path(info.tdev), name)
        self.assertIsNone(darwin_proc.tty_path(darwin_proc.NODEV))

    def test_gone_foreign_and_invalid_processes(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        calls = (
            darwin_proc.bsd_info, darwin_proc.argv, darwin_proc.environ,
            darwin_proc.exec_path, darwin_proc.exe, darwin_proc.cwd,
            darwin_proc.open_vnodes,
        )
        for call in calls:
            with self.subTest(call=call.__name__):
                self.assertIsNone(call(child.pid))
        self.assertEqual(darwin_proc.children(child.pid), [])
        init_owner = os.stat("/proc/1").st_uid if os.path.isdir("/proc/1") else 0
        if os.geteuid() not in (0, init_owner):  # pid 1 is launchd's (init's)
            self.assertIsNone(darwin_proc.bsd_info(1))
            self.assertIsNone(darwin_proc.argv(1))
            self.assertIsNone(darwin_proc.open_vnodes(1))
        for invalid in (-1, 1 << 40, "1", None):
            with self.subTest(invalid=invalid):
                self.assertIsNone(darwin_proc.bsd_info(invalid))
                self.assertIsNone(darwin_proc.children(invalid))

    def test_zombie_has_no_argv(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        self.addCleanup(child.wait)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            info = darwin_proc.bsd_info(child.pid)
            if info is None or info.status == darwin_proc.SZOMB:
                break
            time.sleep(0.05)
        else:
            self.fail("the child did not exit")
        self.assertIsNone(darwin_proc.argv(child.pid))


@unittest.skipUnless(LINUX_PROC, "LinuxLibSystem answers from Linux's /proc")
class SimulatedLibprocTests(ProcessScenarios, unittest.TestCase):
    """The scenarios on Linux, through LinuxLibSystem."""

    environment_may_be_withheld = False

    def setUp(self):
        use_library(self, LinuxLibSystem())
        super().setUp()


@unittest.skipUnless(DARWIN, "needs macOS libproc")
class DarwinLibprocTests(ProcessScenarios, unittest.TestCase):
    """The scenarios against the real libproc and sysctl."""

    def setUp(self):
        self.assertTrue(darwin_proc.available(), darwin_proc.unavailable_reason())
        super().setUp()


@unittest.skipUnless(LINUX_PROC, "LinuxLibSystem answers from Linux's /proc")
class SimulatedErrorTests(unittest.TestCase):
    """The ctypes side's error rules, with LinuxLibSystem made to fail."""

    def hold_files(self, count):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        handles = []
        for index in range(count):
            handle = open(Path(directory.name, f"file-{index}"), "wb")
            self.addCleanup(handle.close)
            handles.append(handle)
        return handles

    def test_descriptors_that_changed_since_the_listing_are_skipped(self):
        closed, reused, reclaimed, kept = self.hold_files(4)
        # Closed meanwhile; the number now names a socket; the vnode was
        # reclaimed (a revoked terminal).
        changes = {
            closed.fileno(): errno.EBADF,
            reused.fileno(): errno.ENOTSUP,
            reclaimed.fileno(): errno.ENOENT,
        }

        class Changed(LinuxLibSystem):
            def proc_pidfdinfo(self, pid, fd, flavor, buffer, size):
                if fd in changes:
                    return self.fail(changes[fd])
                return super().proc_pidfdinfo(pid, fd, flavor, buffer, size)

        use_library(self, Changed())
        files = darwin_proc.open_vnodes(os.getpid())
        self.assertIsNotNone(files)
        fds = {entry.fd for entry in files}
        self.assertFalse(fds & set(changes))
        self.assertIn(kept.fileno(), fds)

    def test_an_answer_of_another_size_means_cannot_tell(self):
        # A struct of another layout: never parsed, never skipped.
        (held,) = self.hold_files(1)

        class OtherLayout(LinuxLibSystem):
            def proc_pidfdinfo(self, pid, fd, flavor, buffer, size):
                done = super().proc_pidfdinfo(pid, fd, flavor, buffer, size)
                return done - 8 if done and fd == held.fileno() else done

        use_library(self, OtherLayout())
        self.assertIsNone(darwin_proc.open_vnodes(os.getpid()))

    def test_any_other_descriptor_error_means_cannot_tell(self):
        (held,) = self.hold_files(1)

        class Refused(LinuxLibSystem):
            def proc_pidfdinfo(self, pid, fd, flavor, buffer, size):
                if fd == held.fileno():
                    return self.fail(errno.EACCES)
                return super().proc_pidfdinfo(pid, fd, flavor, buffer, size)

        use_library(self, Refused())
        self.assertIsNone(darwin_proc.open_vnodes(os.getpid()))

    def test_listing_errors_are_not_empty_lists(self):
        class Failing(LinuxLibSystem):
            def proc_listpids(self, kind, info, buffer, size):
                if buffer is None:
                    return super().proc_listpids(kind, info, buffer, size)
                return self.fail(errno.EPERM)

        use_library(self, Failing())
        self.assertIsNone(darwin_proc.all_pids())
        self.assertIsNone(darwin_proc.children(os.getpid()))
        use_library(self, LinuxLibSystem())
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                 stdin=subprocess.PIPE)
        self.addCleanup(child.wait)
        self.addCleanup(child.stdin.close)
        self.assertEqual(darwin_proc.children(child.pid), [])  # really none

    def test_cut_argument_data_is_refused(self):
        child = subprocess.Popen(
            [sys.executable, "-c", READY_CHILD, "first-argument", "second"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
        self.addCleanup(ProcessScenarios.reap, child)
        ready, _, _ = select.select([child.stdout], [], [], 30)
        self.assertTrue(ready and child.stdout.readline() == b"ready\n")

        class Cut(LinuxLibSystem):
            def procargs2(self, pid):
                data = super().procargs2(pid)
                # The data ends inside an argument that argc still counts.
                return data[:data.index(b"first-argument") + 5] if data else data

        use_library(self, LinuxLibSystem())
        self.assertEqual(darwin_proc.argv(child.pid)[-2:], ["first-argument", "second"])
        use_library(self, Cut())
        self.assertIsNone(darwin_proc.argv(child.pid))
        self.assertIsNone(darwin_proc.procargs(child.pid))

    def test_terminal_name_is_checked_against_the_node(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        rdev = os.fstat(slave).st_rdev & 0xFFFFFFFF

        class Misnamed(LinuxLibSystem):
            def devname(self, dev, mode):
                return b"null"  # a character device, but another one

        use_library(self, Misnamed())
        # /dev/null is refused; the /dev scan looks at /dev itself, where
        # Linux has no pts entries, so nothing is found.
        self.assertIsNone(darwin_proc.tty_path(rdev))
        null = os.stat("/dev/null").st_rdev & 0xFFFFFFFF

        class Unnamed(LinuxLibSystem):
            def devname(self, dev, mode):
                return None

        use_library(self, Unnamed())
        self.assertEqual(darwin_proc.tty_path(null), "/dev/null")


@unittest.skipUnless(LINUX_PROC, "LinuxLibSystem answers from Linux's /proc")
class LoadLibraryTests(unittest.TestCase):
    """available(): libSystem must load and describe this very process."""

    def load(self, lib=None, error=None):
        opener = mock.Mock(return_value=lib, side_effect=error)
        with mock.patch.object(darwin_proc, "sys", types.SimpleNamespace(platform="darwin")), \
             mock.patch.object(darwin_proc, "_open_libsystem", opener):
            return darwin_proc._load_library()

    def test_a_working_library_is_kept(self):
        lib = LinuxLibSystem()
        self.assertEqual(self.load(lib), (lib, LinuxLibSystem.ARGMAX, None))

    def test_every_refusal_has_a_reason(self):
        def shifted(field):
            class Shifted(LinuxLibSystem):
                def bsdinfo(self, pid):
                    info = super().bsdinfo(pid)
                    setattr(info, field, getattr(info, field) + 1)
                    return info
            Shifted.__name__ = f"Shifted_{field}"
            return Shifted()

        class NoArguments(LinuxLibSystem):
            def procargs2(self, pid):
                return None

        class NoDescriptors(LinuxLibSystem):
            def proc_pidinfo(self, pid, flavor, arg, buffer, size):
                if flavor == darwin_proc.PROC_PIDLISTFDS:
                    return self.fail(errno.EPERM)
                return super().proc_pidinfo(pid, flavor, arg, buffer, size)

        class ProbeNotListed(LinuxLibSystem):
            def fds(self, pid):
                return [
                    (fd, kind) for fd, kind in super().fds(pid)
                    if os.path.realpath(f"/proc/{pid}/fd/{fd}") != os.devnull
                ]

        class ProbeMisread(LinuxLibSystem):
            # The vnode fields of another layout: the probe's inode is off.
            def proc_pidfdinfo(self, pid, fd, flavor, buffer, size):
                done = super().proc_pidfdinfo(pid, fd, flavor, buffer, size)
                if done and os.path.realpath(f"/proc/{pid}/fd/{fd}") == os.devnull:
                    VnodeFdInfoWithPath.from_buffer(buffer).pvip.vip_vi.vi_stat.vst_ino += 1
                return done

        class TinyArgmax(LinuxLibSystem):
            ARGMAX = 1024

        cases = (
            (shifted("pbi_pid"), "PROC_PIDTBSDINFO"),
            (shifted("pbi_ppid"), "PROC_PIDTBSDINFO"),
            (shifted("pbi_uid"), "PROC_PIDTBSDINFO"),
            (shifted("pbi_pgid"), "PROC_PIDTBSDINFO"),
            (NoArguments(), "KERN_PROCARGS2"),
            (NoDescriptors(), "PROC_PIDLISTFDS"),
            (ProbeNotListed(), "PROC_PIDLISTFDS"),
            (ProbeMisread(), "PROC_PIDFDVNODEPATHINFO"),
            (TinyArgmax(), "kern.argmax"),
        )
        for lib, reason in cases:
            with self.subTest(lib=type(lib).__name__):
                loaded, argmax, problem = self.load(lib)
                self.assertIsNone(loaded)
                self.assertEqual(argmax, 0)
                self.assertIn(reason, problem)
        loaded, _, problem = self.load(error=OSError("image not found"))
        self.assertIsNone(loaded)
        self.assertIn("image not found", problem)

    def test_a_parent_that_exits_during_the_check_is_tolerated(self):
        lib = LinuxLibSystem()
        parent, impossible = os.getppid(), 0x7FFFFFFF
        # Exiting between the two getppid() calls hands this process to
        # launchd: bsd_info may name either parent.
        for answers in ([parent, impossible], [impossible, parent]):
            with self.subTest(answers=answers), \
                 mock.patch.object(darwin_proc.os, "getppid", side_effect=answers):
                self.assertEqual(self.load(lib), (lib, LinuxLibSystem.ARGMAX, None))
        with mock.patch.object(darwin_proc.os, "getppid", return_value=impossible):
            self.assertIn("PROC_PIDTBSDINFO", self.load(lib)[2])


@unittest.skipIf(DARWIN, "macOS has libproc")
class UnavailableTests(unittest.TestCase):
    def test_every_call_returns_none_off_macos(self):
        with mock.patch.multiple(darwin_proc, _LIB=None, _ARGMAX=0, _PROBLEM=None):
            self.assertFalse(darwin_proc.available())
            self.assertIn(repr(sys.platform), darwin_proc.unavailable_reason())
            self.assertIsNone(darwin_proc.all_pids())
            for call in (
                darwin_proc.children, darwin_proc.bsd_info, darwin_proc.procargs,
                darwin_proc.argv, darwin_proc.environ, darwin_proc.exec_path,
                darwin_proc.exe, darwin_proc.cwd, darwin_proc.open_vnodes,
                darwin_proc.tty_path,
            ):
                with self.subTest(call=call.__name__):
                    self.assertIsNone(call(os.getpid()))


# --- the files Python 3.9 may run ---------------------------------------------

PYTHON39_FILES = ("installer/darwin_proc.py", "tmux/resurrect-ai-session.py")
# The standard-library modules these files use, for interpreters without
# sys.stdlib_module_names (3.9).
KNOWN_STDLIB = {
    "__future__", "collections", "ctypes", "errno", "fcntl", "json", "os",
    "pathlib", "re", "select", "shlex", "shutil", "stat", "struct",
    "subprocess", "sys", "tempfile", "typing", "unicodedata",
}


class Python39Tests(unittest.TestCase):
    def test_python_3_9_grammar(self):
        for name in PYTHON39_FILES:
            with self.subTest(name=name):
                source = (REPO_ROOT / name).read_text(encoding="utf-8")
                ast.parse(source, filename=name, feature_version=(3, 9))

    def test_standard_library_only(self):
        # Checked against KNOWN_STDLIB everywhere, so that the list 3.9 relies
        # on cannot fall behind; newer Pythons check the list itself.
        stdlib = KNOWN_STDLIB
        if hasattr(sys, "stdlib_module_names"):
            self.assertLessEqual(KNOWN_STDLIB, set(sys.stdlib_module_names))
        for name in PYTHON39_FILES:
            tree = ast.parse((REPO_ROOT / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                for module in modules:
                    top = module.split(".")[0]
                    with self.subTest(name=name, module=module):
                        # installer.darwin_proc: the hook's macOS backend
                        self.assertTrue(top in stdlib or module == "installer", module)

    def test_api_is_documented(self):
        for name in (
            "available", "unavailable_reason", "all_pids", "children", "bsd_info",
            "procargs", "argv", "environ", "exec_path", "exe", "cwd",
            "open_vnodes", "tty_path", "parse_bsdinfo", "parse_procargs2",
            "parse_vnode_fdinfowithpath", "parse_vnodepathinfo",
        ):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(darwin_proc, name)))
                self.assertIn(f"  {name}(", darwin_proc.__doc__)


if __name__ == "__main__":
    unittest.main()
