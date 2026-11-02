import unittest

from audit_scene import RATE, audit, union_overlap


class SceneAuditTests(unittest.TestCase):
    def test_union_does_not_double_count(self):
        self.assertEqual(union_overlap((0, 10), [(0, 7), (4, 9), (20, 30)]), 9)

    def test_tracks_unassigned_and_wrong_speaker_spans(self):
        plan = {"rows": [
            {"speaker": "a", "source_start": 0, "source_end": 1},
            {"speaker": "b", "source_start": 1, "source_end": 2},
        ]}
        events = [
            {"ev": "commit.source", "unit": "u1", "span": [0, RATE]},
            {"ev": "commit.source", "unit": "u2", "span": [RATE, RATE * 2]},
            {"ev": "play.start", "unit": "u1"},
            {"ev": "spk.assign", "spk": "s1", "span": [0, RATE * 2]},
        ]
        result = audit(plan, events, 0)
        self.assertEqual(result["gold_span_covered_by_played_commits"], 0.5)
        self.assertEqual(result["gold_span_assigned"], 1.0)
        self.assertEqual(result["gold_span_assigned_to_best_clip_fitted_actor"], 0.5)


if __name__ == "__main__":
    unittest.main()
