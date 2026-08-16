"""Fetch everything the repository deliberately leaves out.

The code is a few hundred kilobytes; the data it needs is gigabytes, all of it
downloadable and none of it worth keeping in git. This rebuilds the lot in
dependency order and can be re-run safely - each stage skips work already done.

    python setup_data.py              card names and prices  (~3 min)
    python setup_data.py --mine       ...plus images for cards you have traded
    python setup_data.py --full       ...plus every card image  (~20 GB, hours)
    python setup_data.py --status     what is present and what is missing
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paths  # noqa: E402


def run(args, label):
    print()
    print("=" * 70)
    print(f"  {label}")
    print("=" * 70)
    t0 = time.time()
    result = subprocess.run([sys.executable, "-u"] + args, cwd=HERE)
    dt = time.time() - t0
    if result.returncode != 0:
        print(f"  FAILED after {dt/60:.1f} min")
        return False
    print(f"  done in {dt/60:.1f} min")
    return True


def status():
    import scryfall

    print()
    print(f"  project : {paths.PROJECT}")
    print(f"  cache   : {paths.cache_dir()}")
    print()

    rows = []

    ok = os.path.exists(paths.INDEX)
    size = os.path.getsize(paths.INDEX) / 1024 / 1024 if ok else 0
    rows.append(("card name index", ok, f"{size:.1f} MB",
                 "python carddb.py --update"))

    try:
        import carddb
        loc = os.path.join(carddb.carddb_dir(), "localised.pkl")
        ok = os.path.exists(loc)
        size = os.path.getsize(loc) / 1024 / 1024 if ok else 0
        rows.append(("non-English names", ok, f"{size:.1f} MB",
                     "python carddb.py --languages"))
    except Exception:
        rows.append(("non-English names", False, "-",
                     "python carddb.py --languages"))

    bulk = scryfall.load_bulk()
    rows.append(("prices (offline)", bool(bulk), f"{len(bulk)} printings",
                 "python carddb.py --update"))

    n, mb, _ = scryfall.cache_info()
    rows.append(("card images", n > 0, f"{n} images, {mb:.0f} MB",
                 "python precache.py --mine"))

    try:
        import arthash
        idx = arthash.load()
        rows.append(("artwork index", idx is not None,
                     f"{len(idx)} cards" if idx else "-",
                     "python arthash.py --build"))
    except Exception:
        rows.append(("artwork index", False, "-", "python arthash.py --build"))

    for name, present, detail, fix in rows:
        mark = "[ok]" if present else "[--]"
        print(f"  {mark} {name:22} {detail}")
        if not present:
            print(f"       get it with: {fix}")
    print()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--mine", action="store_true",
                    help="also fetch images for cards in your history (~3 GB)")
    ap.add_argument("--full", action="store_true",
                    help="also fetch every card image (~20 GB, several hours)")
    ap.add_argument("--skip-languages", action="store_true",
                    help="skip the non-English name index (saves ~5 min)")
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()

    if args.status:
        status()
        return 0

    print()
    print("  This downloads the data the repository leaves out.")
    print("  Everything is resumable: stopping and re-running loses nothing.")
    print()
    print("  1. card names and prices    ~74 MB download,  ~3 min   [always]")
    print("  2. non-English card names   ~374 MB stream,   ~5 min   "
          f"[{'skipped' if args.skip_languages else 'yes'}]")
    if args.full:
        print("  3. every card image         ~20 GB,          hours")
        print("  4. artwork index            local work,      ~15 min")
    elif args.mine:
        print("  3. images for your cards    ~3 GB,           ~1 hour")
        print("  4. artwork index            local work,      ~15 min")
    else:
        print("  3. card images              skipped (--mine or --full)")

    steps = [(["carddb.py", "--update"], "Card names, sets and prices")]
    if not args.skip_languages:
        steps.append(([("carddb.py"), "--languages"],
                      "Card names in other languages"))
    if args.full:
        steps.append((["precache.py", "--all", "--workers", str(args.workers)],
                      "Every card image"))
    elif args.mine:
        steps.append((["precache.py", "--mine", "--workers", str(args.workers)],
                      "Images for cards in your history"))
    if args.mine or args.full:
        steps.append((["arthash.py", "--build"], "Artwork index"))

    for cmd, label in steps:
        if not run(cmd, label):
            print()
            print("  Stopped. Fix the problem above and run this again -")
            print("  completed stages will be skipped.")
            return 1

    status()
    print("  Ready. Start with:  python gui.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
