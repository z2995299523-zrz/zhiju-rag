import unittest
from unittest.mock import patch

from ragcore import ingest_worker


class UploadWorkerTests(unittest.TestCase):
    def test_real_worker_extracts_and_propagates_file_error(self):
        entries = ingest_worker.parse_upload("预算.csv", "项目,金额\nA,12".encode())
        self.assertEqual(entries[-1], {"locator": "第 2 行", "text": "项目：A ｜ 金额：12"})
        with self.assertRaisesRegex(ValueError, "JSONL 第 1 行"):
            ingest_worker.parse_upload("bad.jsonl", b"bad")

    def test_timeout_kills_worker_and_releases_slot(self):
        processes = []
        popen = ingest_worker.subprocess.Popen

        def capture(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process

        with patch.object(ingest_worker, "WALL_TIMEOUT_SECONDS", 0.0001):
            with patch.object(ingest_worker.subprocess, "Popen", side_effect=capture):
                with self.assertRaisesRegex(ValueError, "超时"):
                    ingest_worker.parse_upload("a.txt", b"hello")
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(ingest_worker._SLOTS.acquire(blocking=False))
        ingest_worker._SLOTS.release()

    def test_parallelism_and_preflight(self):
        slots = ingest_worker._SLOTS
        with slots, slots:
            with self.assertRaises(ingest_worker.UploadBusyError):
                ingest_worker.parse_upload("a.txt", b"hello")
        with patch.object(ingest_worker, "_run_worker") as worker:
            with self.assertRaises(ValueError):
                ingest_worker.parse_upload("invalid.exe", b"hello")
            with self.assertRaises(ValueError):
                ingest_worker.parse_upload("empty.txt", b"")
            worker.assert_not_called()
