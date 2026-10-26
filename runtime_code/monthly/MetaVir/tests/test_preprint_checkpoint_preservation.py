"""Private source and uncertain-bill checkpoint protection; no live services."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from automation import monthly_release as release


class PreprintCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = (Path(self.directory.name) / "repo").resolve()
        self.root.mkdir()
        self.patched = patch.object(release, "ROOT", self.root)
        self.patched.start()
        self.config = {"repository": "fixture", "bootstrap": {"last_successful_publication_date": "2025-12-31"}}
        self.run_id = "2026-01-01_2026-01-31"
        self.write("automation/monthly_config.json", self.config)
        self.write(release.ACTIVE_PATH, {"repository": "fixture", "run_id": self.run_id,
                   "start_date": "2026-01-01", "end_date": "2026-01-31"})
        self.write("data/monthly_updates/" + self.run_id + "/" + release.MANIFEST_FILENAME,
                   {"repository": "fixture", "run_id": self.run_id, "config_sha256": release.digest_json(self.config)})
        self.private = self.root / ".monthly-checkpoint/preprint_recovery" / self.run_id
        self.private.mkdir(parents=True)
        (self.private / "extensionless_blob").write_bytes(b"exact raw source bytes")
        (self.private / "unknown_dispatch.json").write_text('{"request_sha256":"abc","response_received":false}')
        (self.private / "prompt.txt").write_text("private prompt")
        self.queue = self.root / "manualReview/preprint_recovery/monthly" / (self.run_id + "_holds.csv")
        self.queue.parent.mkdir(parents=True)
        self.queue.write_text("pmid,reason\n1,unknown billed request\n")

    def tearDown(self):
        self.patched.stop()
        self.directory.cleanup()

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def artifact(self):
        release.checkpoint()
        target = Path(self.directory.name) / "artifact"
        shutil.copytree(self.root / ".monthly-checkpoint", target / ".monthly-checkpoint")
        return target

    def test_cleanup_upload_shape_and_restore_preserve_all_private_bytes(self):
        (self.root / ".monthly-checkpoint/obsolete_native.json").write_text("old checkpoint")
        original = {str(p.relative_to(self.private)): p.read_bytes() for p in self.private.iterdir()}
        artifact = self.artifact()
        self.assertFalse((self.root / ".monthly-checkpoint/obsolete_native.json").exists())
        self.assertEqual({str(p.relative_to(self.private)): p.read_bytes() for p in self.private.iterdir()}, original)
        self.assertTrue((artifact / ".monthly-checkpoint" / self.queue.relative_to(self.root)).is_file())
        shutil.rmtree(self.root / ".monthly-checkpoint")
        self.queue.unlink()
        self.assertGreater(release.restore_checkpoint(artifact, self.config), 0)
        self.assertEqual({str(p.relative_to(self.private)): p.read_bytes() for p in self.private.iterdir()}, original)
        self.assertIn("unknown billed request", self.queue.read_text())

    def test_conflicting_unknown_ledger_aborts_without_overwrite_or_partial_copy(self):
        artifact = self.artifact()
        shutil.rmtree(self.root / ".monthly-checkpoint")
        self.private.mkdir(parents=True)
        ledger = self.private / "unknown_dispatch.json"
        ledger.write_text("existing uncertain bill")
        with self.assertRaisesRegex(RuntimeError, "Private recovery bytes conflict"):
            release.restore_checkpoint(artifact, self.config)
        self.assertEqual(ledger.read_text(), "existing uncertain bill")
        self.assertFalse((self.private / "extensionless_blob").exists())

    def test_completed_window_never_restores_old_private_calls(self):
        artifact = self.artifact()
        shutil.rmtree(self.root / ".monthly-checkpoint")
        self.write(release.STATE_PATH, {"repository": "fixture", "status": "completed",
                                       "last_successful_publication_date": "2026-01-31"})
        self.assertEqual(release.restore_checkpoint(artifact, self.config), 0)
        self.assertFalse(self.private.exists())


if __name__ == '__main__':
    unittest.main()
