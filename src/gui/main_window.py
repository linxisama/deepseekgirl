"""主窗口：标签页布局，集成所有控制功能"""

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QPushButton, QLabel, QLineEdit, QComboBox, QSpinBox,
    QDoubleSpinBox, QGroupBox, QFormLayout, QCheckBox,
    QMessageBox, QApplication, QTextEdit, QSystemTrayIcon
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QFont, QShortcut, QKeySequence

from .widgets import LogViewer, StatusPanel, MessageList
from .tray_icon import TrayIcon


class MonitorTab(QWidget):
    """监控标签页"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        # 状态面板
        self.status_panel = StatusPanel()
        layout.addWidget(self.status_panel)

        # 消息列表
        self.message_list = MessageList()
        layout.addWidget(self.message_list)

        # 控制按钮
        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("启动机器人")
        self.start_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50; color: white;
                border: none; padding: 10px 24px;
                border-radius: 6px; font-weight: bold;
            }
            QPushButton:hover { background-color: #45a049; }
        """)
        self.stop_btn = QPushButton("停止机器人")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet("""
            QPushButton {
                background-color: #F44336; color: white;
                border: none; padding: 10px 24px;
                border-radius: 6px; font-weight: bold;
            }
            QPushButton:hover { background-color: #da190b; }
            QPushButton:disabled { background-color: #666; }
        """)
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.stop_btn)
        layout.addLayout(btn_layout)


class StyleTab(QWidget):
    """风格标签页"""

    style_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        # 风格选择
        select_group = QGroupBox("当前人设")
        select_layout = QHBoxLayout(select_group)
        self.style_combo = QComboBox()
        self.style_combo.setMinimumWidth(200)
        self.style_desc = QLabel("选择一种风格查看描述")
        self.style_desc.setWordWrap(True)
        select_layout.addWidget(QLabel("当前风格:"))
        select_layout.addWidget(self.style_combo)
        select_layout.addWidget(self.style_desc, 1)
        layout.addWidget(select_group)

        self.lock_note = QLabel("傲娇鲸鱼娘人设已永久锁定")
        self.lock_note.setStyleSheet("color: #FFB74D; font-weight: bold;")
        self.lock_note.hide()
        layout.addWidget(self.lock_note)

        # 风格预览
        preview_group = QGroupBox("风格预览")
        preview_layout = QVBoxLayout(preview_group)
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setMaximumHeight(200)
        preview_layout.addWidget(self.preview_text)
        layout.addWidget(preview_group)

        # 应用按钮
        self.apply_btn = QPushButton("应用风格")
        self.apply_btn.setStyleSheet("""
            QPushButton {
                background-color: #2196F3; color: white;
                border: none; padding: 10px 24px;
                border-radius: 6px; font-weight: bold;
            }
            QPushButton:hover { background-color: #0b7dda; }
        """)
        layout.addWidget(self.apply_btn)
        layout.addStretch()

        self.style_combo.currentTextChanged.connect(self._on_style_changed)

    def _on_style_changed(self, name: str) -> None:
        self.style_changed.emit(name)

    def update_styles(self, styles: list[dict]) -> None:
        self.style_combo.clear()
        for s in styles:
            self.style_combo.addItem(s["display_name"], s["name"])
            if s.get("active"):
                self.style_combo.setCurrentText(s["display_name"])

    def set_preview(self, text: str) -> None:
        self.preview_text.setPlainText(text)

    def set_locked(self, locked: bool, locked_name: str | None = None) -> None:
        self.style_combo.setEnabled(not locked)
        self.apply_btn.setEnabled(not locked)
        self.lock_note.setVisible(locked)
        if locked:
            self.lock_note.setText("傲娇鲸鱼娘人设已永久锁定，不能更改为其他风格")


