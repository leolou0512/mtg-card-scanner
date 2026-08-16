"""Card cataloguer with a graphical interface.

Live camera on the left, the card Scryfall thinks it is on the right, so you
verify by looking rather than by reading. Mouse first, with keyboard shortcuts
for everything.

    python gui.py

Accepted cards append to collection_log.csv; rejected ones to rejects.csv with
a photo saved for later hand-cataloguing.
"""
import csv
import io
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
from datetime import date, datetime
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

import carddetect
import paths
import scan
import scryfall
from cardlookup import CardIndex
from ocr import Ocr

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = paths.PROJECT
UI_FONT = paths.ui_font()
MONO_FONT = paths.mono_font()
LOG = os.path.join(ROOT, "collection_log.csv")
REJECTS = os.path.join(ROOT, "rejects.csv")
REJECT_DIR = os.path.join(ROOT, "rejects")
CONFIG = os.path.join(HERE, ".catalogue.json")

LOG_HEADER = ["date", "action", "name", "set", "set_name", "number", "rarity",
              "language", "foil", "qty", "source", "note"]
REJECT_HEADER = ["timestamp", "reason", "best_guess", "score", "set", "number",
                 "ocr_text", "photo"]

BG = "#16181d"
PANEL = "#1e2128"
EDGE = "#2c313c"
FG = "#e6e8ec"
MUTED = "#8b93a3"
ACCENT = "#4da3ff"
OK = "#3ecf8e"
WARN = "#f2b544"
BAD = "#ff6b6b"

CARD_W, CARD_H = 400, 558        # reference pane, 63x88 proportions
VIDEO_W, VIDEO_H = 400, 558      # matched, so the two cards compare directly
REF_VERSION = "large"            # 672x936 from Scryfall, sharper than 'normal'


# --------------------------------------------------------------------- config

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
    raw = (raw or "").strip().rstrip("/")
    raw = re.sub(r"^https?://", "", raw)
    if not raw:
        return None
    host, _, port = raw.partition(":")
    if not host:
        return None
    # anything made only of digits and dots must be a complete IPv4 address,
    # so a half-typed "192.168.1." is rejected rather than treated as a hostname
    if re.fullmatch(r"[\d.]+", host):
        parts = host.split(".")
        if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255
                                      for p in parts):
            return None
    elif not re.fullmatch(r"[A-Za-z0-9._-]+", host):
        return None
    return f"http://{host}:{port or '8080'}"


# -------------------------------------------------------------------- logging

def append_csv(path, header, row):
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
    return (rows[0], rows[1:]) if rows else ([], [])


def rewrite_last(new_row):
    header, body = read_log()
    if not body:
        return False
    body = body[:-1] if new_row is None else body[:-1] + [new_row]
    with open(LOG, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header or LOG_HEADER)
        w.writerows(body)
    return True


# --------------------------------------------------------------- video thread

class VideoFeed(threading.Thread):
    """Polls the phone's preview endpoint and hands frames to the UI."""

    def __init__(self, phone, out_queue, fps=12):
        super().__init__(daemon=True)
        self.phone = phone
        self.q = out_queue
        self.interval = 1.0 / fps
        self.stop_flag = threading.Event()
        self.paused = threading.Event()
        self.dropped = 0

    def run(self):
        consecutive = 0
        while not self.stop_flag.is_set():
            if self.paused.is_set():
                time.sleep(0.1)
                continue
            t0 = time.time()
            try:
                img = self.phone.grab(full=False)
                consecutive = 0
                if self.q.qsize() < 2:
                    self.q.put(("frame", img))
            except Exception as e:
                # a dropped frame is invisible at this rate; only complain once
                # the camera has actually gone away
                self.dropped += 1
                consecutive += 1
                if consecutive >= 5:
                    self.q.put(("error", str(e)))
                    time.sleep(1.0)
            wait = self.interval - (time.time() - t0)
            if wait > 0:
                time.sleep(wait)

    def stop(self):
        self.stop_flag.set()


# ------------------------------------------------------------------ image use

def fit(img, box_w, box_h):
    """Scale to fit inside a box, preserving aspect. None if undecodable."""
    if img is None:
        return None
    try:
        r = min(box_w / img.width, box_h / img.height)
        if r <= 0:
            return img
        return img.resize((max(1, int(img.width * r)),
                           max(1, int(img.height * r))), Image.LANCZOS)
    except OSError:
        # truncated JPEG that slipped past the worker's decode
        return None


def placeholder(w, h, text, colour=EDGE):
    im = Image.new("RGB", (w, h), PANEL)
    return im


# ------------------------------------------------------------- printing picker

