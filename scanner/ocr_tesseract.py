"""Fallback OCR through Tesseract, for Linux or a Mac without pyobjc.

    pip install pytesseract
    brew install tesseract      # macOS
    apt install tesseract-ocr   # Debian/Ubuntu

Slower than the built-in engines and no better on card text, so it is only
used when neither Windows OCR nor Apple's Vision is available.
"""


class TesseractUnavailable(RuntimeError):
    pass


class TesseractOcr:
    """Same interface as the other engines: .read(path) -> dict."""

    def __init__(self, lang="eng"):
        try:
            import pytesseract
            from PIL import Image  # noqa: F401
        except ImportError as e:
            raise TesseractUnavailable(
                "pytesseract is not installed. Run: pip install pytesseract\n"
                "and install the tesseract binary for your system."
            ) from e
        self.pytesseract = pytesseract
        self.lang = lang
        try:
            self.language = str(pytesseract.get_tesseract_version())
        except Exception as e:
            raise TesseractUnavailable(
                "the tesseract binary was not found on PATH"
            ) from e

    def read(self, image_path):
        import os
        import time

        from PIL import Image

        path = os.path.abspath(image_path)
        if not os.path.exists(path):
            raise FileNotFoundError(path)

        t0 = time.perf_counter()
        img = Image.open(path)
        data = self.pytesseract.image_to_data(
            img, lang=self.lang,
            output_type=self.pytesseract.Output.DICT)

        # group word boxes back into lines
        grouped = {}
        for i, text in enumerate(data["text"]):
            text = (text or "").strip()
            if not text:
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            x, y = data["left"][i], data["top"][i]
            w, h = data["width"][i], data["height"][i]
            g = grouped.setdefault(key, {"words": [], "x0": x, "y0": y,
                                         "x1": x + w, "y1": y + h})
            g["words"].append(text)
            g["x0"] = min(g["x0"], x)
            g["y0"] = min(g["y0"], y)
            g["x1"] = max(g["x1"], x + w)
            g["y1"] = max(g["y1"], y + h)

        lines = [{"text": " ".join(g["words"]), "x": g["x0"], "y": g["y0"],
                  "w": g["x1"] - g["x0"], "h": g["y1"] - g["y0"]}
                 for g in grouped.values()]
        lines.sort(key=lambda l: (l["y"], l["x"]))

        return {
            "ok": True,
            "ms": int((time.perf_counter() - t0) * 1000),
            "text": "\n".join(l["text"] for l in lines),
            "lines": lines,
        }

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
