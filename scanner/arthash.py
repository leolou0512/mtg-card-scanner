"""Identify a card by its artwork.

Reads the art out of a photo, hashes it, and compares against hashes of every
cached reference image. Art is the same whatever language a card is printed
in, so this works where OCR cannot read the name at all.

    python arthash.py --build          hash every cached image (slow, once)
    python arthash.py --status
    python arthash.py --match photo.jpg

Matching is fast: comparing against 116,000 cards is one vectorised XOR, a few
milliseconds. Only the one-off build is expensive.
"""
import argparse
import os
import pickle
import sys
import threading
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import scryfall  # noqa: E402

# the art window on a standard card frame, as fractions of the card
ART_BOX = (0.08, 0.11, 0.92, 0.46)

# a distance this small means the same artwork; above the second value the
# answer is not trustworthy on its own
STRONG = 8
WEAK = 16

POPCOUNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def index_path():
    return os.path.join(scryfall.CACHE, "arthash.pkl")


def art_crop(img):
    w, h = img.size
    return img.crop((int(w * ART_BOX[0]), int(h * ART_BOX[1]),
                     int(w * ART_BOX[2]), int(h * ART_BOX[3])))


def phash(img, size=8, factor=4):
    """DCT hash of the image, tolerant of scale, brightness and mild blur."""
    from scipy.fftpack import dct

    g = img.convert("L").resize((size * factor, size * factor), Image.LANCZOS)
    a = np.asarray(g, dtype=float)
    d = dct(dct(a, axis=0, norm="ortho"), axis=1, norm="ortho")
    low = d[:size, :size]
    return np.packbits(low > np.median(low[1:, 1:]))


def hash_card_image(img):
    return phash(art_crop(img))


def hamming_all(one, many):
    return POPCOUNT[np.bitwise_xor(many, one)].sum(axis=1)


# ------------------------------------------------------------------- build

