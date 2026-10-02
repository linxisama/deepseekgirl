"""系统托盘图标：状态指示、快捷操作"""

from PySide6.QtWidgets import QSystemTrayIcon, QMenu, QApplication
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont
from PySide6.QtCore import Signal


class TrayIcon(QSystemTrayIcon):
    """系统托盘图标"""

    show_window = Signal()
    toggle_bot = Signal()
    quit_app = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._status = "disconnected"
        self._create_icon()
        self._create_menu()
        self.activated.connect(self._on_activated)

    def _create_icon(self) -> None:
        """创建托盘图标"""
        self._update_icon("#F44336")  # 默认红色（断开）

    def _update_icon(self, color: str) -> None:
        """更新图标颜色"""
        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(color))
        painter.setPen(QColor(color))
        painter.drawEllipse(4, 4, 24, 24)
        # 画一个简化的聊天气泡
        painter.setBrush(QColor("#fff"))
        painter.setPen(QColor("#fff"))
        font = QFont("Arial", 14, QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), 0x0084, "B")  # AlignCenter
        painter.end()
        self.setIcon(QIcon(pixmap))

    def _create_menu(self) -> None:
        """创建右键菜单"""
        menu = QMenu()

        self._show_action = menu.addAction("显示主窗口")
        self._show_action.triggered.connect(self.show_window.emit)

        menu.addSeparator()

        self._toggle_action = menu.addAction("启动机器人")
        self._toggle_action.triggered.connect(self.toggle_bot.emit)

        menu.addSeparator()

        quit_action = menu.addAction("退出")
        quit_action.triggered.connect(self.quit_app.emit)

        self.setContextMenu(menu)

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_window.emit()

    def set_status(self, status: str) -> None:
        """更新状态：connected/disconnected/running/error"""
        self._status = status
        color_map = {
            "connected": "#4CAF50",
            "running": "#2196F3",
            "disconnected": "#FF9800",
            "error": "#F44336",
        }
        color = color_map.get(status, "#888")
        self._update_icon(color)

        tooltip_map = {
            "connected": "已连接微信",
            "running": "运行中",
            "disconnected": "未连接",
            "error": "错误",
        }
        self.setToolTip(tooltip_map.get(status, "未知状态"))

    def set_running(self, running: bool) -> None:
        self._toggle_action.setText("停止机器人" if running else "启动机器人")
