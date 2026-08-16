"""OCR on macOS using Apple's Vision framework.

Vision ships with the system, needs no download, and is at least as accurate as
the Windows engine on card text. It is reached through pyobjc:

    pip install pyobjc-framework-Vision pyobjc-framework-Quartz

Results are returned in the same shape as the Windows worker, so the rest of
the code does not care which engine ran.
"""


class MacOcrUnavailable(RuntimeError):
    pass


def _load():
    try:
        import Quartz
        import Vision
        from Foundation import NSURL
    except ImportError as e:
        raise MacOcrUnavailable(
            "pyobjc is not installed. Run:\n"
            "  pip install pyobjc-framework-Vision pyobjc-framework-Quartz"
        ) from e
    return Vision, Quartz, NSURL


class MacOcr:
    """Same interface as the Windows worker: .read(path) -> dict."""

    def __init__(self, languages=("en-GB", "en-US"), accurate=True):
        self.Vision, self.Quartz, self.NSURL = _load()
        self.languages = list(languages)
        self.accurate = accurate
        self.language = languages[0] if languages else "en"

    def _image_source(self, path):
        url = self.NSURL.fileURLWithPath_(path)
        src = self.Quartz.CGImageSourceCreateWithURL(url, None)
        if src is None:
            raise OSError(f"could not read image: {path}")
        img = self.Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        if img is None:
            raise OSError(f"could not decode image: {path}")
        return img

    def read(self, image_path):
        import os
        import time

        path = os.path.abspath(image_path)
        if not os.path.exists(path):
            raise FileNotFoundError(path)

        t0 = time.perf_counter()
        cg = self._image_source(path)
        width = self.Quartz.CGImageGetWidth(cg)
        height = self.Quartz.CGImageGetHeight(cg)

        handler = self.Vision.VNImageRequestHandler.alloc() \
            .initWithCGImage_options_(cg, None)
        request = self.Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(
            0 if self.accurate else 1)          # 0 = accurate, 1 = fast
        request.setUsesLanguageCorrection_(False)  # card names are not prose
        try:
            request.setRecognitionLanguages_(self.languages)
        except Exception:
            pass

        ok, err = handler.performRequests_error_([request], None)
        if not ok:
            raise OSError(f"vision request failed: {err}")

        lines = []
        for obs in (request.results() or []):
            cands = obs.topCandidates_(1)
            if not cands:
                continue
            text = cands[0].string()
            if not text:
                continue
            # Vision reports a normalised box with the origin bottom-left;
            # the rest of the code expects pixels from the top-left
            box = obs.boundingBox()
            x = box.origin.x * width
            w = box.size.width * width
            h = box.size.height * height
            y = (1.0 - box.origin.y - box.size.height) * height
            lines.append({"text": text, "x": int(x), "y": int(y),
                          "w": int(w), "h": int(h)})

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


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python ocr_macos.py <image> [image ...]")
        raise SystemExit(2)
    with MacOcr() as ocr:
        for p in sys.argv[1:]:
            res = ocr.read(p)
            print(f"{p}  {res['ms']} ms")
            for line in res["lines"]:
                print(f"   y={line['y']:5} h={line['h']:4}  {line['text']}")
