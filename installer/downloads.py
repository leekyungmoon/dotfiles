"""Verified downloads: fetch, digests, OpenPGP key pinning and gpgv checks.

Everything here fails closed. A digest, fingerprint or signature that does not
match raises :class:`VerificationError`; callers must not use the file.

OpenPGP handling is deliberately narrow: ASCII-armor decoding, packet framing
and v4 primary-key fingerprints are done in the standard library so a key can
be pinned by fingerprint before anything trusts it. Signature *verification*
is delegated to ``gpgv`` with a binary (dearmored) keyring that contains only
the pinned key; gpgv rejects armored keyrings.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import os
import re
import socket
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

DEFAULT_TIMEOUT = 300.0
DEFAULT_MAX_BYTES = 600 * 1024 * 1024
DEFAULT_RETRIES = 3
USER_AGENT = "personal-dotfiles-installer"
GPGV_TIMEOUT = 60.0

_CHUNK = 1024 * 1024


class DownloadError(Exception):
    """A download could not be completed."""


class VerificationError(Exception):
    """A digest, fingerprint or signature did not match its pinned value."""


# --- fetch -------------------------------------------------------------------

class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise DownloadError(f"refusing redirect from {req.full_url} to non-https {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpsOnlyRedirect)


def _certificate_failure(exc: BaseException) -> ssl.SSLCertVerificationError | None:
    """The certificate verification error behind ``exc`` (urllib wraps it in
    a URLError), or None."""

    if isinstance(exc, ssl.SSLCertVerificationError):
        return exc
    reason = getattr(exc, "reason", None) if isinstance(exc, urllib.error.URLError) else None
    return reason if isinstance(reason, ssl.SSLCertVerificationError) else None


_PROXY_REMEDY = "behind a TLS-inspecting proxy, set SSL_CERT_FILE to its CA bundle"


def _homebrew_python(prefix: str) -> bool:
    """Whether ``prefix`` (``sys.base_prefix``, so a venv counts as its base)
    is a Homebrew python formula's: <brew>/opt/python@3.x/... or its Cellar
    path, whatever Homebrew's prefix is."""

    return "/opt/python@" in prefix or "/Cellar/python@" in prefix


def _openssl_formula() -> str | None:
    """The Homebrew formula of the OpenSSL this Python's ssl module links,
    by its major version: openssl@4 for Homebrew's python@3.14 and newer,
    openssl@3 for older ones. None for another TLS library: Apple's
    /usr/bin/python3 has LibreSSL, whose OPENSSL_VERSION_INFO always says 2."""

    if not ssl.OPENSSL_VERSION.startswith("OpenSSL"):
        return None
    return "openssl@%d" % ssl.OPENSSL_VERSION_INFO[0]


def certificate_remedy(platform: str | None = None, *, prefix: str | None = None) -> str:
    """What to do when this Python cannot verify any server certificate.

    On macOS the usual cause is a python.org build, which ships no CA
    certificates until its 'Install Certificates.command' has run, while
    Homebrew's python3 uses Homebrew's CA bundle; when Homebrew's own python3
    fails, that bundle (the cert.pem of the openssl formula it links, which
    points to the ca-certificates formula's) or a TLS-inspecting proxy is
    the cause. ``prefix`` defaults to this Python's ``sys.base_prefix``.
    """

    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        prefix = sys.base_prefix if prefix is None else prefix
        if _homebrew_python(prefix):
            formula = _openssl_formula()
            reinstall = f"ca-certificates {formula}" if formula else "ca-certificates"
            return (f"this is Homebrew's python3: run 'brew reinstall {reinstall}', "
                    f"then rerun; {_PROXY_REMEDY}")
        return ("use Homebrew's python3 ('brew install python3'; ~/.dotfiles/install "
                "runs install.py with it), or, for a python.org Python, run "
                "'Install Certificates.command' in its /Applications/Python 3.x folder; "
                f"{_PROXY_REMEDY}")
    return ("install the system CA certificates ('sudo apt-get install "
            f"ca-certificates'); {_PROXY_REMEDY}")


