"""Locate a single card in a camera frame.

The card is the bright, roughly card-shaped blob against a darker background.
Working out where it is lets us crop the name bar and the collector line by
proportion, which is far more reliable than guessing from the text itself.
"""
import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage

CARD_ASPECT = 63.0 / 88.0          # 0.716 portrait
ASPECT_TOLERANCE = 0.28
MIN_AREA_FRACTION = 0.02


def _threshold(gray):
    """Otsu's method: split the histogram where between-class variance peaks."""
    hist, _ = np.histogram(gray, bins=256, range=(0, 256))
    total = gray.size
    sum_all = np.dot(np.arange(256), hist)
    sum_b = 0.0
    w_b = 0.0
    best_var, best_t = -1.0, 128
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


MAX_AREA_FRACTION = 0.85    # anything bigger is the background, not a card


def _candidates(mask, min_area, max_area):
    """Score card-shaped blobs in a binary mask."""
    mask = ndimage.binary_closing(mask, structure=np.ones((5, 5)))
    mask = ndimage.binary_fill_holes(mask)
    labels, count = ndimage.label(mask)
    if count == 0:
        return []

    out = []
    for sl_y, sl_x in ndimage.find_objects(labels):
        h = sl_y.stop - sl_y.start
        w = sl_x.stop - sl_x.start
        area = w * h
        if area < min_area or area > max_area or w < 10 or h < 10:
            continue
        aspect = w / h
        # accept portrait cards, and landscape in case the frame is rotated
        err = min(abs(aspect - CARD_ASPECT), abs(aspect - 1.0 / CARD_ASPECT))
        if err > ASPECT_TOLERANCE:
            continue
        fill = mask[sl_y, sl_x].mean()      # a card is a solid rectangle
        if fill < 0.55:
            continue
        out.append((area * fill * (1.0 - err),
                    (sl_x.start, sl_y.start, sl_x.stop, sl_y.stop)))
    return out


def detect(img, work_width=500):
    """Return (x0, y0, x1, y1) of the card in `img`, or None.

    Tries both polarities, so it works with a card lighter than its background
    (dark desk) and darker than it (white sheet, where the card's black border
    is what stands out).
    """
    scale = work_width / img.width
    small = ImageOps.grayscale(img.resize(
        (work_width, max(1, int(img.height * scale))), Image.BILINEAR))
    arr = np.asarray(small, dtype=np.uint8)

    thresh = _threshold(arr)
    min_area = arr.size * MIN_AREA_FRACTION
    max_area = arr.size * MAX_AREA_FRACTION

    found = (_candidates(arr > thresh, min_area, max_area)
             + _candidates(arr <= thresh, min_area, max_area))
    if not found:
        return None

    found.sort(key=lambda t: t[0], reverse=True)
    x0, y0, x1, y1 = found[0][1]
    inv = 1.0 / scale
    return (int(x0 * inv), int(y0 * inv), int(x1 * inv), int(y1 * inv))


# Where artwork and the rules panel sit on a standard frame, as fractions of
# card height. Artwork is a picture; the rules panel is a near-uniform light
# background with text on it.
ART_BAND = (0.12, 0.45)
TEXT_BAND = (0.58, 0.88)


def _colourfulness(img):
    """How varied is this region? Artwork scores high, a text panel low."""
    a = np.asarray(img.convert("RGB"), dtype=np.float32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    rg = r - g
    yb = 0.5 * (r + g) - b
    return float(np.sqrt(rg.std() ** 2 + yb.std() ** 2)
                 + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))


def upright_score(card_img):
    """How strongly this looks like a card the right way up.

    Positive means artwork sits above rules text, so the card is upright;
    negative means it is a half-turn out. The outline cannot answer this - a
    rectangle looks identical either way - but the printed layout is strongly
    asymmetric. Unlike reading the name this works whatever language the card
    is in, and on older cards that carry no collector line at all.

    Across English, Chinese and borderless cards this gave the correct sign
    every time. Margins are widest on ordinary frames and narrowest on
    borderless ones, where artwork runs behind the text as well.
    """
    def band(lo, hi):
        w, h = card_img.size
        return card_img.crop((int(w * 0.10), int(h * lo),
                              int(w * 0.90), int(h * hi)))

    try:
        return _colourfulness(band(*ART_BAND)) - _colourfulness(band(*TEXT_BAND))
    except Exception:
        return 0.0


def regions(box):
    """Given a card box, return the name-bar and collector-line crops."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    name = (x0 + int(w * 0.04), y0 + int(h * 0.030),
            x0 + int(w * 0.88), y0 + int(h * 0.105))
    collector = (x0 + int(w * 0.02), y0 + int(h * 0.920),
                 x0 + int(w * 0.60), y0 + int(h * 1.000))
    return {"name": name, "collector": collector}


if __name__ == "__main__":
    import sys

    for path in sys.argv[1:]:
        im = Image.open(path).convert("RGB")
        box = detect(im)
        print(f"{path}  {im.width}x{im.height}  ->  {box}")
        if box:
            w, h = box[2] - box[0], box[3] - box[1]
            print(f"   card {w}x{h}  aspect {w / h:.3f}  "
                  f"({100 * w * h / (im.width * im.height):.0f}% of frame)")
            r = regions(box)
            for k, v in r.items():
                im.crop(v).save(path.rsplit(".", 1)[0] + f"_{k}.png")
                print(f"   wrote {k} crop {v}")
