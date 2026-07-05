from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from storage.conversation import ConversationMemoryStore


def _fmt_time(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "-"


class ConversationMemoryPanel(QWidget):
    def __init__(self, db_path: str | Path, parent=None) -> None:
        super().__init__(parent)
        self.store = ConversationMemoryStore(db_path)
        self.store.ensure_schema()
        self._topics: list[dict] = []
        self.setWindowTitle("Conversation Memory")
        self.resize(900, 560)

        root = QHBoxLayout(self)
        left = QVBoxLayout()
        right = QVBoxLayout()

        self.title = QLabel("Topics")
        self.title.setStyleSheet("font-weight: 700; font-size: 15px;")
        self.topic_list = QListWidget()
        self.topic_list.itemSelectionChanged.connect(self._on_topic_selected)
        left.addWidget(self.title)
        left.addWidget(self.topic_list, 1)

        self.detail_title = QLabel("Select a topic")
        self.detail_title.setStyleSheet("font-weight: 700; font-size: 15px;")
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        right.addWidget(self.detail_title)
        right.addWidget(self.detail, 1)

        root.addLayout(left, 1)
        root.addLayout(right, 2)
        self.refresh()

    def refresh(self) -> None:
        self.topic_list.clear()
        self._topics = self.store.list_recent_topics(200)
        for topic in self._topics:
            item = QListWidgetItem(
                f"{topic.get('title') or '(untitled)'}\n"
                f"{_fmt_time(topic.get('last_message_at') or topic.get('updated_at'))} · "
                f"{topic.get('message_count', 0)} turns"
            )
            item.setData(Qt.UserRole, topic.get("id"))
            self.topic_list.addItem(item)
        if not self._topics:
            self.detail.setPlainText("No conversation topics yet.")

    def _on_topic_selected(self) -> None:
        items = self.topic_list.selectedItems()
        if not items:
            return
        topic_id = items[0].data(Qt.UserRole)
        topic = self.store.get_topic(topic_id)
        if not topic:
            return
        turns = self.store.recent_turns(topic_id, 200)
        signals = self.store.list_user_memory_signals(limit=50)
        self.detail_title.setText(topic.get("title") or "Conversation topic")
        lines = [
            f"Title: {topic.get('title', '')}",
            f"Summary: {topic.get('summary', '')}",
            f"Keywords: {', '.join(topic.get('keywords') or [])}",
            f"Updated: {_fmt_time(topic.get('last_message_at') or topic.get('updated_at'))}",
            f"Turns: {topic.get('message_count', 0)}",
            "",
            "History",
            "-------",
        ]
        if not turns:
            lines.append("(no turns)")
        for turn in turns:
            lines.append(f"[{_fmt_time(turn.get('timestamp'))}] User:")
            lines.append(turn.get("user_text") or "")
            if turn.get("assistant_text"):
                lines.append("Assistant:")
                lines.append(turn.get("assistant_text") or "")
            lines.append("")
        lines.extend(["", "User memory beta", "----------------"])
        if signals:
            for signal in signals:
                lines.append(
                    f"- {signal.get('type')} [{signal.get('status')}] "
                    f"{signal.get('content')} (confidence={signal.get('confidence')})"
                )
        else:
            lines.append("(none)")
        self.detail.setPlainText("\n".join(lines))
