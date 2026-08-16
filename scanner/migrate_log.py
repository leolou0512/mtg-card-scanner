"""Repair collection_log.csv and bring it to the current schema.

Two writers disagreed early on: scan.py wrote 9 columns, catalogue.py wrote 11,
and the header on disk was the 9-column one. This rewrites every row to the
current 12-column schema, filling in full set names.
"""
import csv
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG = os.path.join(ROOT, "collection_log.csv")

sys.path.insert(0, HERE)
from cardlookup import CardIndex  # noqa: E402

SCHEMA = ["date", "action", "name", "set", "set_name", "number", "rarity",
          "language", "foil", "qty", "source", "note"]

OLD9 = ["date", "action", "name", "set", "number", "foil", "qty", "source", "note"]
OLD11 = ["date", "action", "name", "set", "number", "rarity", "language",
         "foil", "qty", "source", "note"]


def to_dict(row):
    """Map a row of unknown vintage onto named fields."""
    if len(row) == len(SCHEMA):
        return dict(zip(SCHEMA, row))
    if len(row) == len(OLD11):
        return dict(zip(OLD11, row))
    if len(row) == len(OLD9):
        return dict(zip(OLD9, row))
    # unknown shape: keep what we can
    d = dict(zip(SCHEMA, row))
    return d


def main():
    if not os.path.exists(LOG):
        print("nothing to migrate")
        return 0

    with open(LOG, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        print("empty log")
        return 0

    header = rows[0]
    body = rows[1:] if header and header[0].lower() == "date" else rows

    idx = CardIndex()
    out = []
    shapes = {}
    for row in body:
        if not any(cell.strip() for cell in row):
            continue
        shapes[len(row)] = shapes.get(len(row), 0) + 1
        d = to_dict(row)
        code = (d.get("set") or "").strip()
        d["set_name"] = idx.set_name(code) if code else ""
        out.append([d.get(k, "") for k in SCHEMA])

    backup = LOG + ".bak"
    shutil.copy2(LOG, backup)

    with open(LOG, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(SCHEMA)
        w.writerows(out)

    print(f"  original row shapes: {shapes}")
    print(f"  backup written     : {backup}")
    print(f"  rewrote {len(out)} row(s) to {len(SCHEMA)} columns")
    print()
    for r in out:
        print("   ", r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
