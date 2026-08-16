"""Scan Magic cards with a phone camera and log them.

The phone runs IP Webcam, which serves frames over HTTP. This polls the low
resolution stream, waits until a card is placed and the image stops moving,
then grabs one full resolution still, reads it, and appends it to the log.

  python scan.py --url http://192.168.1.100:8080     live scanning
  python scan.py --image photo.jpg                   test on one photo
  python scan.py --url ... --probe                   check the connection
  python scan.py --url ... --setup                   apply good camera settings

Recognition works in three steps: locate the card in the frame, read the name
bar, then read the collector line at the bottom to pin down which printing it
is. Nothing is written to the phone.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
from datetime import date

from PIL import Image, ImageChops, ImageOps

import carddetect
import cardlookup
import paths
from cardlookup import CardIndex, describe
from ocr import Ocr

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = paths.LOG
LOG_HEADER = ["date", "action", "name", "set", "number", "foil", "qty", "source", "note"]

AUTO_SCORE = 0.86       # accept without asking above this match score
AUTO_GAP = 0.06         # ...and this much clear of the runner up
NAME_ZONE = 0.45        # name lines live in the top of the frame

RE_COLLECTOR_PAIR = re.compile(r"\b(\d{1,4})\s*/\s*(\d{1,4})\b")
RE_COLLECTOR_SOLO = re.compile(r"\b(\d{3,4})\b")
RE_SETCODE = re.compile(r"\b([A-Z0-9]{3,5})\b")
RE_RARITY = re.compile(r"\b([CURMSTL])\b")

# Order matters: the phone clamps photo_size against video_size, so the preview
# has to be set first. Measured on a 12MP phone at quality 50:
#   video 1280x960  -> photo 4096x3072 allowed, preview 27 fps
#   video 1920x1440 -> photo clamped to 3840x2160, preview 23 fps
# The capture is what OCR reads, and 4096x3072 gives 3072 rows down the card
# against 2160 for the 16:9 alternative, so the smaller preview is the better
# trade. Quality above ~50 makes the stream choppy for no gain in readability.
CAMERA_SETTINGS = [
    ("quality", "50"),
    ("video_size", "1280x960"),
    ("photo_size", "4096x3072"),
    ("focusmode", "continuous-picture"),
]

# fallbacks if the phone rejects the preferred value
PHOTO_FALLBACKS = ["4096x3072", "4000x3000", "3840x2880", "2560x1920", "1920x1440"]


# --------------------------------------------------------------------------- io

def fetch(url, timeout=25):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


class Phone:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def setting(self, key, value):
        try:
            fetch(f"{self.base}/settings/{key}?set={value}", timeout=10)
            return True
        except Exception:
            return False

    def status(self):
        """Current camera settings as reported by the phone."""
        try:
            raw = fetch(f"{self.base}/status.json", timeout=10)
            return json.loads(raw.decode("utf-8", "replace")).get("curvals", {})
        except Exception:
            return {}

    def configure(self, verify=True):
        """Apply the camera settings, in order, and confirm they took.

        photo_size is clamped against video_size, so it is applied last and
        checked; if the phone quietly substituted something smaller we walk
        down the fallback list.
        """
        ok = {}
        for k, v in CAMERA_SETTINGS:
            ok[k] = self.setting(k, v)
            time.sleep(0.4)

        if not verify:
            return ok

        time.sleep(1.0)
        cur = self.status()
        want = dict(CAMERA_SETTINGS)
        got = cur.get("photo_size")
        if got and got != want["photo_size"]:
            for candidate in PHOTO_FALLBACKS:
                if candidate == got:
                    break
                self.setting("photo_size", candidate)
                time.sleep(0.8)
                if self.status().get("photo_size") == candidate:
                    ok["photo_size"] = True
                    break
            else:
                ok["photo_size"] = False
        self.settings_applied = self.status()
        return ok

    def focus(self):
        try:
            fetch(f"{self.base}/focus", timeout=5)
        except Exception:
            pass

    def grab(self, full=True, retries=2):
        """Fetch a frame, fully decoded.

        Image.open is lazy: a truncated JPEG only raises when something later
        touches the pixels, which used to be on the UI thread. Decoding here
        means a bad frame fails in the worker that fetched it, where it can be
        retried or dropped harmlessly.
        """
        url = f"{self.base}/photo.jpg" if full else f"{self.base}/shot.jpg"
        last = None
        for attempt in range(retries + 1):
            try:
                raw = fetch(url)
                if len(raw) < 1024:
                    raise OSError(f"short read ({len(raw)} bytes)")
                img = Image.open(io.BytesIO(raw))
                img.load()
                return img
            except Exception as e:
                last = e
                if attempt < retries:
                    time.sleep(0.12)
        raise last


# ------------------------------------------------------------------------- ocr

_seq = [0]


def ocr_image(img, ocr, tmpdir):
    """Write to BMP and OCR.

    BMP matters: encoding a 2560px PNG costs ~800 ms, a BMP ~9 ms, and the OCR
    engine reads both identically.

    Filenames never repeat. The Windows imaging stack can still hold a handle on
    the previous file when the next frame arrives, and reusing a name then fails
    with EINVAL mid-scan. Old files are swept up as we go.
    """
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    _seq[0] += 1
    n = _seq[0]
    path = os.path.join(tmpdir, f"f{n:08d}.bmp")
    img.save(path)
    try:
        return ocr.read(path)
    finally:
        if n % 25 == 0:
            _sweep(tmpdir, keep_after=n - 10)


def _sweep(tmpdir, keep_after):
    """Delete temp frames the OCR engine has finished with."""
    try:
        for fn in os.listdir(tmpdir):
            if not fn.startswith("f") or not fn.endswith(".bmp"):
                continue
            try:
                if int(fn[1:-4]) < keep_after:
                    os.remove(os.path.join(tmpdir, fn))
            except (ValueError, OSError):
                pass
    except OSError:
        pass


def enlarge(crop, target_h, min_factor=2):
    """Upscale a crop so characters clear the engine's minimum height.

    `target_h` is the wanted height of the whole crop, not of the text in it, so
    aim high: the collector line is only a fraction of its strip.
    """
    if crop.height <= 0 or crop.width <= 0:
        return None
    f = max(min_factor, min(6, int(round(target_h / max(crop.height, 1)))))
    if f > 1:
        crop = crop.resize((crop.width * f, crop.height * f), Image.LANCZOS)
    return ImageOps.autocontrast(ImageOps.grayscale(crop))


# ------------------------------------------------------------------ name / set

def match_lines(lines, index, limit_y=None, frame_h=1):
    """Best card match across a set of OCR lines."""
    best = None
    for ln in lines:
        text = (ln.get("text") or "").strip()
        if len(text) < 3:
            continue
        if limit_y is not None and ln.get("y", 0) / max(frame_h, 1) > limit_y:
            continue
        hits = index.lookup(text, limit=5)
        if not hits:
            continue
        card, score = hits[0]
        runner = hits[1][1] if len(hits) > 1 else 0.0
        if best is None or score > best["score"]:
            best = {"card": card, "score": score, "runner": runner,
                    "text": text, "line": ln, "candidates": hits}
    return best


BODY_CHECK_SCORE = 0.90     # below this, ask the rules text for a second opinion
BODY_CHECK_MARGIN = 0.08    # or when the runner-up is this close


def needs_body_check(candidates):
    if not candidates or len(candidates) < 2:
        return False
    top, second = candidates[0][1], candidates[1][1]
    return top < BODY_CHECK_SCORE or (top - second) <= BODY_CHECK_MARGIN


# Recognisers to try when the English one fails. Japanese needs a Windows
# language pack; macOS Vision has it already. Cards it cannot read are still
# caught by the collector line or by artwork matching.
FOREIGN_ENGINES = ["zh-Hans-CN", "ja", "ko", "ru"]

LANG_LABEL = {"zhs": "S-Chinese", "zht": "T-Chinese", "ja": "Japanese",
              "ko": "Korean", "ru": "Russian", "de": "German",
              "fr": "French", "it": "Italian", "es": "Spanish",
              "pt": "Portuguese"}


def try_foreign_name(view, ocr, index, tmpdir):
    """Re-read the name bar with non-English recognisers."""
    regions = carddetect.regions((0, 0, view.width, view.height))
    crop = enlarge(view.crop(regions["name"]), 120)
    if crop is None:
        return None

    supports = getattr(ocr, "supports", lambda _l: True)
    best = None
    for lang in FOREIGN_ENGINES:
        if not supports(lang):
            continue
        try:
            res = ocr_image_lang(crop, ocr, tmpdir, lang)
        except Exception:
            continue
        for line in res.get("lines", []):
            text = (line.get("text") or "").strip()
            if len(text) < 2:
                continue
            if cardlookup.detect_script(text) == "latin":
                continue          # the English pass already handled this
            hits = index.lookup_localised(text, limit=3)
            if not hits:
                continue
            card, score, code = hits[0]
            if best is None or score > best["score"]:
                best = {"card": card, "score": score,
                        "runner": hits[1][1] if len(hits) > 1 else 0.0,
                        "text": text, "line": line,
                        "candidates": [(c, s) for c, s, _ in hits],
                        "language": LANG_LABEL.get(code, code)}
    return best


def ocr_image_lang(img, ocr, tmpdir, lang):
    """OCR with a specific recogniser, falling back to the default."""
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    _seq[0] += 1
    path = os.path.join(tmpdir, f"f{_seq[0]:08d}.bmp")
    img.save(path)
    try:
        return ocr.read(path, lang=lang)
    except TypeError:
        return ocr.read(path)


ART_FALLBACK_SCORE = 0.90     # try artwork when the text is less sure than this

# Choosing between printings of a known card is an easier problem than
# identifying the card, so these are looser than the whole-index thresholds -
# but a clear gap is still required, since reprints often share artwork and
# then no amount of looking will separate them.
ART_PRINTING_MAX = 18
ART_PRINTING_GAP = 4

_art_index = [None, False]    # cached index, and whether we already tried


def art_index():
    """Load the artwork index once, if it has been built."""
    if not _art_index[1]:
        _art_index[1] = True
        try:
            import arthash
            _art_index[0] = arthash.load()
        except Exception:
            _art_index[0] = None
    return _art_index[0]


def rank_printings_by_art(full, box, rotated, card):
    """Order this card's printings by how well each one's art matches the photo.

    Without it the first printing wins by default, which is wrong whenever a
    card exists in several treatments - and those variants are usually the ones
    where the price differs most.
    """
    idx = art_index()
    if idx is None or not card:
        return []
    prints = card.get("printings") or []
    if len(prints) < 2:
        return []
    try:
        view = full.crop(box)
        if rotated:
            view = view.transpose(Image.ROTATE_180)
        return idx.rank_printings(view, prints)
    except Exception:
        return []


def art_match(full, box, rotated, index):
    """Identify the card by its artwork."""
    idx = art_index()
    if idx is None:
        return None
    try:
        import arthash

        view = full.crop(box)
        if rotated:
            view = view.transpose(Image.ROTATE_180)
        results = idx.match(view, limit=4)
        if not results:
            return None
        code, num, dist = results[0]
        card, _unique = index.by_printing(code, num)
        if card is None:
            return None
        # 0 bits apart is a perfect match, WEAK bits apart is worthless
        score = max(0.0, 1.0 - dist / float(arthash.WEAK))
        return {"card": card, "setcode": code, "number": num,
                "distance": dist, "score": round(min(score, 0.97), 3),
                "confident": idx.confident(results),
                "runners": results[1:]}
    except Exception:
        return None


def read_body(view, ocr, tmpdir, width=900):
    """OCR the whole card, for the type line and rules text."""
    try:
        scale = width / max(view.width, 1)
        work = view.resize((width, max(1, int(view.height * scale))),
                           Image.LANCZOS)
        work = ImageOps.autocontrast(work)
        res = ocr_image(work, ocr, tmpdir)
        return "\n".join(l.get("text", "") for l in res.get("lines", []))
    except Exception:
        return ""


def parse_collector(lines):
    """Pull the collector number, set code and rarity from the bottom strip."""
    number, setcode, rarity = None, None, None
    for ln in lines:
        t = (ln.get("text") or "").strip()
        if not t:
            continue
        up = t.upper()
        if number is None:
            m = RE_COLLECTOR_PAIR.search(t) or RE_COLLECTOR_SOLO.search(t)
            if m:
                number = m.group(1).lstrip("0") or "0"
        if setcode is None:
            for cand in RE_SETCODE.findall(up):
                if cand.isdigit():
                    continue
                if cand in ("EN", "DE", "FR", "IT", "ES", "JP", "PT", "RU", "KO", "CN"):
                    continue
                setcode = cand
                break
        if rarity is None:
            m = RE_RARITY.search(up)
            if m:
                rarity = m.group(1)
    return number, setcode, rarity


COLLECTOR_BANDS = ((0.920, 1.000), (0.895, 0.965), (0.940, 1.000))


def read_collector_line(view, ocr, tmpdir):
    """Read the collector line, trying a few crops and both polarities.

    Borderless and full-art cards print this line in white over the artwork, so
    a single fixed crop at one contrast misses a lot of them.
    """
    w, h = view.width, view.height
    best_lines = []
    for lo, hi in COLLECTOR_BANDS:
        crop = view.crop((int(w * 0.02), int(h * lo), int(w * 0.62), int(h * hi)))
        base = enlarge(crop, 300, min_factor=3)
        if base is None:
            continue
        for variant in (base, ImageOps.invert(base)):
            res = ocr_image(variant, ocr, tmpdir)
            lines = res.get("lines", [])
            number, setcode, rarity = parse_collector(lines)
            if number and setcode:
                return number, setcode, rarity, lines
            if lines and not best_lines:
                best_lines = lines
    number, setcode, rarity = parse_collector(best_lines)
    return number, setcode, rarity, best_lines


CARD_LANGS = {
    "EN": "English", "DE": "German", "FR": "French", "IT": "Italian",
    "ES": "Spanish", "PT": "Portuguese", "JA": "Japanese", "JP": "Japanese",
    "KO": "Korean", "RU": "Russian", "ZHS": "S-Chinese", "ZHT": "T-Chinese",
    "CS": "S-Chinese", "CT": "T-Chinese",
}


def parse_language(lines):
    """Read the two-letter language code printed on the collector line."""
    for ln in lines:
        for tok in re.findall(r"\b([A-Z]{2,3})\b", (ln.get("text") or "").upper()):
            if tok in CARD_LANGS:
                return CARD_LANGS[tok]
    return None


def pick_printing(card, number, setcode):
    """Choose the printing that best matches what we read off the card."""
    prints = card.get("printings") or []
    if not prints:
        return (None, None, None)
    if setcode:
        same_set = [p for p in prints if p[0].upper() == setcode.upper()]
        if same_set:
            if number:
                for p in same_set:
                    if p[1].lstrip("0") == number:
                        return p
            return same_set[0]
    if number:
        for p in prints:
            if p[1].lstrip("0") == number:
                return p
    return prints[0]


# ------------------------------------------------------------------ recognise

def recognise(img, ocr, index, tmpdir, verbose=False):
    """Locate the card, read its name and its collector line."""
    full = img if img.mode == "RGB" else img.convert("RGB")
    box = carddetect.detect(full)
    info = {"box": box, "name_lines": [], "collector_lines": []}

    if box:
        card_img = full.crop(box)
        # a card dropped under the camera lands either way up, so try both and
        # keep whichever reads better
        best = None
        for rot in (0, 180):
            view = card_img if rot == 0 else card_img.transpose(Image.ROTATE_180)
            r = carddetect.regions((0, 0, view.width, view.height))
            crop = enlarge(view.crop(r["name"]), 120)
            if crop is None:
                continue
            res = ocr_image(crop, ocr, tmpdir)
            lines = res.get("lines", [])
            cand = match_lines(lines, index)
            score = cand["score"] if cand else 0.0
            if best is None or score > best["score"]:
                best = {"score": score, "guess": cand, "lines": lines,
                        "view": view, "regions": r, "rot": rot}

        guess = best["guess"] if best else None
        info["rotated"] = bool(best and best["rot"])
        info["name_lines"] = best["lines"] if best else []

        # When the name did not read cleanly, read the rest of the card and let
        # the rules text vote. Only done when needed, so a clean read stays fast.
        # A name printed in another script will not match the English index at
        # all, so re-read the name bar with a recogniser for that script.
        if best and (not guess or guess["score"] < 0.80):
            foreign = try_foreign_name(best["view"], ocr, index, tmpdir)
            if foreign and (not guess or foreign["score"] > guess["score"]):
                guess = foreign
                info["resolved_by"] = "localised name"
                info["language"] = foreign.get("language")

        candidates = (guess or {}).get("candidates") or []
        if best and guess and needs_body_check(candidates):
            body_text = read_body(best["view"], ocr, tmpdir)
            info["body_text"] = body_text
            if body_text:
                reordered, detail = cardlookup.rerank(candidates, body_text)
                info["body_detail"] = detail
                if reordered and reordered[0][0] is not guess["card"]:
                    guess = dict(guess)
                    guess["card"] = reordered[0][0]
                    guess["score"] = reordered[0][1]
                    guess["resolved_by"] = "body text"
                    info["resolved_by"] = "body"

        number = setcode = rarity = None
        if best:
            number, setcode, rarity, lines = read_collector_line(
                best["view"], ocr, tmpdir)
            info["collector_lines"] = lines
    else:
        # no card found: fall back to reading the whole frame
        work = full
        if work.width > 1600:
            s = 1600 / work.width
            work = work.resize((1600, int(work.height * s)), Image.LANCZOS)
        work = ImageOps.autocontrast(work)
        res = ocr_image(work, ocr, tmpdir)
        info["name_lines"] = res.get("lines", [])
        guess = match_lines(info["name_lines"], index, NAME_ZONE, work.height)
        number, setcode, rarity = parse_collector(info["name_lines"])

    # Last resort: match the artwork. Independent of language and of whether
    # any text could be read at all, so it catches cards the OCR cannot touch.
    if box and (guess is None or guess["score"] < ART_FALLBACK_SCORE):
        art = art_match(full, box, info.get("rotated"), index)
        if art:
            info["art"] = art
            if guess is None or art["confident"]:
                guess = {"card": art["card"], "score": art["score"],
                         "runner": 0.0, "text": f"artwork {art['distance']}",
                         "line": None, "candidates": []}
                info["resolved_by"] = "artwork"
                if not number and art.get("setcode"):
                    number, setcode = art["number"], art["setcode"]

    # With no collector number the printing would otherwise be whichever came
    # first alphabetically. Let the artwork choose instead.
    if guess and box and not number:
        ranked = rank_printings_by_art(full, box, info.get("rotated"),
                                       guess["card"])
        if ranked:
            info["art_printings"] = ranked
            best_code, best_num, dist = ranked[0]
            runner = ranked[1][2] if len(ranked) > 1 else 64
            # only when the winner is both close and clearly ahead
            if dist <= ART_PRINTING_MAX and (runner - dist) >= ART_PRINTING_GAP:
                setcode, number = best_code, best_num
                info["printing_by"] = "artwork"
                info["printing_distance"] = dist

    language = parse_language(info.get("collector_lines") or info.get("name_lines") or [])
    info.update(number=number, setcode=setcode, rarity=rarity, language=language)

    # Fall back to the collector line when the name did not read well. This is
    # what makes Japanese and Chinese cards work: their names are printed in
    # characters we cannot match against an English index, but the set code and
    # collector number are in Latin script on every card.
    if (guess is None or guess["score"] < AUTO_SCORE) and number and setcode:
        card, unique = index.by_printing(setcode, number)
        if card is not None:
            by_name_score = guess["score"] if guess else 0.0
            if unique and by_name_score < AUTO_SCORE:
                info["resolved_by"] = "collector"
                guess = {"card": card, "score": 0.95 if unique else 0.80,
                         "runner": 0.0, "text": f"{setcode} {number}",
                         "line": guess["line"] if guess else None}

    return guess, info


def confident(guess):
    return (guess is not None
            and guess["score"] >= AUTO_SCORE
            and (guess["score"] - guess["runner"]) >= AUTO_GAP)


# --------------------------------------------------------------------- logging

def append_log(row):
    exists = os.path.exists(LOG)
    with open(LOG, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if not exists:
            w.writerow(LOG_HEADER)
        w.writerow(row)


def log_card(card, printing, foil, qty, source, note=""):
    code, num, _r = printing
    append_log([date.today().isoformat(), "+", card["name"], code or "",
                num or "", "foil" if foil else "", qty, source, note])


# ------------------------------------------------------------------- live loop

def thumb(img, size=(160, 120)):
    return img.convert("L").resize(size)


def frame_diff(a, b):
    if a is None or b is None:
        return 255.0
    d = ImageChops.difference(a, b)
    hist = d.histogram()
    return sum(i * n for i, n in enumerate(hist)) / max(sum(hist), 1)


def live(phone, ocr, index, args):
    tmpdir = tempfile.mkdtemp(prefix="cardscan_")
    print(f"\nconnected to {phone.base}")
    print("place a card under the camera. ctrl-c to stop.\n")
    prev, settled, last = None, False, None
    count, pending = 0, []

    try:
        while True:
            try:
                small = thumb(phone.grab(full=False))
            except Exception as e:
                print(f"  ! preview failed: {e}")
                time.sleep(1.0)
                continue

            motion = frame_diff(prev, small)
            prev = small
            if motion > args.motion:
                settled = False
                time.sleep(args.poll)
                continue
            if settled:
                time.sleep(args.poll)
                continue

            try:
                full = phone.grab(full=True)
            except Exception as e:
                print(f"  ! capture failed: {e}")
                time.sleep(1.0)
                continue

            try:
                guess, info = recognise(full, ocr, index, tmpdir)
            except Exception as e:
                # never let one bad frame end a long scanning session
                print(f"  !   recognition error, skipping frame: {e}")
                settled = True
                time.sleep(args.poll)
                continue
            settled = True

            if not guess:
                print("  ?   no card name found - reposition and try again")
                continue

            card = guess["card"]
            if card["name"] == last and not args.allow_repeat:
                print(f"  =   {card['name']} (same as last, ignored)")
                continue

            printing = pick_printing(card, info["number"], info["setcode"])
            code, num, _r = printing
            where = f"{code}#{num}" if code else "?"

            if confident(guess):
                count += 1
                last = card["name"]
                log_card(card, printing, args.foil, 1, "scan")
                print(f"  {count:4}  {card['name']:<38} {where:<12} {guess['score']:.2f}")
            else:
                pending.append((guess, info))
                print(f"  ??   {card['name']:<38} {where:<12} {guess['score']:.2f}"
                      f"  (read '{guess['text']}')")

            time.sleep(args.poll)

    except KeyboardInterrupt:
        print(f"\nstopped. {count} card(s) logged to {LOG}")
        if pending:
            print(f"\n{len(pending)} uncertain, not logged:")
            for g, _i in pending:
                print(f"   read '{g['text']}'  best guess {g['card']['name']} ({g['score']:.2f})")


# ----------------------------------------------------------------------- entry

def main():
    ap = argparse.ArgumentParser(description="Scan Magic cards with a phone camera.")
    ap.add_argument("--url", help="IP Webcam base URL")
    ap.add_argument("--image", help="process one image file instead")
    ap.add_argument("--probe", action="store_true", help="test the connection and exit")
    ap.add_argument("--setup", action="store_true", help="apply camera settings and exit")
    ap.add_argument("--foil", action="store_true", help="log this run as foil")
    ap.add_argument("--motion", type=float, default=2.0, help="stillness threshold")
    ap.add_argument("--poll", type=float, default=0.25, help="seconds between preview frames")
    ap.add_argument("--allow-repeat", action="store_true")
    args = ap.parse_args()

    if not args.url and not args.image:
        ap.error("give either --url or --image")

    if args.url and (args.setup or args.probe):
        phone = Phone(args.url)
        if args.setup:
            for k, ok in phone.configure().items():
                print(f"  {k:12} {'set' if ok else 'FAILED'} -> {CAMERA_SETTINGS[k]}")
            time.sleep(2)
        try:
            t0 = time.perf_counter()
            im = phone.grab(full=True)
            print(f"  still   {im.width}x{im.height}  {(time.perf_counter()-t0)*1000:.0f} ms")
            t0 = time.perf_counter()
            p = phone.grab(full=False)
            print(f"  preview {p.width}x{p.height}  {(time.perf_counter()-t0)*1000:.0f} ms")
        except Exception as e:
            print(f"  FAILED to reach {args.url}: {e}")
            return 1
        return 0

    index = CardIndex()
    print(f"card index: {len(index.cards)} names")

    if args.image:
        with Ocr() as ocr, tempfile.TemporaryDirectory() as tmp:
            img = Image.open(args.image)
            print(f"image: {img.width}x{img.height}")
            t0 = time.perf_counter()
            guess, info = recognise(img, ocr, index, tmp)
            dt = (time.perf_counter() - t0) * 1000
            print(f"recognised in {dt:.0f} ms")
            print(f"card box: {info['box']}")
            print(f"name lines     : {[l['text'] for l in info['name_lines']][:4]}")
            print(f"collector lines: {[l['text'] for l in info['collector_lines']]}")
            print(f"number={info['number']}  set={info['setcode']}  rarity={info['rarity']}")
            print()
            if guess:
                card = guess["card"]
                p = pick_printing(card, info["number"], info["setcode"])
                mark = "AUTO" if confident(guess) else "ASK "
                print(f"{mark}  {card['name']}   score {guess['score']:.3f} "
                      f"(runner up {guess['runner']:.3f})")
                print(f"      printing chosen: {p[0]}#{p[1]} ({p[2]})")
                print(f"      {describe(card)}")
            else:
                print("no card identified")
        return 0

    phone = Phone(args.url)
    with Ocr() as ocr:
        live(phone, ocr, index, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
