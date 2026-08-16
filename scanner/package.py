"""Build a portable copy of the project, ready to move to another machine.

    python package.py                  code, card index and your data
    python package.py --with-cache     also bundle the card images
    python package.py --cache mine     only images for cards you have handled

The result is a single .zip whose contents unpack into one folder. Nothing
inside refers to this machine: config.json is deliberately left out so the new
computer picks its own locations.
"""
import argparse
import fnmatch
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paths  # noqa: E402

# Machine-specific, redundant, or rebuildable - none of it should travel
EXCLUDE_DIRS = {"__pycache__", ".git", "_duplicates", "cache"}
EXCLUDE_GLOBS = [
    "config.json",          # cache path from the machine that built this
    ".cache_path",          # its predecessor
    ".catalogue.json",      # last phone address
    "*.pyc", "*.bak", "*.tmp",
    "cardmarket_stats_*.zip",   # the failed scrape, no usable data
]

# Included even though they are large, because they are your actual records
KEEP_DIRS = ["tools", "data", "_variants", "rejects", "_source_html"]
KEEP_FILES = ["README.md", "requirements.txt", "setup_mac.command",
              "MANIFEST.md", "collection_log.csv", "rejects.csv"]


def excluded(name):
    return any(fnmatch.fnmatch(name, g) for g in EXCLUDE_GLOBS)


def walk_project():
    root = paths.PROJECT
    for name in KEEP_FILES:
        p = os.path.join(root, name)
        if os.path.exists(p) and not excluded(name):
            yield p, name

    for d in KEEP_DIRS:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in EXCLUDE_DIRS]
            for fn in filenames:
                if excluded(fn):
                    continue
                full = os.path.join(dirpath, fn)
                yield full, os.path.relpath(full, root)


def cache_files(scope):
    """Image cache entries to bundle, as (path, archive name)."""
    import scryfall

    img = scryfall.IMG_CACHE
    if not os.path.isdir(img):
        return

    wanted = None
    if scope == "mine":
        import precache
        from cardlookup import CardIndex
        index = CardIndex()
        names = precache.traded_names(index)
        jobs, _matched, _missing = precache.printings_for_names(index, names)
        wanted = set()
        for code, num in jobs:
            key = scryfall._key(code, num)
            for v in ("small", "large"):
                wanted.add(f"{key}_{v}.jpg")

    for fn in os.listdir(img):
        if not fn.endswith(".jpg"):
            continue
        if wanted is not None and fn not in wanted:
            continue
        yield os.path.join(img, fn), os.path.join("cache", "scryfall", "img", fn)

    meta = scryfall.bulk_path()
    if os.path.exists(meta):
        yield meta, os.path.join("cache", "scryfall", os.path.basename(meta))

    jdir = scryfall.JSON_CACHE
    if os.path.isdir(jdir):
        for fn in os.listdir(jdir):
            yield (os.path.join(jdir, fn),
                   os.path.join("cache", "scryfall", "json", fn))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="path for the zip")
    ap.add_argument("--with-cache", action="store_true",
                    help="bundle every cached image (large)")
    ap.add_argument("--cache", choices=["none", "mine", "all"], default=None,
                    help="how much of the image cache to include")
    ap.add_argument("--list", action="store_true", help="show what would go in")
    args = ap.parse_args()

    scope = args.cache or ("all" if args.with_cache else "none")
    out = args.out or os.path.join(
        os.path.dirname(paths.PROJECT),
        f"cardmarket_tracker_portable{'_' + scope if scope != 'none' else ''}.zip")

    entries = list(walk_project())
    if scope != "none":
        entries += list(cache_files(scope))

    total = sum(os.path.getsize(p) for p, _ in entries)
    print(f"  {len(entries)} files, {total/1024/1024:.0f} MB uncompressed")
    print(f"  cache scope: {scope}")
    print(f"  writing {out}")

    if args.list:
        for _p, name in entries[:40]:
            print(f"    {name}")
        if len(entries) > 40:
            print(f"    ... and {len(entries)-40} more")
        return 0

    done = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for src, name in entries:
            # images are already compressed; storing them is much faster
            method = (zipfile.ZIP_STORED if name.lower().endswith(".jpg")
                      else zipfile.ZIP_DEFLATED)
            z.write(src, os.path.join("cardmarket_tracker", name),
                    compress_type=method)
            done += 1
            if done % 2000 == 0:
                print(f"    {done}/{len(entries)}", flush=True)

    size = os.path.getsize(out) / 1024 / 1024
    print(f"  done: {size:.0f} MB")
    print(f"  {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
