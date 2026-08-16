# Card Cataloguer

Scan Magic cards with a phone camera, identify them, and build a collection log.
Runs on Windows and macOS from the same folder.

```
python tools/doctor.py     check this machine is ready
python tools/gui.py        the graphical cataloguer
```

---

## What it does

Your phone acts as a camera over WiFi. Press scan, and it reads the card's name
and collector line, matches them against a 37,408-name card database, shows you
the card Scryfall has on file next to your actual card, and logs it.

- **Name recognition** survives poor OCR, because it matches against a fixed
  list of real card names. 20% character error still resolves correctly 97% of
  the time.
- **Printing identification** comes from the collector line at the bottom of the
  card (`0206  TLA`), which is printed in Latin characters on every card
  regardless of language — so Japanese and Chinese cards work too, provided they
  were printed from about 2015 onward.
- **Prices** are EUR, held offline.

---

## Setting up on a new computer

### 1. Copy the whole folder

Everything travels together: code, card database, your logs, and the image
cache. On an external drive it will be roughly 20 GB once the cache is full,
or about 300 MB without it.

### 2. Install Python 3.9 or newer

macOS already has Python 3, but the version from [python.org](https://python.org)
is easier to add packages to.

### 3. Install the dependencies

```bash
pip install -r requirements.txt
```

On **macOS** also install the OCR bridge and the tk toolkit:

```bash
pip install pyobjc-framework-Vision pyobjc-framework-Quartz
brew install python-tk
```

Apple's Vision framework is built into macOS — the pyobjc packages just let
Python reach it. Nothing large is downloaded.

### 4. Fetch the data

The repository holds only code — about 200 KB. Everything it *works on* is
downloadable and deliberately left out of git: the card database is rebuilt in
minutes, and the card images run to 20 GB.

```bash
python tools/setup_data.py            # card names and prices   (~3 min)
python tools/setup_data.py --mine     # ...plus images for cards you own (~3 GB)
python tools/setup_data.py --full     # ...plus every card image (~20 GB, hours)
```

Start with the plain version — it is enough to scan cards. Images only make the
reference pane instant and enable artwork matching; without them the app
fetches each card as it needs it.

Every stage is resumable. Stop it, re-run it, nothing is lost or repeated.

| What | Size | Time | Rebuild with |
|---|---|---|---|
| Card name index | 14 MB | 3 min | `carddb.py --update` |
| Non-English names | 17 MB | 5 min | `carddb.py --languages` |
| Prices (offline) | 22 MB | included | `carddb.py --update` |
| Card images | 3–20 GB | 1 h – 8 h | `precache.py --mine` / `--all` |
| Artwork index | 1.6 MB | 15 min | `arthash.py --build` |

To see what is present and what is missing:

```bash
python tools/setup_data.py --status
```

### 5. Check

```bash
python tools/doctor.py
```

It reports what works and gives a specific command for anything missing.

### Where the cache goes

Card images are large, so they are kept outside the project by default. To put
them somewhere specific — an external drive, say:

```bash
python tools/precache.py --cache-dir "/Volumes/MyDrive/cardmarket/scryfall"
```

The location is remembered in `tools/config.json`, which is not committed. If
that file arrives from another machine and points somewhere unusable, it is
ignored and a sensible local default is chosen instead.

---

## Using it

### On the phone

Install **IP Webcam** (Android, free). Set photo resolution to maximum and
focus mode to continuous, then scroll down and tap **Start server**. It shows an
address like `http://192.168.1.189:8080`.

Phone and computer must be on the same WiFi.

### On the computer

```bash
python tools/gui.py
```

Enter the address, then put a card under the camera and press **SCAN**.

| Key | Action |
|---|---|
| `Space` | scan the card under the camera |
| `A` / `Enter` | accept and log it |
| `R` | reject — sets it aside and saves a photo |
| `T` | retry, without logging |
| `P` | choose a different printing, shown as images |
| `F` | toggle foil (stays on until turned off) |
| `1`–`9` | quantity |
| `E` | edit the last entry |

Everything is also clickable.

### Terminal version

`python tools/catalogue.py` does the same job without a window.

---

## Where things are kept

| What | Where |
|---|---|
| Your collection | `collection_log.csv` |
| Rejected cards | `rejects.csv` + `rejects/` photos |
| Cardmarket history | `data/` |
| Card database index | `tools/index.pkl` |
| Card images and prices | wherever `tools/config.json` points |

`collection_log.csv` is a plain 12-column CSV. Nothing is locked away in a
format that needs this program to read.

### Moving the image cache

```bash
python tools/precache.py --cache-dir "/Volumes/MyDrive/cardmarket/scryfall"
```

The location is remembered in `tools/config.json`. Leave that file behind when
moving between machines and each will pick its own sensible default.

---

## Downloading images ahead of time

Images load on demand, but pre-fetching removes the wait.

```bash
python tools/precache.py --mine         every printing of cards you have handled
python tools/precache.py --all          every printing that exists (~20 GB)
python tools/precache.py --status       what is cached now
```

All of it is resumable — stopping and restarting never re-downloads anything.

---

## Keeping the card database current

Everything comes from Scryfall's public bulk files; no other tool is needed.

```bash
python tools/carddb.py --status      is a newer release available?
python tools/carddb.py --update      fetch it and rebuild
python tools/carddb.py --languages   rebuild the non-English name index
```

The graphical app has an **Update card database** button that does the same
thing and reloads the index without restarting.

After a new set is released, run `--update`, then top up the extras:

```bash
python tools/precache.py --mine      images for the new cards
python tools/arthash.py --build      artwork hashes (incremental)
```

Both skip work already done, so the top-up is quick.

`build_index.py` still exists and builds the same index from a local
[Cockatrice](https://cockatrice.github.io/) installation, if you would rather
not download from Scryfall. It is not required.

---

## Known limits

- **Cards printed before ~2015 in a non-English language** cannot be identified
  automatically. The name is in a script the OCR cannot match, and those cards
  have no printed collector number. Log them by hand.
- **Foils** can defeat the camera through glare. Light from the side, not
  overhead.
- **The printing is a guess** when the collector line cannot be read and the
  card has several printings. The interface flags this and `P` fixes it.
- Around **1 printing in 300** has no image on Scryfall and will show a blank
  reference pane.
