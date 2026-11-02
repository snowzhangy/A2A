"""Experimental Qwen worker: prepare conditioning without synthesizing a throwaway line.

The direct-conditioning implementation has been adopted by the normal worker. This
entry point retains the experimental model label for reproducibility of earlier runs.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tts_worker


class ConditioningBackend(tts_worker.ModelBackend):
    def load(self):
        if self.model_key != "qwen":
            raise ValueError("this experimental worker requires --model qwen")
        super().load()
        self.name += "#conditioning-only"

if __name__ == "__main__":
    tts_worker.ModelBackend = ConditioningBackend
    tts_worker.main()
