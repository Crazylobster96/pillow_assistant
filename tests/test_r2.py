"""R2 unit tests (non-GUI): file-type classification + preview extractors.

Run: ``python tests/test_r2.py``. GUI panels are syntax-checked separately.
Note: these modules avoid importing PySide6 so they run headless.
"""

from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pillow_assistant.ui import viewer_registry
from pillow_assistant.ui.panels.extract import list_entries, read_table_rows

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def test_classify():
    print("classify by extension / folder")
    check("image", viewer_registry.classify("a.PNG") == "image")
    check("pdf", viewer_registry.classify("x.pdf") == "pdf")
    check("table csv", viewer_registry.classify("d.csv") == "table")
    check("table xlsx", viewer_registry.classify("d.xlsx") == "table")
    check("code py", viewer_registry.classify("s.py") == "code")
    check("media mp4", viewer_registry.classify("v.mp4") == "media")
    check("archive zip", viewer_registry.classify("a.zip") == "archive")
    check("generic unknown", viewer_registry.classify("x.bin") == "generic")
    with tempfile.TemporaryDirectory() as d:
        check("folder", viewer_registry.classify(d) == "folder")


def test_panel_kind():
    print("panel_kind (drop -> kind)")
    check("single csv -> table", viewer_registry.panel_kind(["a.csv"]) == "table")
    check("multi -> multi", viewer_registry.panel_kind(["a.csv", "b.png"]) == "multi")
    check("single py -> code", viewer_registry.panel_kind(["x.py"]) == "code")


def test_read_table_rows():
    print("read_table_rows (csv)")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "t.csv"
        f.write_text("a,b,c\n1,2,3\n4,5,6\n", "utf-8")
        rows = read_table_rows(f)
        check("header parsed", rows[0] == ["a", "b", "c"])
        check("row count", len(rows) == 3)
        check("cells strings", rows[1] == ["1", "2", "3"])


def test_list_entries():
    print("list_entries (folder + zip)")
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "sub").mkdir()
        (base / "sub" / "x.txt").write_text("hi", "utf-8")
        (base / "top.md").write_text("# t", "utf-8")
        names = list_entries(base)
        check("folder lists file", "top.md" in names)
        check("folder lists nested", any("x.txt" in n for n in names))

        zpath = base / "a.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("inside/readme.txt", "hello")
        znames = list_entries(zpath)
        check("zip lists entry", any("readme.txt" in n for n in znames))


if __name__ == "__main__":
    for t in (test_classify, test_panel_kind, test_read_table_rows, test_list_entries):
        t()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
