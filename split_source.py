"""Split the collected frames into disjoint train / test halves.

Without this the evaluation is circular. The first run scored mAP50 0.71 with
100% recall and 0% false alarms on "real" frames -- but all 49 clean frames used
as negatives were themselves training negatives, and all 29 positives had their
tangle cut out and pasted into ~600 training composites. The model was being
asked about pixels it had memorised.

Splitting is done in BLOCKS of consecutive frames, not at random. Frames were
captured seconds apart, so neighbours are near-duplicates: a random split would
put a frame in train and its twin in test, which leaks just as effectively as
sharing the frame outright.

    python split_source.py                  # -> datasets/a1_train, a1_test

Then:
    python synth_dataset.py   --src datasets/a1_train --out datasets/synth_ho
    python build_real_eval.py --src datasets/a1_test  --out datasets/real_ho
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import shutil


def block_split(names, block, test_every):
    """Alternate blocks of consecutive frames between train and test."""
    train, test = [], []
    for i, n in enumerate(names):
        (test if (i // block) % test_every == 0 else train).append(n)
    return train, test


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--src", type=pathlib.Path,
                   default=pathlib.Path("datasets") / "a1_camera")
    p.add_argument("--train-out", type=pathlib.Path,
                   default=pathlib.Path("datasets") / "a1_train")
    p.add_argument("--test-out", type=pathlib.Path,
                   default=pathlib.Path("datasets") / "a1_test")
    p.add_argument("--block", type=int, default=4,
                   help="consecutive frames kept on the same side")
    p.add_argument("--test-every", type=int, default=3,
                   help="1 block in N goes to test (3 -> ~33%% test)")
    a = p.parse_args()

    images = a.src / "images"
    for out in (a.train_out, a.test_out):
        if out.exists():
            shutil.rmtree(out)
        (out / "images").mkdir(parents=True)

    counts = {}
    for label in ("clean", "spaghetti"):
        names = sorted(images.glob(f"*_{label}.jpg"))
        tr, te = block_split(names, a.block, a.test_every)
        for group, out in ((tr, a.train_out), (te, a.test_out)):
            for f in group:
                shutil.copy2(f, out / "images" / f.name)
        counts[label] = (len(tr), len(te))

    for out in (a.train_out, a.test_out):
        rows = [["filename", "iso_time", "unix_time", "label"]]
        for f in sorted((out / "images").glob("*.jpg")):
            rows.append([f.name, "", "", f.stem.split("_")[-1]])
        with open(out / "manifest.csv", "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)

    for label, (tr, te) in counts.items():
        print(f"{label:10} train {tr:3d}   test {te:3d}")
    print(f"\ntrain -> {a.train_out}\ntest  -> {a.test_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
