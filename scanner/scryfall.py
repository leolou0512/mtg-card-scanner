"""Card images and EUR prices from Scryfall, cached on disk.

Lookup is by set code + collector number, which is exactly what the scanner
reads off the card. Everything is cached under LOCALAPPDATA rather than in the
project folder, so thousands of card images never touch OneDrive.
"""
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.scryfall.com"
UA = "cardmarket-tracker/0.1 (personal collection tool; contact via local use)"

HERE = os.path.dirname(os.path.abspath(__file__))

import paths as _paths  # noqa: E402  (local module, resolves portable locations)

CACHE = _paths.cache_dir()
IMG_CACHE = os.path.join(CACHE, "img")
JSON_CACHE = os.path.join(CACHE, "json")


def set_cache_root(path):
    """Point the cache somewhere else and remember it for next time."""
    global CACHE, IMG_CACHE, JSON_CACHE
    CACHE = path
    IMG_CACHE = os.path.join(CACHE, "img")
    JSON_CACHE = os.path.join(CACHE, "json")
    _ensure_dirs()
    _paths.save_config(cache_dir=path)
    return CACHE

# Scryfall asks for no more than ~10 requests a second
MIN_INTERVAL = 0.1
_rate_lock = threading.Lock()
_last_call = [0.0]

_mem_json = {}
_mem_img = {}
_mem_lock = threading.Lock()


def _ensure_dirs():
    os.makedirs(IMG_CACHE, exist_ok=True)
    os.makedirs(JSON_CACHE, exist_ok=True)


def _throttle():
    with _rate_lock:
        wait = MIN_INTERVAL - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def _get(url, timeout=20):
    _throttle()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _key(setcode, number):
    """Cache key. Kept filesystem-safe: collector numbers contain characters
    like the star on premium printings (7ED #252★) that cannot go in a path."""
    raw = f"{(setcode or '').lower()}_{str(number or '').lower()}"
    return re.sub(r"[^a-z0-9._-]", lambda m: f"x{ord(m.group(0)):x}", raw)


def _url_path(setcode, number):
    """Percent-encode the path segments.

    Collector numbers are not all ASCII - premium printings in the older core
    sets use a star - and an unencoded one raises UnicodeEncodeError before the
    request is even sent.
    """
    return (urllib.parse.quote(str(setcode).lower(), safe=""),
            urllib.parse.quote(str(number), safe=""))


# --------------------------------------------------------------- bulk metadata

BULK_LIST = f"{API}/bulk-data"
_bulk = None
_bulk_lock = threading.Lock()


def bulk_path():
    return os.path.join(CACHE, "bulk_meta.pkl")


def load_bulk():
    """Local map of (set, collector number) -> metadata, if it was built.

    One 74 MB download replaces roughly 112,000 individual API calls, so prices
    and finishes are available instantly and offline.
    """
    global _bulk
    with _bulk_lock:
        if _bulk is not None:
            return _bulk
        try:
            import pickle
            with open(bulk_path(), "rb") as fh:
                _bulk = pickle.load(fh)
        except Exception:
            _bulk = {}
        return _bulk


def build_bulk(progress=None):
    """Download Scryfall's bulk card file and index it by set + number."""
    import gzip
    import pickle

    _ensure_dirs()
    meta = json.loads(_get(BULK_LIST, timeout=30).decode("utf-8"))
    entry = next((e for e in meta.get("data", [])
                  if e.get("type") == "default_cards"), None)
    if not entry:
        raise RuntimeError("scryfall did not offer a default_cards bulk file")

    url = entry.get("download_uri") or entry.get("jsonl_download_uri")
    if not url:
        raise RuntimeError("no download link in the bulk listing")
    if progress:
        progress(f"downloading {entry.get('name')} "
                 f"({entry.get('compressed_size', 0)/1024/1024:.0f} MB)")

    _throttle()
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()

    # the header does not always declare the encoding, so sniff the magic bytes
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)

    text = raw.decode("utf-8")
    if progress:
        progress(f"parsing {len(raw)/1024/1024:.0f} MB")

    records = []
    stripped = text.lstrip()
    if stripped.startswith("["):
        records = json.loads(text)
    else:
        for line in text.splitlines():
            line = line.strip().rstrip(",")
            if line and line not in ("[", "]"):
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    index = {}
    for c in records:
        code = (c.get("set") or "").upper()
        num = (c.get("collector_number") or "").lstrip("0").lower()
        if not code or not num:
            continue
        key = (code, num)
        # prefer English when a printing exists in several languages
        if key in index and index[key].get("lang") == "en":
            continue
        index[key] = {
            "name": c.get("name"),
            "set": code,
            "set_name": c.get("set_name"),
            "collector_number": c.get("collector_number"),
            "rarity": c.get("rarity"),
            "lang": c.get("lang"),
            "finishes": c.get("finishes"),
            "prices": c.get("prices"),
        }

    with open(bulk_path(), "wb") as fh:
        pickle.dump(index, fh, protocol=pickle.HIGHEST_PROTOCOL)

    global _bulk
    with _bulk_lock:
        _bulk = index
    if progress:
        progress(f"indexed {len(index)} printings")
    return index


