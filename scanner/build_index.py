"""Build a fast lookup index from the Cockatrice card database.

Reads cards.xml + tokens.xml and writes index.pkl containing:
  - every card name, normalised for fuzzy matching
  - every printing (set code, collector number, rarity)
  - a trigram index for approximate matching against OCR output

Pure standard library. No installs.
"""
import os
import pickle
import re
import sys
import unicodedata
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths  # noqa: E402

COCKATRICE = paths.cockatrice_dir()
OUT = paths.INDEX

RE_NAME = re.compile(r"^\s*<name>(.*?)</name>\s*$")
RE_SET = re.compile(r'^\s*<set\s+num="([^"]*)"[^>]*?rarity="([^"]*)"[^>]*>([^<]+)</set>\s*$')
RE_LONGNAME = re.compile(r"^\s*<longname>(.*?)</longname>\s*$")
RE_SETTYPE = re.compile(r"^\s*<settype>(.*?)</settype>\s*$")
RE_RELEASE = re.compile(r"^\s*<releasedate>(.*?)</releasedate>\s*$")


def parse_sets(path):
    """Read the <sets> block: set code -> full name, type, release date."""
    sets = {}
    if not os.path.exists(path):
        return sets
    in_sets = False
    code = longname = settype = release = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if "<sets>" in line:
                in_sets = True
                continue
            if "</sets>" in line:
                break
            if not in_sets:
                continue
            m = RE_NAME.match(line)
            if m:
                code = m.group(1)
                longname = settype = release = None
                continue
            m = RE_LONGNAME.match(line)
            if m:
                longname = m.group(1)
                continue
            m = RE_SETTYPE.match(line)
            if m:
                settype = m.group(1)
                continue
            m = RE_RELEASE.match(line)
            if m:
                release = m.group(1)
                continue
            if "</set>" in line and code:
                sets[code.upper()] = {
                    "name": longname or code,
                    "type": settype or "",
                    "released": release or "",
                }
                code = None
    return sets


def normalise(s):
    """Lowercase, strip accents and punctuation. OCR-friendly comparison form."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("&amp;", "&").replace("&apos;", "'").replace("&quot;", '"')
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def trigrams(s):
    s = f"  {s} "
    return {s[i:i + 3] for i in range(len(s) - 2)}


def parse(path, is_token):
    """Yield (name, [(setcode, number, rarity), ...])."""
    if not os.path.exists(path):
        print(f"  ! missing {path}")
        return
    name, printings = None, []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = RE_NAME.match(line)
            if m:
                if name:
                    yield name, printings, is_token
                name, printings = m.group(1), []
                continue
            m = RE_SET.match(line)
            if m and name:
                printings.append((m.group(3), m.group(1), m.group(2)))
    if name:
        yield name, printings, is_token


def main():
    sets = parse_sets(os.path.join(COCKATRICE, "cards.xml"))
    print(f"  sets         {len(sets):6} definitions")

    cards = []
    for fname, is_token in (("cards.xml", False), ("tokens.xml", True)):
        path = os.path.join(COCKATRICE, fname)
        n = 0
        for name, printings, tok in parse(path, is_token):
            cards.append({"name": name, "printings": printings, "token": tok})
            n += 1
        print(f"  {fname:12} {n:6} names")

    # normalised lookup: exact hits resolve instantly
    exact = {}
    for i, c in enumerate(cards):
        key = normalise(c["name"])
        c["norm"] = key
        # first writer wins, so real cards beat tokens of the same name
        exact.setdefault(key, i)

    # trigram postings, for fuzzy matching when OCR is imperfect
    postings = defaultdict(list)
    for i, c in enumerate(cards):
        for t in trigrams(c["norm"]):
            postings[t].append(i)

    # front-face aliases: "Fire // Ice" is also findable as "Fire"
    for i, c in enumerate(cards):
        if "//" in c["name"] or " / " in c["name"]:
            front = normalise(re.split(r"\s*//\s*|\s+/\s+", c["name"])[0])
            if front and front not in exact:
                exact[front] = i

    data = {
        "cards": cards,
        "exact": exact,
        "postings": {k: v for k, v in postings.items()},
        "sets": sets,
    }
    with open(OUT, "wb") as fh:
        pickle.dump(data, fh, protocol=pickle.HIGHEST_PROTOCOL)

    total_printings = sum(len(c["printings"]) for c in cards)
    size = os.path.getsize(OUT) / 1024 / 1024
    print()
    print(f"  cards+tokens : {len(cards)}")
    print(f"  printings    : {total_printings}")
    print(f"  trigrams     : {len(postings)}")
    print(f"  aliases      : {len(exact)}")
    print(f"  written      : {OUT}  ({size:.1f} MB)")


if __name__ == "__main__":
    sys.exit(main())
