import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_metrics import summarize


class RunMetricsTests(unittest.TestCase):
    def test_interrupted_playback_does_not_count_as_completed_coverage(self):
        events = [
            {"ev": "commit.source", "unit": "u1", "span": [0, 48000], "reason": "finalized"},
            {"ev": "commit.source", "unit": "u2", "span": [48000, 96000], "reason": "finalized"},
            {"ev": "play.start", "unit": "u1", "out_sample": 48000},
            {"ev": "play.start", "unit": "u2", "out_sample": 96000},
            {"ev": "play.end", "unit": "u2", "out_sample": 144000},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "trace.jsonl").write_text("\n".join(map(json.dumps, events)))
            result = summarize(tmp)
        self.assertEqual(result["played"], 2)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["coverage_by_source_seconds"], 1)
        self.assertEqual(result["completed_coverage_by_source_seconds"], 0.5)


if __name__ == "__main__":
    unittest.main()
