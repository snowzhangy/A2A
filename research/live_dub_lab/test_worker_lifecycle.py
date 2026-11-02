import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class WorkerLifecycleTests(unittest.TestCase):
    def test_parent_loss_during_startup_does_not_leave_worker_resident(self):
        worker = Path(__file__).with_name("context_worker.py").resolve()
        with tempfile.TemporaryDirectory() as tmp:
            gate = Path(tmp) / "not-created"
            script = """import subprocess,sys,time
p=subprocess.Popen([sys.executable,sys.argv[1],'--fake','--fake-startup-gate',sys.argv[2]],
    stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print(p.pid,flush=True)
time.sleep(1)
"""
            parent = subprocess.run([sys.executable, "-c", script, str(worker), str(gate)],
                                    capture_output=True, text=True, timeout=10, check=True)
            pid = int(parent.stdout.strip())
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return
                    time.sleep(0.05)
                self.fail("worker survived parent loss during gated startup")
            finally:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