class PrintingPicker(tk.Toplevel):
    """All printings of a card, as images, click to choose."""

    THUMB_W, THUMB_H = 176, 245
    COLS = 5

    def __init__(self, parent, index, card, current, on_pick):
        super().__init__(parent)
        self.title(f"Printings of {card['name']}")
        self.configure(bg=BG)
        self.index = index
        self.card = card
        self.on_pick = on_pick
        self.current = current
        self._photos = []
        self._tiles = {}
        self.result = None

        prints = card.get("printings") or []
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(head, text=card["name"], bg=BG, fg=FG,
                 font=(UI_FONT, 15, "bold")).pack(side="left")
        tk.Label(head, text=f"   {len(prints)} printings - click one, or press 1-9",
                 bg=BG, fg=MUTED, font=(UI_FONT, 10)).pack(side="left")

        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True, padx=10, pady=6)
        canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        vbar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        self.inner = tk.Frame(canvas, bg=BG)
        self.inner.bind("<Configure>",
                        lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.inner, anchor="nw")
        canvas.configure(yscrollcommand=vbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        for i, pr in enumerate(prints):
            self._add_tile(i, pr)

        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", padx=14, pady=(4, 12))
        tk.Button(foot, text="Cancel  (Esc)", command=self.destroy,
                  bg=PANEL, fg=FG, relief="flat", padx=14, pady=6,
                  activebackground=EDGE, activeforeground=FG).pack(side="right")

        self.bind("<Escape>", lambda e: self.destroy())
        for n in range(1, 10):
            self.bind(str(n), lambda e, n=n: self._pick_index(n - 1))

        self.transient(parent)
        self.grab_set()
        self.geometry(self._geometry_for(len(prints)))
        threading.Thread(target=self._load_images, args=(prints,), daemon=True).start()

    def _geometry_for(self, n):
        cols = min(self.COLS, max(1, n))
        rows = min(3, (n + cols - 1) // cols)
        w = cols * (self.THUMB_W + 16) + 40
        h = rows * (self.THUMB_H + 54) + 120
        return f"{w}x{h}"

    def _add_tile(self, i, pr):
        code, num, rarity = pr
        col, row = i % self.COLS, i // self.COLS
        is_current = (code, num) == (self.current[0], self.current[1])

        cell = tk.Frame(self.inner, bg=ACCENT if is_current else BG,
                        padx=2, pady=2)
        cell.grid(row=row, column=col, padx=6, pady=6)
        inner = tk.Frame(cell, bg=PANEL)
        inner.pack()

        # fixed-pixel holder: a Label sizes in characters while it holds text
        hold = tk.Frame(inner, width=self.THUMB_W, height=self.THUMB_H, bg=PANEL)
        hold.pack_propagate(False)
        hold.pack()
        img_lbl = tk.Label(hold, bg=PANEL, text="loading...", fg=MUTED,
                           font=(UI_FONT, 9))
        img_lbl.pack(expand=True)
        cap = f"{code} #{num}"
        if i < 9:
            cap = f"[{i+1}]  " + cap
        tk.Label(inner, text=cap, bg=PANEL, fg=FG,
                 font=(UI_FONT, 9, "bold")).pack(fill="x")
        tk.Label(inner, text=self.index.set_name(code)[:26], bg=PANEL, fg=MUTED,
                 font=(UI_FONT, 8)).pack(fill="x", pady=(0, 4))

        for w in (cell, inner, img_lbl):
            w.bind("<Button-1>", lambda e, p=pr: self._choose(p))
        self._tiles[i] = img_lbl

    def _load_images(self, prints):
        for i, (code, num, _r) in enumerate(prints):
            im = scryfall.card_image(code, num, version="small")
            if im is None:
                self.after(0, self._no_image, i)
                continue
            self.after(0, self._set_image, i, fit(im, self.THUMB_W, self.THUMB_H))

    def _set_image(self, i, im):
        lbl = self._tiles.get(i)
        if not lbl or not lbl.winfo_exists():
            return
        photo = ImageTk.PhotoImage(im)
        self._photos.append(photo)
        lbl.configure(image=photo, text="")

    def _no_image(self, i):
        lbl = self._tiles.get(i)
        if lbl and lbl.winfo_exists():
            lbl.configure(text="no image", fg=MUTED)

    def _pick_index(self, i):
        prints = self.card.get("printings") or []
        if 0 <= i < len(prints):
            self._choose(prints[i])

    def _choose(self, pr):
        self.result = pr
        self.on_pick(pr)
        self.destroy()


# ------------------------------------------------------------------- main app

class App(tk.Tk):
    def __init__(self, url, index, ocr):
        super().__init__()
        self.title("Card Cataloguer")
        self.configure(bg=BG)
        self.index = index
        self.ocr = ocr
        self.phone = scan.Phone(url)
        self.tmpdir = tempfile.mkdtemp(prefix="gui_")

        self.frames = queue.Queue()
        self.results = queue.Queue()
        self.video = VideoFeed(self.phone, self.frames)

        self.card = None
        self.info = {}
        self.guess = None
        self.printing = (None, None, None)
        self.confirmed = False
        self.last_full = None
        self.frozen = False
        self.scan_busy = False
        self.accepted = 0
        self.rejected = 0
        self.data = None

        self.foil = tk.BooleanVar(value=False)
        self.qty = tk.IntVar(value=1)

        self._photo_video = None
        self._photo_card = None

        self._build()
        self._bind_keys()

        self.video.start()
        self.after(40, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._quit)

    # ------------------------------------------------------------------ build
    def _build(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TSpinbox", fieldbackground=PANEL, foreground=FG,
                        background=PANEL, arrowcolor=FG)

        top = tk.Frame(self, bg=BG)
        top.pack(fill="both", expand=True, padx=12, pady=(12, 6))

        # live camera
        cam = tk.Frame(top, bg=PANEL, highlightbackground=EDGE, highlightthickness=1)
        cam.grid(row=0, column=0, sticky="n", padx=(0, 10))
        tk.Label(cam, text="YOUR CARD  -  live until you scan", bg=PANEL, fg=MUTED,
                 font=(UI_FONT, 9, "bold")).pack(anchor="w", padx=10, pady=(8, 4))
        # A Label sizes itself in characters when it holds text and in pixels
        # when it holds an image, so a fixed-pixel Frame does the sizing and the
        # label just sits inside it.
        vhold = tk.Frame(cam, width=VIDEO_W, height=VIDEO_H, bg="#0d0f13")
        vhold.pack_propagate(False)
        vhold.pack(padx=10, pady=(0, 10))
        self.video_lbl = tk.Label(vhold, bg="#0d0f13", text="connecting...",
                                  fg=MUTED)
        self.video_lbl.pack(expand=True)
        self.cam_status = tk.Label(cam, text="", bg=PANEL, fg=MUTED,
                                   font=(UI_FONT, 8))
        self.cam_status.pack(anchor="w", padx=10, pady=(0, 8))

        # reference card
        ref = tk.Frame(top, bg=PANEL, highlightbackground=EDGE, highlightthickness=1)
        ref.grid(row=0, column=1, sticky="n", padx=(0, 10))
        tk.Label(ref, text="READ AS  -  reference image", bg=PANEL, fg=MUTED,
                 font=(UI_FONT, 9, "bold")).pack(anchor="w", padx=10, pady=(8, 4))
        chold = tk.Frame(ref, width=CARD_W, height=CARD_H, bg="#0d0f13")
        chold.pack_propagate(False)
        chold.pack(padx=10, pady=(0, 10))
        self.card_lbl = tk.Label(chold, bg="#0d0f13", text="press SCAN", fg=MUTED)
        self.card_lbl.pack(expand=True)

        # details
        det = tk.Frame(top, bg=BG)
        det.grid(row=0, column=2, sticky="nsew")
        top.grid_columnconfigure(2, weight=1)

        self.name_lbl = tk.Label(det, text="Ready", bg=BG, fg=FG,
                                 font=(UI_FONT, 19, "bold"), anchor="w",
                                 wraplength=330, justify="left")
        self.name_lbl.pack(fill="x", pady=(2, 2))
        self.set_lbl = tk.Label(det, text="", bg=BG, fg=ACCENT,
                                font=(UI_FONT, 11), anchor="w",
                                wraplength=330, justify="left")
        self.set_lbl.pack(fill="x")
        self.meta_lbl = tk.Label(det, text="", bg=BG, fg=MUTED,
                                 font=(UI_FONT, 10), anchor="w", justify="left")
        self.meta_lbl.pack(fill="x", pady=(6, 0))
        self.price_lbl = tk.Label(det, text="", bg=BG, fg=OK,
                                  font=(UI_FONT, 15, "bold"), anchor="w")
        self.price_lbl.pack(fill="x", pady=(10, 0))
        self.conf_lbl = tk.Label(det, text="", bg=BG, fg=MUTED,
                                 font=(UI_FONT, 10), anchor="w", justify="left")
        self.conf_lbl.pack(fill="x", pady=(10, 0))
        self.warn_lbl = tk.Label(det, text="", bg=BG, fg=WARN,
                                 font=(UI_FONT, 10, "bold"), anchor="w",
                                 wraplength=330, justify="left")
        self.warn_lbl.pack(fill="x", pady=(8, 0))

        opts = tk.Frame(det, bg=BG)
        opts.pack(fill="x", pady=(14, 0))

        # big foil toggle - the state has to be readable at a glance
        self.foil_btn = tk.Button(
            opts, text="FOIL  -  off", command=self._toggle_foil,
            bg=PANEL, fg=MUTED, relief="flat", pady=14,
            font=(UI_FONT, 13, "bold"),
            activebackground=EDGE, activeforeground=FG)
        self.foil_btn.pack(fill="x")

        qty_row = tk.Frame(opts, bg=BG)
        qty_row.pack(fill="x", pady=(8, 0))
        tk.Button(qty_row, text="−", command=lambda: self._bump_qty(-1),
                  bg=PANEL, fg=FG, relief="flat", width=3, pady=10,
                  font=(UI_FONT, 16, "bold"),
                  activebackground=EDGE, activeforeground=FG).pack(side="left")
        self.qty_lbl = tk.Label(qty_row, text="1", bg=PANEL, fg=FG,
                                font=(UI_FONT, 20, "bold"))
        self.qty_lbl.pack(side="left", fill="both", expand=True, padx=2)
        tk.Button(qty_row, text="+", command=lambda: self._bump_qty(1),
                  bg=PANEL, fg=FG, relief="flat", width=3, pady=10,
                  font=(UI_FONT, 16, "bold"),
                  activebackground=EDGE, activeforeground=FG).pack(side="left")
        tk.Label(opts, text="quantity  -  or press 1-9", bg=BG, fg=MUTED,
                 font=(UI_FONT, 8)).pack(anchor="w")

        self.print_btn = tk.Button(opts, text="CHANGE PRINTING  (P)",
                                   command=self.open_picker, bg=PANEL, fg=FG,
                                   relief="flat", pady=14,
                                   font=(UI_FONT, 13, "bold"),
                                   activebackground=EDGE, activeforeground=FG,
                                   disabledforeground="#4a505c",
                                   state="disabled")
        self.print_btn.pack(fill="x", pady=(10, 0))

        # Actions sit directly under the controls they follow, so the mouse
        # never leaves this column: adjust foil or quantity, then accept.
        tk.Frame(opts, bg=EDGE, height=1).pack(fill="x", pady=(14, 10))

        self.scan_btn = self._button(opts, "SCAN   (Space)", self.do_scan,
                                     ACCENT, 10)
        self.scan_btn.pack(fill="x", ipady=6)

        decide = tk.Frame(opts, bg=BG)
        decide.pack(fill="x", pady=(6, 0))
        decide.grid_columnconfigure(0, weight=1)
        decide.grid_columnconfigure(1, weight=1)
        self.accept_btn = self._button(decide, "Accept  (A)", self.do_accept,
                                       OK, 8)
        self.accept_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3), ipady=4)
        self.reject_btn = self._button(decide, "Reject  (R)", self.do_reject,
                                       BAD, 8)
        self.reject_btn.grid(row=0, column=1, sticky="ew", padx=(3, 0), ipady=4)
        self.retry_btn = self._button(decide, "Retry  (T)", self.do_retry,
                                      WARN, 8)
        self.retry_btn.grid(row=1, column=0, sticky="ew", padx=(0, 3),
                            pady=(6, 0), ipady=4)
        self._button(decide, "Edit last  (E)", self.do_edit_last, PANEL, 8).grid(
            row=1, column=1, sticky="ew", padx=(3, 0), pady=(6, 0), ipady=4)

        # secondary, kept out of the way of the scanning rhythm
        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", padx=12, pady=(2, 4))
        self.update_btn = self._button(foot, "Update card database",
                                       self.do_update_carddb, PANEL, 10)
        self.update_btn.pack(side="left")
        self.count_lbl = tk.Label(foot, text="", bg=BG, fg=MUTED,
                                  font=(UI_FONT, 11))
        self.count_lbl.pack(side="right")

        # history
        hist = tk.Frame(self, bg=BG)
        hist.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        cols = ("n", "name", "set", "num", "qty", "foil", "eur")
        self.tree = ttk.Treeview(hist, columns=cols, show="headings", height=7)
        for c, w, t in (("n", 40, "#"), ("name", 260, "Card"), ("set", 190, "Set"),
                        ("num", 70, "No."), ("qty", 50, "Qty"),
                        ("foil", 60, "Foil"), ("eur", 90, "EUR")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=FG, rowheight=22, borderwidth=0)
        style.configure("Treeview.Heading", background=EDGE, foreground=FG,
                        relief="flat")

        self._set_buttons(scanned=False)
        self._update_counts()
        self._refresh_foil_btn()
        self._refresh_qty()

    def _bump_qty(self, delta):
        self.qty.set(max(1, min(99, int(self.qty.get() or 1) + delta)))
        self._refresh_qty()

    def _refresh_qty(self):
        self.qty_lbl.configure(text=str(self.qty.get()))

    def _refresh_foil_btn(self):
        on = self.foil.get()
        self.foil_btn.configure(
            text="FOIL  -  ON" if on else "FOIL  -  off",
            bg=WARN if on else PANEL,
            fg="#10131a" if on else MUTED,
            activebackground=WARN if on else EDGE)

    def _button(self, parent, text, cmd, colour, size):
        return tk.Button(parent, text=text, command=cmd, bg=colour, fg="#10131a",
                         relief="flat", padx=size, pady=8,
                         font=(UI_FONT, 10, "bold"),
                         activebackground=colour, activeforeground="#10131a",
                         disabledforeground="#5b6270")

    def _bind_keys(self):
        self.bind("<space>", lambda e: self.do_scan())
        self.bind("<Return>", lambda e: self.do_accept())
        self.bind("a", lambda e: self.do_accept())
        self.bind("r", lambda e: self.do_reject())
        self.bind("t", lambda e: self.do_retry())
        self.bind("e", lambda e: self.do_edit_last())
        self.bind("p", lambda e: self.open_picker())
        self.bind("f", lambda e: self._toggle_foil())
        self.bind("q", lambda e: self._quit())
        for n in range(1, 10):
            self.bind(str(n), lambda e, n=n: self._set_qty(n))

    def _toggle_foil(self):
        self.foil.set(not self.foil.get())
        self._refresh_foil_btn()
        self._refresh_details()

    def _set_qty(self, n):
        self.qty.set(n)
        self._refresh_qty()

    # ------------------------------------------------------------- video pump
    def _pump(self):
        """Drain the worker queues onto the UI thread.

        Everything is wrapped and the reschedule sits in a finally: if a handler
        ever raises, the exception escapes this callback, `after` is never
        called again, and the whole interface silently stops repainting - the
        video included. One bad frame must not be able to kill the loop.
        """
        try:
            try:
                while True:
                    kind, payload = self.frames.get_nowait()
                    if kind == "frame":
                        self._show_frame(payload)
                        if not self.frozen:
                            drops = self.video.dropped
                            extra = f"   {drops} dropped" if drops else ""
                            self.cam_status.configure(
                                text=f"{payload.width}x{payload.height}   live{extra}",
                                fg=MUTED)
                    else:
                        self.cam_status.configure(
                            text=f"camera error: {payload}", fg=BAD)
            except queue.Empty:
                pass
            except Exception as e:
                self._pump_error("video", e)

            try:
                while True:
                    kind, payload = self.results.get_nowait()
                    if kind == "scan":
                        self._on_scan_result(*payload)
                    elif kind == "image":
                        self._on_card_image(payload)
                    elif kind == "data":
                        self._on_card_data(payload)
                    elif kind == "carddb":
                        self._on_carddb(payload)
            except queue.Empty:
                pass
            except Exception as e:
                self._pump_error("scan", e)
                self.scan_busy = False
                self.video.paused.clear()
                self.scan_btn.configure(text="SCAN  (Space)", state="normal")
        finally:
            self.after(40, self._pump)

    def _pump_error(self, where, exc):
        import traceback
        traceback.print_exc()
        try:
            self.warn_lbl.configure(
                text=f"internal error in {where}: {exc}\n"
                     f"the window is still running - press Retry (T)")
        except Exception:
            pass

    def _show_frame(self, img):
        if self.frozen:
            return                      # showing the captured card instead
        im = fit(img, VIDEO_W, VIDEO_H)
        if im is None:
            return
        self._photo_video = ImageTk.PhotoImage(im)
        self.video_lbl.configure(image=self._photo_video, text="")

    def _freeze_on_capture(self, full, box):
        """Show the card cropped from the full-resolution still.

        The live preview is 1280x960 of the whole desk; the capture is
        2560x1920, and cropping to the card gives far more detail than the
        preview can - which is what makes comparing against the reference
        image worthwhile.
        """
        if full is None:
            return
        try:
            shown = full.crop(box) if box else full
        except Exception:
            shown = full
        im = fit(shown, VIDEO_W, VIDEO_H)
        self._photo_video = ImageTk.PhotoImage(im)
        self.video_lbl.configure(image=self._photo_video, text="")
        self.frozen = True
        self.cam_status.configure(
            text=f"captured {shown.width}x{shown.height}  -  frozen", fg=ACCENT)

    def _unfreeze(self):
        self.frozen = False
        self.cam_status.configure(text="live", fg=MUTED)

    # ------------------------------------------------------------------ scan
    def do_scan(self):
        if self.scan_busy:
            return
        self.scan_busy = True
        self.scan_btn.configure(text="scanning...", state="disabled")
        self.warn_lbl.configure(text="")
        self._unfreeze()
        self.video.paused.set()
        threading.Thread(target=self._scan_worker, daemon=True).start()

    do_retry = do_scan

    def _scan_worker(self):
        try:
            full = self.phone.grab(full=True)
        except Exception as e:
            self.results.put(("scan", (None, {}, None, None, f"capture failed: {e}")))
            return
        box = carddetect.detect(full)
        if box is None:
            self.results.put(("scan", (None, {"box": None}, None, full,
                                       "no card visible")))
            return
        try:
            guess, info = scan.recognise(full, self.ocr, self.index, self.tmpdir)
        except Exception as e:
            self.results.put(("scan", (None, {"box": box}, None, full,
                                       f"recognition error: {e}")))
            return
        self.results.put(("scan", (guess["card"] if guess else None, info, guess,
                                   full, None)))

    def _on_scan_result(self, card, info, guess, full, error):
        self.scan_busy = False
        self.video.paused.clear()
        self.scan_btn.configure(text="SCAN  (Space)", state="normal")
        self.card, self.info, self.guess = card, info, guess
        self.last_full = full
        self.confirmed = False
        self.data = None
        self._freeze_on_capture(full, info.get("box"))

        # `card` is None whenever nothing matched, even without an explicit
        # error - a card was seen but its name could not be read
        if error or card is None:
            read = ""
            if not error:
                lines = [l.get("text", "") for l in info.get("name_lines", [])][:3]
                read = "read: " + ", ".join(t for t in lines if t) if lines else \
                       "no text could be read from the card"
            self.name_lbl.configure(text="No card read", fg=BAD)
            self.set_lbl.configure(text="")
            self.meta_lbl.configure(text=error or read)
            self.price_lbl.configure(text="")
            self.conf_lbl.configure(text="")
            self.warn_lbl.configure(
                text="Reposition or relight the card, then Retry (T).\n"
                     "Reject (R) sets it aside for hand-cataloguing.")
            self.card_lbl.configure(image="", text="no match", fg=MUTED)
            self._photo_card = None
            self._set_buttons(scanned=full is not None, matched=False)
            return

        self.printing = scan.pick_printing(card, info.get("number"),
                                           info.get("setcode"))
        self._set_buttons(scanned=True, matched=True)
        self._refresh_details()
        self._load_card_visual()

    def _load_card_visual(self):
        code, num, _r = self.printing
        self.card_lbl.configure(image="", text="loading image...", fg=MUTED)
        self._photo_card = None
        threading.Thread(target=self._fetch_visual, args=(code, num),
                         daemon=True).start()

    def _fetch_visual(self, code, num):
        data = scryfall.card_data(code, num)
        self.results.put(("data", (code, num, data)))
        im = scryfall.card_image(code, num, version=REF_VERSION)
        self.results.put(("image", (code, num, im)))
        self._prefetch_siblings()

    def _prefetch_siblings(self):
        """Warm the other printings while you are still looking at this card.

        By the time the picker opens its thumbnails are usually already on
        disk, so the grid appears at once instead of filling in one by one.
        """
        card = self.card
        if not card:
            return
        prints = card.get("printings") or []
        if len(prints) < 2:
            return

        def work():
            for code, num, _r in prints[:12]:
                if self.card is not card:
                    return              # moved on to another card
                try:
                    scryfall.card_image(code, num, version="small")
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True).start()

    def _on_card_image(self, payload):
        code, num, im = payload
        if (code, num) != (self.printing[0], self.printing[1]):
            return
        if im is None:
            self.card_lbl.configure(image="", text="no image available", fg=MUTED)
            return
        shown = fit(im, CARD_W, CARD_H)
        self._photo_card = ImageTk.PhotoImage(shown)
        self.card_lbl.configure(image=self._photo_card, text="")

    def _on_card_data(self, payload):
        code, num, data = payload
        if (code, num) != (self.printing[0], self.printing[1]):
            return
        self.data = data
        self._refresh_details()

    # --------------------------------------------------------------- details
    def _refresh_details(self):
        card, info = self.card, self.info
        if not card:
            return
        code, num, rarity = self.printing
        self.name_lbl.configure(text=card["name"], fg=FG)
        self.set_lbl.configure(
            text=f"{self.index.set_name(code)}  ({code} {self.index.set_year(code)})")
        lang = info.get("language") or "English (assumed)"
        self.meta_lbl.configure(text=f"#{num}   {rarity or '?'}   {lang}")

        price = scryfall.eur_price(self.data, self.foil.get())
        fin = scryfall.finishes(self.data)
        if price:
            tag = " foil" if self.foil.get() else ""
            self.price_lbl.configure(text=f"{price}{tag}")
        else:
            self.price_lbl.configure(text="")

        score = self.guess["score"] if self.guess else 0
        how = ("collector line" if info.get("resolved_by") == "collector"
               else "card name")
        self.conf_lbl.configure(
            text=f"confidence {score:.2f}  -  matched on {how}\n"
                 f"read as '{self.guess['text'] if self.guess else ''}'")

        warn = []
        prints = card.get("printings") or []
        if len(prints) > 1 and not info.get("number") and not self.confirmed:
            warn.append(f"Printing is a guess - {len(prints)} exist, press P")
        if self.foil.get() and fin and "foil" not in fin:
            warn.append("Scryfall says this printing has no foil version")
        self.warn_lbl.configure(text="\n".join(warn))

        self.print_btn.configure(state="normal" if len(prints) > 1 else "disabled")

    def _set_buttons(self, scanned, matched=False):
        self.accept_btn.configure(state="normal" if matched else "disabled")
        self.reject_btn.configure(state="normal" if scanned else "disabled")
        self.retry_btn.configure(state="normal" if scanned else "disabled")
        if not matched:
            self.print_btn.configure(state="disabled")

    def _update_counts(self):
        self.count_lbl.configure(
            text=f"accepted {self.accepted}    rejected {self.rejected}")

    # --------------------------------------------------------------- printing
    def open_picker(self):
        if not self.card or len(self.card.get("printings") or []) < 2:
            return
        PrintingPicker(self, self.index, self.card, self.printing, self._picked)

    def _picked(self, pr):
        self.printing = pr
        self.confirmed = True
        self.data = None
        self._refresh_details()
        self._load_card_visual()

    # ---------------------------------------------------------------- actions
    def do_accept(self):
        if not self.card:
            return
        code, num, rarity = self.printing
        qty = max(1, int(self.qty.get() or 1))
        foil = self.foil.get()
        append_csv(LOG, LOG_HEADER, [
            date.today().isoformat(), "+", self.card["name"], code or "",
            self.index.set_name(code) if code else "", num or "", rarity or "",
            self.info.get("language") or "", "foil" if foil else "", qty,
            "scan", "",
        ])
        self.accepted += qty
        price = scryfall.eur_price(self.data, foil) or ""
        self.tree.insert("", 0, values=(
            self.accepted, self.card["name"],
            self.index.set_name(code), f"#{num}", qty,
            "yes" if foil else "", price.replace("EUR ", "")))
        self._update_counts()
        self._set_qty(1)
        self._clear_card("accepted - place the next card")

    def do_reject(self):
        if self.last_full is None:
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs(REJECT_DIR, exist_ok=True)
        photo = os.path.join(REJECT_DIR, f"{stamp}.jpg")
        try:
            self.last_full.save(photo, quality=88)
        except Exception:
            photo = ""
        append_csv(REJECTS, REJECT_HEADER, [
            datetime.now().isoformat(timespec="seconds"),
            "manual" if self.card else "unrecognised",
            self.card["name"] if self.card else "",
            f"{self.guess['score']:.2f}" if self.guess else "",
            self.info.get("setcode") or "", self.info.get("number") or "",
            self.guess["text"] if self.guess else "",
            os.path.basename(photo),
        ])
        self.rejected += 1
        self._update_counts()
        self._clear_card("rejected - put that card aside")

    def _clear_card(self, message):
        self._unfreeze()
        self.card = None
        self.guess = None
        self.info = {}
        self.data = None
        self.printing = (None, None, None)
        self._photo_card = None
        self.card_lbl.configure(image="", text=message, fg=MUTED)
        self.name_lbl.configure(text="Ready", fg=FG)
        self.set_lbl.configure(text="")
        self.meta_lbl.configure(text="")
        self.price_lbl.configure(text="")
        self.conf_lbl.configure(text="")
        self.warn_lbl.configure(text="")
        self._set_buttons(scanned=False)

    def do_update_carddb(self):
        """Fetch the latest card data and rebuild the index, in the background."""
        import carddb

        if getattr(self, "_updating", False):
            return
        self._updating = True
        self.update_btn.configure(text="checking...", state="disabled")

        def work():
            lines = []

            def note(msg):
                lines.append(str(msg))
                self.results.put(("carddb", ("progress", str(msg))))

            try:
                needed, local, remote = carddb.needs_update()
                if not needed and os.path.exists(paths.INDEX):
                    self.results.put(("carddb", ("done", f"Already current.\n\n"
                                                         f"Card data: {local}")))
                    return
                note("downloading card data ...")
                version = carddb.build(progress=note)
                self.results.put(("carddb", ("reload", version)))
            except Exception as e:
                self.results.put(("carddb", ("error", str(e))))

        threading.Thread(target=work, daemon=True).start()

    def _on_carddb(self, payload):
        kind, data = payload
        if kind == "progress":
            self.update_btn.configure(text=str(data)[:28])
            return

        self._updating = False
        self.update_btn.configure(text="Update card database", state="normal")

        if kind == "error":
            messagebox.showerror("Card database", f"Update failed:\n\n{data}")
            return
        if kind == "done":
            messagebox.showinfo("Card database", data)
            return

        # reload the index in place so the new cards are usable straight away
        try:
            self.index = CardIndex()
            messagebox.showinfo(
                "Card database",
                f"Updated.\n\n{data['cards']} card names\n"
                f"{data['printings']} printings\n{data['sets']} sets\n\n"
                f"Card data dated {data['updated_at']}")
        except Exception as e:
            messagebox.showerror(
                "Card database",
                f"Rebuilt, but reloading failed:\n\n{e}\n\nRestart the app.")

    def do_edit_last(self):
        header, body = read_log()
        if not body:
            messagebox.showinfo("Edit last", "Nothing logged yet.")
            return
        EditLast(self, self.index, header or LOG_HEADER, body[-1])

    def _quit(self):
        try:
            self.video.stop()
        except Exception:
            pass
        self.destroy()


