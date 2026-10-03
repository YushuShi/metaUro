"""The dependency installer must reject untrusted bytes before executing them."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from automation.verify_poppler import sample_pdf, verify

ROOT = Path(__file__).resolve().parents[1]


class PopplerRuntimeTests(unittest.TestCase):
    def test_bad_archive_rejected_without_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root/'bad.tar.xz'; archive.write_bytes(b'not a verified archive')
            destination = root/'installed'
            result = subprocess.run(['bash', str(ROOT/'automation/install_poppler.sh'),
                                     str(destination), str(archive)], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('source checksum mismatch', result.stderr)
            self.assertFalse(destination.exists())

    def test_wrong_tool_version_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('pdfinfo', 'pdftotext', 'pdfdetach'):
                executable = root/name
                executable.write_text(f'#!/bin/sh\necho "{name} version 24.02.0" >&2\n')
                executable.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, 'must be exactly 26.07.0'):
                verify(root)

    def test_fixture_is_deterministic_and_has_two_pages(self):
        fixture = sample_pdf()
        self.assertEqual(fixture, sample_pdf())
        self.assertIn(b'/Count 2', fixture)
        self.assertTrue(fixture.endswith(b'%%EOF\n'))


if __name__ == '__main__':
    unittest.main()
