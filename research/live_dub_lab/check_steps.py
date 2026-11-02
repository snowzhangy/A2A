"""Local back-transcription of matched samples. ASR agreement is not listener quality."""
import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def words(s):
    return re.findall(r"[a-z0-9]+", s.lower().replace("that's", "that is").replace("’", "'"))


def edits(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        row = [i]
        for j, y in enumerate(b, 1):
            row.append(min(row[-1] + 1, prev[j] + 1, prev[j - 1] + (x != y)))
        prev = row
    return prev[-1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--run", type=Path, required=True)
    a = p.parse_args()
    for path in (a.plan, a.run):
        path.resolve().relative_to((ROOT / ".local").resolve())
    plan = {r["id"]: r for r in json.loads(a.plan.read_text())["rows"]}
    records = json.loads((a.run / "results.json").read_text())["rows"]
    rows = []
    for r in records:
        file = a.run / r["file"]
        file.resolve().relative_to(a.run.resolve())
        result = subprocess.run([str(ROOT / "apple/.build/debug/a2a-transcribe"),
            "--input", str(file), "--locale", "en-US"], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr)
        data = json.loads(result.stdout)
        file.with_suffix(".asr.json").write_text(json.dumps(data, indent=2) + "\n")
        expected, heard = plan[r["id"]]["text"], data["text"]
        ref, hyp = words(expected), words(heard)
        rows.append(dict(id=r["id"], steps=r["steps"], expected=expected, heard=heard,
                         edits=edits(ref, hyp), ref_words=len(ref)))
        print(json.dumps({k: rows[-1][k] for k in ("id", "steps", "edits", "ref_words")}), flush=True)
    (a.run / "backcheck.json").write_text(json.dumps(rows, indent=2) + "\n")


if __name__ == "__main__":
    main()
