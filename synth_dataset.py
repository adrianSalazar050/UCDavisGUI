"""Copy-paste augmentation: turn a few real failures into a trainable dataset.

The collected set has 63 real spaghetti frames, but the tangle sits in roughly
the same place in nearly all of them -- so a detector trained on it directly
learns "dark blob near the centre", not what spaghetti looks like. It also has
49 clean frames across 7 bed positions.

This cuts the real tangles out of the failure frames and pastes them onto the
clean frames at many positions, scales and orientations, writing YOLO labels
from the paste geometry. Labels are exact and free -- we know where we put it,
which is the whole appeal of copy-paste over hand-labelling.

    python synth_dataset.py                      # build with defaults
    python synth_dataset.py --per-bg 12 --out datasets/synth_v2

Output is a ready-to-train YOLO layout:

    <out>/images/{train,val}/*.jpg
    <out>/labels/{train,val}/*.txt
    <out>/data.yaml

WHAT THIS CANNOT DO. Copy-paste fixes position coverage and volume. It cannot
invent optics: this camera is a wide fisheye, so a tangle photographed near the
centre does not look like one at the frame edge (different distortion and
foreshortening). Pasting a centre cutout far out to the edge produces something
that never occurs optically, and a model can learn that artifact. Paste
displacement is therefore CAPPED (--max-shift) rather than uniform across the
frame, and the real fix for the empty right-hand third of the bed is a short
collection pass there, not more synthesis.

Clean frames are also emitted unchanged, with empty label files. YOLO treats
those as background/negatives, and they are what stop the model deciding that
every print is a failure.
"""
from __future__ import annotations

import argparse
import pathlib
import random
import sys

import cv2
import numpy as np

# Detection ROI on this camera: the bed occupies the top half of the frame while
# printing. Pastes are confined to it -- a tangle floating in the room would
# teach the model nothing useful.
BED_ROI = (0.0, 0.02, 1.0, 0.46)      # x, y, w, h as fractions

CLASS_NAMES = ["spaghetti"]


def load_pairs(images: pathlib.Path):
    clean = sorted(images.glob("*_clean.jpg"))
    spag = sorted(images.glob("*_spaghetti.jpg"))
    if not clean or not spag:
        raise SystemExit(f"need both clean and spaghetti frames in {images}")
    return clean, spag


def _thumb(path, cache={}):
    if path not in cache:
        g = cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2GRAY)
        cache[path] = cv2.resize(g, (96, 96)).astype(np.float32)
    return cache[path]


def best_background(spag_path, clean_paths):
    """The clean frame most similar to this failure frame.

    Matching matters: the bed moves, so differencing against an arbitrary clean
    frame would flag the bed's own displacement as "the failure" and produce a
    cutout that is mostly bed. Picking the nearest background keeps the diff
    dominated by the tangle itself.
    """
    t = _thumb(spag_path)
    return min(clean_paths, key=lambda c: float(np.abs(_thumb(c) - t).mean()))


def is_plausible_failure(bgr, alpha, *, max_bright=80.0, min_edge=0.06) -> bool:
    """Reject cutouts that are not actually a spaghetti tangle.

    Background differencing catches whatever changed, which on this data
    includes the operator's HAND placing the tangle, bare patches of bed where
    it had moved, and slices of toolhead. Those were being pasted in and
    labelled "spaghetti" -- teaching the detector that hands and bed texture are
    print failures, which is worse than not training at all.

    Two properties separate them, measured over the 58 real cutouts:
      * black PLA is DARK -- tangles sit at mean grey 16-60, while bed texture
        lands near 100-103, the metal rail 118-125 and lit skin 135-146;
      * a tangle is STRINGY, so edge density inside the mask is high, whereas
        skin and smooth panels are flat.
    A colour-based skin test was tried first and abandoned: the tan bed texture
    occupies the same YCrCb range as skin, so it flagged 49 of 58 cutouts.
    """
    m = alpha > 40
    if m.sum() < 500:
        return False
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if float(g[m].mean()) > max_bright:
        return False
    edges = cv2.Canny(g, 60, 160) > 0
    return float(edges[m].mean()) >= min_edge


