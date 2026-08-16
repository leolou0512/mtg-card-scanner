"""Build and update the card database, with no dependency on Cockatrice.

Cockatrice fetches MTGJSON's AllPrintings.json.xz and converts it to cards.xml
with its oracle tool. We skip that: Scryfall's bulk file carries every printing
with the name, set, collector number and rarity, and it is the same file already
downloaded for prices - so one source serves both.

    python carddb.py --status      what is installed and how old it is
    python carddb.py --update      fetch the latest and rebuild
    python carddb.py --update --force    rebuild even if already current

The downloaded source and the built index live in the cache folder beside the
card images, so the whole lot travels together.
"""
import argparse
import gzip
import json
import os
import pickle
import re
import sys
import time
import unicodedata
import urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paths  # noqa: E402

API = "https://api.scryfall.com"
UA = "cardmarket-tracker/0.1 (personal collection tool)"

# what Cockatrice itself uses, kept for reference and as a fallback source
MTGJSON_ALL = "https://www.mtgjson.com/api/v5/AllPrintings.json.xz"
MTGJSON_META = "https://www.mtgjson.com/api/v5/Meta.json"
COCKATRICE_TOKENS = ("https://raw.githubusercontent.com/Cockatrice/"
                     "Magic-Token/master/tokens.xml")

# card layouts that are not real cards you would own
SKIP_LAYOUTS = {"art_series", "double_faced_token", "emblem", "token",
                "vanguard", "scheme", "planar"}


def carddb_dir():
    d = os.path.join(os.path.dirname(paths.cache_dir()), "carddb")
    os.makedirs(d, exist_ok=True)
    return d


VERSION_FILE = lambda: os.path.join(carddb_dir(), "version.json")  # noqa: E731


