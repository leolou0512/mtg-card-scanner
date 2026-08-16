"""Interactive card cataloguer.

Run it, type the phone's address, then feed cards under the camera one at a
time. You press a key to shoot, then accept or reject what it read.

    python catalogue.py

Accepted cards are appended to collection_log.csv.
Rejected cards go to rejects.csv, with a photo saved alongside, so you can
catalogue them by hand later.
"""
import csv
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import date, datetime

import carddetect
import paths
import scan
from cardlookup import CardIndex
from ocr import Ocr

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = paths.PROJECT
LOG = os.path.join(ROOT, "collection_log.csv")
REJECTS = os.path.join(ROOT, "rejects.csv")
REJECT_DIR = os.path.join(ROOT, "rejects")
CONFIG = os.path.join(HERE, ".catalogue.json")

LOG_HEADER = ["date", "action", "name", "set", "set_name", "number", "rarity",
              "language", "foil", "qty", "source", "note"]
REJECT_HEADER = ["timestamp", "reason", "best_guess", "score", "set", "number",
                 "ocr_text", "photo"]

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"


def enable_ansi():
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)
    except Exception:
        pass


def getkey():
    """Read a single keypress without waiting for Enter.

    Windows has msvcrt; macOS and Linux need the terminal put into raw mode for
    the duration of the read, otherwise the line is buffered until Enter.
    """
    try:
        import msvcrt
    except ImportError:
        pass
    else:
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):      # function/arrow key: eat the second byte
            msvcrt.getch()
            return ""
        try:
            return ch.decode("utf-8", "ignore").lower()
        except Exception:
            return ""

    try:
        import termios
        import tty
    except ImportError:
        return (sys.stdin.readline().strip()[:1] or "\r").lower()

    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:
        return (sys.stdin.readline().strip()[:1] or "\r").lower()
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":                  # escape sequence: discard the rest
            import select
            while select.select([sys.stdin], [], [], 0.02)[0]:
                sys.stdin.read(1)
            return ""
        if ch == "\x03":                  # ctrl-c must still interrupt
            raise KeyboardInterrupt
        return ch.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


# ------------------------------------------------------------------- settings

def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
    except Exception:
        pass


def normalise_url(raw):
    raw = raw.strip().rstrip("/")
    raw = re.sub(r"^https?://", "", raw)
    if not raw:
        return None
    host, _, port = raw.partition(":")
    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host) and not re.match(r"^[\w.-]+$", host):
        return None
    return f"http://{host}:{port or '8080'}"


def ask_address(default=None):
    prompt = f"Phone address [{default}]: " if default else "Phone address (e.g. 192.168.1.189): "
    while True:
        raw = input(prompt).strip()
        if not raw and default:
            raw = default
        if not raw:
            continue
        url = normalise_url(raw)
        if url:
            return url
        print("  not a valid address - try 192.168.1.189 or 192.168.1.189:8080")


# -------------------------------------------------------------------- logging

def append(path, header, row):
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if not exists:
            w.writerow(header)
        w.writerow(row)