def bed_mask(bg_bgr):
    """Boolean mask of the bed surface in a clean frame.

    Pastes must land ON the plate. Confining them to the ROI rectangle was not
    enough -- tangles ended up floating on the metal rail and in the room behind,
    which is not a failure mode the printer can produce.

    Found from the image itself, NOT from a fixed band. An earlier version
    searched a hardcoded top-half rectangle and picked the wrong region
    entirely: these backgrounds were captured at several homed bed positions
    where the plate sits lower in frame than it does mid-print, so the band was
    mostly room and tangles ended up pasted into thin air above the printer.
    Nothing about the bed's position in frame is safe to hardcode.

    The plate is the large, mid-dark, TEXTURED region: the powder-coated surface
    has strong high-frequency speckle, whereas the walls, panels and desk behind
    are smooth. Texture is what separates them; brightness alone does not.
    """
    h, w = bg_bgr.shape[:2]
    g = cv2.cvtColor(bg_bgr, cv2.COLOR_BGR2GRAY)
    # Local standard deviation = texture energy.
    f = g.astype(np.float32)
    mu = cv2.blur(f, (15, 15))
    sd = cv2.sqrt(cv2.blur((f - mu) ** 2, (15, 15)))
    m = ((sd > 9) & (g > 40) & (g < 165)).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((31, 31), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((21, 21), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    if n < 2:
        return np.zeros((h, w), bool)
    i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return lab == i


def extract_cutout(spag_path, clean_paths, *, thresh=38, min_area=3000):
    """-> (bgr_crop, alpha_crop) of the failure, or None if nothing found."""
    img = cv2.imread(str(spag_path))
    bg = cv2.imread(str(best_background(spag_path, clean_paths)))
    if img is None or bg is None or img.shape != bg.shape:
        return None
    h, w = img.shape[:2]
    y1 = int((BED_ROI[1] + BED_ROI[3]) * h)

    diff = cv2.absdiff(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY),
                       cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY))
    mask = (diff > thresh).astype(np.uint8)
    mask[y1:] = 0                                     # bed region only
    k = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))

    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n < 2:
        return None
    i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if stats[i, cv2.CC_STAT_AREA] < min_area:
        return None
    x, y, bw, bh = (stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_TOP],
                    stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT])
    alpha = ((lab == i).astype(np.uint8) * 255)[y:y+bh, x:x+bw]
    # Feather the edge: a hard cut leaves a 1px seam that a CNN can key on,
    # learning "sharp silhouette" instead of "spaghetti".
    alpha = cv2.GaussianBlur(alpha, (7, 7), 0)
    crop = img[y:y+bh, x:x+bw].copy()
    if not is_plausible_failure(crop, alpha):
        return None
    return crop, alpha


def paste(bg, cutout, alpha, *, rng, max_shift, src_centre, plate=None):
    """Alpha-composite a jittered cutout onto bg. -> (image, bbox) or None."""
    H, W = bg.shape[:2]
    scale = rng.uniform(0.55, 1.45)
    ang = rng.uniform(-25, 25)
    ch, cw = cutout.shape[:2]
    nw, nh = max(8, int(cw * scale)), max(8, int(ch * scale))
    c = cv2.resize(cutout, (nw, nh), interpolation=cv2.INTER_AREA)
    al = cv2.resize(alpha, (nw, nh), interpolation=cv2.INTER_AREA)
    if rng.random() < 0.5:
        c, al = cv2.flip(c, 1), cv2.flip(al, 1)
    M = cv2.getRotationMatrix2D((nw / 2, nh / 2), ang, 1.0)
    c = cv2.warpAffine(c, M, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=0)
    al = cv2.warpAffine(al, M, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=0)

    # Lighting jitter, so the model cannot key on the exact pixel values of the
    # handful of source tangles.
    c = np.clip(c.astype(np.float32) * rng.uniform(0.82, 1.18)
                + rng.uniform(-14, 14), 0, 255).astype(np.uint8)

    # Placement: sample an actual PLATE PIXEL as the resting point, rather than
    # a rectangle. The rectangle version put tangles in mid-air above the
    # printer, because where the plate sits in frame varies with the bed's Y
    # position and cannot be hardcoded. Sampling the mask is correct by
    # construction: every candidate point is, by definition, on the plate.
    if plate is None or not plate.any():
        return None
    ys, xs = np.nonzero(plate)
    # Bias toward the source position -- see the fisheye note in the module
    # docstring. Fall back to the whole plate if that neighbourhood is empty.
    sx, _sy = src_centre
    near = np.abs(xs - sx) <= max_shift * W
    if near.sum() > 50:
        ys, xs = ys[near], xs[near]
    j = rng.randrange(len(xs))
    # The sampled pixel is where the tangle RESTS, so it is the bottom-centre.
    cx, rest_y = int(xs[j]), int(ys[j])
    x0, y0 = int(cx - nw / 2), int(rest_y - nh * 0.85)
    x1, y1 = x0 + nw, y0 + nh
    if x0 < 0 or y0 < 0 or x1 > W or y1 > H:
        return None

    a = (al.astype(np.float32) / 255.0)[..., None]
    out = bg.copy()
    out[y0:y1, x0:x1] = (c * a + out[y0:y1, x0:x1] * (1 - a)).astype(np.uint8)

    ys, xs = np.where(al > 40)                 # tight box on actual pixels
    if len(xs) == 0:
        return None
    bx0, bx1 = x0 + int(xs.min()), x0 + int(xs.max())
    by0, by1 = y0 + int(ys.min()), y0 + int(ys.max())
    return out, (bx0, by0, bx1, by1)


