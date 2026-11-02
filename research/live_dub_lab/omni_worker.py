"""Experimental worker: explicit lower-step OmniVoice, using the normal IPC protocol.

Select with a2a-pipeline --clone-worker .../omni_worker.py --clone-model omnivoice.
A2A_LAB_OMNI_STEPS selects 8, 16, or 32 (default 8). Product defaults are unchanged.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tts_worker
from omni_steps import StepAdapter


class ExperimentalBackend(tts_worker.ModelBackend):
    def load(self):
        if self.model_key != "omnivoice":
            raise ValueError("this experimental worker requires --model omnivoice")
        steps = int(os.environ.get("A2A_LAB_OMNI_STEPS", "8"))
        if steps not in (8, 16, 32):
            raise ValueError("A2A_LAB_OMNI_STEPS must be 8, 16, or 32")
        super().load()
        self.adapter = StepAdapter(self.adapter, steps)
        self.name += f"#steps{steps}"


if __name__ == "__main__":
    tts_worker.ModelBackend = ExperimentalBackend
    tts_worker.main()