class ConfigTab(QWidget):
    """配置标签页"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        # AI 配置
        ai_group = QGroupBox("AI 配置")
        ai_layout = QFormLayout(ai_group)
        self.api_url = QLineEdit()
        self.api_url.setPlaceholderText("https://api.example.com/v1/chat/completions")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("sk-...")
        self.model = QLineEdit()
        self.model.setPlaceholderText("gpt-4")
        self.max_tokens = QSpinBox()
        self.max_tokens.setRange(1, 4000)
        self.max_tokens.setValue(60)
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0, 2)
        self.temperature.setSingleStep(0.1)
        self.temperature.setValue(0.7)

        ai_layout.addRow("API URL:", self.api_url)
        ai_layout.addRow("API Key:", self.api_key)
        ai_layout.addRow("模型:", self.model)
        ai_layout.addRow("最大Token:", self.max_tokens)
        ai_layout.addRow("Temperature:", self.temperature)
        layout.addWidget(ai_group)

        # 微信配置
        wx_group = QGroupBox("微信配置")
        wx_layout = QFormLayout(wx_group)
        self.target_groups = QLineEdit()
        self.target_groups.setPlaceholderText("群名1, 群名2（逗号分隔）")
        self.reply_delay = QDoubleSpinBox()
        self.reply_delay.setRange(0, 30)
        self.reply_delay.setValue(0.0)
        self.reply_delay.setSuffix(" 秒")
        self.auto_reply = QCheckBox("启用自动回复")
        self.auto_reply.setChecked(True)

        wx_layout.addRow("监控群聊:", self.target_groups)
        wx_layout.addRow("回复延迟:", self.reply_delay)
        wx_layout.addRow(self.auto_reply)
        layout.addWidget(wx_group)

        # 操作按钮
        btn_layout = QHBoxLayout()
        self.test_btn = QPushButton("测试 API 连接")
        self.test_btn.setStyleSheet("""
            QPushButton {
                background-color: #FF9800; color: white;
                border: none; padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #e68a00; }
        """)
        self.save_btn = QPushButton("保存配置")
        self.save_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50; color: white;
                border: none; padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #45a049; }
        """)
        btn_layout.addWidget(self.test_btn)
        btn_layout.addWidget(self.save_btn)
        layout.addLayout(btn_layout)
        layout.addStretch()


