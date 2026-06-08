"""Table preview panel for csv / tsv / xlsx (first N rows); window fits table."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout

from pillow_assistant.core.i18n import t
from pillow_assistant.ui.panels.base_panel import FilePanel
from pillow_assistant.ui.panels.extract import read_table_rows


class TablePanel(FilePanel):
    TITLE = t("panel.table")

    def build_preview(self, layout: QVBoxLayout) -> None:
        self._table = None
        try:
            rows = read_table_rows(self.paths[0])
        except ImportError:
            layout.addWidget(QLabel(t("panel.table_need_openpyxl"), self))
            return
        if not rows:
            layout.addWidget(QLabel(t("panel.table_empty"), self))
            return
        ncols = max(len(r) for r in rows)
        table = QTableWidget(len(rows), ncols, self)
        for ri, row in enumerate(rows):
            for ci in range(ncols):
                table.setItem(ri, ci, QTableWidgetItem(row[ci] if ci < len(row) else ""))
        table.resizeColumnsToContents()
        # QTableWidget shows both scrollbars as needed by default.
        self._table = table
        layout.addWidget(table)

    def initial_size(self) -> tuple[int, int]:
        t = getattr(self, "_table", None)
        if t is None:
            return (640, 520)
        width = t.verticalHeader().width() + sum(t.columnWidth(c) for c in range(t.columnCount())) + 70
        rows_shown = min(t.rowCount(), 16)
        height = t.horizontalHeader().height() + sum(t.rowHeight(r) for r in range(rows_shown)) + 210
        return (max(480, width), max(360, height))
