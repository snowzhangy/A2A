"""Stand-in for hy-worker in the A2AModels protocol tests (standard library only; no model).

Speaks the worker's frames (see hy_worker.cpp): `ready` at start; `append` buffers samples (a span may
come in several appends); `flush` answers `done` with "<seconds> s <direction>" for everything appended
since the previous flush or discard, or "" when every appended sample is zero (silence); `discard`
forgets the open span. `--die-on-flush` exits instead of answering; `--slow S` waits S seconds before `done`; `--protocol-1`
announces a worker that predates streamed spans.
"""
import json
import struct
import sys
import time

out = sys.stdout.buffer


def send(header):
    h = json.dumps(header).encode()
    out.write(struct.pack("<II", len(h), 0) + h)
    out.flush()


def read_exact(n):
    data = b""
    while len(data) < n:
        chunk = sys.stdin.buffer.read(n - len(data))
        if not chunk:
            sys.exit(0)
        data += chunk
    return data


slow = float(sys.argv[sys.argv.index("--slow") + 1]) if "--slow" in sys.argv else 0.0
send({"ev": "ready", "model": "fake-hy", "load_s": 0.0, "warmup_s": 0.0,
      **({} if "--protocol-1" in sys.argv else {"protocol": 2})})
seconds, loud, direction = 0.0, False, "zh-en"
while True:
    hl, pl = struct.unpack("<II", read_exact(8))
    req = json.loads(read_exact(hl))
    payload = read_exact(pl) if pl else b""
    if req["op"] == "append":
        direction = req.get("direction", direction)
        samples = struct.unpack(f"<{pl // 4}f", payload)
        seconds += len(samples) / req.get("rate", 16000)
        loud |= any(abs(v) > 0 for v in samples)
    elif req["op"] == "flush":
        if "--die-on-flush" in sys.argv:
            sys.exit(3)
        time.sleep(slow)
        send({"ev": "done", "id": req["id"], "text": f"{seconds:.1f} s {direction}" if loud else "", "turns": 1,
              "st": False, "flush_ms": int(slow * 1000)})
        seconds, loud = 0.0, False
    elif req["op"] in ("reset", "discard"):
        seconds, loud = 0.0, False