def yolo_line(box, W, H, cls=0):
    x0, y0, x1, y1 = box
    return (f"{cls} {((x0+x1)/2)/W:.6f} {((y0+y1)/2)/H:.6f} "
            f"{(x1-x0)/W:.6f} {(y1-y0)/H:.6f}")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--src", type=pathlib.Path,
                   default=pathlib.Path("datasets") / "a1_camera")
    p.add_argument("--out", type=pathlib.Path,
                   default=pathlib.Path("datasets") / "synth")
    p.add_argument("--per-bg", type=int, default=10,
                   help="synthetic images generated per clean background")
    p.add_argument("--max-objects", type=int, default=2,
                   help="up to this many tangles per image")
    p.add_argument("--max-shift", type=float, default=0.22,
                   help="max paste displacement from the source position, as a "
                        "fraction of the frame (see the fisheye note)")
    p.add_argument("--val-frac", type=float, default=0.2)
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args()

    rng = random.Random(a.seed)
    np.random.seed(a.seed)
    images = a.src / "images"
    clean, spag = load_pairs(images)
    print(f"source: {len(clean)} clean, {len(spag)} spaghetti")

    cutouts = []
    for sp in spag:
        got = extract_cutout(sp, clean)
        if got is None:
            continue
        img = cv2.imread(str(sp))
        H, W = img.shape[:2]
        # remember where it came from, to constrain how far we move it
        c, al = got
        m = cv2.moments((al > 40).astype(np.uint8))
        if m["m00"] == 0:
            continue
        cutouts.append((c, al))
    print(f"extracted {len(cutouts)} cutouts from {len(spag)} failure frames")
    if not cutouts:
        raise SystemExit("no cutouts extracted -- check the threshold")

    for split in ("train", "val"):
        (a.out / "images" / split).mkdir(parents=True, exist_ok=True)
        (a.out / "labels" / split).mkdir(parents=True, exist_ok=True)

    made = {"train": 0, "val": 0}
    n = 0
    for bgp in clean:
        bg0 = cv2.imread(str(bgp))
        if bg0 is None:
            continue
        H, W = bg0.shape[:2]
        plate = bed_mask(bg0)
        if plate.sum() < 0.02 * H * W:
            print(f"  skipping {bgp.name}: no plate found")
            continue
        for _ in range(a.per_bg):
            split = "val" if rng.random() < a.val_frac else "train"
            img = bg0.copy()
            lines = []
            for _o in range(rng.randint(1, a.max_objects)):
                c, al = cutouts[rng.randrange(len(cutouts))]
                src_c = (W * 0.45, H * 0.22)       # typical source location
                r = paste(img, c, al, rng=rng, max_shift=a.max_shift,
                          src_centre=src_c, plate=plate)
                if r is None:
                    continue
                img, box = r
                lines.append(yolo_line(box, W, H))
            if not lines:
                continue
            n += 1
            stem = f"syn_{n:06d}"
            cv2.imwrite(str(a.out / "images" / split / f"{stem}.jpg"), img,
                        [cv2.IMWRITE_JPEG_QUALITY, 92])
            (a.out / "labels" / split / f"{stem}.txt").write_text(
                "\n".join(lines) + "\n", encoding="utf-8")
            made[split] += 1

    # Real clean frames as negatives, with EMPTY label files. Without these the
    # model has never seen a bed that is fine and will call every print a
    # failure.
    for i, bgp in enumerate(clean):
        split = "val" if rng.random() < a.val_frac else "train"
        stem = f"neg_{i:05d}"
        img = cv2.imread(str(bgp))
        if img is None:
            continue
        cv2.imwrite(str(a.out / "images" / split / f"{stem}.jpg"), img,
                    [cv2.IMWRITE_JPEG_QUALITY, 92])
        (a.out / "labels" / split / f"{stem}.txt").write_text("", encoding="utf-8")
        made[split] += 1

    (a.out / "data.yaml").write_text(
        f"path: {a.out.resolve().as_posix()}\n"
        f"train: images/train\nval: images/val\n"
        f"nc: {len(CLASS_NAMES)}\nnames: {CLASS_NAMES}\n", encoding="utf-8")

    print(f"wrote {made['train']} train / {made['val']} val images to {a.out}")
    print(f"data.yaml -> {a.out / 'data.yaml'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
