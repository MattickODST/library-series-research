import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
sys.path.insert(0, str(SRC))

import batch


class RetryQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = batch.DB_PATH
        batch.DB_PATH = Path(self.tmp.name) / "test.sqlite"

    def tearDown(self):
        batch.DB_PATH = self.original_db_path
        self.tmp.cleanup()

    def seed(self, statuses):
        db = batch.connect()

        for row_number, status in enumerate(statuses, start=2):
            db.execute(
                """
                INSERT INTO jobs
                (
                    row_number,
                    raw_title,
                    raw_author,
                    publication_year,
                    status,
                    attempts,
                    result_json,
                    last_error,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, NULL, NULL, ?)
                """,
                (
                    row_number,
                    f"Book {row_number}",
                    "Test Author",
                    "2026",
                    status,
                    batch.now_iso(),
                ),
            )

        db.commit()
        db.close()

    def statuses(self):
        db = batch.connect()
        result = dict(
            db.execute(
                "SELECT row_number, status FROM jobs ORDER BY row_number"
            ).fetchall()
        )
        db.close()
        return result

    def test_retry_requeues_only_unresolved_statuses(self):
        self.seed(
            [
                "VERIFIED",
                "NOT_SERIES",
                "LIKELY_SERIES",
                "LIKELY_NOT_SERIES",
                "CONFLICT",
                "UNFOUND",
                "TIMED_OUT",
                "ERROR",
            ]
        )

        batch.retry_unresolved()

        result = self.statuses()

        self.assertEqual(result[2], "VERIFIED")
        self.assertEqual(result[3], "NOT_SERIES")
        self.assertEqual(result[4], "LIKELY_SERIES")
        self.assertEqual(result[5], "LIKELY_NOT_SERIES")

        self.assertEqual(result[6], "PENDING")
        self.assertEqual(result[7], "PENDING")
        self.assertEqual(result[8], "PENDING")
        self.assertEqual(result[9], "PENDING")

    def test_retry_refuses_active_worker_state(self):
        self.seed(["IN_PROGRESS", "ERROR"])

        with self.assertRaises(RuntimeError):
            batch.retry_unresolved()

        result = self.statuses()

        self.assertEqual(result[2], "IN_PROGRESS")
        self.assertEqual(result[3], "ERROR")


if __name__ == "__main__":
    unittest.main()