class LogTab(QWidget):
    """日志标签页"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        self.log_viewer = LogViewer()
        layout.addWidget(self.log_viewer)

        btn_layout = QHBoxLayout()
        clear_btn = QPushButton("清空日志")
        clear_btn.clicked.connect(self.log_viewer.clear)
        btn_layout.addStretch()
        btn_layout.addWidget(clear_btn)
        layout.addLayout(btn_layout)


class MainWindow(QMainWindow):
    """主窗口"""

    # 信号
    start_requested = Signal()
    stop_requested = Signal()
    style_change_requested = Signal(str)
    config_save_requested = Signal()
    api_test_requested = Signal()
    _gui_event = Signal(str, dict)  # 线程安全的 GUI 更新信号
    _log_event = Signal(str, str)   # 线程安全的日志更新信号

    def __init__(self):
        super().__init__()
        self.setWindowTitle("微信群聊 AI 机器人")
        self.setMinimumSize(900, 650)
        self._setup_ui()
        self._setup_tray()

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # 线程安全的 GUI 更新信号
        self._gui_event.connect(self._handle_gui_event)
        self._log_event.connect(self.add_log)

        # Ctrl+Q 退出
        QShortcut(QKeySequence("Ctrl+Q"), self, self.close)

        # 标题
        title = QLabel("微信群聊 AI 机器人")
        title.setFont(QFont("Microsoft YaHei", 16, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color: #fff; padding: 12px;")
        layout.addWidget(title)

        # 标签页
        self.tabs = QTabWidget()
        self.monitor_tab = MonitorTab()
        self.style_tab = StyleTab()
        self.config_tab = ConfigTab()
        self.log_tab = LogTab()

        self.tabs.addTab(self.monitor_tab, "监控")
        self.tabs.addTab(self.style_tab, "风格")
        self.tabs.addTab(self.config_tab, "配置")
        self.tabs.addTab(self.log_tab, "日志")

        layout.addWidget(self.tabs)

        # 连接信号
        self.monitor_tab.start_btn.clicked.connect(self.start_requested.emit)
        self.monitor_tab.stop_btn.clicked.connect(self.stop_requested.emit)
        self.style_tab.apply_btn.clicked.connect(self._on_apply_style)
        self.config_tab.test_btn.clicked.connect(self.api_test_requested.emit)
        self.config_tab.save_btn.clicked.connect(self.config_save_requested.emit)

        # 全局样式
        self.setStyleSheet("""
            QMainWindow { background-color: #12121a; }
            QTabWidget::pane { border: 1px solid #333; background: #1a1a2e; }
            QTabBar::tab {
                background: #1a1a2e; color: #888;
                padding: 10px 20px; margin-right: 2px;
                border-top-left-radius: 6px; border-top-right-radius: 6px;
            }
            QTabBar::tab:selected { background: #16213e; color: #fff; }
            QGroupBox {
                color: #fff; border: 1px solid #333;
                border-radius: 6px; margin-top: 10px; padding-top: 15px;
                font-weight: bold;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QLabel { color: #ccc; }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
                background: #1e1e2e; color: #fff; border: 1px solid #444;
                padding: 6px; border-radius: 4px;
            }
            QTextEdit { background: #1e1e2e; color: #fff; border: 1px solid #444; }
            QCheckBox { color: #ccc; }
        """)

    def _setup_tray(self) -> None:
        self.tray = TrayIcon(self)
        self.tray.show_window.connect(self.show)
        self.tray.toggle_bot.connect(self._toggle_bot)
        self.tray.quit_app.connect(self._quit)
        self.tray.show()

    def _on_apply_style(self) -> None:
        name = self.style_tab.style_combo.currentData()
        if name:
            self.style_change_requested.emit(name)

    def _toggle_bot(self) -> None:
        if self.monitor_tab.stop_btn.isEnabled():
            self.stop_requested.emit()
        else:
            self.start_requested.emit()

    def _quit(self) -> None:
        QApplication.quit()

    def set_running_state(self, running: bool) -> None:
        self.monitor_tab.start_btn.setEnabled(not running)
        self.monitor_tab.start_btn.setText("运行中" if running else "启动机器人")
        self.monitor_tab.stop_btn.setEnabled(running)
        self.tray.set_running(running)
        self.tray.set_status("running" if running else "connected")

    def update_status(self, name: str, value: str, color: str = "#fff") -> None:
        self.monitor_tab.status_panel.update_status(name, value, color)

    def add_message(self, direction: str, room: str, sender: str, content: str) -> None:
        if direction == "in":
            self.monitor_tab.message_list.add_incoming(room, sender, content)
        else:
            self.monitor_tab.message_list.add_outgoing(room, content)

    def add_log(self, level: str, message: str) -> None:
        self.log_tab.log_viewer.append_log(level, message)

    def _handle_gui_event(self, event: str, data: dict) -> None:
        """在 Qt 主线程中处理来自其他线程的事件"""
        if event == "bot_starting":
            self.monitor_tab.start_btn.setEnabled(False)
            self.monitor_tab.start_btn.setText("连接中...")
            self.monitor_tab.stop_btn.setEnabled(False)
            self.tray.set_status("disconnected")
        elif event == "bot_running":
            self.set_running_state(True)
        elif event == "bot_start_failed":
            self.monitor_tab.start_btn.setEnabled(True)
            self.monitor_tab.start_btn.setText("启动机器人")
            self.monitor_tab.stop_btn.setEnabled(False)
            self.tray.set_running(False)
            self.tray.set_status("error")
        elif event == "bot_stopping":
            self.monitor_tab.start_btn.setEnabled(False)
            self.monitor_tab.start_btn.setText("停止中...")
            self.monitor_tab.stop_btn.setEnabled(False)
        elif event == "bot_stopped":
            self.set_running_state(False)
            self.tray.set_status("disconnected")
        elif event == "message_received":
            self.add_message("in", data.get("room", ""), data.get("sender", ""), data.get("content", ""))
        elif event == "message_replied":
            self.add_message("out", data.get("room", ""), "", data.get("reply", ""))
        elif event == "sticker_sent":
            self.add_message("out", data.get("room", ""), "", "[收藏表情]")

    def closeEvent(self, event) -> None:
        """关闭窗口时退出应用"""
        self.tray.hide()
        QApplication.quit()
        event.accept()
