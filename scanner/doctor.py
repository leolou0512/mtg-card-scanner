"""Check this machine can run the cataloguer, and say what is missing.

    python doctor.py

Run it first on a new computer. Everything it reports is either fine, or has a
specific instruction attached.
"""
import importlib
import os
import platform
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paths  # noqa: E402

OK = "  [ok]  "
WARN = "  [--]  "
BAD = "  [!!]  "


def check_python():
    v = sys.version_info
    good = v >= (3, 9)
    print((OK if good else BAD) + f"python {platform.python_version()}"
          + ("" if good else "   needs 3.9 or newer"))
    return good


def check_module(name, why, install):
    try:
        m = importlib.import_module(name)
        ver = getattr(m, "__version__", "")
        print(OK + f"{name} {ver}".rstrip())
        return True
    except ImportError:
        print(BAD + f"{name} missing - {why}")
        print(f"         fix: {install}")
        return False


def check_optional(name, why, install):
    try:
        importlib.import_module(name)
        print(OK + f"{name}")
        return True
    except ImportError:
        print(WARN + f"{name} not installed - {why}")
        print(f"         fix: {install}")
        return False


def check_ocr():
    import ocr
    engines = ocr.available_engines()
    print("  OCR engines:")
    for name, note in engines:
        mark = OK if "needs" not in note else WARN
        print(mark.rstrip() + f"  {name}: {note}")
    try:
        with ocr.Ocr() as engine:
            print(OK + f"OCR working: {type(engine).__name__} "
                       f"(language {engine.language})")
        return True
    except Exception as e:
        print(BAD + f"no working OCR engine: {e}")
        return False


def check_files():
    ok = True
    index_ok = os.path.exists(paths.INDEX)
    size = os.path.getsize(paths.INDEX) / 1024 / 1024 if index_ok else 0
    print((OK if index_ok else BAD)
          + f"card index {'present' if index_ok else 'MISSING'}"
          + (f" ({size:.1f} MB)" if index_ok else ""))
    if not index_ok:
        print("         fix: python build_index.py   (needs the Cockatrice files)")
        ok = False

    cock = paths.cockatrice_dir()
    has_cock = os.path.isdir(cock)
    print((OK if has_cock else WARN)
          + f"cockatrice data: {cock}"
          + ("" if has_cock else "   not found (only needed to rebuild the index)"))

    data_ok = os.path.isdir(paths.DATA_DIR)
    n = len([f for f in os.listdir(paths.DATA_DIR)]) if data_ok else 0
    print((OK if data_ok else WARN)
          + f"cardmarket data: {n} files" if data_ok
          else WARN + "cardmarket data folder not found")
    return ok


def check_cache():
    import scryfall
    n, mb, where = scryfall.cache_info()
    bulk = scryfall.load_bulk()
    print(OK + f"image cache: {n} images, {mb:.0f} MB")
    print(f"         {where}")
    if bulk:
        print(OK + f"price data: {len(bulk)} printings (offline)")
    else:
        print(WARN + "price data not downloaded")
        print("         fix: python precache.py --metadata")
    free = shutil.disk_usage(os.path.dirname(where) if os.path.isdir(where)
                             else os.path.expanduser("~")).free / 1024 ** 3
    print(OK + f"free space where the cache lives: {free:.0f} GB")
    return True


def main():
    print()
    print(f"  {platform.platform()}")
    print(f"  project: {paths.PROJECT}")
    print()

    results = [check_python()]
    print()
    for name, why, install in [
        ("PIL", "image handling", "pip install pillow"),
        ("numpy", "card detection", "pip install numpy"),
        ("scipy", "card detection", "pip install scipy"),
    ]:
        results.append(check_module(name, why, install))
    check_optional("tkinter", "the graphical interface",
                   "macOS: brew install python-tk    Linux: apt install python3-tk")
    print()
    results.append(check_ocr())
    print()
    check_files()
    print()
    check_cache()
    print()

    if all(results):
        print("  ready. run:  python gui.py")
    else:
        print("  fix the [!!] items above, then run this again")
    print()
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
