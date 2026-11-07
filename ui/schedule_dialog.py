"""定时播放计划编辑对话框（按播放时段配置）。"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QTime
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTimeEdit,
    QVBoxLayout,
)


class ScheduleDialog(QDialog):
    """编辑定时播放计划：每天几点到几点播放哪个文件。

    每项形如 {"start": "08:30", "end": "18:00", "file": "a.mp4",
              "enabled": True, "loop": True}；
    end 留空表示从开始时间一直播到当天结束。
    file 可以是接收目录里的文件名，也可以是本地完整路径。
    """

    def __init__(self, items: list[dict] | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("定时播放计划")
        self.resize(560, 340)
        self._items: list[dict] = [dict(item) for item in (items or [])]

        self.list_view = QListWidget()
        self.list_view.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)

        self.start_edit = QTimeEdit()
        self.start_edit.setDisplayFormat("HH:mm")
        self.start_edit.setTime(QTime.currentTime())
        self.end_edit = QTimeEdit()
        self.end_edit.setDisplayFormat("HH:mm")
        self.end_edit.setTime(QTime.currentTime().addSecs(3600))
        self.chk_end = QCheckBox("到时停止")
        self.chk_end.setChecked(True)
        self.chk_end.toggled.connect(self.end_edit.setEnabled)

        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText("接收目录里的文件名，或本地完整路径")
        choose = QPushButton("选择文件…")
        choose.clicked.connect(self._choose_file)
        self.chk_loop = QCheckBox("循环播放")
        self.chk_loop.setChecked(True)

        add = QPushButton("添加时段")
        add.clicked.connect(self._add)
        remove = QPushButton("删除选中")
        remove.clicked.connect(self._remove)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)

        period = QHBoxLayout()
        period.addWidget(QLabel("开始"))
        period.addWidget(self.start_edit)
        period.addWidget(QLabel("结束"))
        period.addWidget(self.end_edit)
        period.addWidget(self.chk_end)

        form = QHBoxLayout()
        form.addWidget(QLabel("内容"))
        form.addWidget(self.file_edit, 1)
        form.addWidget(choose)
        form.addWidget(self.chk_loop)

        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("每天在时段内自动播放，过了结束时间自动停止（本机时间为准）："))
        layout.addWidget(self.list_view, 1)
        layout.addLayout(period)
        layout.addLayout(form)
        layout.addLayout(buttons)
        self._refresh()

    # ---------- 内部 ----------
    def _refresh(self) -> None:
        self.list_view.clear()
        for item in self._items:
            end = item.get("end") or "当天结束"
            mark = "启用" if item.get("enabled", True) else "停用"
            loop = "循环" if item.get("loop", True) else "单次"
            entry = QListWidgetItem(
                f"{item.get('start')} → {end}  {item.get('file')}  [{loop} / {mark}]")
            self.list_view.addItem(entry)

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择要定时播放的文件", str(Path.home()))
        if path:
            self.file_edit.setText(path)

    def _add(self) -> None:
        name = self.file_edit.text().strip()
        if not name:
            return
        self._items.append({
            "start": self.start_edit.time().toString("HH:mm"),
            "end": self.end_edit.time().toString("HH:mm") if self.chk_end.isChecked() else "",
            "file": name,
            "loop": self.chk_loop.isChecked(),
            "enabled": True,
        })
        self._items.sort(key=lambda item: str(item.get("start")))
        self._refresh()

    def _remove(self) -> None:
        row = self.list_view.currentRow()
        if 0 <= row < len(self._items):
            self._items.pop(row)
            self._refresh()

    def items(self) -> list[dict]:
        return [dict(item) for item in self._items]
