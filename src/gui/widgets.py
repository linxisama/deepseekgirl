"""自定义 GUI 控件"""

import html
from datetime import datetime
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QLabel,
    QProgressBar, QFrame, QPushButton
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QColor, QTextCursor


class LogViewer(QTextEdit):
    """日志查看器：带颜色和自动滚动"""

    MAX_LINES = 5000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Consolas", 9))
        self._line_count = 0

    def append_log(
        self,
        level: str,
        message: str,
        timestamp: str | None = None,
    ) -> None:
        timestamp = timestamp or datetime.now().strftime("%H:%M:%S")
        color_map = {
            "INFO": "#4CAF50",
            "WARNING": "#FF9800",
            "ERROR": "#F44336",
            "DEBUG": "#9E9E9E",
            "SUCCESS": "#2196F3",
        }
        color = color_map.get(level, "#FFFFFF")
        safe_timestamp = html.escape(str(timestamp))
        safe_level = html.escape(str(level))
        safe_message = html.escape(str(message)).replace("\n", "<br>")
        markup = (
            f'<span style="color:#888">[{safe_timestamp}]</span> '
            f'<span style="color:{color}">[{safe_level}]</span> '
            f"{safe_message}"
        )
        self.append(markup)
        self._line_count += 1

        # 限制行数
        if self._line_count > self.MAX_LINES:
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.movePosition(
                QTextCursor.MoveOperation.Down,
                QTextCursor.MoveMode.KeepAnchor,
                self._line_count - self.MAX_LINES,
            )
            cursor.removeSelectedText()
            self._line_count = self.MAX_LINES

        # 自动滚动到底部
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())


class StatusPanel(QFrame):
    """状态面板：显示各模块状态"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameStyle(QFrame.Shape.StyledPanel)
        self.setStyleSheet("""
            StatusPanel {
                background-color: #1e1e2e;
                border-radius: 8px;
                padding: 12px;
            }
        """)
        layout = QHBoxLayout(self)
        layout.setSpacing(20)

        self.wechat_status = self._create_status_widget("微信连接")
        self.qq_status = self._create_status_widget("QQ 连接")
        self.ai_status = self._create_status_widget("AI 引擎")
        self.queue_status = self._create_status_widget("消息队列")
        self.style_status = self._create_status_widget("当前风格")

        for widget in [
            self.wechat_status,
            self.qq_status,
            self.ai_status,
            self.queue_status,
            self.style_status,
        ]:
            layout.addWidget(widget)

    def _create_status_widget(self, label: str) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel(label)
        title.setStyleSheet("color: #888; font-size: 11px;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        value = QLabel("--")
        value.setStyleSheet("color: #fff; font-size: 14px; font-weight: bold;")
        value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        value.setObjectName("value")

        layout.addWidget(title)
        layout.addWidget(value)
        return widget

    def update_status(self, name: str, value: str, color: str = "#fff") -> None:
        widget_map = {
            "wechat": self.wechat_status,
            "qq": self.qq_status,
            "ai": self.ai_status,
            "queue": self.queue_status,
            "style": self.style_status,
        }
        widget = widget_map.get(name)
        if widget:
            label = widget.findChild(QLabel, "value")
            if label:
                label.setText(value)
                label.setStyleSheet(f"color: {color}; font-size: 14px; font-weight: bold;")


class MessageList(QFrame):
    """消息列表：实时显示收发的消息"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameStyle(QFrame.Shape.StyledPanel)
        self.setStyleSheet("""
            MessageList {
                background-color: #1e1e2e;
                border-radius: 8px;
            }
        """)
        layout = QVBoxLayout(self)

        header = QLabel("消息流")
        header.setStyleSheet("color: #fff; font-size: 13px; font-weight: bold; padding: 8px;")
        layout.addWidget(header)

        self.viewer = LogViewer()
        self.viewer.setStyleSheet("background-color: #1e1e2e; border: none;")
        layout.addWidget(self.viewer)

    def add_incoming(self, room: str, sender: str, content: str) -> None:
        self.viewer.append_log("INFO", f"[{room}] {sender}: {content}")

    def add_outgoing(self, room: str, content: str) -> None:
        self.viewer.append_log("SUCCESS", f"[{room}] 回复: {content}")

    def add_event(self, level: str, message: str) -> None:
        self.viewer.append_log(level, message)
