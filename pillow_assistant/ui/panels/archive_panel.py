"""Folder / archive listing panel (zip, tar, directories)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QLabel, QListWidget, QVBoxLayout

from pillow_assistant.core.i18n import t
from pillow_assistant.ui.panels.base_panel import FilePanel
from pillow_assistant.ui.panels.extract import MAX_ENTRIES, list_entries


TREE_QSS = """
QTreeView { background: rgba(10, 14, 20, 235); color: #F4F8FC;
    border: 1px solid rgba(255,255,255,40); border-radius: 8px; }
QTreeView::item:selected { background: #4a82c0; }
"""


class ArchivePanel(FilePanel):
    TITLE = t("panel.archive")

    def build_preview(self, layout: QVBoxLayout) -> None:
        self._selected_files: list = []  # files picked in the tree -> question targets them
        self._fs_model = None
        if len(self.paths) != 1:
            listing = QListWidget(self)
            listing.addItems([Path(p).name for p in self.paths])
            layout.addWidget(listing)
            return
        p = Path(self.paths[0])
        if p.is_dir():
            # A real expandable tree of the folder structure (lazy, system model).
            try:
                try:
                    from PySide6.QtGui import QFileSystemModel
                except ImportError:  # Qt<6.4 keeps it in QtWidgets
                    from PySide6.QtWidgets import QFileSystemModel
                from PySide6.QtWidgets import QAbstractItemView, QTreeView

                model = QFileSystemModel(self)
                model.setRootPath(str(p))
                tree = QTreeView(self)
                tree.setModel(model)
                tree.setRootIndex(model.index(str(p)))
                tree.setHeaderHidden(True)
                tree.setAnimated(True)
                # Ctrl/Shift multi-select across files.
                tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
                for col in range(1, model.columnCount()):
                    tree.hideColumn(col)  # name only; sizes/dates add noise
                tree.setStyleSheet(TREE_QSS)
                self._fs_model = model
                tree.selectionModel().selectionChanged.connect(
                    lambda *_, tv=tree: self._on_tree_selection(tv))
                layout.addWidget(tree)
                self._sel_label = QLabel(t("panel.archive_hint"), self)
                self._sel_label.setWordWrap(True)
                self._sel_label.setStyleSheet("color:#9fc0ec; font-size:12px;")
                layout.addWidget(self._sel_label)
                return
            except Exception:
                pass  # fall through to the flat listing
        try:
            names = list_entries(self.paths[0])
        except Exception as exc:
            layout.addWidget(QLabel(t("panel.archive_read_failed", err=exc), self))
            return
        if not names:
            layout.addWidget(QLabel(t("panel.archive_empty"), self))
            return
        listing = QListWidget(self)
        listing.addItems(names)
        layout.addWidget(listing)
        if len(names) >= MAX_ENTRIES:
            layout.addWidget(QLabel(t("panel.archive_first_n", n=MAX_ENTRIES), self))

    # -- file selection inside the folder tree (Ctrl/Shift multi-select) -----
    def _on_tree_selection(self, tree) -> None:
        model = self._fs_model
        files: list = []
        if model is not None:
            seen = set()
            for idx in tree.selectedIndexes():
                # selectedIndexes yields one index per (row, column); de-dup by row.
                key = (idx.row(), idx.parent())
                if key in seen:
                    continue
                seen.add(key)
                path = Path(model.filePath(idx))
                if path.is_file():
                    files.append(str(path))
        self._selected_files = files

        label = getattr(self, "_sel_label", None)
        if label is not None:
            if not files:
                label.setText(t("panel.archive_whole"))
            elif len(files) == 1:
                label.setText(t("panel.archive_selected", name=Path(files[0]).name))
            else:
                names = "、".join(Path(f).name for f in files[:5])
                if len(files) > 5:
                    names += " …"
                label.setText(t("panel.archive_selected_n", n=len(files), names=names))

    def _question_references(self) -> list:
        # Files selected in the tree scope the question to just those files.
        if getattr(self, "_selected_files", None):
            return list(self._selected_files)
        return super()._question_references()