def fetch(
    url: str,
    dest: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    retries: int = DEFAULT_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Download ``url`` to ``dest`` atomically; https only, bounded size.

    Transient network errors and 5xx/408/429 responses are retried; other
    client errors, size-limit violations and certificate verification
    failures (every retry would fail the same way) are not.
    """

    if not url.lower().startswith("https://"):
        raise DownloadError(f"refusing non-https URL {url}")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for attempt in range(max(1, retries)):
        if attempt:
            sleep(min(2 ** attempt, 20))
        fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".part", dir=dest.parent)
        tmp = Path(tmp_name)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with os.fdopen(fd, "wb") as out, _OPENER.open(request, timeout=timeout) as resp:
                declared = resp.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise DownloadError(f"{url}: {declared} bytes exceeds limit {max_bytes}")
                total = 0
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise DownloadError(f"{url}: body exceeds limit {max_bytes}")
                    out.write(chunk)
                if declared and declared.isdigit() and total != int(declared):
                    raise http.client.IncompleteRead(b"", int(declared) - total)
            os.replace(tmp, dest)
            return dest
        except DownloadError:
            tmp.unlink(missing_ok=True)
            raise
        except urllib.error.HTTPError as exc:
            tmp.unlink(missing_ok=True)
            if exc.code < 500 and exc.code not in (408, 429):
                raise DownloadError(f"{url}: HTTP {exc.code}") from None
            last = exc
        except (urllib.error.URLError, http.client.HTTPException, socket.timeout,
                TimeoutError, ConnectionError, OSError) as exc:
            tmp.unlink(missing_ok=True)
            failure = _certificate_failure(exc)
            if failure is not None:
                detail = getattr(failure, "verify_message", None) or str(failure)
                raise DownloadError(
                    f"{url}: TLS certificate verification failed ({detail}); "
                    f"{certificate_remedy()}") from None
            last = exc
    raise DownloadError(f"{url}: failed after {max(1, retries)} attempts: {last}")


# --- digests -----------------------------------------------------------------

def file_digest(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str, *, size: int | None = None) -> str:
    expected = expected.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise VerificationError(f"malformed pinned sha256 {expected!r}")
    if size is not None:
        actual_size = Path(path).stat().st_size
        if actual_size != size:
            raise VerificationError(f"{Path(path).name}: size {actual_size} != pinned {size}")
    actual = file_digest(path, "sha256")
    if actual != expected:
        raise VerificationError(f"{Path(path).name}: sha256 {actual} != pinned {expected}")
    return actual


def verify_sri(path: Path, integrity: str) -> None:
    """Check an npm/SRI integrity string such as ``sha512-<base64>``."""

    algorithm, sep, encoded = integrity.strip().partition("-")
    if not sep or algorithm not in ("sha256", "sha384", "sha512"):
        raise VerificationError(f"unsupported integrity {integrity!r}")
    try:
        expected = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise VerificationError(f"malformed integrity {integrity!r}") from None
    digest = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    if digest.digest() != expected:
        raise VerificationError(f"{Path(path).name}: {algorithm} does not match pinned integrity")


def parse_checksum_file(text: str) -> dict[str, str]:
    """Parse ``sha256sum``-style lines into ``{filename: hexdigest}``."""

    entries: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64}|[0-9a-fA-F]{128})\s+[* ]?(.+?)\s*$", line)
        if not match:
            continue
        name = match.group(2)
        if name.startswith("./"):
            name = name[2:]
        entries[name] = match.group(1).lower()
    return entries


# --- ASCII armor -------------------------------------------------------------

def crc24(data: bytes) -> int:
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def is_armored(data: bytes) -> bool:
    return data.lstrip().startswith(b"-----BEGIN PGP ")


def dearmor(data: bytes | str) -> bytes:
    """Decode every ASCII-armored block in ``data`` and concatenate them."""

    text = data.decode("ascii") if isinstance(data, bytes) else data
    lines = [line.rstrip("\r ") for line in text.splitlines()]
    out = bytearray()
    index = 0
    blocks = 0
    while index < len(lines):
        begin = re.fullmatch(r"-----BEGIN PGP ([A-Z0-9 ,/]+)-----", lines[index])
        index += 1
        if not begin:
            continue
        label = begin.group(1)
        # Armor headers ("Key: value") end at the first blank line.
        cursor = index
        while cursor < len(lines) and re.match(r"^[A-Za-z][A-Za-z0-9-]*: ", lines[cursor]):
            cursor += 1
        if cursor < len(lines) and lines[cursor] == "":
            cursor += 1
        body: list[str] = []
        checksum: str | None = None
        end_line = f"-----END PGP {label}-----"
        while cursor < len(lines) and lines[cursor] != end_line:
            line = lines[cursor]
            if re.fullmatch(r"=[A-Za-z0-9+/]{4}", line):
                checksum = line[1:]
            elif line:
                body.append(line)
            cursor += 1
        if cursor >= len(lines):
            raise VerificationError(f"armor block {label!r} has no end line")
        try:
            decoded = base64.b64decode("".join(body), validate=True)
        except (binascii.Error, ValueError):
            raise VerificationError("armor body is not valid base64") from None
        if checksum is not None:
            expected = int.from_bytes(base64.b64decode(checksum), "big")
            if crc24(decoded) != expected:
                raise VerificationError("armor CRC24 checksum mismatch")
        out += decoded
        blocks += 1
        index = cursor + 1
    if not blocks:
        raise VerificationError("no ASCII-armored OpenPGP block found")
    return bytes(out)


# --- OpenPGP packets ---------------------------------------------------------

TAG_SIGNATURE = 2
TAG_PUBLIC_KEY = 6
TAG_PUBLIC_SUBKEY = 14


def iter_packets(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield ``(tag, body)`` for each OpenPGP packet; partial lengths rejected."""

    i = 0
    n = len(data)
    while i < n:
        ctb = data[i]
        i += 1
        if not ctb & 0x80:
            raise VerificationError(f"invalid OpenPGP packet header at offset {i - 1}")
        if ctb & 0x40:
            tag = ctb & 0x3F
            if i >= n:
                raise VerificationError("truncated packet length")
            first = data[i]
            i += 1
            if first < 192:
                length = first
            elif first < 224:
                if i >= n:
                    raise VerificationError("truncated packet length")
                length = ((first - 192) << 8) + data[i] + 192
                i += 1
            elif first == 255:
                length = int.from_bytes(data[i:i + 4], "big")
                i += 4
            else:
                raise VerificationError("partial-length packets are not supported in keys")
        else:
            tag = (ctb >> 2) & 0x0F
            length_type = ctb & 0x03
            if length_type == 3:
                raise VerificationError("indeterminate-length packets are not supported")
            width = (1, 2, 4)[length_type]
            length = int.from_bytes(data[i:i + width], "big")
            i += width
        body = data[i:i + length]
        if len(body) != length:
            raise VerificationError("truncated OpenPGP packet")
        i += length
        yield tag, body


def key_fingerprint(body: bytes) -> str:
    """Fingerprint of a public key packet body (v4: SHA1 over 0x99||len||body)."""

    if not body:
        raise VerificationError("empty key packet")
    version = body[0]
    if version != 4:
        raise VerificationError(f"unsupported OpenPGP key version {version}")
    if len(body) > 0xFFFF:
        raise VerificationError("key packet too large for a v4 fingerprint")
    return hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest().upper()


def primary_fingerprints(keyring: bytes) -> list[str]:
    return [key_fingerprint(body) for tag, body in iter_packets(keyring) if tag == TAG_PUBLIC_KEY]


def normalize_fingerprint(value: str) -> str:
    fpr = re.sub(r"\s+", "", value).upper()
    if not re.fullmatch(r"[0-9A-F]{40}", fpr):
        raise VerificationError(f"malformed pinned fingerprint {value!r}")
    return fpr


def pinned_keyring(key_data: bytes, fingerprints: Iterable[str]) -> bytes:
    """Return the binary keyring iff its primary keys are exactly the pinned set."""

    expected = {normalize_fingerprint(f) for f in fingerprints}
    if not expected:
        raise VerificationError("no pinned fingerprint")
    binary = dearmor(key_data) if is_armored(key_data) else bytes(key_data)
    found = primary_fingerprints(binary)
    if not found:
        raise VerificationError("key file contains no primary public key")
    if set(found) != expected or len(found) != len(set(found)):
        raise VerificationError(
            f"key primary fingerprint(s) {', '.join(found)} != pinned {', '.join(sorted(expected))}"
        )
    return binary


def write_keyring(path: Path, key_data: bytes, fingerprints: Iterable[str]) -> Path:
    binary = pinned_keyring(key_data, fingerprints)
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(binary)
    return path


def verify_detached(
    runner,
    keyring: Path,
    signature: Path,
    data: Path,
    fingerprints: Sequence[str],
    *,
    gpgv: str = "gpgv",
    timeout: float = GPGV_TIMEOUT,
) -> str:
    """Verify a detached signature with gpgv; return the signer's primary fpr.

    gpgv runs with an empty private ``--homedir`` so no user trustedkeys file
    participates, and the ``VALIDSIG`` status line must name a pinned primary.
    """

    expected = {normalize_fingerprint(f) for f in fingerprints}
    with tempfile.TemporaryDirectory(prefix="gpgv-home-") as home:
        os.chmod(home, 0o700)
        argv = [gpgv, "--homedir", home, "--status-fd", "1",
                "--keyring", str(Path(keyring).resolve()), str(signature), str(data)]
        try:
            completed = runner.run(argv, timeout=timeout, check=False, read_only=True)
        except Exception as exc:  # RunnerError or a missing binary
            raise VerificationError(f"gpgv could not run: {exc}") from None
    status = (completed.stdout or b"").decode("utf-8", "replace")
    if completed.returncode != 0:
        tail = (completed.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        raise VerificationError(f"gpgv rejected {Path(signature).name}: {tail or completed.returncode}")
    good = False
    signer: str | None = None
    for line in status.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "[GNUPG:]":
            if parts[1] in ("BADSIG", "ERRSIG", "EXPKEYSIG", "REVKEYSIG", "NO_PUBKEY"):
                raise VerificationError(f"gpgv status {parts[1]} for {Path(signature).name}")
            if parts[1] == "GOODSIG":
                good = True
            if parts[1] == "VALIDSIG" and len(parts) >= 3:
                signer = parts[-1].upper()
    if not good or signer is None:
        raise VerificationError(f"gpgv reported no valid signature for {Path(signature).name}")
    if signer not in expected:
        raise VerificationError(f"signature primary key {signer} is not pinned")
    return signer


__all__ = [
    "DEFAULT_MAX_BYTES", "DownloadError", "VerificationError", "certificate_remedy",
    "crc24", "dearmor", "fetch", "file_digest", "is_armored", "iter_packets",
    "key_fingerprint",
    "normalize_fingerprint", "parse_checksum_file", "pinned_keyring",
    "primary_fingerprints", "verify_detached", "verify_sha256", "verify_sri",
    "write_keyring",
]
