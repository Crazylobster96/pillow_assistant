"""Pure (no-Qt) preview extractors, so they're unit-testable headless.

Used by the table and archive panels.
"""

from __future__ import annotations

import csv
import os
import tarfile
import zipfile
from pathlib import Path

MAX_ROWS = 100
MAX_COLS = 40
MAX_ENTRIES = 500


def read_table_rows(path: str | Path, max_rows: int = MAX_ROWS) -> list[list[str]]:
    """Return up to ``max_rows`` rows (incl. header) as lists of strings.

    xlsx/xls require openpyxl (raises ImportError if missing).
    """
    p = Path(path)
    ext = p.suffix.lower()
    rows: list[list[str]] = []
    if ext in {".csv", ".tsv"}:
        delim = "\t" if ext == ".tsv" else ","
        with p.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            for i, row in enumerate(csv.reader(fh, delimiter=delim)):
                if i >= max_rows:
                    break
                rows.append([str(c) for c in row[:MAX_COLS]])
    elif ext in {".xlsx", ".xls"}:
        import openpyxl  # optional dependency

        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        ws = wb.active
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= max_rows:
                break
            rows.append(["" if c is None else str(c) for c in row[:MAX_COLS]])
        wb.close()
    return rows


def list_entries(path: str | Path, max_entries: int = MAX_ENTRIES) -> list[str]:
    """List entries inside a folder or archive (relative names)."""
    p = Path(path)
    names: list[str] = []
    if p.is_dir():
        for root, dirs, files in os.walk(p):
            rel_root = os.path.relpath(root, p)
            for d in sorted(dirs):
                names.append(("" if rel_root == "." else rel_root + os.sep) + d + os.sep)
            for f in sorted(files):
                names.append(("" if rel_root == "." else rel_root + os.sep) + f)
            if len(names) >= max_entries:
                break
    elif zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as zf:
            names = zf.namelist()[:max_entries]
    else:
        try:
            with tarfile.open(p) as tf:
                names = tf.getnames()[:max_entries]
        except (tarfile.TarError, OSError):
            names = []
    return names[:max_entries]
