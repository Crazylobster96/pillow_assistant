"""R0.5 unit tests (non-GUI): references field, Session, reference materialization.

Run headless: ``python tests/test_r05.py``.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pillow_assistant.contracts import AppRequest
from pillow_assistant.core import llm, references
from pillow_assistant.core.session import Session

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def test_contract_references():
    print("contract references field")
    r = AppRequest(prompt="q", references=["/a/b.txt"])
    check("references stored", r.references == ["/a/b.txt"])
    check("default empty", AppRequest(prompt="x").references == [])


def test_session():
    print("session (paths only, no copy)")
    s = Session()
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "data.csv"
        f.write_text("a,b\n1,2\n", "utf-8")
        added = s.add_reference(str(f))
        check("added once", added is True and len(s) == 1)
        check("dedup", s.add_reference(str(f)) is False and len(s) == 1)
        check("stored absolute path", s.references[0] == str(Path(f).expanduser()))
        check("original file untouched", f.read_text("utf-8") == "a,b\n1,2\n")
        s.remove_reference(str(f))
        check("removed", len(s) == 0)
        s.add_reference(str(f))
        s.clear()
        check("cleared", len(s) == 0)


def test_materialize():
    print("reference materialization")
    with tempfile.TemporaryDirectory() as d:
        txt = Path(d) / "note.md"
        txt.write_text("# Hello\nbody", "utf-8")
        sub = Path(d) / "sub"
        sub.mkdir()
        (sub / "x.py").write_text("print(1)", "utf-8")
        img = Path(d) / "pic.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n fake")
        missing = str(Path(d) / "nope.txt")

        ctx, images = references.materialize([str(txt), str(sub), str(img), missing])
        check("text inlined", "# Hello" in ctx and "```" in ctx)
        check("dir listed", "x.py" in ctx and "目录" in ctx)
        check("image collected", images == [str(img)])
        check("missing noted", "引用缺失" in ctx)


def test_build_messages_multi_image():
    print("build_messages with image list")
    with tempfile.TemporaryDirectory() as d:
        a = Path(d) / "a.png"
        a.write_bytes(b"\x89PNG fake a")
        msgs = llm.build_messages("look", [str(a)])
        content = msgs[0]["content"]
        check("multimodal content list", isinstance(content, list))
        kinds = [c["type"] for c in content]
        check("has text + image", "text" in kinds and "image_url" in kinds)
        check("text only when no images", llm.build_messages("hi")[0]["content"] == "hi")


if __name__ == "__main__":
    for t in (test_contract_references, test_session, test_materialize, test_build_messages_multi_image):
        t()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
