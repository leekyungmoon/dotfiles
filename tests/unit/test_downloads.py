"""Unit tests for installer.downloads: digests, armor, fingerprints, gpgv, fetch."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from installer import downloads as dl  # noqa: E402
from installer.downloads import DownloadError, VerificationError  # noqa: E402
from installer.runner import Runner  # noqa: E402


def key_packet(seed: bytes = b"k", *, version: int = 4, old_format: bool = False) -> tuple[bytes, bytes]:
    """A syntactically framed public-key packet; returns (packet, body)."""

    body = bytes([version]) + b"\x5f\x00\x00\x00" + b"\x16" + hashlib.sha256(seed).digest()
    if old_format:
        return bytes([0x80 | (6 << 2) | 0, len(body)]) + body, body
    return bytes([0xC0 | 6, len(body)]) + body, body


def armor(data: bytes, label: str = "PUBLIC KEY BLOCK", *, headers: str = "",
          crc: bool = True) -> str:
    b64 = base64.b64encode(data).decode()
    lines = [f"-----BEGIN PGP {label}-----"]
    if headers:
        lines.append(headers)
    lines.append("")
    lines += [b64[i:i + 64] for i in range(0, len(b64), 64)]
    if crc:
        lines.append("=" + base64.b64encode(dl.crc24(data).to_bytes(3, "big")).decode())
    lines.append(f"-----END PGP {label}-----")
    return "\n".join(lines) + "\n"


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.file = self.tmp / "f"
        self.file.write_bytes(b"hello")

    def test_sha256_match_and_mismatch(self):
        good = hashlib.sha256(b"hello").hexdigest()
        self.assertEqual(dl.verify_sha256(self.file, good.upper(), size=5), good)
        with self.assertRaises(VerificationError):
            dl.verify_sha256(self.file, "0" * 64)
        with self.assertRaises(VerificationError):
            dl.verify_sha256(self.file, good, size=6)
        with self.assertRaises(VerificationError):
            dl.verify_sha256(self.file, "not-a-digest")

    def test_sri(self):
        sri = "sha512-" + base64.b64encode(hashlib.sha512(b"hello").digest()).decode()
        dl.verify_sri(self.file, sri)
        with self.assertRaises(VerificationError):
            dl.verify_sri(self.file, "sha512-" + base64.b64encode(b"x" * 64).decode())
        with self.assertRaises(VerificationError):
            dl.verify_sri(self.file, "md5-abc")

    def test_checksum_file(self):
        text = ("a" * 64 + "  node.tar.xz\n" + "B" * 64 + " *./other.tgz\n"
                "garbage line\n")
        self.assertEqual(dl.parse_checksum_file(text),
                         {"node.tar.xz": "a" * 64, "other.tgz": "b" * 64})


class ArmorAndFingerprintTests(unittest.TestCase):
    def test_crc24_initial_value(self):
        self.assertEqual(dl.crc24(b""), 0xB704CE)

    def test_dearmor_roundtrip_with_headers(self):
        data, _ = key_packet()
        text = armor(data, headers="Comment: fixture\nVersion: test")
        self.assertTrue(dl.is_armored(text.encode()))
        self.assertEqual(dl.dearmor(text), data)

    def test_dearmor_concatenates_blocks_and_accepts_missing_crc(self):
        a, _ = key_packet(b"a")
        b, _ = key_packet(b"b")
        self.assertEqual(dl.dearmor(armor(a) + armor(b, crc=False)), a + b)

    def test_dearmor_rejects_bad_crc_and_garbage(self):
        data, _ = key_packet()
        lines = armor(data).splitlines()
        crc_index = next(i for i, line in enumerate(lines) if line.startswith("=") and len(line) == 5)
        lines[crc_index] = "=AAAA"
        with self.assertRaises(VerificationError):
            dl.dearmor("\n".join(lines))
        with self.assertRaises(VerificationError):
            dl.dearmor("no armor here")
        with self.assertRaises(VerificationError):
            dl.dearmor("-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nAAAA\n")

    def test_v4_fingerprint_framing(self):
        packet, body = key_packet()
        expected = hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest().upper()
        self.assertEqual(dl.primary_fingerprints(packet), [expected])
        old, _ = key_packet(old_format=True)
        self.assertEqual(dl.primary_fingerprints(old), [expected])

    def test_subkeys_are_not_primaries_and_v5_rejected(self):
        packet, body = key_packet(b"p")
        sub_body = key_packet(b"s")[1]
        subkey = bytes([0xC0 | 14, len(sub_body)]) + sub_body
        self.assertEqual(len(dl.primary_fingerprints(packet + subkey)), 1)
        v5, _ = key_packet(version=5)
        with self.assertRaises(VerificationError):
            dl.primary_fingerprints(v5)
        with self.assertRaises(VerificationError):
            dl.primary_fingerprints(packet[:-3])

    def test_pinned_keyring_requires_exact_primary_set(self):
        packet, body = key_packet()
        fpr = dl.key_fingerprint(body)
        self.assertEqual(dl.pinned_keyring(armor(packet).encode(), [fpr]), packet)
        spaced = " ".join(fpr[i:i + 4] for i in range(0, 40, 4))
        self.assertEqual(dl.pinned_keyring(packet, [spaced.lower()]), packet)
        with self.assertRaises(VerificationError):
            dl.pinned_keyring(packet, ["0" * 40])
        other, _ = key_packet(b"other")
        with self.assertRaises(VerificationError):
            dl.pinned_keyring(packet + other, [fpr])


class _Resp(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _Opener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def open(self, request, timeout):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Resp(outcome)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.saved = dl._OPENER
        self.addCleanup(setattr, dl, "_OPENER", self.saved)

    def test_rejects_non_https(self):
        with self.assertRaises(DownloadError):
            dl.fetch("http://example.invalid/x", self.tmp / "x")

    def test_retries_transient_errors_then_succeeds(self):
        dl._OPENER = _Opener([urllib.error.URLError("down"), b"payload"])
        dl.fetch("https://example.invalid/x", self.tmp / "x", sleep=lambda s: None)
        self.assertEqual((self.tmp / "x").read_bytes(), b"payload")
        self.assertEqual(dl._OPENER.calls, 2)
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["x"])

    def test_client_error_is_not_retried(self):
        err = urllib.error.HTTPError("https://example.invalid/x", 404, "nf", {}, None)
        dl._OPENER = _Opener([err, b"never"])
        with self.assertRaises(DownloadError):
            dl.fetch("https://example.invalid/x", self.tmp / "x", sleep=lambda s: None)
        self.assertEqual(dl._OPENER.calls, 1)
        self.assertFalse((self.tmp / "x").exists())

    def test_size_limit(self):
        dl._OPENER = _Opener([b"x" * 100])
        with self.assertRaises(DownloadError):
            dl.fetch("https://example.invalid/x", self.tmp / "x", max_bytes=10)
        self.assertEqual(list(self.tmp.iterdir()), [])


@unittest.skipUnless(shutil.which("gpg") and shutil.which("gpgv") and shutil.which("gpgconf"),
                     "gpg/gpgv not available")
class RealGpgTests(unittest.TestCase):
    """Real OpenPGP material from a throwaway GNUPGHOME; never the user's."""

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix="pdg"))
        os.chmod(cls.home, 0o700)
        cls.env = {"GNUPGHOME": str(cls.home), "PATH": "/usr/bin:/bin", "HOME": str(cls.home)}

        def gpg(*args, data=None):
            return subprocess.run(["gpg", "--homedir", str(cls.home), "--batch", "--quiet",
                                   "--pinentry-mode", "loopback", "--passphrase", "", *args],
                                  input=data, capture_output=True, check=True, env=cls.env,
                                  timeout=120).stdout

        cls.gpg = staticmethod(gpg)
        gpg("--quick-gen-key", "Fixture Signer <fixture@example.invalid>", "ed25519", "sign", "never")
        listing = gpg("--with-colons", "--list-keys").decode()
        cls.fpr = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
        cls.armored = gpg("--armor", "--export")
        cls.data = cls.home / "SHASUMS256.txt"
        cls.data.write_bytes(b"a" * 64 + b"  file.tar.xz\n")
        cls.sig = cls.home / "SHASUMS256.txt.sig"
        gpg("--output", str(cls.sig), "--detach-sign", str(cls.data))

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["gpgconf", "--homedir", str(cls.home), "--kill", "all"],
                       capture_output=True, env=cls.env, timeout=30)
        shutil.rmtree(cls.home, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_stdlib_fingerprint_matches_gpg(self):
        self.assertEqual(dl.primary_fingerprints(dl.dearmor(self.armored)), [self.fpr])

    def test_gpgv_accepts_good_signature_with_dearmored_keyring(self):
        keyring = dl.write_keyring(self.tmp / "k.gpg", self.armored, [self.fpr])
        signer = dl.verify_detached(Runner(), keyring, self.sig, self.data, [self.fpr])
        self.assertEqual(signer, self.fpr)

    def test_tampered_data_is_rejected(self):
        keyring = dl.write_keyring(self.tmp / "k.gpg", self.armored, [self.fpr])
        tampered = self.tmp / "SHASUMS256.txt"
        tampered.write_bytes(b"b" * 64 + b"  file.tar.xz\n")
        with self.assertRaises(VerificationError):
            dl.verify_detached(Runner(), keyring, self.sig, tampered, [self.fpr])

    def test_valid_signature_from_unpinned_primary_is_rejected(self):
        keyring = dl.write_keyring(self.tmp / "k.gpg", self.armored, [self.fpr])
        with self.assertRaises(VerificationError):
            dl.verify_detached(Runner(), keyring, self.sig, self.data, ["1" * 40])

    def test_armored_keyring_is_rejected_by_gpgv(self):
        armored = self.tmp / "k.asc"
        armored.write_bytes(self.armored)
        with self.assertRaises(VerificationError):
            dl.verify_detached(Runner(), armored, self.sig, self.data, [self.fpr])


if __name__ == "__main__":
    unittest.main()
