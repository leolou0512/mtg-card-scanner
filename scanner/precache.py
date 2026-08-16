"""Warm the Scryfall image cache ahead of scanning.

Waiting ~800 ms for each card image is only annoying because it happens one at
a time while you work. This fetches them in bulk beforehand.

Scope matters: there are 111,862 printings in the card database, which is over
10 GB. The useful subset is far smaller - every printing of every card you have
actually traded or logged, which is what --mine selects.

    python precache.py --mine                 cards from your history (default)
    python precache.py --mine --version small only picker thumbnails
    python precache.py --sets MH2,LTR,EOE     specific sets
    python precache.py --all                  everything (10+ GB, hours)
    python precache.py --status               what is cached already

Safe to stop and re-run: anything already on disk is skipped.
"""
import argparse
import csv
import os
import re
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import scryfall  # noqa: E402
from cardlookup import CardIndex  # noqa: E402

LOG = os.path.join(ROOT, "collection_log.csv")
DATA = os.path.join(ROOT, "data")


def traded_names(index):
    """Card names from the Cardmarket exports and the collection log."""
    names = set()

    if os.path.isdir(DATA):
        for fn in os.listdir(DATA):
            if not fn.endswith("_articles.csv"):
                continue
            with open(os.path.join(DATA, fn), encoding="utf-8") as fh:
                fh.readline()
                for line in fh:
                    parts = line.rstrip("\n").split(";")
                    if len(parts) > 2 and parts[2]:
                        names.add(re.sub(r"\s*\(V\.\d+\)\s*$", "", parts[2]).strip())

    if os.path.exists(LOG):
        with open(LOG, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("name"):
                    names.add(row["name"].strip())
    return names


def printings_for_names(index, names):
    """Every printing of every card whose name we recognise."""
    from cardlookup import normalise

    wanted = []
    seen_cards = 0
    lookup = {}
    for i, c in enumerate(index.cards):
        lookup.setdefault(c["norm"], i)

    missing = []
    for n in names:
        i = lookup.get(normalise(n))
        if i is None:
            hits = index.lookup(n, limit=1)
            if hits and hits[0][1] >= 0.92:
                i = index.cards.index(hits[0][0])
        if i is None:
            missing.append(n)
            continue
        seen_cards += 1
        for code, num, _r in index.cards[i]["printings"]:
            wanted.append((code, num))
    return wanted, seen_cards, missing


def printings_for_sets(index, codes):
    codes = {c.strip().upper() for c in codes if c.strip()}
    out = []
    for c in index.cards:
        for code, num, _r in c["printings"]:
            if code.upper() in codes:
                out.append((code, num))
    return out


def all_printings(index):
    return [(code, num) for c in index.cards for code, num, _r in c["printings"]]


def run(jobs, versions, limit=None, quiet=False, workers=8):
    """Fetch missing images concurrently.

    A single-threaded loop only manages about 1.2 images a second, because each
    request spends most of its time waiting on the network rather than being
    rate-limited. Several workers share the global 10-per-second throttle in
    scryfall, so the limit is still respected while the waiting overlaps.
    """
    import queue as _queue
    import threading

    # one directory listing beats tens of thousands of stat calls
    have = scryfall.cached_names()
    todo = []
    for code, num in jobs:
        for v in versions:
            key = f"{scryfall._key(code, num)}_{v}"
            if key not in have:
                todo.append((code, num, v))
    if limit:
        todo = todo[:limit]

    total = len(todo)
    if not total:
        print("  everything requested is already cached")
        return 0, 0

    print(f"  {total} image(s) to fetch  -  roughly "
          f"{total/10/60:.0f} min with {workers} workers")
    print("  (safe to stop with ctrl-c; progress is kept)")
    print()

    q = _queue.Queue()
    for item in todo:
        q.put(item)

    counts = {"ok": 0, "fail": 0, "done": 0}
    lock = threading.Lock()
    stop = threading.Event()
    t0 = time.time()

    def worker():
        while not stop.is_set():
            try:
                code, num, v = q.get_nowait()
            except _queue.Empty:
                return
            try:
                got = scryfall.image_bytes(code, num, v)
            except Exception:
                got = None
            with lock:
                counts["ok" if got else "fail"] += 1
                counts["done"] += 1
                i = counts["done"]
            if not quiet and (i % 100 == 0 or i == total):
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed else 0
                left = (total - i) / rate if rate else 0
                print(f"  {i:6}/{total}  ok {counts['ok']:6}  "
                      f"missing {counts['fail']:4}  {rate:4.1f}/s  "
                      f"~{left/60:5.1f} min left", flush=True)

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(workers)]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=0.2)
    except KeyboardInterrupt:
        stop.set()
        print("\n  stopping - rerun to carry on")
        for t in threads:
            t.join(timeout=2)

    return counts["ok"], counts["fail"]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--mine", action="store_true",
                   help="every printing of cards in your history (default)")
    g.add_argument("--sets", help="comma separated set codes")
    g.add_argument("--all", action="store_true", help="every printing (10+ GB)")
    g.add_argument("--status", action="store_true", help="report cache size")
    ap.add_argument("--metadata", action="store_true",
                    help="download Scryfall's bulk file so prices load instantly")
    ap.add_argument("--cache-dir", help="where to keep the cache")
    # these are the two versions the interface actually asks for: 'small' for
    # the printing picker's thumbnails and 'large' for the reference pane
    ap.add_argument("--version", default="small,large",
                    help="image versions, comma separated (default small,large)")
    ap.add_argument("--limit", type=int, help="stop after this many downloads")
    ap.add_argument("--workers", type=int, default=8,
                    help="concurrent fetches (default 8; the global rate limit "
                         "still applies)")
    args = ap.parse_args()

    if args.cache_dir:
        scryfall.set_cache_root(args.cache_dir)

    n, mb, where = scryfall.cache_info()
    if args.status:
        bulk = scryfall.load_bulk()
        print(f"cache: {n} images, {mb:.0f} MB")
        print(f"  {where}")
        print(f"  bulk metadata: {len(bulk)} printings"
              if bulk else "  bulk metadata: not built (run --metadata)")
        return 0

    if args.metadata:
        scryfall.build_bulk(progress=lambda m: print(f"  {m}", flush=True))
        if not (args.mine or args.sets or args.all):
            return 0

    index = CardIndex()
    versions = [v.strip() for v in args.version.split(",") if v.strip()]

    if args.all:
        jobs = all_printings(index)
        label = "every printing"
    elif args.sets:
        jobs = printings_for_sets(index, args.sets.split(","))
        label = f"sets {args.sets}"
    else:
        names = traded_names(index)
        jobs, matched, missing = printings_for_names(index, names)
        label = f"{matched} cards from your history"
        print(f"card names found in your data : {len(names)}")
        print(f"  matched to the card database: {matched}")
        if missing:
            print(f"  unmatched (skipped)         : {len(missing)}")

    jobs = sorted(set(jobs))
    print(f"scope: {label}")
    print(f"  {len(jobs)} printing(s) x {len(versions)} version(s)")
    print(f"  cache currently holds {n} images, {mb:.0f} MB")
    print(f"  {where}")
    print()

    ok, fail = run(jobs, versions, args.limit, workers=max(1, args.workers))

    n2, mb2, _ = scryfall.cache_info()
    print()
    print(f"  fetched {ok}, {fail} unavailable")
    print(f"  cache now {n2} images, {mb2:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