def read_log():
    if not os.path.exists(LOG):
        return [], []
    with open(LOG, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return [], []
    return rows[0], rows[1:]


def rewrite_last(new_row):
    """Replace the final data row, or drop it when new_row is None."""
    header, body = read_log()
    if not body:
        return False
    body = body[:-1] if new_row is None else body[:-1] + [new_row]
    with open(LOG, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header or LOG_HEADER)
        w.writerows(body)
    return True


def make_row(card, printing, language, foil, qty, sets):
    code, num, rarity = printing
    return [date.today().isoformat(), "+", card["name"], code or "",
            sets.set_name(code) if code else "", num or "", rarity or "",
            language or "", "foil" if foil else "", qty, "scan", ""]


# -------------------------------------------------------------------- display

RE_ANSI = re.compile(r"\033\[[0-9;]*m")


def visible_len(s):
    return len(RE_ANSI.sub("", s))


def clip(s, width):
    """Truncate to a visible width, ignoring colour codes.

    Wrapped lines would break the redraw, because moving the cursor up by the
    number of strings printed only works if each occupies exactly one row.
    """
    if visible_len(s) <= width:
        return s
    out, shown = [], 0
    i = 0
    while i < len(s):
        m = RE_ANSI.match(s, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        if shown >= width - 1:
            break
        out.append(s[i])
        shown += 1
        i += 1
    return "".join(out) + RESET


class Panel:
    """A block of terminal lines that can be redrawn in place."""

    def __init__(self):
        self.drawn = 0

    def draw(self, lines):
        width = max(40, shutil.get_terminal_size((80, 25)).columns - 1)
        lines = [clip(l, width) for l in lines]
        buf = []
        if self.drawn:
            buf.append(f"\033[{self.drawn}A\033[J")
        buf.append("\n".join(lines))
        buf.append("\n")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        self.drawn = len(lines)

    def release(self):
        """Leave the current block on screen; the next draw starts fresh."""
        self.drawn = 0


def set_label(index, code):
    """'Edge of Eternities (EOE, 2025)' rather than a bare code."""
    if not code:
        return "?"
    name = index.set_name(code)
    year = index.set_year(code)
    if name and name != code:
        return f"{name}  {DIM}({code}{', ' + year if year else ''}){RESET}"
    return code


def render(index, card, info, guess, foil, qty, count, printing, picking=False,
           confirmed=False):
    """Build the card panel as a list of single-row strings."""
    code, num, rarity = printing if printing else (None, None, None)
    rarity = rarity or "?"
    lang = info.get("language") or "English (assumed)"
    score = guess["score"] if guess else 0.0
    how = "collector line" if info.get("resolved_by") == "collector" else "card name"
    prints = (card.get("printings") or []) if card else []
    # only a guess while the collector number is unread and you have not picked
    unsure = len(prints) > 1 and not info.get("number") and not confirmed

    colour = GREEN if score >= scan.AUTO_SCORE else YELLOW
    out = ["", "=" * 70,
           f" {BOLD}{card['name'] if card else 'UNRECOGNISED'}{RESET}",
           "=" * 70]

    if card:
        flag = (f"   {YELLOW}<- GUESS, press p{RESET}" if unsure
                else (f"   {GREEN}(picked){RESET}" if confirmed else ""))
        out += [
            f"  set         {set_label(index, code)}",
            f"  number      #{num or '?'}   {rarity}{flag}",
            f"  language    {lang}",
            f"  foil        {(GREEN + BOLD + 'YES' + RESET) if foil else DIM + 'no' + RESET}"
            f"   {DIM}[f]{RESET}",
            f"  quantity    {BOLD}{qty}{RESET}   {DIM}[1-9]{RESET}",
            f"  confidence  {colour}{score:.2f}{RESET}  (matched on {how})",
            f"  {DIM}read as     '{guess['text'] if guess else ''}'{RESET}",
        ]
    else:
        lines = [l.get("text", "") for l in info.get("name_lines", [])][:3]
        out += [f"  {RED}could not identify this card{RESET}",
                f"  {DIM}text read: {lines}{RESET}"]
        if info.get("setcode") or info.get("number"):
            out.append(f"  {DIM}collector: {info.get('setcode')} "
                       f"{info.get('number')}{RESET}")

    if picking and prints:
        out.append("-" * 70)
        out.append(f"  {BOLD}pick a printing{RESET}")
        for i, (c, n, r) in enumerate(prints[:9], 1):
            mark = f"  {GREEN}<- current{RESET}" if (c, n) == (code, num) else ""
            out.append(f"    {BOLD}{i}{RESET}  {c}#{n:<9} {r:<9} "
                       f"{index.set_name(c)}{mark}")
        if len(prints) > 9:
            out.append(f"    {DIM}({len(prints)-9} more not shown){RESET}")
        out.append("-" * 70)
        out.append(f"  {DIM}press 1-{min(9, len(prints))}, or any other key to cancel{RESET}")
        return out

    if card and len(prints) > 1:
        alt = ", ".join(f"{c}#{n}" for c, n, _ in prints[:8])
        if len(prints) > 8:
            alt += f"  (+{len(prints)-8})"
        out.append(f"  {DIM}printings   {alt}{RESET}")

    out.append("-" * 70)
    pick = f"   {BOLD}[p]{RESET} printing" if len(prints) > 1 else ""
    out.append(f"  {BOLD}[enter]{RESET} accept   {BOLD}[r]{RESET} reject   "
               f"{BOLD}[f]{RESET} foil   {BOLD}[1-9]{RESET} qty{pick}   "
               f"{BOLD}[q]{RESET} quit   {DIM}ok: {count}{RESET}")
    return out


# ---------------------------------------------------------------- edit last

def edit_last(index, state):
    """Change or delete the most recent accepted entry."""
    header, body = read_log()
    if not body:
        print(f"  {YELLOW}nothing logged yet{RESET}")
        return
    last = dict(zip(header or LOG_HEADER, body[-1]))
    card = state.get("card")
    printing = state.get("printing")
    foil = (last.get("foil") == "foil")
    try:
        qty = int(last.get("qty") or 1)
    except ValueError:
        qty = 1

    prints = (card.get("printings") or []) if card else []
    can_pick = len(prints) > 1
    panel = Panel()
    picking = False

    while True:
        out = ["", "-" * 70,
               f"  {BOLD}last entry{RESET}   {last.get('name')}",
               f"    set       {set_label(index, last.get('set'))}",
               f"    number    #{last.get('number')}   {last.get('rarity') or '?'}",
               f"    foil      {(GREEN + BOLD + 'YES' + RESET) if foil else DIM + 'no' + RESET}",
               f"    quantity  {BOLD}{qty}{RESET}",
               "-" * 70]
        if picking:
            out.append(f"  {BOLD}pick a printing{RESET}")
            for i, (c, n, r) in enumerate(prints[:9], 1):
                mark = (f"  {GREEN}<- current{RESET}"
                        if (c, n) == (last.get("set"), last.get("number")) else "")
                out.append(f"    {BOLD}{i}{RESET}  {c}#{n:<9} {r:<9} "
                           f"{index.set_name(c)}{mark}")
            out.append("-" * 70)
            out.append(f"  {DIM}press 1-{min(9, len(prints))}, or any other key to cancel{RESET}")
        else:
            out.append(f"  {BOLD}[f]{RESET} foil   {BOLD}[1-9]{RESET} qty"
                       f"{'   ' + BOLD + '[p]' + RESET + ' printing' if can_pick else ''}"
                       f"   {BOLD}[d]{RESET} delete   {BOLD}[x]{RESET} done")
        panel.draw(out)

        k = getkey()

        if picking:
            if k and k.isdigit() and k != "0":
                i = int(k)
                if 1 <= i <= min(9, len(prints)):
                    printing = prints[i - 1]
                    last["set"] = printing[0] or ""
                    last["set_name"] = index.set_name(printing[0]) if printing[0] else ""
                    last["number"] = printing[1] or ""
                    last["rarity"] = printing[2] or ""
                    state["printing"] = printing
            picking = False
            continue

        if k == "f":
            foil = not foil
            last["foil"] = "foil" if foil else ""
        elif k and k.isdigit() and k != "0":
            qty = int(k)
            last["qty"] = qty
        elif k == "p" and can_pick:
            picking = True
        elif k == "d":
            rewrite_last(None)
            panel.release()
            print(f"  {RED}deleted{RESET} {last.get('name')}")
            return
        elif k in ("x", "\r", "\n", "q"):
            row = [last.get(c, "") for c in (header or LOG_HEADER)]
            rewrite_last(row)
            panel.draw(out)
            panel.release()
            print(f"  {GREEN}saved{RESET}  {last.get('name')}  "
                  f"{last.get('set')}#{last.get('number')}  x{qty}"
                  f"{' foil' if foil else ''}")
            return


# ----------------------------------------------------------------------- main

def main():
    enable_ansi()
    cfg = load_config()

    print()
    print(f"{BOLD}Card cataloguer{RESET}")
    print(f"{DIM}Open IP Webcam on the phone, tap 'Start server', enter the address it shows.{RESET}")
    print()
    url = ask_address(cfg.get("url"))
    cfg["url"] = url
    save_config(cfg)

    phone = scan.Phone(url)
    print(f"\nconnecting to {url} ...")
    try:
        img = phone.grab(full=True)
    except Exception as e:
        print(f"{RED}could not reach the phone: {e}{RESET}")
        print("check the phone is on the same wifi and the server is running.")
        return 1
    print(f"  connected. still image {img.width}x{img.height}")

    print("  applying camera settings ...")
    for k, ok in phone.configure().items():
        print(f"    {k:12} {'ok' if ok else 'skipped'}")
    time.sleep(2)

    print("  loading card index ...")
    index = CardIndex()
    print(f"    {len(index.cards)} card names, {len(index.sets)} sets")

    os.makedirs(REJECT_DIR, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="catalogue_")

    accepted = rejected = 0
    foil = False
    qty = 1
    state = {}

    print()
    print("=" * 70)
    print(f" {CYAN}Ready.{RESET}  Put a card under the camera, then press "
          f"{BOLD}space{RESET} to scan it.")
    print("=" * 70)

    with Ocr() as ocr:
        try:
            while True:
                print()
                print(f"  {BOLD}[space]{RESET} scan   {BOLD}[e]{RESET} edit last   "
                      f"{BOLD}[f]{RESET} foil {GREEN + 'ON' + RESET if foil else 'off'}   "
                      f"{BOLD}[q]{RESET} quit"
                      f"   {DIM}accepted: {accepted}   rejected: {rejected}{RESET}")
                k = getkey()

                if k == "q":
                    break
                if k == "e":
                    edit_last(index, state)
                    continue
                if k == "f":
                    foil = not foil
                    continue
                if k not in ("\r", "\n", " ", "s"):
                    continue

                print(f"  {DIM}capturing ...{RESET}")
                try:
                    full = phone.grab(full=True)
                except Exception as e:
                    print(f"  {RED}!{RESET} capture failed: {e}")
                    continue

                if carddetect.detect(full) is None:
                    print(f"  {YELLOW}no card visible{RESET} - reposition and press space again")
                    continue

                try:
                    guess, info = scan.recognise(full, ocr, index, tmpdir)
                except Exception as e:
                    print(f"  {RED}!{RESET} recognition error: {e}")
                    continue

                card = guess["card"] if guess else None
                printing = (scan.pick_printing(card, info.get("number"),
                                               info.get("setcode"))
                            if card else (None, None, None))

                panel = Panel()
                picking = False
                confirmed = False
                prints = (card.get("printings") or []) if card else []

                while True:
                    panel.draw(render(index, card, info, guess, foil, qty,
                                      accepted, printing, picking, confirmed))
                    k = getkey()

                    if picking:
                        # a number chooses a printing; anything else cancels
                        if k and k.isdigit() and k != "0":
                            i = int(k)
                            if 1 <= i <= min(9, len(prints)):
                                printing = prints[i - 1]
                                confirmed = True
                        picking = False
                        continue

                    if k in ("\r", "\n", " ", "a", "y"):
                        if not card:
                            panel.release()
                            print(f"  {YELLOW}nothing to accept - rejecting instead{RESET}")
                            k = "r"
                        else:
                            row = make_row(card, printing, info.get("language"),
                                           foil, qty, index)
                            append(LOG, LOG_HEADER, row)
                            accepted += qty
                            state.update(card=card, printing=printing)
                            panel.draw(render(index, card, info, guess, foil, qty,
                                              accepted, printing, False, confirmed))
                            panel.release()
                            print(f"  {GREEN}accepted{RESET}  {card['name']}  "
                                  f"{printing[0]}#{printing[1]}  x{qty}"
                                  f"{' foil' if foil else ''}"
                                  f"   {DIM}[e] to change it{RESET}")
                            qty = 1
                            break
                    if k == "p" and len(prints) > 1:
                        picking = True
                        continue
                    if k == "r":
                        panel.release()
                        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        photo = os.path.join(REJECT_DIR, f"{stamp}.jpg")
                        try:
                            full.save(photo, quality=88)
                        except Exception:
                            photo = ""
                        append(REJECTS, REJECT_HEADER, [
                            datetime.now().isoformat(timespec="seconds"),
                            "manual" if card else "unrecognised",
                            card["name"] if card else "",
                            f"{guess['score']:.2f}" if guess else "",
                            info.get("setcode") or "", info.get("number") or "",
                            guess["text"] if guess else "",
                            os.path.basename(photo),
                        ])
                        rejected += 1
                        print(f"  {YELLOW}rejected{RESET}  put this card aside"
                              f"  {DIM}(photo saved){RESET}")
                        qty = 1
                        break
                    if k == "f":
                        foil = not foil
                        continue                    # panel redraws in place
                    if k and k.isdigit() and k != "0":
                        qty = int(k)
                        continue                    # panel redraws in place
                    if k == "q":
                        raise KeyboardInterrupt
                    # unknown key: just redraw

        except KeyboardInterrupt:
            pass

    print()
    print("=" * 70)
    print(f"  accepted {GREEN}{accepted}{RESET} card(s)  ->  {LOG}")
    if rejected:
        print(f"  rejected {YELLOW}{rejected}{RESET} card(s)  ->  {REJECTS}")
        print(f"  {DIM}photos in {REJECT_DIR}{RESET}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
