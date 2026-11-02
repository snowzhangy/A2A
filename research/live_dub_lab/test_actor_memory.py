import math
import tempfile
import unittest
from pathlib import Path

from actor_memory import ActorMemory


class ActorMemoryTests(unittest.TestCase):
    def test_identity_grouping_and_model_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ActorMemory(Path(tmp) / "db", "model-v1")
            db.enroll("alice", "first", [1, 0])
            db.enroll("alice", "second", [0.9, 0.1])
            db.enroll("bob", "first", [0, 1])
            ranked = db.rank([0.9, 0.1])
            self.assertEqual([a for a, _ in ranked], ["alice", "bob"])
            self.assertAlmostEqual(ranked[0][1], 1.0)
            self.assertEqual([a for a, _ in db.rank([1, 0], excluded_actor="alice")], ["bob"])
            db.close()
            other = ActorMemory(Path(tmp) / "db", "model-v2")
            self.assertEqual(other.rank([1, 0]), [])
            other.close()

    def test_rejects_invalid_vectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ActorMemory(Path(tmp) / "db", "model-v1")
            for vector in ([], [0, 0], [math.nan, 1]):
                with self.assertRaises(ValueError):
                    db.enroll("actor", "sample", vector)
            db.close()


if __name__ == "__main__":
    unittest.main()
