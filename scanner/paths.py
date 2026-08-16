"""Where everything lives, resolved so the project can move between machines.

Nothing here hardcodes a drive letter or a home directory. Copy the project
folder to an external disk, plug it into another computer, and the defaults
still resolve; anything machine-specific goes in config.json beside this file,
which is deliberately not part of the project data.
"""
import json
import os
import platform
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
CONFIG = os.path.join(HERE, "config.json")

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

INDEX = os.path.join(HERE, "index.pkl")

_config = None


def inventory_dir():
    """Where scanned cards are written.

    This tool produces an inventory; it does not own one. Point
    `inventory_dir` at a separate folder - a private repository, say - and the
    log, the rejects and any Cardmarket exports live there instead of inside
    this checkout.
    """
    env = os.environ.get("CARDMARKET_INVENTORY")
    if env:
        return env
    cfg = config().get("inventory_dir")
    if cfg and (os.path.isdir(cfg) or _usable(cfg)):
        return cfg
    return os.path.join(PROJECT, "inventory")


def ensure_inventory():
    d = inventory_dir()
    os.makedirs(d, exist_ok=True)
    return d


def __getattr__(name):
    """Resolve inventory paths on access, so changing the location takes
    effect immediately rather than at import time."""
    targets = {"DATA_DIR": "data",
               "LOG": "collection_log.csv",
               "REJECTS": "rejects.csv",
               "REJECT_DIR": "rejects"}
    if name in targets:
        return os.path.join(inventory_dir(), targets[name])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def config():
    """Read config.json, tolerating a byte-order mark.

    Windows editors and PowerShell's Set-Content write UTF-8 with a BOM, which
    json.load rejects when told the encoding is plain utf-8. Reading as
    utf-8-sig strips it if present and changes nothing if it is not - and a
    hand-edited config failing silently is a miserable thing to debug.
    """
    global _config
    if _config is None:
        try:
            with open(CONFIG, encoding="utf-8-sig") as fh:
                _config = json.load(fh)
        except FileNotFoundError:
            _config = {}
        except (json.JSONDecodeError, OSError) as e:
            sys.stderr.write(f"warning: could not read {CONFIG}: {e}\n")
            _config = {}
    return _config


def save_config(**changes):
    cfg = config()
    cfg.update({k: v for k, v in changes.items() if v is not None})
    try:
        with open(CONFIG, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError:
        pass
    return cfg


def _first_existing(candidates):
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return None


def _usable(path):
    """Can this machine write here, or create it?

    A path recorded on another computer - a Windows drive letter, or a volume
    that is not plugged in - must not be used just because it is written down.
    """
    if not path:
        return False
    if os.path.isdir(path):
        return os.access(path, os.W_OK)
    # Only the immediate parent counts. Walking further up would accept
    # /Volumes/SomeDrive/cache while that drive is unplugged, because /Volumes
    # itself exists - and then quietly create a phantom folder there.
    parent = os.path.dirname(os.path.normpath(path))
    return bool(parent) and os.path.isdir(parent) and os.access(parent, os.W_OK)


LEGACY_CACHE_FILE = os.path.join(HERE, ".cache_path")


def cache_dir():
    """Where card images live.

    Priority: environment variable, then config.json, then the older
    .cache_path file, then a 'cache' folder inside the project (so it travels
    with an external drive), and finally the platform's scratch location.
    """
    env = os.environ.get("CARDMARKET_CACHE")
    if env:
        return env

    # A configured path is only honoured if this machine can actually use it.
    # config.json travels with the project, so a Windows "F:\..." would
    # otherwise be inherited by a Mac that has no such drive.
    cfg = config().get("cache_dir")
    if cfg and _usable(cfg):
        return cfg

    # migrate the single-purpose file written by earlier versions
    try:
        with open(LEGACY_CACHE_FILE, encoding="utf-8") as fh:
            legacy = fh.read().strip()
        if legacy and _usable(legacy):
            save_config(cache_dir=legacy)
            return legacy
    except OSError:
        pass

    inside = os.path.join(PROJECT, "cache", "scryfall")
    if os.path.isdir(inside):
        return inside

    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif IS_MAC:
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "cardmarket_tracker", "scryfall")


def cockatrice_dir():
    """The Cockatrice card database, used only when rebuilding the index."""
    env = os.environ.get("COCKATRICE_DATA")
    if env:
        return env
    cfg = config().get("cockatrice_dir")
    if cfg:
        return cfg

    home = os.path.expanduser("~")
    candidates = [
        os.path.join(PROJECT, "carddb"),
        os.path.join(home, "Desktop", "CockatricePortable", "data"),
        # Windows Desktop is often redirected into OneDrive
        os.path.join(home, "OneDrive", "Desktop", "CockatricePortable", "data"),
        os.path.join(home, "CockatricePortable", "data"),
    ]
    if IS_WINDOWS:
        candidates += [
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Cockatrice", "Cockatrice"),
            os.path.join(os.environ.get("APPDATA", ""), "Cockatrice", "Cockatrice"),
        ]
    elif IS_MAC:
        candidates += [
            os.path.join(home, "Library", "Application Support", "Cockatrice", "Cockatrice"),
        ]
    else:
        candidates += [os.path.join(home, ".local", "share", "Cockatrice", "Cockatrice")]
    return _first_existing(candidates) or candidates[0]


def ui_font():
    """A sensible interface font for this platform."""
    if IS_WINDOWS:
        return "Segoe UI"
    if IS_MAC:
        return "Helvetica Neue"
    return "DejaVu Sans"


def mono_font():
    if IS_WINDOWS:
        return "Consolas"
    if IS_MAC:
        return "Menlo"
    return "DejaVu Sans Mono"


def describe():
    return {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "project": PROJECT,
        "cache": cache_dir(),
        "cockatrice": cockatrice_dir(),
        "index": INDEX,
        "index_present": os.path.exists(INDEX),
    }


if __name__ == "__main__":
    for k, v in describe().items():
        print(f"  {k:15} {v}")
