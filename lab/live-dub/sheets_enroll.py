"""Contact sheets for enrolled actors: frames at evenly spread member lines (see frames.py caveats).

usage: python sheets_enroll.py --series NAME [--actors A01,A02] [--per 8]
"""
import argparse
import json

from common import ACTORS
from frames import sheet

ap = argparse.ArgumentParser()
ap.add_argument("--series", required=True)
ap.add_argument("--actors", default="")
ap.add_argument("--per", type=int, default=8)
a = ap.parse_args()
rep = json.load(open(ACTORS / a.series / "enrollment.json"))
want = set(a.actors.split(",")) if a.actors else None
for x in rep:
    if want and x["id"] not in want:
        continue
    m = sorted(x["members"], key=lambda r: (r["episode"], r["start"]))
    step = max(1, len(m) // a.per)
    pick = m[::step][: a.per]
    ep = pick[0]["episode"]
    path = ACTORS / a.series / "sheets" / f"{x['id']}.jpg"
    path.parent.mkdir(exist_ok=True)
    sheet(ep, [r for r in pick if r["episode"] == ep], f"{a.series} {x['id']}: {x['lines']} lines, {x['seconds']:.0f} s, cohesion {x['cohesion']}").save(path, quality=80)
    print(path)
