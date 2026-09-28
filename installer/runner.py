"""The single seam through which the installer runs external commands.

Every system command (apt, git, systemctl, gsettings, ...) goes through a
:class:`Runner` so tests can script responses and ``--dry-run`` can refuse
anything that would mutate the machine. Output is always captured; errors
carry only a bounded stderr tail, never the stdout payload, because stdout of
tools like ``gsettings`` or ``git config`` may contain user data.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

STDERR_TAIL_BYTES = 2000


def _tail(data: bytes | None) -> str:
    if not data:
        return ""
    return data[-STDERR_TAIL_BYTES:].decode("utf-8", errors="replace").strip()


class RunnerError(Exception):
    """An external command failed, timed out or could not be started."""

    def __init__(
        self,
        argv: Sequence[str],
        returncode: int | None,
        stderr_tail: str,
        reason: str = "",
    ) -> None:
        self.argv = [str(a) for a in argv]
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        name = self.argv[0] if self.argv else "<empty>"
        if reason:
            message = f"{name}: {reason}"
        else:
            message = f"{name} exited with status {returncode}"
        if stderr_tail:
            message = f"{message}: {stderr_tail}"
        super().__init__(message)


class Runner:
    """Run commands with captured output, a mandatory timeout and no stdin.

    ``env``, when given, is the complete child environment (``subprocess``
    semantics); callers that want to extend the current one pass
    ``{**os.environ, ...}`` explicitly. ``read_only`` declares that the call
    does not mutate the machine; it matters only for :class:`DryRunRunner`.
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        check: bool = True,
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
        cwd: Path | None = None,
        read_only: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        argv = [str(a) for a in argv]
        if not argv:
            raise RunnerError(argv, None, "", "empty command")
        try:
            completed = subprocess.run(
                argv,
                input=input,
                stdin=subprocess.DEVNULL if input is None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                env=dict(env) if env is not None else None,
                cwd=str(cwd) if cwd is not None else None,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(
                argv, None, _tail(exc.stderr), f"timed out after {timeout:g}s"
            ) from None
        except OSError as exc:
            raise RunnerError(
                argv, 127, "", f"cannot execute: {exc.strerror or exc}"
            ) from None
        if check and completed.returncode != 0:
            raise RunnerError(argv, completed.returncode, _tail(completed.stderr))
        return completed

    def which(self, name: str) -> str | None:
        return shutil.which(name)


class DryRunRunner(Runner):
    """Execute only ``read_only=True`` calls; record the rest as a plan."""

    def __init__(self) -> None:
        self.recorded: list[list[str]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        check: bool = True,
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
        cwd: Path | None = None,
        read_only: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        if read_only:
            return super().run(
                argv,
                timeout=timeout,
                check=check,
                env=env,
                input=input,
                cwd=cwd,
                read_only=True,
            )
        argv = [str(a) for a in argv]
        self.recorded.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")


__all__ = ["DryRunRunner", "Runner", "RunnerError", "STDERR_TAIL_BYTES"]