# ----------------------------------------------------------------- edit last

class EditLast(tk.Toplevel):
    def __init__(self, parent, index, header, row):
        super().__init__(parent)
        self.title("Edit last entry")
        self.configure(bg=BG)
        self.index = index
        self.header = header
        self.row = dict(zip(header, row))

        tk.Label(self, text=self.row.get("name", ""), bg=BG, fg=FG,
                 font=(UI_FONT, 15, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(self, text=f"{self.row.get('set_name','')}  "
                            f"({self.row.get('set','')} #{self.row.get('number','')})",
                 bg=BG, fg=ACCENT, font=(UI_FONT, 10)).pack(anchor="w", padx=16)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="x", padx=16, pady=14)
        self.foil = tk.BooleanVar(value=self.row.get("foil") == "foil")
        tk.Checkbutton(body, text="Foil", variable=self.foil, bg=BG, fg=FG,
                       selectcolor=PANEL, activebackground=BG,
                       activeforeground=FG).grid(row=0, column=0, sticky="w")
        tk.Label(body, text="Qty", bg=BG, fg=MUTED).grid(row=0, column=1, padx=(18, 4))
        self.qty = tk.IntVar(value=int(self.row.get("qty") or 1))
        ttk.Spinbox(body, from_=1, to=99, width=4,
                    textvariable=self.qty).grid(row=0, column=2)

        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", padx=16, pady=(0, 14))
        tk.Button(foot, text="Save", command=self._save, bg=OK, fg="#10131a",
                  relief="flat", padx=16, pady=6,
                  font=(UI_FONT, 10, "bold")).pack(side="left")
        tk.Button(foot, text="Delete entry", command=self._delete, bg=BAD,
                  fg="#10131a", relief="flat", padx=16, pady=6,
                  font=(UI_FONT, 10, "bold")).pack(side="left", padx=(8, 0))
        tk.Button(foot, text="Cancel", command=self.destroy, bg=PANEL, fg=FG,
                  relief="flat", padx=16, pady=6).pack(side="right")

        self.transient(parent)
        self.grab_set()

    def _save(self):
        self.row["foil"] = "foil" if self.foil.get() else ""
        self.row["qty"] = self.qty.get()
        rewrite_last([self.row.get(c, "") for c in self.header])
        self.destroy()

    def _delete(self):
        rewrite_last(None)
        self.destroy()


# --------------------------------------------------------------------- launch

# adapters that exist on most machines but never carry the phone
VIRTUAL_PREFIXES = ("192.168.56.",      # VirtualBox host-only
                    "172.17.", "172.18.", "172.19.",
                    "172.20.", "172.21.", "172.22.", "172.23.",
                    "172.24.", "172.25.", "172.26.", "172.27.",
                    "172.28.", "172.29.", "172.30.", "172.31.",   # WSL / Docker
                    "100.", "26.")                                # Tailscale, Radmin


def primary_ip():
    """The address of the interface holding the default route.

    Opening a UDP socket to an outside address makes the OS pick that
    interface; nothing is actually sent.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def local_subnets():
    """/24 prefixes to search, the real network interface first."""
    import socket
    found = []

    main = primary_ip()
    if main and not main.startswith(("127.", "169.254.")):
        found.append(main.rsplit(".", 1)[0] + ".")

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith(("127.", "169.254.")):
                continue
            prefix = ip.rsplit(".", 1)[0] + "."
            if prefix not in found:
                found.append(prefix)
    except Exception:
        pass

    # keep virtual adapters, but only after the real ones
    real = [p for p in found if not p.startswith(VIRTUAL_PREFIXES)]
    virtual = [p for p in found if p.startswith(VIRTUAL_PREFIXES)]
    return (real + virtual) or ["192.168.1."]


def probe(host, port=8080, timeout=0.35):
    """Is something answering on this address?"""
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


class Connect(tk.Tk):
    """Ask for the phone's address, with a scan of the local network."""

    def __init__(self, default):
        super().__init__()
        self.title("Connect to phone")
        self.configure(bg=BG)
        self.url = None
        self.default = default
        self.scanning = False
        self._found = []

        tk.Label(self, text="Card Cataloguer", bg=BG, fg=FG,
                 font=(UI_FONT, 16, "bold")).pack(padx=24, pady=(20, 2))
        tk.Label(self, text="Open IP Webcam on the phone and tap 'Start server'.",
                 bg=BG, fg=MUTED, font=(UI_FONT, 9)).pack(padx=24)

        # the prefix comes from the host alone; the port is kept for the
        # "last used" shortcut, since the phone does not always pick 8080
        host = (default or "").split(":", 1)[0]
        prefix = (host.rsplit(".", 1)[0] + "."
                  if host.count(".") == 3 else local_subnets()[0])

        self.entry = tk.Entry(self, font=(MONO_FONT, 15), bg=PANEL, fg=FG,
                              insertbackground=FG, relief="flat", justify="center")
        self.entry.insert(0, prefix)
        self.entry.pack(fill="x", padx=24, pady=(16, 4), ipady=7)
        # cursor sits after the prefix, ready for the last part
        self.entry.icursor("end")
        self.entry.focus_set()
        tk.Label(self, text="type the rest of the address, e.g. 189",
                 bg=BG, fg=MUTED, font=(UI_FONT, 8)).pack()

        quick = tk.Frame(self, bg=BG)
        quick.pack(fill="x", padx=24, pady=(10, 0))
        if default:
            tk.Button(quick, text=f"Last used: {default}",
                      command=lambda: self._fill(default), bg=PANEL, fg=ACCENT,
                      relief="flat", padx=10, pady=5,
                      font=(UI_FONT, 9)).pack(side="left")
        self.scan_btn = tk.Button(quick, text="Find phone on network",
                                  command=self._scan, bg=PANEL, fg=FG,
                                  relief="flat", padx=10, pady=5,
                                  font=(UI_FONT, 9))
        self.scan_btn.pack(side="right")

        self.found_box = tk.Frame(self, bg=BG)
        self.found_box.pack(fill="x", padx=24)

        self.err = tk.Label(self, text="", bg=BG, fg=BAD, font=(UI_FONT, 9),
                            wraplength=320)
        self.err.pack(padx=24, pady=(6, 0))

        tk.Button(self, text="Connect", command=self._go, bg=ACCENT, fg="#10131a",
                  relief="flat", padx=20, pady=8,
                  font=(UI_FONT, 10, "bold")).pack(pady=(10, 22))
        self.bind("<Return>", lambda e: self._go())
        self.bind("<Escape>", lambda e: self.destroy())
        self.minsize(380, 0)
        self.eval("tk::PlaceWindow . center")

    def _fill(self, text):
        self.entry.delete(0, "end")
        self.entry.insert(0, text)
        self.entry.icursor("end")

    # ----------------------------------------------------------- network scan
    def _scan(self):
        if self.scanning:
            return
        self.scanning = True
        self._found = []
        for w in self.found_box.winfo_children():
            w.destroy()
        self.scan_btn.configure(text="scanning...", state="disabled")
        self.err.configure(text="")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        typed = self.entry.get().strip()
        prefixes = []
        if typed.count(".") >= 2:
            prefixes.append(typed.rsplit(".", 1)[0] + "."
                            if typed.count(".") == 3 else typed)
        for p in local_subnets():
            if p not in prefixes:
                prefixes.append(p)

        hits = []
        lock = threading.Lock()

        def worker(host):
            if probe(host):
                with lock:
                    hits.append(host)

        for prefix in prefixes[:2]:
            threads = []
            for i in range(1, 255):
                t = threading.Thread(target=worker, args=(f"{prefix}{i}",),
                                     daemon=True)
                t.start()
                threads.append(t)
                if len(threads) >= 64:
                    for t2 in threads:
                        t2.join()
                    threads = []
            for t2 in threads:
                t2.join()
            if hits:
                break

        self.after(0, self._scan_done, sorted(hits))

    def _scan_done(self, hits):
        self.scanning = False
        self.scan_btn.configure(text="Find phone on network", state="normal")
        if not hits:
            self.err.configure(
                text="nothing answering on :8080 - is the server started?")
            return
        tk.Label(self.found_box, text="found:", bg=BG, fg=MUTED,
                 font=(UI_FONT, 9)).pack(anchor="w", pady=(8, 2))
        for h in hits[:6]:
            tk.Button(self.found_box, text=h, command=lambda h=h: self._fill(h),
                      bg=PANEL, fg=OK, relief="flat", padx=10, pady=4,
                      font=(MONO_FONT, 10)).pack(fill="x", pady=1)

    def _go(self):
        raw = self.entry.get().strip()
        if raw.endswith("."):
            self.err.configure(text="finish the address - the last part is missing")
            return
        url = normalise_url(raw)
        if not url:
            self.err.configure(text="that does not look like an address")
            return
        self.url = url
        self.destroy()


def main():
    cfg = load_config()
    # keep the port: stripping it makes "last used" quietly wrong whenever the
    # phone picks something other than 8080
    last = re.sub(r"^https?://", "", cfg.get("url") or "").rstrip("/")
    dlg = Connect(last)
    dlg.mainloop()
    if not dlg.url:
        return 0
    cfg["url"] = dlg.url
    save_config(cfg)

    phone = scan.Phone(dlg.url)
    try:
        phone.grab(full=True)
    except Exception as e:
        messagebox.showerror("Cannot reach phone",
                             f"{dlg.url}\n\n{e}\n\nIs the server running?")
        return 1
    phone.configure()

    index = CardIndex()
    with Ocr() as ocr:
        app = App(dlg.url, index, ocr)
        app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