def build(version="small", workers=8, progress=print, limit=None,
          incremental=True):
    """Hash cached reference images into a searchable index.

    'small' images are used by default: at 146x204 they still carry more
    detail than an 8x8 hash needs, and they decode roughly ten times faster
    than the large ones.

    Incremental by default, because the image cache grows: a top-up after more
    images arrive only hashes the new ones instead of redoing the lot.
    """
    img_dir = scryfall.IMG_CACHE
    suffix = f"_{version}.jpg"
    files = [f for f in os.listdir(img_dir) if f.endswith(suffix)]
    if limit:
        files = files[:limit]
    if not files:
        raise RuntimeError(f"no cached '{version}' images in {img_dir}")

    results = {}
    if incremental:
        existing = load()
        if existing is not None and existing.version == version:
            results = {k: existing.hashes[i]
                       for i, k in enumerate(existing.keys)}
            before = len(files)
            files = [f for f in files if f[:-len(suffix)] not in results]
            progress(f"  {len(results)} already hashed, "
                     f"{len(files)} new of {before}")
            if not files:
                progress("  nothing new to hash")
                return {"keys": existing.keys, "hashes": existing.hashes,
                        "version": version}

    progress(f"  hashing {len(files)} images with {workers} workers ...")
    lock = threading.Lock()
    counter = {"n": 0, "bad": 0}
    t0 = time.time()

    def worker(chunk):
        """Report every few hundred images rather than once per chunk.

        Reporting only on completion means a long build looks frozen: with
        eight workers nothing at all prints until the first one finishes.
        """
        local = {}
        bad = 0
        for fn in chunk:
            try:
                im = Image.open(os.path.join(img_dir, fn))
                im.load()
                local[fn[:-len(suffix)]] = hash_card_image(im)
            except Exception:
                bad += 1
            if len(local) % 250 == 0 and local:
                with lock:
                    results.update(local)
                    counter["n"] += len(local)
                    counter["bad"] += bad
                    local, bad = {}, 0
                    done = counter["n"]
                    rate = done / max(time.time() - t0, 1e-9)
                    left = (len(files) - done) / max(rate, 1e-9)
                    progress(f"    {done}/{len(files)}  {rate:.0f}/s  "
                             f"~{left/60:.1f} min left")
        with lock:
            results.update(local)
            counter["n"] += len(local)
            counter["bad"] += bad

    size = max(1, len(files) // workers)
    chunks = [files[i:i + size] for i in range(0, len(files), size)]
    threads = [threading.Thread(target=worker, args=(c,), daemon=True)
               for c in chunks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    keys = sorted(results)
    table = np.array([results[k] for k in keys], dtype=np.uint8)
    blob = {"keys": keys, "hashes": table, "version": version,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "art_box": ART_BOX}
    with open(index_path(), "wb") as fh:
        pickle.dump(blob, fh, protocol=pickle.HIGHEST_PROTOCOL)

    dt = time.time() - t0
    progress(f"  hashed {len(keys)} in {dt/60:.1f} min "
             f"({counter['bad']} unreadable)")
    progress(f"  {os.path.getsize(index_path())/1024/1024:.1f} MB "
             f"-> {index_path()}")
    return blob


# ------------------------------------------------------------------ search

class ArtIndex:
    def __init__(self, path=None):
        with open(path or index_path(), "rb") as fh:
            blob = pickle.load(fh)
        self.keys = blob["keys"]
        self.hashes = blob["hashes"]
        self.version = blob.get("version")
        self.built_at = blob.get("built_at")

    def __len__(self):
        return len(self.keys)

    def match(self, card_img, limit=5):
        """Return [(set, number, distance)] for the closest artwork."""
        q = hash_card_image(card_img)
        d = hamming_all(q, self.hashes)
        order = np.argsort(d)[:limit]
        out = []
        for i in order:
            key = self.keys[i]
            code, _, num = key.partition("_")
            out.append((code.upper(), num, int(d[i])))
        return out

    def rank_printings(self, card_img, printings, limit=24):
        """Order a known card's printings by how well each matches the photo.

        This hashes the *whole card*, not just the art window, which is the
        opposite of what identification wants. Identification ignores the frame
        so that a photo matches whatever the reference looks like; but printings
        of one card frequently share the same artwork and differ only in the
        frame - borderless, extended, showcase - so the frame is precisely the
        signal here. Measured on Starfield Shepherd EOE#37 vs EOE#393: art alone
        cannot separate them at all (gap 0), whole-card separates them by 13.

        Only a handful of images per card, so they are hashed on demand rather
        than kept in the index.

        Returns [(setcode, number, distance)] closest first; printings with no
        cached image are left out.
        """
        if not printings:
            return []
        q = phash(card_img)
        rows, meta = [], []
        for p in printings[:limit]:
            code, num = p[0], p[1]
            im = (scryfall.cached_only(code, num, "large")
                  or scryfall.cached_only(code, num, "small"))
            if im is None:
                continue
            rows.append(phash(im))
            meta.append((code, num))
        if not rows:
            return []
        d = hamming_all(q, np.array(rows, dtype=np.uint8))
        out = [(meta[j][0], meta[j][1], int(d[j])) for j in range(len(meta))]
        out.sort(key=lambda t: t[2])
        return out

    def confident(self, results):
        """Is the best artwork match trustworthy on its own?

        Requires both a close match and a clear gap to the runner-up: a blurry
        photo can sit 15 away from the right card and 18 from a wrong one,
        which is not a decision worth acting on.
        """
        if not results:
            return False
        best = results[0][2]
        second = results[1][2] if len(results) > 1 else 64
        return best <= STRONG and (second - best) >= 6


def load():
    try:
        return ArtIndex()
    except Exception:
        return None


# -------------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--match", help="photo of a card to identify")
    ap.add_argument("--version", default="small",
                    choices=["small", "normal", "large"])
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--rebuild", action="store_true",
                    help="hash everything again instead of topping up")
    args = ap.parse_args()

    if args.build:
        build(version=args.version, workers=args.workers, limit=args.limit,
              incremental=not args.rebuild)
        return 0

    if args.match:
        import carddetect
        from cardlookup import CardIndex

        idx = ArtIndex()
        cards = CardIndex()
        img = Image.open(args.match).convert("RGB")
        box = carddetect.detect(img)
        if not box:
            print("no card found in that image")
            return 1
        card = img.crop(box)
        best = None
        for rot in (0, 180):
            view = card if rot == 0 else card.transpose(Image.ROTATE_180)
            t = time.perf_counter()
            res = idx.match(view)
            dt = (time.perf_counter() - t) * 1000
            if best is None or res[0][2] < best[1][0][2]:
                best = (rot, res, dt)
        rot, res, dt = best
        print(f"searched {len(idx)} cards in {dt:.1f} ms "
              f"(orientation {rot} degrees)")
        for code, num, dist in res:
            card_obj, unique = cards.by_printing(code, num)
            name = card_obj["name"] if card_obj else "?"
            print(f"   {dist:3}  {code}#{num:<8} {name}")
        print(f"confident: {idx.confident(res)}")
        return 0

    idx = load()
    if idx is None:
        print("  no art index yet - run: python arthash.py --build")
    else:
        print(f"  art index: {len(idx)} cards from '{idx.version}' images")
        print(f"  built     : {idx.built_at}")
        print(f"  file      : {index_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
