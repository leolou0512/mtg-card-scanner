"""Text recognition, using whatever engine this machine has.

Every backend returns the same shape, so callers never branch on platform:

    {"ok": True, "ms": 12, "text": "...",
     "lines": [{"text": "...", "x": 0, "y": 0, "w": 0, "h": 0}, ...]}

  Windows  the OCR engine built into Windows 10/11 (no install)
  macOS    Apple's Vision framework via pyobjc
  other    Tesseract

Use it as a context manager:

    with Ocr() as ocr:
        result = ocr.read("frame.bmp")
"""
import json
import os
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "ocr_worker.ps1")


class OcrError(RuntimeError):
    pass


class WindowsOcr:
    """Drives a long-lived PowerShell worker.

    The engine is created once in that process; starting a new PowerShell per
    image would cost about 300 ms each time instead of 12.
    """

    def __init__(self, worker=WORKER, timeout=30):
        self.timeout = timeout
        self._proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", worker],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        self._lock = threading.Lock()
        hello = self._readline()
        if not hello.get("ok"):
            raise OcrError(f"worker failed to start: {hello}")
        self.language = hello.get("lang", "?")
        self.available = [t for t in (hello.get("available") or "").split(",")
                          if t]

    def _readline(self):
        line = self._proc.stdout.readline()
        if not line:
            err = self._proc.stderr.read()
            raise OcrError(f"worker died. stderr:\n{err}")
        line = line.strip()
        if not line:
            return {}
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"unparseable: {line[:200]}"}

    def read(self, image_path, lang=None):
        """OCR an image. `lang` picks a recogniser, e.g. 'zh-Hans-CN'."""
        path = os.path.abspath(image_path)
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        payload = path if not lang else f"{path}\t{lang}"
        with self._lock:
            self._proc.stdin.write(payload + "\n")
            self._proc.stdin.flush()
            res = self._readline()
        if not res.get("ok"):
            raise OcrError(res.get("error", "unknown OCR failure"))
        return res

    def supports(self, lang):
        if not lang:
            return True
        want = lang.lower()
        return any(t.lower() == want or t.lower().startswith(want.split("-")[0])
                   for t in self.available)

    def close(self):
        try:
            if self._proc.poll() is None:
                self._proc.stdin.write("QUIT\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def available_engines():
    """Which engines this machine could use, best first."""
    found = []
    if sys.platform == "win32":
        found.append(("windows", "built into Windows, no install"))
    if sys.platform == "darwin":
        try:
            import Vision  # noqa: F401
            found.append(("macos", "Apple Vision, built in"))
        except ImportError:
            found.append(("macos", "needs: pip install pyobjc-framework-Vision "
                                   "pyobjc-framework-Quartz"))
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        found.append(("tesseract", "installed"))
    except Exception:
        found.append(("tesseract", "needs: pip install pytesseract + the binary"))
    return found


def Ocr(prefer=None):
    """Return the best available engine for this machine.

    `prefer` forces one of "windows", "macos", "tesseract"; otherwise the
    platform's built-in engine is used, falling back to Tesseract.
    """
    order = []
    if prefer:
        order = [prefer]
    elif sys.platform == "win32":
        order = ["windows", "tesseract"]
    elif sys.platform == "darwin":
        order = ["macos", "tesseract"]
    else:
        order = ["tesseract"]

    problems = []
    for name in order:
        try:
            if name == "windows":
                if sys.platform != "win32":
                    raise OcrError("windows engine needs Windows")
                return WindowsOcr()
            if name == "macos":
                from ocr_macos import MacOcr
                return MacOcr()
            if name == "tesseract":
                from ocr_tesseract import TesseractOcr
                return TesseractOcr()
        except Exception as e:
            problems.append(f"{name}: {e}")

    raise OcrError(
        "no OCR engine available on this machine.\n  " + "\n  ".join(problems))


if __name__ == "__main__":
    import time

    print(f"platform: {sys.platform}")
    print("engines:")
    for name, note in available_engines():
        print(f"  {name:10} {note}")
    print()

    paths = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not paths:
        print("usage: python ocr.py <image> [image ...]")
        sys.exit(0)

    t0 = time.perf_counter()
    with Ocr() as ocr:
        print(f"engine ready in {(time.perf_counter()-t0)*1000:.0f} ms "
              f"({type(ocr).__name__}, language {ocr.language})")
        for p in paths:
            t = time.perf_counter()
            res = ocr.read(p)
            dt = (time.perf_counter() - t) * 1000
            txt = res["text"].replace("\n", " | ")
            print(f"  {os.path.basename(p):22} {dt:6.1f} ms  ->  '{txt}'")
