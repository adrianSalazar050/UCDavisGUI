"""Guards against documentation rot.

These are real tests, not linting for its own sake. Each one exists because
the failure it catches actually happened in this repo:

* Hardcoded test counts went stale FIVE times in a single day's work
  (261 -> 270 -> 299 -> 316 -> 358 -> 362), restated across two documents.
  They are now banned from the maintained docs.
* A doc linked `LoDISA-GUI/COLORS.md`, a file that lives in a different
  repository entirely, so the link had never once resolved.
* master.md's own cross-references (`section 5.3` and friends) broke when a
  section was renumbered.

`docs/superpowers/` is exempt from the count and heading rules: it is an
explicitly historical record (see its README) whose stale numbers are called
out by each file's banner rather than corrected in place. Its LINKS are still
checked, because a broken link is never useful.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
HISTORICAL = "superpowers"

MAINTAINED = ("README.md", "master.md", "CONNECTION.md",
              "FAILURE_DETECTOR_REPORT.md", "FRONTEND-STACK-GUIDE.md")

# [text](target) and [text](target#anchor); the anchor is not verified.
LINK = re.compile(r"\[[^\]]*\]\(([^)#]+?)(?:#[^)]*)?\)")
SECTION_REF = re.compile(r"§(\d+(?:\.\d+)?)")
# "362 tests", "228 passed", "316 Python tests" -- any hardcoded suite size.
TEST_COUNT = re.compile(r"\b\d{2,4}\s+(?:tests|passed|Python tests)\b")


def all_markdown() -> list[pathlib.Path]:
    files = [ROOT / name for name in MAINTAINED]
    files += sorted((ROOT / "docs").rglob("*.md"))
    return [f for f in files if f.exists()]


def maintained_markdown() -> list[pathlib.Path]:
    return [f for f in all_markdown() if HISTORICAL not in f.parts]


def numbered_headings(path: pathlib.Path) -> set[str]:
    found = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^#{1,4}\s+(\d+(?:\.\d+)?)[.\s]", line)
        if m:
            found.add(m.group(1))
    return found


@pytest.mark.parametrize("path", all_markdown(), ids=lambda p: p.name)
def test_every_relative_link_resolves(path):
    text = path.read_text(encoding="utf-8")
    broken = []
    for target in LINK.finditer(text):
        href = target.group(1)
        if href.startswith(("http://", "https://", "mailto:")):
            continue
        if not (path.parent / href).resolve().exists():
            line = text[:target.start()].count("\n") + 1
            broken.append(f"{path.name}:{line} -> {href}")
    assert not broken, (
        "Markdown links pointing at files that do not exist:\n  "
        + "\n  ".join(broken)
        + "\nIf the target lives in another repository, write it as plain "
          "text rather than a link.")


def test_master_section_references_exist():
    master = ROOT / "master.md"
    headings = numbered_headings(master)
    text = master.read_text(encoding="utf-8")
    dangling = []
    for m in SECTION_REF.finditer(text):
        if m.group(1) not in headings:
            line = text[:m.start()].count("\n") + 1
            dangling.append(f"master.md:{line} -> §{m.group(1)}")
    assert not dangling, (
        "master.md cross-references sections that do not exist:\n  "
        + "\n  ".join(dangling)
        + "\nRenumbering a section means updating every §N that points at it "
          "(including the ones in server/*.py comments).")


@pytest.mark.parametrize("path", maintained_markdown(), ids=lambda p: p.name)
def test_no_hardcoded_test_counts(path):
    text = path.read_text(encoding="utf-8")
    hits = []
    for m in TEST_COUNT.finditer(text):
        line = text[:m.start()].count("\n") + 1
        hits.append(f"{path.name}:{line} {m.group(0)!r}")
    assert not hits, (
        "Hardcoded suite sizes in maintained documentation:\n  "
        + "\n  ".join(hits)
        + "\nThese go stale on the next commit that adds a test -- it happened "
          "five times in one day. Describe what the tests COVER and let "
          "`python -m pytest -q` report the number.")


def test_historical_docs_are_labelled_as_historical():
    """Every file under docs/superpowers must declare its status up front.

    Without this, a shipped plan with unticked checkboxes reads as open work:
    one spec in here describes a PyQt6 GUI that was never built and was marked
    "Approved by user", which is exactly the kind of thing an agent picks up
    and starts implementing.
    """
    unlabelled = []
    for path in sorted((ROOT / "docs" / HISTORICAL).rglob("*.md")):
        if path.name == "README.md":
            continue
        head = path.read_text(encoding="utf-8")[:800]
        if "STATUS:" not in head:
            unlabelled.append(path.name)
    assert not unlabelled, (
        "Files under docs/superpowers with no STATUS banner in their first "
        "800 characters:\n  " + "\n  ".join(unlabelled)
        + "\nAdd one saying SHIPPED / SUPERSEDED / ABANDONED, so the file "
          "cannot be mistaken for pending work.")
