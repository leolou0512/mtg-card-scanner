"""Fuzzy card-name lookup against the Cockatrice index.

Designed for OCR output: tolerant of wrong characters, missing letters and
joined-up words. Trigram prefilter narrows 37k names to a few dozen, then a
sequence match ranks those. Pure standard library.
"""
import heapq
import os
import pickle
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.abspath(__file__))
import paths
INDEX = paths.INDEX


def normalise(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("&amp;", "&").replace("&apos;", "'").replace("&quot;", '"')
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def trigrams(s):
    s = f"  {s} "
    return {s[i:i + 3] for i in range(len(s) - 2)}


class CardIndex:
    def __init__(self, path=INDEX):
        with open(path, "rb") as fh:
            data = pickle.load(fh)
        self.cards = data["cards"]
        self.exact = data["exact"]
        self.postings = data["postings"]
        self.sets = data.get("sets", {})

        # (set code, collector number) -> card indices.
        # This is how non-English cards get identified: the collector line is
        # printed in Latin characters and Arabic numerals whatever the card's
        # language, so it works when the name is unreadable.
        self.by_print = {}
        for i, c in enumerate(self.cards):
            for code, num, _rarity in c["printings"]:
                key = (code.upper(), num.lstrip("0").lower() or "0")
                self.by_print.setdefault(key, []).append(i)

        self._localised = None
        self._loc_bigrams = None

    # -------------------------------------------------- non-English names
    def _load_localised(self):
        """Lazily load the printed-name index; it is only needed for foreign
        cards and costs memory nobody else wants."""
        if self._localised is not None:
            return self._localised
        self._localised = {}
        try:
            import carddb
            path = os.path.join(carddb.carddb_dir(), "localised.pkl")
            with open(path, "rb") as fh:
                blob = pickle.load(fh)
            self._localised = blob.get("names", {})
        except Exception:
            self._localised = {}

        # character bigrams, so a partly misread name can still be found
        self._loc_bigrams = {}
        for key in self._localised:
            for i in range(max(1, len(key) - 1)):
                self._loc_bigrams.setdefault(key[i:i + 2], []).append(key)
        return self._localised

    def lookup_localised(self, text, limit=5):
        """Match a name printed in another language.

        Returns [(card, score, language)], best first.
        """
        loc = self._load_localised()
        if not loc:
            return []
        q = normalise_cjk(text)
        if len(q) < 2:
            return []

        hit = loc.get(q)
        if hit:
            found = self.lookup(hit["name"], limit=1)
            if found:
                return [(found[0][0], 1.0, hit["lang"])]

        counts = Counter()
        for i in range(max(1, len(q) - 1)):
            for cand in self._loc_bigrams.get(q[i:i + 2], ()):
                counts[cand] += 1
        if not counts:
            return []

        out = []
        for cand, _n in counts.most_common(40):
            ratio = SequenceMatcher(None, q, cand).ratio()
            if ratio < 0.5:
                continue
            entry = loc[cand]
            found = self.lookup(entry["name"], limit=1)
            if found:
                out.append((found[0][0], min(ratio, 0.999), entry["lang"]))
        out.sort(key=lambda t: -t[1])
        return out[:limit]

    def set_name(self, code):
        """Full set name for a code, falling back to the code itself."""
        if not code:
            return ""
        info = self.sets.get(code.upper())
        return info["name"] if info else code

    def set_year(self, code):
        if not code:
            return ""
        info = self.sets.get(code.upper())
        return (info.get("released") or "")[:4] if info else ""

    def by_printing(self, setcode, number):
        """Resolve a card from its set code and collector number.

        Returns (card, unique) or (None, False).
        """
        if not setcode or not number:
            return None, False
        key = (setcode.upper(), str(number).lstrip("0").lower() or "0")
        hits = self.by_print.get(key)
        if not hits:
            return None, False
        return self.cards[hits[0]], len(hits) == 1

    def lookup(self, query, limit=5, prefilter=60):
        """Return [(card, score)] best first. score 1.0 == exact match."""
        q = normalise(query)
        if not q:
            return []

        hit = self.exact.get(q)
        if hit is not None:
            return [(self.cards[hit], 1.0)]

        # trigram overlap gives a cheap candidate shortlist
        counts = Counter()
        qt = trigrams(q)
        for t in qt:
            for i in self.postings.get(t, ()):
                counts[i] += 1
        if not counts:
            return []

        shortlist = [i for i, _ in counts.most_common(prefilter)]

        scored = []
        for i in shortlist:
            cand = self.cards[i]["norm"]
            ratio = SequenceMatcher(None, q, cand).ratio()
            # nudge candidates that share a prefix with the query: OCR tends to
            # get the start of a name right and mangle the tail
            if cand.startswith(q[:4]) or q.startswith(cand[:4]):
                ratio += 0.05
            scored.append((ratio, i))

        scored.sort(reverse=True)
        return [(self.cards[i], min(r, 0.999)) for r, i in scored[:limit]]

    def prefix(self, text, limit=9):
        """Type-ahead: names starting with `text`, then names containing it.

        Within each group the most reprinted cards come first. Sorting by name
        length instead buried the cards anyone is actually likely to be holding
        - typing 'light' offered seven obscure commons before Lightning Bolt,
        because they happen to have shorter names.
        """
        q = normalise(text)
        if not q:
            return []
        starts, contains = [], []
        for c in self.cards:
            n = c["norm"]
            if n.startswith(q):
                starts.append(c)
            elif q in n:
                contains.append(c)

        def rank(c):
            return (c["norm"] != q,                   # an exact name wins
                    -len(c.get("printings") or ()),   # then how often reprinted
                    len(c["norm"]), c["norm"])

        # a two-letter query can match thousands; only the first few are wanted,
        # so pick them rather than sorting the lot
        head = heapq.nsmallest(limit, starts, key=rank)
        tail = heapq.nsmallest(limit, contains, key=rank)
        # always keep a few rows for names that merely contain the query, or
        # 'bolt' would fill up with Bolt Bend and never reach Lightning Bolt
        keep = min(3, len(tail))
        return (head[:limit - keep] + tail)[:limit]


# ------------------------------------------------------- non-English names

def detect_script(text):
    """Which writing system is this? Decides which index to search.

    Cheap and decisive: a card name printed in Japanese contains kana or han
    characters that simply cannot appear on an English card.
    """
    if not text:
        return "latin"
    kana = han = hangul = latin = 0
    for ch in text:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF:
            kana += 1
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            han += 1
        elif 0xAC00 <= o <= 0xD7AF:
            hangul += 1
        elif ch.isalpha() and ord(ch) < 0x250:
            latin += 1
    if kana:
        return "japanese"
    if hangul:
        return "korean"
    if han:
        return "han"          # Chinese, or Japanese written without kana
    return "latin"


def normalise_cjk(s):
    """Keep letters, digits and CJK; drop punctuation and spacing."""
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


# ------------------------------------------------- corroboration by body text

# words that appear on nearly every card and so distinguish nothing
STOPWORDS = {
    "the", "a", "an", "of", "to", "and", "or", "you", "your", "this", "that",
    "it", "its", "is", "are", "be", "with", "for", "from", "on", "in", "at",
    "as", "if", "when", "whenever", "then", "than", "may", "can", "each",
    "any", "all", "target", "creature", "creatures", "card", "cards", "player",
    "players", "control", "controls", "controlled", "battlefield", "hand",
    "graveyard", "library", "turn", "end", "enters", "put", "get", "gets",
}


def words(text):
    """Content words, lowercased, punctuation stripped."""
    if not text:
        return []
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    raw = re.findall(r"[a-z]{3,}", text.lower())
    return [w for w in raw if w not in STOPWORDS]


def _tri(word):
    w = f" {word} "
    return {w[i:i + 3] for i in range(len(w) - 2)}


def corroborate(ocr_text, card, min_words=3):
    """How well does text read off the card agree with this card's oracle text?

    Returns 0.0 to 1.0, or None when there is too little to judge.

    Matching is per word and fuzzy, because OCR mangles rules text far worse
    than it mangles the large name at the top: 'Creatures you contro with
    flying get' still has to line up with 'Creatures you control with flying
    get +1/+1.'
    """
    body = (card or {}).get("body") or ""
    if not body:
        return None
    seen = words(ocr_text)
    if len(seen) < min_words:
        return None

    expected = set(words(body))
    if not expected:
        return None
    expected_tri = {w: _tri(w) for w in expected}

    hits = 0.0
    for w in seen:
        if w in expected:
            hits += 1.0
            continue
        # allow a mangled word to count partially
        wt = _tri(w)
        best = 0.0
        for cand, ct in expected_tri.items():
            if abs(len(cand) - len(w)) > 3:
                continue
            inter = len(wt & ct)
            if not inter:
                continue
            score = inter / max(len(wt | ct), 1)
            if score > best:
                best = score
        if best >= 0.6:
            hits += best
    return min(1.0, hits / len(seen))


def rerank(candidates, ocr_text, weight=0.35, margin=0.08,
           low_confidence=0.85):
    """Re-order name matches using the rest of the text on the card.

    Intervenes when the top two names are close, or when the best match is
    weak on its own. Both cases matter: a badly-read name can produce a wrong
    winner with a comfortable margin, which a close-call test alone would
    never look at.

    Measured on 400 cards with the name deliberately corrupted and the body
    text read at 20% error: at 45% name corruption this lifts accuracy from
    73.2% to 79.0%, and across every level tested it never demoted a correct
    top match.
    """
    if not candidates or len(candidates) < 2 or not ocr_text:
        return candidates, None

    top, second = candidates[0][1], candidates[1][1]
    if top - second > margin and top >= low_confidence:
        return candidates, None

    scored = []
    for card, score in candidates:
        support = corroborate(ocr_text, card)
        adjusted = score if support is None else score + weight * support
        scored.append((adjusted, score, support, card))
    scored.sort(key=lambda t: -t[0])

    reordered = [(card, score) for _adj, score, _sup, card in scored]
    detail = {"changed": reordered[0][0]["name"] != candidates[0][0]["name"],
              "support": scored[0][2],
              "runner_support": scored[1][2] if len(scored) > 1 else None}
    return reordered, detail


def describe(card):
    sets = card["printings"]
    if not sets:
        return card["name"]
    codes = []
    for code, num, _rarity in sets[:4]:
        codes.append(f"{code}#{num}")
    more = "" if len(sets) <= 4 else f" +{len(sets) - 4}"
    return f"{card['name']}  [{', '.join(codes)}{more}]"


if __name__ == "__main__":
    import sys
    import time

    idx = CardIndex()
    print(f"loaded {len(idx.cards)} names")
    for q in sys.argv[1:]:
        t0 = time.perf_counter()
        res = idx.lookup(q)
        dt = (time.perf_counter() - t0) * 1000
        print(f"\n  '{q}'   ({dt:.1f} ms)")
        for card, score in res:
            print(f"    {score:.3f}  {describe(card)}")