# ---------------------------------------------------------------------- data

def card_data(setcode, number):
    """Metadata dict for a printing, or None. Cached indefinitely."""
    if not setcode or not number:
        return None

    bulk = load_bulk()
    if bulk:
        hit = bulk.get((setcode.upper(), str(number).lstrip("0").lower()))
        if hit:
            return hit

    k = _key(setcode, number)
    with _mem_lock:
        if k in _mem_json:
            return _mem_json[k]

    _ensure_dirs()
    path = os.path.join(JSON_CACHE, k + ".json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            with _mem_lock:
                _mem_json[k] = d
            return d
        except Exception:
            pass

    try:
        s, n = _url_path(setcode, number)
        raw = _get(f"{API}/cards/{s}/{n}")
        d = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        d = {"_error": f"HTTP {e.code}"} if e.code == 404 else None
    except Exception:
        d = None

    if d is not None:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(d, fh)
        except Exception:
            pass
        with _mem_lock:
            _mem_json[k] = d
    return d


def eur_price(data, foil=False):
    """EUR price as a display string, or None when Scryfall has none."""
    if not data or "prices" not in data:
        return None
    p = data["prices"] or {}
    val = p.get("eur_foil") if foil else p.get("eur")
    if not val and foil:
        val = p.get("eur")
    if not val:
        return None
    try:
        return f"EUR {float(val):.2f}"
    except (TypeError, ValueError):
        return None


def finishes(data):
    return (data or {}).get("finishes") or []


# -------------------------------------------------------------------- images

def image_bytes(setcode, number, version="normal"):
    """JPEG/PNG bytes for a printing, or None. Cached on disk."""
    if not setcode or not number:
        return None
    k = f"{_key(setcode, number)}_{version}"
    _ensure_dirs()
    path = os.path.join(IMG_CACHE, k + ".jpg")
    if os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except Exception:
            pass

    s, n = _url_path(setcode, number)
    url = f"{API}/cards/{s}/{n}?format=image&version={version}"
    try:
        raw = _get(url)
    except Exception:
        return None
    try:
        with open(path, "wb") as fh:
            fh.write(raw)
    except Exception:
        pass
    return raw


def card_image(setcode, number, version="normal"):
    """PIL image for a printing, or None. Kept in memory once loaded."""
    from PIL import Image

    k = f"{_key(setcode, number)}_{version}"
    with _mem_lock:
        if k in _mem_img:
            return _mem_img[k]
    raw = image_bytes(setcode, number, version)
    if not raw:
        return None
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        return None
    with _mem_lock:
        _mem_img[k] = im
    return im


def cache_path_for(setcode, number, version="normal"):
    return os.path.join(IMG_CACHE, f"{_key(setcode, number)}_{version}.jpg")


def is_cached(setcode, number, version="normal"):
    """Is this image on disk? Checks the filename only.

    Deliberately does not decode: bulk callers ask this tens of thousands of
    times, and loading each image just to answer costs minutes of CPU and
    gigabytes of memory.
    """
    k = f"{_key(setcode, number)}_{version}"
    with _mem_lock:
        if k in _mem_img:
            return True
    return os.path.exists(cache_path_for(setcode, number, version))


def cached_names(version=None):
    """Every cached image key, read once from the directory listing."""
    _ensure_dirs()
    out = set()
    try:
        for fn in os.listdir(IMG_CACHE):
            if fn.endswith(".jpg"):
                out.add(fn[:-4])
    except OSError:
        pass
    return out


def cached_only(setcode, number, version="normal"):
    """Return a cached image without hitting the network, else None."""
    if not is_cached(setcode, number, version):
        return None
    return card_image(setcode, number, version)


def cache_info():
    _ensure_dirs()
    n = len([f for f in os.listdir(IMG_CACHE) if f.endswith(".jpg")])
    size = sum(os.path.getsize(os.path.join(IMG_CACHE, f))
               for f in os.listdir(IMG_CACHE)) / 1024 / 1024
    return n, size, CACHE


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3:
        s, n = sys.argv[1], sys.argv[2]
        t0 = time.perf_counter()
        d = card_data(s, n)
        im = card_image(s, n)
        dt = (time.perf_counter() - t0) * 1000
        print(f"{s}/{n}  in {dt:.0f} ms")
        if d:
            print(f"  {d.get('name')}  [{d.get('set_name')}]  {d.get('rarity')}")
            print(f"  finishes {finishes(d)}")
            print(f"  eur {eur_price(d)}   eur foil {eur_price(d, True)}")
        print(f"  image {im.size if im else None}")
    cnt, mb, where = cache_info()
    print(f"cache: {cnt} images, {mb:.1f} MB in {where}")