def normalise(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("&amp;", "&").replace("&apos;", "'").replace("&quot;", '"')
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def trigrams(s):
    s = f"  {s} "
    return {s[i:i + 3] for i in range(len(s) - 2)}


def _get(url, timeout=120):
    # Scryfall rejects requests without an Accept header with a 400
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "*/*",
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw


def installed():
    try:
        with open(VERSION_FILE(), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def remote_version():
    """Scryfall's published timestamp for the bulk card file."""
    meta = json.loads(_get(f"{API}/bulk-data", timeout=30).decode("utf-8"))
    entry = next((e for e in meta.get("data", [])
                  if e.get("type") == "default_cards"), None)
    if not entry:
        raise RuntimeError("scryfall did not offer a default_cards file")
    return entry


def fetch_sets():
    """Set codes -> full name, type and release date."""
    out, url = {}, f"{API}/sets"
    while url:
        page = json.loads(_get(url, timeout=60).decode("utf-8"))
        for s in page.get("data", []):
            code = (s.get("code") or "").upper()
            if not code:
                continue
            out[code] = {"name": s.get("name") or code,
                         "type": s.get("set_type") or "",
                         "released": s.get("released_at") or ""}
        url = page.get("next_page") if page.get("has_more") else None
    return out


def parse_records(raw):
    text = raw.decode("utf-8")
    stripped = text.lstrip()
    if stripped.startswith("["):
        return json.loads(text)
    records = []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if line and line not in ("[", "]"):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def build(progress=print, keep_source=False):
    entry = remote_version()
    progress(f"  source: {entry.get('name')} updated {entry.get('updated_at')}")

    url = entry.get("download_uri") or entry.get("jsonl_download_uri")
    progress("  downloading card data ...")
    raw = _get(url, timeout=600)
    progress(f"  {len(raw)/1024/1024:.0f} MB, parsing ...")
    records = parse_records(raw)
    progress(f"  {len(records)} printings")

    if keep_source:
        src = os.path.join(carddb_dir(), "default_cards.json")
        with open(src, "wb") as fh:
            fh.write(raw)
        progress(f"  kept source at {src}")

    progress("  fetching set names ...")
    sets = fetch_sets()
    progress(f"  {len(sets)} sets")

    # group printings under one entry per card name
    by_name = defaultdict(list)
    prices = {}
    tokens = set()
    body = {}
    skipped_digital = 0
    for c in records:
        name = c.get("name")
        code = (c.get("set") or "").upper()
        num = c.get("collector_number") or ""
        if not name or not code or not num:
            continue

        # Arena and Magic Online printings have no physical card, so they can
        # never be the thing under the camera. Including them only pads the
        # printing list with versions that cannot be owned on paper.
        games = c.get("games") or []
        if games and "paper" not in games:
            skipped_digital += 1
            continue

        layout = c.get("layout") or ""
        is_token = layout in SKIP_LAYOUTS
        rarity = c.get("rarity") or ""
        by_name[name].append((code, num, rarity))
        if is_token:
            tokens.add(name)

        # the printed text, used to corroborate a name read off a blurry photo.
        # English only: the OCR is matching against English oracle text.
        if name not in body and (c.get("lang") or "en") == "en":
            parts = []
            if c.get("type_line"):
                parts.append(c["type_line"])
            if c.get("oracle_text"):
                parts.append(c["oracle_text"])
            for face in (c.get("card_faces") or []):
                if face.get("type_line"):
                    parts.append(face["type_line"])
                if face.get("oracle_text"):
                    parts.append(face["oracle_text"])
            if parts:
                body[name] = "\n".join(parts)

        key = (code, num.lstrip("0").lower() or "0")
        if key not in prices or c.get("lang") == "en":
            prices[key] = {
                "name": name, "set": code, "set_name": c.get("set_name"),
                "collector_number": num, "rarity": rarity,
                "lang": c.get("lang"), "finishes": c.get("finishes"),
                "prices": c.get("prices"),
            }

    cards = []
    for name, printings in by_name.items():
        seen, unique = set(), []
        for p in printings:
            if p[:2] not in seen:
                seen.add(p[:2])
                unique.append(p)
        unique.sort(key=lambda p: (p[0], p[1]))
        cards.append({"name": name, "printings": unique,
                      "token": name in tokens, "norm": normalise(name),
                      "body": body.get(name, "")})
    cards.sort(key=lambda c: c["name"])

    exact = {}
    for i, c in enumerate(cards):
        exact.setdefault(c["norm"], i)
    for i, c in enumerate(cards):
        if "//" in c["name"] or " / " in c["name"]:
            front = normalise(re.split(r"\s*//\s*|\s+/\s+", c["name"])[0])
            if front and front not in exact:
                exact[front] = i

    postings = defaultdict(list)
    for i, c in enumerate(cards):
        for t in trigrams(c["norm"]):
            postings[t].append(i)

    data = {"cards": cards, "exact": exact,
            "postings": dict(postings), "sets": sets}
    with open(paths.INDEX, "wb") as fh:
        pickle.dump(data, fh, protocol=pickle.HIGHEST_PROTOCOL)

    import scryfall
    scryfall._ensure_dirs()
    with open(scryfall.bulk_path(), "wb") as fh:
        pickle.dump(prices, fh, protocol=pickle.HIGHEST_PROTOCOL)

    version = {
        "updated_at": entry.get("updated_at"),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cards": len(cards),
        "printings": sum(len(c["printings"]) for c in cards),
        "sets": len(sets),
        "source": "scryfall default_cards",
    }
    with open(VERSION_FILE(), "w", encoding="utf-8") as fh:
        json.dump(version, fh, indent=2)

    progress(f"  skipped {skipped_digital} digital-only printings "
             f"(Arena / Magic Online)")
    progress(f"  built {version['cards']} names, {version['printings']} printings")
    progress(f"  index:  {paths.INDEX}")
    progress(f"  prices: {scryfall.bulk_path()}")
    return version


# --------------------------------------------------- localised card names

# Scryfall language tags we care about, mapped to the labels used in the log
LANGS = {
    "zhs": "S-Chinese", "zht": "T-Chinese", "ja": "Japanese",
    "ko": "Korean", "ru": "Russian", "de": "German", "fr": "French",
    "it": "Italian", "es": "Spanish", "pt": "Portuguese",
}


def stream_records(url, progress=None):
    """Yield records from a bulk file without holding it all in memory.

    all_cards is several gigabytes once decompressed, so it is read as a
    stream: the JSONL form is one object per line, and the JSON array form is
    handled by trimming the surrounding brackets.
    """
    import gzip as _gzip
    import io as _io

    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "*/*",
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=900) as r:
        raw = r
        if r.headers.get("Content-Encoding") == "gzip" or url.endswith(".gz"):
            raw = _gzip.GzipFile(fileobj=r)
        text = _io.TextIOWrapper(raw, encoding="utf-8")
        n = 0
        for line in text:
            line = line.strip().rstrip(",")
            if not line or line in ("[", "]"):
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
            n += 1
            if progress and n % 50000 == 0:
                progress(f"  read {n} records ...")


def build_localised(progress=print):
    """Index every printed name in a non-English language.

    Makes foreign cards findable by the name actually on the card, rather than
    only by the collector line.
    """
    meta = json.loads(_get(f"{API}/bulk-data", timeout=30).decode("utf-8"))
    entry = next((e for e in meta.get("data", [])
                  if e.get("type") == "all_cards"), None)
    if not entry:
        raise RuntimeError("scryfall did not offer an all_cards file")
    url = entry.get("download_uri") or entry.get("jsonl_download_uri")
    progress(f"  source: {entry.get('name')} "
             f"({entry.get('compressed_size', 0)/1024/1024:.0f} MB compressed)")

    # printed name -> english name, plus which languages a card was printed in
    localised = {}
    per_lang = defaultdict(int)
    seen = 0
    for c in stream_records(url, progress):
        seen += 1
        lang = c.get("lang")
        if lang == "en" or lang not in LANGS:
            continue
        printed = c.get("printed_name")
        english = c.get("name")
        if not printed or not english:
            continue
        key = normalise_cjk(printed)
        if not key:
            continue
        if key not in localised:
            localised[key] = {"name": english, "lang": lang,
                              "printed": printed}
            per_lang[lang] += 1

    progress(f"  scanned {seen} records")
    for lang, n in sorted(per_lang.items(), key=lambda kv: -kv[1]):
        progress(f"    {LANGS[lang]:12} {n} names")

    out = os.path.join(carddb_dir(), "localised.pkl")
    with open(out, "wb") as fh:
        pickle.dump({"names": localised,
                     "langs": {k: LANGS[k] for k in per_lang}}, fh,
                    protocol=pickle.HIGHEST_PROTOCOL)
    progress(f"  {len(localised)} localised names -> {out}")
    return localised


def normalise_cjk(s):
    """Normalise a printed name for matching.

    CJK text has no spaces to lose and OCR rarely invents punctuation, so this
    keeps letters, digits and CJK characters and discards everything else.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    keep = []
    for ch in s:
        o = ord(ch)
        if ch.isalnum() or (0x3040 <= o <= 0x30FF) or (0x4E00 <= o <= 0x9FFF) \
                or (0xAC00 <= o <= 0xD7AF) or (0x3400 <= o <= 0x4DBF):
            keep.append(ch)
    return "".join(keep).lower()


def needs_update():
    """(needed, local_stamp, remote_stamp)."""
    local = installed().get("updated_at")
    try:
        remote = remote_version().get("updated_at")
    except Exception:
        return False, local, None
    return (local != remote), local, remote


def status():
    v = installed()
    print(f"  index file : {paths.INDEX}")
    print(f"  exists     : {os.path.exists(paths.INDEX)}")
    if v:
        print(f"  source     : {v.get('source')}")
        print(f"  card data  : {v.get('updated_at')}")
        print(f"  built      : {v.get('built_at')}")
        print(f"  contents   : {v.get('cards')} names, "
              f"{v.get('printings')} printings, {v.get('sets')} sets")
    else:
        print("  no version record - the index was built by an older tool")
    try:
        needed, local, remote = needs_update()
        if remote:
            print(f"  latest     : {remote}")
            print("  " + ("UPDATE AVAILABLE - run: python carddb.py --update"
                          if needed else "up to date"))
    except Exception as e:
        print(f"  could not check for updates: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--keep-source", action="store_true",
                    help="also save the raw download in the cache folder")
    ap.add_argument("--languages", action="store_true",
                    help="build the index of non-English printed names")
    args = ap.parse_args()

    if args.languages:
        build_localised()
        if not args.update:
            return 0

    if args.update:
        if not args.force:
            needed, local, remote = needs_update()
            if not needed and os.path.exists(paths.INDEX):
                print(f"  already current ({local})")
                return 0
        build(keep_source=args.keep_source)
        return 0

    status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
