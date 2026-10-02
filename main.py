"""微信聊天机器人 - 主入口"""

import faulthandler
import re
import socket
import sys
import asyncio
import random
import threading
import time
from datetime import datetime
from pathlib import Path

from loguru import logger
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QTimer, QObject, qInstallMessageHandler

from src.config_manager import ConfigManager, LOCKED_STYLE_NAME
from src.security import SecurityManager
from src.ai_engine import AIEngine
from src.style_manager import StyleManager
from src.wechat_bridge import WeChatBridge
from src.qq_bridge import QQBridge
from src.wecom_bridge import WxWorkBridge
from src.message_handler import MessageHandler
from src.tts_service import TTSService
from src.song_service import SongService
from src.music_provider import LXMusicProvider
from src.asr_service import ASRService
from src.error_logger import (
    install_exception_hooks,
    log_error_message,
    log_exception,
    log_loguru_error,
    mark_startup,
)
from src.gui.main_window import MainWindow
from src.web.app import app as web_app
from src.web.routes import init_routes
from src.web.websocket import broadcast

import uvicorn


_SINGLE_INSTANCE_SOCKET: socket.socket | None = None
_CRASH_STREAM = None
_RUNTIME_DIR = Path("runtime")
_HEARTBEAT_FILE = _RUNTIME_DIR / "bot.heartbeat"
_CLEAN_EXIT_FILE = _RUNTIME_DIR / "bot.clean-exit"


def _install_crash_logging() -> None:
    """记录原生崩溃堆栈，并安装 Python 未捕获异常钩子。"""
    global _CRASH_STREAM
    try:
        log_dir = Path("logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        _CRASH_STREAM = (log_dir / "crash.log").open(
            "a",
            encoding="utf-8",
            errors="replace",
            buffering=1,
        )
        faulthandler.enable(file=_CRASH_STREAM, all_threads=True)
    except Exception as exc:
        log_error_message(
            context="main._install_crash_logging",
            error_type=type(exc).__name__,
            error_message=str(exc),
            stack="启用 faulthandler 失败",
        )
        print(f"启用崩溃日志失败: {exc}", file=sys.stderr)

    install_exception_hooks()


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _write_heartbeat() -> None:
    try:
        _write_text_atomic(_HEARTBEAT_FILE, str(datetime.now().timestamp()))
    except Exception as exc:
        log_error_message(
            context="main._write_heartbeat",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        print(f"写入机器人心跳失败: {exc}", file=sys.stderr)


def _mark_clean_exit() -> None:
    try:
        _write_text_atomic(
            _CLEAN_EXIT_FILE,
            datetime.now().isoformat(),
        )
    except Exception as exc:
        log_error_message(
            context="main._mark_clean_exit",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        print(f"写入正常退出标记失败: {exc}", file=sys.stderr)


_LOG_LINE_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)"
    r"\s+\|\s+(?P<level>TRACE|DEBUG|INFO|SUCCESS|WARNING|ERROR|CRITICAL)"
    r"\s+\|\s+.*?\s+-\s+(?P<message>.*)$"
)


def _restore_recent_monitor_text(window, limit: int = 250) -> None:
    """恢复上次运行最近一段日志，避免重启后监控文本空白。"""
    path = Path("logs") / "chatbot.log"
    if not path.is_file():
        return

    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 1024 * 1024))
            text = handle.read().decode("utf-8", errors="replace")
    except Exception as exc:
        window.log_tab.log_viewer.append_log(
            "WARNING",
            f"恢复上次监控日志失败: {exc}",
        )
        return

    entries: list[tuple[str, str, str]] = []
    for raw_line in text.splitlines()[-limit * 2:]:
        match = _LOG_LINE_RE.match(raw_line)
        if not match:
            continue
        entries.append(
            (
                match.group("level"),
                match.group("message"),
                match.group("time")[11:19],
            )
        )

    for level, message, timestamp in entries[-limit:]:
        window.log_tab.log_viewer.append_log(
            level,
            message,
            timestamp=timestamp,
        )
    if entries:
        window.log_tab.log_viewer.append_log(
            "SUCCESS",
            f"已恢复上次运行监控日志 {min(len(entries), limit)} 条",
        )


def acquire_single_instance(port: int = 47653) -> bool:
    """用本地端口占位确保同一时间只有一个机器人实例在运行。

    重复启动会导致两个进程同时操作微信界面：消息重复、发送超时、
    触发微信风控，看起来就像卡死。
    """
    global _SINGLE_INSTANCE_SOCKET
    if _SINGLE_INSTANCE_SOCKET is not None:
        return True

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
    except OSError:
        try:
            sock.close()
        except Exception:
            pass
        return False
    _SINGLE_INSTANCE_SOCKET = sock
    return True


def _report_duplicate_start(message: str) -> None:
    """重复启动时给出提示（不弹模态框，避免把进程挂住）。"""
    print(message)
    try:
        log_dir = Path("logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(
            log_dir / "duplicate-start.log",
            "a",
            encoding="utf-8",
        ) as f:
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


def _install_qt_message_filter() -> None:
    """过滤 Qt/Windows 外壳产生的无文件名重复警告，保留其他错误。"""
    ignored = {
        "QFSFileEngine::open: No file name specified",
    }

    def handler(mode, context, message: str) -> None:
        if message.strip() in ignored:
            return
        if sys.stderr is not None:
            sys.stderr.write(f"{message}\n")
            sys.stderr.flush()

    qInstallMessageHandler(handler)


class AsyncBridge(QObject):
    """Qt 线程与 asyncio 线程之间的桥梁"""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self._loop = loop

    def call(self, coro_func):
        """传入一个返回协程的函数（不要传协程对象）"""
        try:
            coro = coro_func()
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            future.add_done_callback(self._handle_task_error)
        except Exception as e:
            logger.error(f"异步任务创建失败: {e}")

    def _handle_task_error(self, future):
        if future.cancelled():
            return
        exc = future.exception()
        if exc:
            logger.error(f"异步任务异常: {exc}")


class ChatBot:
    """聊天机器人主控制器"""

    def __init__(self):
        self.config: ConfigManager | None = None
        self.security: SecurityManager | None = None
        self.ai: AIEngine | None = None
        self.style: StyleManager | None = None
        self.wechat: WeChatBridge | None = None
        self.handler: MessageHandler | None = None
        # QQ 端：独立桥接、独立安全策略、独立处理管线
        self.qq: QQBridge | None = None
        self.qq_handler: MessageHandler | None = None
        self.qq_security: SecurityManager | None = None
        # 企业微信端：独立桥接、独立安全策略、独立处理管线
        self.wecom: WxWorkBridge | None = None
        self.wecom_handler: MessageHandler | None = None
        self.wecom_security: SecurityManager | None = None
        self._wecom_enabled = False
        self._wecom_running = False
        self._wecom_watchdog_task: asyncio.Task | None = None
        self.window: MainWindow | None = None
        self._async_bridge: AsyncBridge | None = None
        self._running = False
        self._qq_running = False
        self._sticker_task: asyncio.Task | None = None
        self._connection_watchdog_task: asyncio.Task | None = None
        self._qq_watchdog_task: asyncio.Task | None = None
        self._reconnect_lock: asyncio.Lock | None = None
        self._recent_messages: list[dict] = []
        self._logs: list[dict] = []
        self._max_recent = 500

    # ------------------------------------------------------------------
    # 语音 / 点歌（双端共用）
    # ------------------------------------------------------------------
    def _setup_voice(self) -> None:
        """构建双端共用的语音合成与点歌服务。"""
        voice_cfg = self.config.get("voice", {}) or {}
        songs_cfg = voice_cfg.get("songs", {}) or {}
        quick_cfg = voice_cfg.get("quick_replies", {}) or {}

        self.voice_enabled = bool(voice_cfg.get("enabled", True))
        self.voice_cfg = {
            "enabled": self.voice_enabled,
            "engine": str(voice_cfg.get("engine", "auto")),
            "prewarm": bool(voice_cfg.get("prewarm", True)),
            "prewarm_count": int(voice_cfg.get("prewarm_count", 6) or 6),
            "quick_enabled": bool(quick_cfg.get("enabled", True)),
            "quick_max_chars": int(quick_cfg.get("max_chars", 10) or 10),
            "songs_enabled": bool(songs_cfg.get("enabled", True)),
            "songs_max_duration": float(
                songs_cfg.get("max_duration", 55.0) or 55.0
            ),
        }

        self.tts = TTSService(
            cache_dir=voice_cfg.get("cache_dir", "./data/voice_cache"),
            enabled=self.voice_enabled,
            engine=voice_cfg.get("engine", "auto"),
            default_voice=voice_cfg.get(
                "default_voice",
                "zh-CN-XiaoxiaoNeural",
            ),
            rate=voice_cfg.get("rate", "+0%"),
            pitch=voice_cfg.get("pitch", "+0Hz"),
            timeout=voice_cfg.get("timeout", 25.0),
            max_chars=voice_cfg.get("max_chars", 400),
            retries=voice_cfg.get("retries", 2),
        )
        # 真实歌曲下载源（洛雪音乐服务器，校园网内直连）
        music_cfg = voice_cfg.get("music", {}) or {}
        self.music = LXMusicProvider(
            base_url=music_cfg.get("base_url", ""),
            password=music_cfg.get("password", ""),
            username=music_cfg.get("username", ""),
            user_password=music_cfg.get("user_password", ""),
            quality=music_cfg.get("quality", "128k"),
            enabled=bool(music_cfg.get("enabled", False)),
            timeout=music_cfg.get("timeout", 30.0),
            cache_dir=music_cfg.get("cache_dir", "./data/music_cache"),
            max_size_mb=music_cfg.get("max_size_mb", 50.0),
        )
        self.song = SongService(
            tts=self.tts,
            cache_dir=songs_cfg.get("cache_dir", "./data/song_cache"),
            enabled=self.voice_enabled and self.voice_cfg["songs_enabled"],
            max_duration=songs_cfg.get("max_duration", 55.0),
            sing_rate=songs_cfg.get("sing_rate", "+8%"),
            sing_pitch=songs_cfg.get("sing_pitch", "+35Hz"),
            voice=songs_cfg.get("voice", "") or "",
            max_lyrics_chars=songs_cfg.get("max_lyrics_chars", 300),
            music=self.music,
            music_timeout=music_cfg.get("timeout", 30.0),
        )

        if not self.voice_enabled:
            logger.info("语音 / 点歌功能已关闭（voice.enabled=false）")
        else:
            status = self.tts.status()
            music_note = (
                f"，音乐源已启用({self.music.base_url})"
                if self.music.enabled
                else ""
            )
            logger.info(
                f"语音引擎就绪：{status['engine_label']}，"
                f"曲库 {self.song.song_count()} 首{music_note}"
            )
        self.tts.purge_tmp()

    def _setup_asr(self) -> None:
        """Build the shared local speech recognition service."""
        asr_cfg = self.config.get("asr", {}) or {}
        self.asr = ASRService(
            enabled=bool(asr_cfg.get("enabled", True)),
            model=str(asr_cfg.get("model", "base") or "base"),
            model_dir=str(
                asr_cfg.get(
                    "model_dir",
                    "./data/models/faster-whisper",
                )
                or "./data/models/faster-whisper"
            ),
            cache_dir=str(
                asr_cfg.get("cache_dir", "./data/voice_inbox")
                or "./data/voice_inbox"
            ),
            language=str(asr_cfg.get("language", "zh") or "zh"),
            compute_type=str(
                asr_cfg.get("compute_type", "int8") or "int8"
            ),
            cpu_threads=int(asr_cfg.get("cpu_threads", 4) or 4),
            beam_size=int(asr_cfg.get("beam_size", 1) or 1),
            max_audio_mb=float(asr_cfg.get("max_audio_mb", 20.0) or 20.0),
            hf_endpoint=str(
                asr_cfg.get("hf_endpoint", "https://hf-mirror.com") or ""
            ),
        )
        if self.asr.enabled:
            logger.info(
                f"语音识别已就绪：{self.asr.model_name} "
                f"({self.asr.compute_type}, {self.asr.cpu_threads} 线程)"
            )
        else:
            logger.info("语音识别已关闭（asr.enabled=false）")

    async def rebuild_voice(self) -> dict:
        """按当前 config 重建语音 / 点歌 / 音乐服务，并热更新到双端管线。"""
        old_music = getattr(self, "music", None)
        if old_music is not None:
            try:
                await old_music.close()
            except Exception:
                pass
        self._setup_voice()
        kwargs = self._voice_handler_kwargs({})
        for handler in (self.handler, getattr(self, "qq_handler", None)):
            if handler is not None and hasattr(handler, "apply_voice_config"):
                handler.apply_voice_config(**kwargs)
        return self.voice_status()

    def _voice_handler_kwargs(self, platform_cfg: dict) -> dict:
        """按平台生成传给 MessageHandler 的语音相关参数。"""
        platform_voice = (platform_cfg or {}).get("voice", {}) or {}
        enabled = self.voice_enabled and bool(
            platform_voice.get("enabled", True)
        )
        if not getattr(self, "voice_cfg", None):
            return {
                "song_service": None,
                "voice_enabled": False,
                "asr_service": getattr(self, "asr", None),
            }
        return {
            "song_service": self.song if enabled else None,
            "voice_enabled": enabled,
            "asr_service": getattr(self, "asr", None),
            "quick_replies_enabled": bool(
                self.voice_cfg.get("quick_enabled", True)
            ),
            "quick_replies_max_chars": int(
                self.voice_cfg.get("quick_max_chars", 10)
            ),
            "song_max_duration": float(
                self.voice_cfg.get("songs_max_duration", 55.0)
            ),
        }

    def voice_status(self) -> dict:
        """语音 / 点歌状态，供 Web 与状态接口展示。"""
        tts_status = self.tts.status() if getattr(self, "tts", None) else {}
        song_status = self.song.status() if getattr(self, "song", None) else {}
        songs = self.song.list_songs() if getattr(self, "song", None) else []
        music_status = (
            self.music.status() if getattr(self, "music", None) else {}
        )
        return {
            "enabled": bool(getattr(self, "voice_enabled", False)),
            "tts": tts_status,
            "song": song_status,
            "music": music_status,
            "songs": songs,
            "asr": (
                self.asr.status()
                if getattr(self, "asr", None)
                else {"enabled": False}
            ),
        }

    def initialize(self, config_path: str = "config.yaml") -> bool:
        try:
            self.config = ConfigManager(config_path)
            password = self.config.get("security.password") or "default"
            self.config.load(password)

            log_cfg = self.config.get("logging", {})
            logger.remove()
            if sys.stderr is not None:
                logger.add(sys.stderr, level=log_cfg.get("level", "INFO"))
            log_file = log_cfg.get("file", "./logs/chatbot.log")
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            logger.add(
                log_file,
                level=log_cfg.get("level", "INFO"),
                rotation=log_cfg.get("rotation", "10 MB"),
                retention=log_cfg.get("retention", "7 days"),
            )
            logger.add(
                log_loguru_error,
                level="ERROR",
                format="{message}",
                backtrace=True,
                diagnose=True,
                catch=True,
                enqueue=False,
            )

            sec_cfg = self.config.get("security", {})
            self.security = SecurityManager(
                rate_limit=sec_cfg.get("rate_limit", 20),
                circuit_breaker_threshold=sec_cfg.get("circuit_breaker_threshold", 5),
                circuit_breaker_timeout=sec_cfg.get("circuit_breaker_timeout", 5),
            )

            ai_cfg = self.config.get("ai", {})
            api_key = self.config.get_decrypted("ai.api_key_encrypted")
            if not api_key:
                api_key = ai_cfg.get("api_key", "")
                if api_key:
                    encrypted = self.config.encrypt_value(api_key)
                    self.config.set("ai.api_key_encrypted", encrypted)
                    self.config.set("ai.api_key", "")
                    self.config.save()

            self.ai = AIEngine(
                api_url=ai_cfg.get("api_url", ""),
                api_key=api_key,
                model=ai_cfg.get("model", "gpt-4"),
                max_tokens=ai_cfg.get("max_tokens", 60),
                temperature=ai_cfg.get("temperature", 0.7),
                timeout=ai_cfg.get("timeout", 10),
                max_retries=ai_cfg.get("max_retries", 2),
                api_style=ai_cfg.get("api_style", "auto"),
            )

            style_cfg = self.config.get("style", {})
            self.style = StyleManager(
                styles_dir="styles",
                custom_dir=style_cfg.get("custom_styles_dir", "styles/custom"),
                locked_name=LOCKED_STYLE_NAME,
            )
            self.style.load_all()
            self.style.set_active(LOCKED_STYLE_NAME)
            self.config.set("style.active", LOCKED_STYLE_NAME)

            # 语音合成 + 点歌：双端共用同一套服务与磁盘缓存
            self._setup_voice()
            self._setup_asr()

            wx_cfg = self.config.get("wechat", {})
            demo_mode = self.config.get("demo_mode", False)

            if demo_mode:
                from src.demo_bridge import DemoBridge
                self.wechat = DemoBridge(
                    target_groups=["Demo群"],
                    on_message=self._on_message,
                    reply_delay=0.0,
                )
                logger.info("已启用 Demo 模式（无需微信连接）")
            else:
                self.wechat = WeChatBridge(
                    target_groups=wx_cfg.get("target_groups", []),
                    on_message=self._on_message,
                    reply_delay=wx_cfg.get("reply_delay", 0.0),
                    backend=wx_cfg.get("backend", "auto"),
                    listen_private=wx_cfg.get("listen_private", True),
                )

            proactive_cfg = wx_cfg.get("proactive", {}) or {}
            idle_cfg = proactive_cfg.get("idle", {}) or {}
            image_search_cfg = wx_cfg.get("image_search", {}) or {}
            self.handler = MessageHandler(
                wechat=self.wechat,
                ai_engine=self.ai,
                style_manager=self.style,
                security=self.security,
                max_queue_size=sec_cfg.get("max_queue_size", 100),
                on_event=self._on_event,
                context_file=wx_cfg.get(
                    "context_file",
                    "./data/conversation_contexts.json",
                ),
                context_history=wx_cfg.get("context_history", 50),
                ai_context_messages=wx_cfg.get("ai_context_messages", 12),
                trigger_keywords=wx_cfg.get(
                    "trigger_keywords",
                    ["大肥鲸", "deepseek", "鲸鱼娘", "吃大米", "token不够用了"],
                ),
                fast_reply_timeout=ai_cfg.get("fast_reply_timeout", 1.2),
                fast_reply_fallback=ai_cfg.get("fast_reply_fallback", True),
                answered_cache_file=wx_cfg.get(
                    "answered_cache_file",
                    "./data/answered_questions.json",
                ),
                dedupe_window=wx_cfg.get("dedupe_window", 21600),
                computer_issue_timeout=wx_cfg.get(
                    "computer_issue_timeout",
                    10,
                ),
                computer_max_tokens=wx_cfg.get("computer_max_tokens", 600),
                reply_sticker_enabled=wx_cfg.get("sticker", {}).get(
                    "enabled",
                    False,
                ),
                reply_sticker_interval=wx_cfg.get("sticker", {}).get(
                    "reply_interval",
                    3,
                ),
                reply_sticker_count=wx_cfg.get("sticker", {}).get(
                    "reply_sticker_count",
                    3,
                ),
                proactive_enabled=proactive_cfg.get("enabled", True),
                proactive_interval_hours=proactive_cfg.get(
                    "interval_hours",
                    2,
                ),
                proactive_start_hour=proactive_cfg.get("start_hour", 8),
                proactive_end_hour=proactive_cfg.get("end_hour", 20),
                proactive_groups=(
                    proactive_cfg.get("groups")
                    or wx_cfg.get("target_groups", [])
                ),
                proactive_timeout=proactive_cfg.get("timeout", 8.0),
                idle_greet_enabled=idle_cfg.get("enabled", True),
                idle_greet_minutes=idle_cfg.get("idle_minutes", 30),
                idle_greet_cooldown_minutes=idle_cfg.get(
                    "cooldown_minutes",
                    60,
                ),
                idle_greet_check_interval=idle_cfg.get("check_interval", 60),
                idle_greet_min_messages=idle_cfg.get("min_messages", 0),
                sensitivity=wx_cfg.get("sensitivity", "normal"),
                poke_cooldown_seconds=wx_cfg.get("poke_cooldown", 3.0),
                group_reply_limit=wx_cfg.get("group_reply_limit", 6),
                group_reply_window=wx_cfg.get("group_reply_window", 60),
                max_message_age=wx_cfg.get("max_message_age", 600),
                user_burst_cooldown=wx_cfg.get(
                    "user_burst_cooldown",
                    3.0,
                ),
                user_reply_timeout=wx_cfg.get(
                    "user_reply_timeout",
                    20.0,
                ),
                image_search_enabled=image_search_cfg.get(
                    "enabled",
                    True,
                ),
                image_search_timeout=image_search_cfg.get(
                    "timeout",
                    3.0,
                ),
                image_cache_dir=image_search_cfg.get(
                    "cache_dir",
                    "./data/images",
                ),
                **self._voice_handler_kwargs(wx_cfg),
            )

            qq_cfg = self.config.get("qq", {}) or {}
            self._qq_enabled = bool(qq_cfg.get("enabled", False))
            if self._qq_enabled:
                self._setup_qq(qq_cfg, sec_cfg, ai_cfg, wx_cfg)
            else:
                logger.info("QQ 端未启用（qq.enabled=false），仅运行微信端")

            # 企业微信端初始化
            wecom_cfg = self.config.get("wecom", {}) or {}
            self._wecom_enabled = bool(wecom_cfg.get("enabled", False))
            if self._wecom_enabled:
                self._setup_wecom(wecom_cfg, sec_cfg, ai_cfg, wx_cfg)
            else:
                logger.info("企业微信端未启用（wecom.enabled=false）")

            init_routes(self)
            logger.info("所有模块初始化完成")
            return True

        except Exception as e:
            logger.error(f"初始化失败: {e}")
            return False

    def _setup_qq(
        self,
        qq_cfg: dict,
        sec_cfg: dict,
        ai_cfg: dict,
        wx_cfg: dict,
    ) -> None:
        """构建 QQ 端：独立桥接、独立安全策略、独立消息处理管线。

        与微信端共享 AI 引擎和人设，但队列、上下文、去重缓存、
        限流熔断全部独立，确保两端互不干扰。
        """
        # QQ 端使用独立的限流与熔断，避免一端异常拖垮另一端
        self.qq_security = SecurityManager(
            rate_limit=qq_cfg.get("rate_limit", sec_cfg.get("rate_limit", 20)),
            circuit_breaker_threshold=qq_cfg.get(
                "circuit_breaker_threshold",
                sec_cfg.get("circuit_breaker_threshold", 5),
            ),
            circuit_breaker_timeout=qq_cfg.get(
                "circuit_breaker_timeout",
                sec_cfg.get("circuit_breaker_timeout", 5),
            ),
        )

        client_secret = self.config.get_decrypted("qq.client_secret_encrypted")
        if not client_secret:
            client_secret = str(qq_cfg.get("client_secret", "") or "")
            if client_secret:
                self.config.set(
                    "qq.client_secret_encrypted",
                    self.config.encrypt_value(client_secret),
                )
                self.config.set("qq.client_secret", "")
                self.config.save()

        # QQ 走纯 HTTP 接口，没有微信那种窗口风控，
        # 把发送间隔和消息有效期压下来即可显著提升回复手感
        qq_eff = qq_cfg.get("efficiency", {}) or {}
        eff_on = bool(qq_eff.get("enabled", True))
        qq_send_interval = float(
            qq_eff.get("send_interval", qq_cfg.get("send_interval", 1.2))
            if eff_on
            else qq_cfg.get("send_interval", 1.2)
        )
        qq_max_age = float(
            qq_eff.get("max_message_age", qq_cfg.get("max_message_age", 600))
            if eff_on
            else qq_cfg.get("max_message_age", 600)
        )
        qq_fast_timeout = float(
            qq_eff.get("fast_reply_timeout", ai_cfg.get("fast_reply_timeout", 1.2))
            if eff_on
            else ai_cfg.get("fast_reply_timeout", 1.2)
        )

        self.qq = QQBridge(
            app_id=qq_cfg.get("app_id", ""),
            client_secret=client_secret,
            sandbox=qq_cfg.get("sandbox", False),
            target_groups=qq_cfg.get("target_groups", []) or [],
            on_message=self._on_qq_message,
            reply_delay=qq_cfg.get("reply_delay", 0.0),
            backend=qq_cfg.get("backend", "official"),
            intents=qq_cfg.get("intents", 0) or 0,
            group_aliases=qq_cfg.get("group_aliases", {}) or {},
            listen_private=qq_cfg.get("listen_private", True),
            send_interval=qq_send_interval,
        )

        qq_proactive = qq_cfg.get("proactive", {}) or {}
        qq_idle = qq_proactive.get("idle", {}) or {}
        qq_image = qq_cfg.get("image_search", {}) or {}
        self.qq_handler = MessageHandler(
            wechat=self.qq,
            ai_engine=self.ai,
            style_manager=self.style,
            security=self.qq_security,
            max_queue_size=sec_cfg.get("max_queue_size", 100),
            on_event=self._make_event_callback("qq"),
            # 独立的数据文件，避免与微信端互相覆盖
            context_file=qq_cfg.get(
                "context_file",
                "./data/qq_conversation_contexts.json",
            ),
            context_history=qq_cfg.get(
                "context_history",
                wx_cfg.get("context_history", 50),
            ),
            ai_context_messages=qq_cfg.get(
                "ai_context_messages",
                wx_cfg.get("ai_context_messages", 12),
            ),
            trigger_keywords=qq_cfg.get("trigger_keywords")
            or wx_cfg.get("trigger_keywords", []),
            fast_reply_timeout=qq_fast_timeout,
            fast_reply_fallback=ai_cfg.get("fast_reply_fallback", True),
            answered_cache_file=qq_cfg.get(
                "answered_cache_file",
                "./data/qq_answered_questions.json",
            ),
            dedupe_window=qq_cfg.get("dedupe_window", 21600),
            computer_issue_timeout=qq_cfg.get(
                "computer_issue_timeout",
                wx_cfg.get("computer_issue_timeout", 8),
            ),
            computer_max_tokens=qq_cfg.get(
                "computer_max_tokens",
                wx_cfg.get("computer_max_tokens", 600),
            ),
            # QQ 官方 API 没有收藏表情，关闭表情回复
            reply_sticker_enabled=False,
            proactive_enabled=qq_proactive.get("enabled", False),
            proactive_interval_hours=qq_proactive.get("interval_hours", 2),
            proactive_start_hour=qq_proactive.get("start_hour", 8),
            proactive_end_hour=qq_proactive.get("end_hour", 20),
            proactive_groups=qq_proactive.get("groups") or [],
            proactive_timeout=qq_proactive.get("timeout", 8.0),
            idle_greet_enabled=qq_idle.get("enabled", False),
            idle_greet_minutes=qq_idle.get("idle_minutes", 30),
            idle_greet_cooldown_minutes=qq_idle.get("cooldown_minutes", 60),
            idle_greet_check_interval=qq_idle.get("check_interval", 60),
            idle_greet_min_messages=qq_idle.get("min_messages", 0),
            sensitivity=qq_cfg.get("sensitivity", "normal"),
            poke_cooldown_seconds=qq_cfg.get("poke_cooldown", 3.0),
            group_reply_limit=qq_cfg.get(
                "group_reply_limit",
                wx_cfg.get("group_reply_limit", 6),
            ),
            group_reply_window=qq_cfg.get(
                "group_reply_window",
                wx_cfg.get("group_reply_window", 60),
            ),
            max_message_age=qq_max_age,
            user_burst_cooldown=qq_cfg.get(
                "user_burst_cooldown",
                wx_cfg.get("user_burst_cooldown", 3.0),
            ),
            user_reply_timeout=qq_cfg.get(
                "user_reply_timeout",
                wx_cfg.get("user_reply_timeout", 20.0),
            ),
            image_search_enabled=qq_image.get("enabled", True),
            image_search_timeout=qq_image.get("timeout", 3.0),
            image_cache_dir=qq_image.get("cache_dir", "./data/qq-images"),
            **self._voice_handler_kwargs(qq_cfg),
        )
        logger.info(
            f"QQ 官方机器人已启用（后端: {qq_cfg.get('backend', 'official')}，"
            f"沙箱: {bool(qq_cfg.get('sandbox', False))}）"
        )

    def _setup_wecom(
        self,
        wecom_cfg: dict,
        sec_cfg: dict,
        ai_cfg: dict,
        wx_cfg: dict,
    ) -> None:
        """构建企业微信端：独立桥接、独立安全策略、独立消息处理管线。"""
        self.wecom_security = SecurityManager(
            rate_limit=wecom_cfg.get("rate_limit", sec_cfg.get("rate_limit", 20)),
            circuit_breaker_threshold=wecom_cfg.get(
                "circuit_breaker_threshold",
                sec_cfg.get("circuit_breaker_threshold", 5),
            ),
            circuit_breaker_timeout=wecom_cfg.get(
                "circuit_breaker_timeout",
                sec_cfg.get("circuit_breaker_timeout", 5),
            ),
        )

        # 企业微信secret直接读取明文（不加密，因为是本地配置）
        corp_secret = str(wecom_cfg.get("corpsecret", "") or "")
        if not corp_secret:
            corp_secret = self.config.get_decrypted("wecom.corpsecret_encrypted")

        wecom_fast_timeout = float(ai_cfg.get("fast_reply_timeout", 1.0))

        self.wecom = WxWorkBridge(
            bot_id=wecom_cfg.get("bot_id", ""),
            secret=corp_secret,
            agentid=wecom_cfg.get("agentid", ""),
            token=wecom_cfg.get("token", ""),
            aes_key=wecom_cfg.get("aes_key", ""),
            enabled=True,
            on_message=self._on_wecom_message,
        )

        self.wecom_handler = MessageHandler(
            wechat=self.wecom,
            ai_engine=self.ai,
            style_manager=self.style,
            security=self.wecom_security,
            max_queue_size=sec_cfg.get("max_queue_size", 100),
            on_event=self._make_event_callback("wecom"),
            context_file=wecom_cfg.get(
                "context_file",
                "./data/wecom_conversation_contexts.json",
            ),
            context_history=wecom_cfg.get("context_history", 30),
            ai_context_messages=wecom_cfg.get("ai_context_messages", 8),
            trigger_keywords=wecom_cfg.get("trigger_keywords")
            or ["@鲸鱼娘", "鲸鱼娘", "deepseek"],
            fast_reply_timeout=wecom_fast_timeout,
            fast_reply_fallback=ai_cfg.get("fast_reply_fallback", True),
            answered_cache_file=wecom_cfg.get(
                "answered_cache_file",
                "./data/wecom_answered_questions.json",
            ),
            dedupe_window=wecom_cfg.get("dedupe_window", 21600),
            computer_issue_timeout=wecom_cfg.get(
                "computer_issue_timeout",
                wx_cfg.get("computer_issue_timeout", 8),
            ),
            computer_max_tokens=wecom_cfg.get(
                "computer_max_tokens",
                wx_cfg.get("computer_max_tokens", 400),
            ),
            reply_sticker_enabled=False,
            image_search_enabled=False,
            song_service=getattr(self, "song", None),
            voice_enabled=getattr(self, "voice_enabled", True),
            quick_replies_enabled=getattr(self, "voice_cfg", {}).get(
                "quick_enabled", True
            ),
            song_max_duration=getattr(
                self, "voice_cfg", {}
            ).get("songs_max_duration", 55.0),
            asr_service=getattr(self, "asr", None),
        )

        self._apply_voice_handler_kwargs(self.wecom_handler, wecom_cfg)
        logger.info(
            f"企业微信机器人已初始化（agentid: {wecom_cfg.get('agentid', '')}）"
        )

    def _apply_voice_handler_kwargs(self, handler, cfg):
        """应用语音配置到 MessageHandler。"""
        if handler and getattr(self, "voice_enabled", True):
            handler.apply_voice_config(
                voice_enabled=self.voice_enabled,
                song_service=getattr(self, "song", None),
                quick_replies_enabled=getattr(self, "voice_cfg", {}).get("quick_enabled", True),
            )

    async def _start_wecom(self) -> None:
        """启动企业微信端。"""
        if not self.wecom or not self.wecom_handler:
            logger.info("企业微信端未初始化，跳过启动")
            return

        if self._wecom_running and self.wecom.is_connected:
            logger.info("企业微信端已在运行，忽略重复启动请求")
            return

        try:
            if self.wecom.is_connected:
                try:
                    await asyncio.wait_for(self.wecom.disconnect(), timeout=10)
                except Exception as e:
                    logger.warning(f"清理残留企业微信连接失败: {e}")

            connected = await self.wecom.connect()
            if not connected:
                reason = self.wecom.last_error or "未知原因"
                logger.error(f"企业微信端连接失败: {reason}")
                self._wecom_running = False
                self._on_event(
                    "bot_start_failed",
                    {"platform": "wecom", "message": f"企业微信连接失败: {reason}"},
                    platform="wecom",
                )
                return

            loop = asyncio.get_event_loop()
            self.wecom.start_listening(loop)
            await self.wecom_handler.start()
            self._wecom_running = True
            self._start_wecom_watchdog()
            logger.info("企业微信端已启动")
            self._on_event("bot_running", {}, platform="wecom")
        except Exception as e:
            self._wecom_running = False
            logger.error(f"企业微信端启动失败: {e}")
            self._on_event(
                "bot_start_failed",
                {"platform": "wecom", "message": str(e)},
                platform="wecom",
            )

    async def _stop_wecom(self) -> None:
        """停止企业微信端。"""
        if not self._wecom_running:
            return
        try:
            if self.wecom_handler:
                await self.wecom_handler.stop()
        except Exception as e:
            logger.warning(f"停止企业微信处理管线失败: {e}")
        try:
            if self.wecom:
                await asyncio.wait_for(self.wecom.close(), timeout=10)
        except Exception as e:
            logger.warning(f"关闭企业微信连接失败: {e}")
        await self._stop_wecom_watchdog()
        self._wecom_running = False
        logger.info("企业微信端已停止")

    async def _start_wecom_watchdog(self) -> None:
        """启动企业微信端连接看门狗。"""
        if self._wecom_watchdog_task and not self._wecom_watchdog_task.done():
            return
        self._wecom_watchdog_task = asyncio.create_task(
            self._wecom_watchdog(), name="wecom-watchdog"
        )

    async def _stop_wecom_watchdog(self) -> None:
        task = self._wecom_watchdog_task
        self._wecom_watchdog_task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _wecom_watchdog(self) -> None:
        """企业微信端连接看门狗：断线后自动重连。"""
        try:
            while self._wecom_running:
                await asyncio.sleep(30)
                if not self._wecom_running or not self.wecom:
                    break
                if not self.wecom.is_connected:
                    logger.warning("企业微信连接断开，尝试重连...")
                    await self._start_wecom()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"企业微信看门狗异常: {e}")

    def set_async_bridge(self, bridge: AsyncBridge):
        self._async_bridge = bridge

    async def _on_message(self, message) -> None:
        self._add_recent_message({
            "platform": "wechat",
            "direction": "in",
            "room": message.room_name,
            "sender": message.sender_name,
            "content": message.content,
        })
        await self.handler.enqueue(message)

    async def _on_qq_message(self, message) -> None:
        """QQ 端消息入口：与微信端完全并行，互不阻塞。"""
        if not self.qq_handler:
            return
        self._add_recent_message({
            "platform": "qq",
            "direction": "in",
            "room": message.room_name,
            "sender": message.sender_name,
            "content": message.content,
        })
        await self.qq_handler.enqueue(message)

    async def _on_wecom_message(self, message) -> None:
        """企业微信端消息入口。"""
        if not self.wecom_handler:
            return
        self._add_recent_message({
            "platform": "wecom",
            "direction": "in",
            "room": message.room_name,
            "sender": message.sender_name,
            "content": message.content,
        })
        await self.wecom_handler.enqueue(message)

    def _resolve_platforms(self, platform: str | None) -> list[str]:
        """把平台参数展开为需要操作的平台列表。"""
        name = str(platform or "").strip().lower()
        if name in {"wechat", "weixin", "wx"}:
            return ["wechat"]
        if name in {"qq", "qqbot"}:
            return ["qq"] if self._qq_enabled else []
        if name in {"wecom", "wework", "企业微信"}:
            return ["wecom"] if self._wecom_enabled else []
        targets = ["wechat"]
        if self._qq_enabled:
            targets.append("qq")
        if self._wecom_enabled:
            targets.append("wecom")
        return targets

    async def apply_target_groups(
        self,
        groups: list[str],
        platform: str = "wechat",
    ) -> dict:
        """通过主事件循环把 WebUI 的群聊变更同步到对应平台的监听器。"""
        if platform == "qq":
            if not self.qq:
                return {
                    "success": False,
                    "live": False,
                    "message": "QQ 桥接器未初始化",
                }
            result = await self.qq.set_target_groups(groups)
            qq_proactive = self.config.get("qq.proactive", {}) or {}
            if self.qq_handler and not (qq_proactive.get("groups") or []):
                self.qq_handler.set_proactive_groups(
                    groups,
                    start_if_needed=bool(self._qq_running),
                )
            return result

        if platform == "wecom":
            if not self.wecom:
                return {
                    "success": False,
                    "live": False,
                    "message": "企业微信桥接器未初始化",
                }
            result = await self.wecom.set_target_groups(groups)
            return result

        if not self.wechat:
            return {
                "success": False,
                "live": False,
                "message": "微信桥接器未初始化",
            }

        result = await self.wechat.set_target_groups(groups)
        proactive_cfg = self.config.get("wechat.proactive", {}) or {}
        if self.handler and not (proactive_cfg.get("groups") or []):
            self.handler.set_proactive_groups(
                groups,
                start_if_needed=bool(self._running),
            )
        return result

    def _on_event(self, event: str, data: dict, platform: str = "wechat") -> None:
        """把管线事件广播给 Web / GUI，并打上平台标记避免双端串台。"""
        payload = dict(data or {})
        payload.setdefault("platform", platform)
        self._add_log("INFO", f"[{platform}] {event}: {payload}")
        try:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(broadcast(event, payload))
            )
        except RuntimeError:
            pass

        # 通过信号安全地更新 GUI（跨线程）
        if self.window:
            self.window._gui_event.emit(event, payload)

    def _make_event_callback(self, platform: str):
        """生成带平台标记的事件回调，供各平台独立管线使用。"""

        def _callback(event: str, data: dict) -> None:
            self._on_event(event, data, platform=platform)

        return _callback

    def _add_recent_message(self, msg: dict) -> None:
        record = dict(msg or {})
        record.setdefault("platform", "wechat")
        self._recent_messages.append(record)
        if len(self._recent_messages) > self._max_recent:
            self._recent_messages = self._recent_messages[-self._max_recent // 2:]

    def _add_log(self, level: str, message: str) -> None:
        from datetime import datetime
        self._logs.append({"level": level, "message": message, "time": datetime.now().isoformat()})
        if len(self._logs) > self._max_recent:
            self._logs = self._logs[-self._max_recent // 2:]
        # 通过信号安全地更新日志（跨线程）
        if self.window:
            self.window._log_event.emit(level, message)

    def get_recent_messages(self, limit: int = 50, platform: str = "") -> list:
        """按平台读取最近消息；platform 为空时返回全部。"""
        items = self._recent_messages
        if platform:
            items = [
                item for item in items
                if str(item.get("platform", "wechat")) == platform
            ]
        return items[-limit:]

    def get_logs(self, limit: int = 100) -> list:
        return self._logs[-limit:]

    async def start(self, platform: str | None = None) -> None:
        """启动机器人；platform 为空时启动所有已启用的平台。

        各平台独立启停：一端失败不会影响另一端。
        """
        if getattr(self, "asr", None) is not None:
            await self.asr.start()
        for name in self._resolve_platforms(platform):
            try:
                if name == "wechat":
                    await self._start_wechat()
                elif name == "qq":
                    await self._start_qq()
                else:
                    await self._start_wecom()
            except Exception as exc:
                logger.error(f"{name} 端启动异常: {exc}")
                log_exception(f"main.start.{name}", exc=exc)

        # 任一平台起来后就预热热门歌曲，等用户点歌时几乎瞬间可发
        self._start_song_prewarm()

    def _start_song_prewarm(self) -> None:
        song = getattr(self, "song", None)
        voice_cfg = getattr(self, "voice_cfg", {}) or {}
        if not song or not voice_cfg.get("prewarm", True):
            return
        if not (self._running or self._qq_running or self._wecom_running):
            return
        try:
            song.start_prewarm(int(voice_cfg.get("prewarm_count", 6)))
        except Exception as exc:
            logger.warning(f"点歌预热启动失败: {exc}")

    async def _start_wechat(self) -> None:
        handler_running = bool(self.handler and self.handler.is_processing)
        if self._running and handler_running:
            logger.info("微信端已在运行，忽略重复的启动请求")
            return
        if handler_running:
            logger.warning("检测到上次启动残留的处理管线，先停止再重新启动")
            try:
                await self.handler.stop()
            except Exception as e:
                logger.warning(f"停止残留处理管线失败: {e}")

        if self.window:
            self.window._gui_event.emit("bot_starting", {"platform": "wechat"})

        try:
            if self.wechat and self.wechat.is_connected:
                logger.warning("检测到残留的微信连接，先断开再重新连接")
                try:
                    await asyncio.wait_for(self.wechat.disconnect(), timeout=15)
                except Exception as e:
                    logger.warning(f"清理残留微信连接失败: {e}")

            loop = asyncio.get_event_loop()
            connected = await self.wechat.connect()
            if not connected:
                logger.error("微信窗口不可用")
                self._running = False
                if self.window:
                    self.window._gui_event.emit(
                        "bot_start_failed",
                        {
                            "platform": "wechat",
                            "message": (
                                "微信窗口不可用，请先手动登录微信，"
                                "再点击启动机器人"
                            ),
                        },
                    )
                return
            self.wechat.start_listening(loop)
            await self.handler.start()
            self._running = True
            self._start_sticker_scheduler()
            self._start_connection_watchdog()
            logger.info("微信端已启动")
            if self.window:
                self.window._gui_event.emit("bot_running", {"platform": "wechat"})
        except Exception as e:
            self._running = False
            logger.error(f"微信端启动失败: {e}")
            if self.window:
                self.window._gui_event.emit(
                    "bot_start_failed",
                    {"platform": "wechat", "message": str(e)},
                )

    async def _start_qq(self) -> None:
        """启动 QQ 端（官方机器人 API）。"""
        if not self.qq or not self.qq_handler:
            logger.info("QQ 端未初始化，跳过启动")
            return

        handler_running = bool(self.qq_handler.is_processing)
        if self._qq_running and handler_running:
            logger.info("QQ 端已在运行，忽略重复的启动请求")
            return
        if handler_running:
            try:
                await self.qq_handler.stop()
            except Exception as e:
                logger.warning(f"停止残留的 QQ 处理管线失败: {e}")

        if self.window:
            self.window._gui_event.emit("bot_starting", {"platform": "qq"})

        try:
            if self.qq.is_connected:
                try:
                    await asyncio.wait_for(self.qq.disconnect(), timeout=15)
                except Exception as e:
                    logger.warning(f"清理残留 QQ 连接失败: {e}")

            loop = asyncio.get_event_loop()
            connected = await self.qq.connect()
            if not connected:
                reason = self.qq.last_error or self.qq._unavailable_reason() or "未知原因"
                logger.error(f"QQ 端连接失败: {reason}")
                self._qq_running = False
                self._on_event(
                    "bot_start_failed",
                    {"platform": "qq", "message": f"QQ 连接失败: {reason}"},
                    platform="qq",
                )
                return

            self.qq.start_listening(loop)
            await self.qq_handler.start()
            self._qq_running = True
            self._start_qq_watchdog()
            logger.info("QQ 端已启动")
            self._on_event("bot_running", {}, platform="qq")
            if self.window:
                self.window._gui_event.emit("bot_running", {"platform": "qq"})
        except Exception as e:
            self._qq_running = False
            logger.error(f"QQ 端启动失败: {e}")
            log_exception("main._start_qq", exc=e)
            self._on_event(
                "bot_start_failed",
                {"platform": "qq", "message": str(e)},
                platform="qq",
            )
            if self.window:
                self.window._gui_event.emit(
                    "bot_start_failed",
                    {"platform": "qq", "message": str(e)},
                )

    async def stop(self, platform: str | None = None) -> None:
        """停止机器人；platform 为空时停止所有平台。"""
        targets = self._resolve_platforms(platform)
        for name in targets:
            try:
                if name == "wechat":
                    await self._stop_wechat()
                elif name == "qq":
                    await self._stop_qq()
                else:
                    await self._stop_wecom()
            except Exception as exc:
                logger.error(f"{name} 端停止异常: {exc}")

        # 两端都停了就顺带结束点歌预热任务
        if not (self._running or self._qq_running or self._wecom_running):
            song = getattr(self, "song", None)
            if song is not None:
                try:
                    await song.stop()
                except Exception as exc:
                    logger.warning(f"停止点歌预热任务失败: {exc}")
            asr = getattr(self, "asr", None)
            if asr is not None:
                try:
                    await asr.close()
                except Exception as exc:
                    logger.warning(f"停止语音识别任务失败: {exc}")

    async def _stop_wechat(self) -> None:
        if not self._running and not (
            self.handler and self.handler.is_processing
        ):
            logger.info("微信端当前未运行，忽略停止请求")
            return
        self._running = False
        if self.window:
            self.window._gui_event.emit("bot_stopping", {"platform": "wechat"})
        try:
            await self._stop_connection_watchdog()
        except Exception as e:
            logger.warning(f"停止微信连接看门狗失败: {e}")
        try:
            await self._stop_sticker_scheduler()
        except Exception as e:
            logger.warning(f"停止收藏表情任务失败: {e}")
        try:
            await asyncio.wait_for(self.handler.stop(), timeout=20)
        except asyncio.TimeoutError:
            logger.error("停止消息处理管线超时")
        except Exception as e:
            logger.warning(f"停止消息处理管线失败: {e}")
        try:
            await asyncio.wait_for(self.wechat.disconnect(), timeout=20)
        except asyncio.TimeoutError:
            logger.error("断开微信连接超时")
        except Exception as e:
            logger.warning(f"断开微信连接失败: {e}")
        logger.info("微信端已停止")
        if self.window:
            self.window._gui_event.emit("bot_stopped", {"platform": "wechat"})

    async def _stop_qq(self) -> None:
        """停止 QQ 端，不影响微信端运行。"""
        if not self._qq_running and not (
            self.qq_handler and self.qq_handler.is_processing
        ):
            logger.info("QQ 端当前未运行，忽略停止请求")
            return
        self._qq_running = False
        if self.window:
            self.window._gui_event.emit("bot_stopping", {"platform": "qq"})
        try:
            await self._stop_qq_watchdog()
        except Exception as e:
            logger.warning(f"停止 QQ 看门狗失败: {e}")
        try:
            await asyncio.wait_for(self.qq_handler.stop(), timeout=20)
        except asyncio.TimeoutError:
            logger.error("停止 QQ 消息处理管线超时")
        except Exception as e:
            logger.warning(f"停止 QQ 消息处理管线失败: {e}")
        try:
            await asyncio.wait_for(self.qq.disconnect(), timeout=20)
        except asyncio.TimeoutError:
            logger.error("断开 QQ 连接超时")
        except Exception as e:
            logger.warning(f"断开 QQ 连接失败: {e}")
        logger.info("QQ 端已停止")
        self._on_event("bot_stopped", {}, platform="qq")
        if self.window:
            self.window._gui_event.emit("bot_stopped", {"platform": "qq"})

    def _get_reconnect_lock(self) -> asyncio.Lock:
        if self._reconnect_lock is None:
            self._reconnect_lock = asyncio.Lock()
        return self._reconnect_lock

    def _start_connection_watchdog(self) -> None:
        if (
            self._connection_watchdog_task
            and not self._connection_watchdog_task.done()
        ):
            return
        self._connection_watchdog_task = asyncio.create_task(
            self._connection_watchdog(),
            name="wechat-connection-watchdog",
        )

    async def _stop_connection_watchdog(self) -> None:
        task = self._connection_watchdog_task
        self._connection_watchdog_task = None
        if not task or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _connection_watchdog(self) -> None:
        """Recover when WeChat restarts or a UIA send stops returning."""
        while self._running:
            await asyncio.sleep(3)
            if not self._running or not self.wechat:
                break
            try:
                if self.handler and not self.handler.is_processing:
                    logger.warning("检测到消息处理管线已退出，正在自动恢复")
                    await self.handler.start()
                    logger.info("消息处理管线已自动恢复")
                    continue
                if self.wechat.window_process_changed():
                    recovered = await self._recover_wechat_connection(
                        "微信主窗口已重启"
                    )
                    if not recovered:
                        await asyncio.sleep(7)
                elif self.wechat.is_sender_stuck(5):
                    recovered = await self._recover_wechat_connection(
                        "微信发送线程超过 5 秒未返回"
                    )
                    if not recovered:
                        await asyncio.sleep(7)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"微信连接看门狗异常: {e}")
                await asyncio.sleep(3)

    async def _recover_wechat_connection(self, reason: str) -> bool:
        lock = self._get_reconnect_lock()
        if lock.locked():
            return False

        async with lock:
            if not self._running or not self.wechat:
                return False
            if (
                self.wechat.is_connected
                and not self.wechat.window_process_changed()
                and not self.wechat.is_sender_stuck(5)
            ):
                return False

            logger.warning(f"{reason}，正在自动重连微信...")
            try:
                recovered = await asyncio.wait_for(
                    self.wechat.recover_connection(),
                    timeout=50,
                )
            except asyncio.TimeoutError:
                logger.error("微信自动重连超时，稍后继续重试")
                return False
            except Exception as e:
                logger.error(f"微信自动重连失败: {e}")
                return False

            if recovered:
                logger.info("微信连接已自动恢复")
                return True

            logger.error("微信自动恢复失败，保持重试")
            return False

    def _start_qq_watchdog(self) -> None:
        if self._qq_watchdog_task and not self._qq_watchdog_task.done():
            return
        self._qq_watchdog_task = asyncio.create_task(
            self._qq_watchdog(),
            name="qq-connection-watchdog",
        )

    async def _stop_qq_watchdog(self) -> None:
        task = self._qq_watchdog_task
        self._qq_watchdog_task = None
        if not task or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _qq_watchdog(self) -> None:
        """QQ 端看门狗：管线退出自动重启，连接断开自动重连。"""
        while self._qq_running:
            await asyncio.sleep(5)
            if not self._qq_running or not self.qq:
                break
            try:
                if self.qq_handler and not self.qq_handler.is_processing:
                    logger.warning("检测到 QQ 处理管线已退出，正在自动恢复")
                    await self.qq_handler.start()
                    logger.info("QQ 处理管线已自动恢复")
                    continue
                if (
                    getattr(self.qq, "backend", "official") != "simulate"
                    and not self.qq.is_connected
                ):
                    logger.warning("检测到 QQ 连接已断开，正在自动重连")
                    recovered = await self.qq.recover_connection()
                    if not recovered:
                        await asyncio.sleep(8)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"QQ 连接看门狗异常: {e}")
                await asyncio.sleep(5)

    def platform_status(self) -> dict:
        """聚合两个平台的运行状态，供 Web / GUI 展示。"""
        wechat_running = bool(self._running)
        qq_running = bool(self._qq_running)
        return {
            "wechat": {
                "enabled": True,
                "running": wechat_running,
                "bridge": self.wechat.status_info if self.wechat else {},
                "handler": self.handler.status if self.handler else {},
                "security": self.security.status if self.security else {},
            },
            "qq": {
                "enabled": bool(getattr(self, "_qq_enabled", False)),
                "running": qq_running,
                "bridge": self.qq.status_info if self.qq else {},
                "handler": self.qq_handler.status if self.qq_handler else {},
                "security": self.qq_security.status if self.qq_security else {},
            },
            "voice": self.voice_status(),
        }

    async def configure_qq(
        self,
        app_id: str,
        client_secret: str = "",
        sandbox: bool = False,
        backend: str = "official",
        target_groups: list[str] | None = None,
        reply_delay: float | None = None,
        enabled: bool = True,
    ) -> dict:
        """保存并热更新 QQ 配置；无需重启进程。"""
        app_id = str(app_id or "").strip()
        backend = str(backend or "official").strip().lower()
        if backend not in {"official", "simulate"}:
            backend = "official"
        if backend == "official" and not app_id:
            return {"success": False, "message": "请填写机器人 AppID"}

        secret = str(client_secret or "").strip()
        if secret:
            self.config.set(
                "qq.client_secret_encrypted",
                self.config.encrypt_value(secret),
            )
            self.config.set("qq.client_secret", "")
        else:
            secret = self.config.get_decrypted("qq.client_secret_encrypted")
            if not secret:
                secret = str(self.config.get("qq.client_secret", "") or "")

        if backend == "official" and not secret:
            return {"success": False, "message": "请填写机器人 AppSecret"}

        self.config.set("qq.enabled", bool(enabled))
        self.config.set("qq.app_id", app_id)
        self.config.set("qq.sandbox", bool(sandbox))
        self.config.set("qq.backend", backend)
        if target_groups is not None:
            groups = [str(g).strip() for g in target_groups if str(g).strip()]
            self.config.set("qq.target_groups", groups)
        if reply_delay is not None:
            self.config.set("qq.reply_delay", float(reply_delay))
        self.config.save()

        # 若 QQ 端正在运行，先停再按新配置重建
        was_running = self._qq_running
        if was_running:
            await self._stop_qq()

        sec_cfg = self.config.get("security", {}) or {}
        ai_cfg = self.config.get("ai", {}) or {}
        wx_cfg = self.config.get("wechat", {}) or {}
        qq_cfg = self.config.get("qq", {}) or {}
        self._qq_enabled = bool(qq_cfg.get("enabled", True))
        if self._qq_enabled:
            self._setup_qq(qq_cfg, sec_cfg, ai_cfg, wx_cfg)

        message = "QQ 配置已保存"
        if was_running and self._qq_enabled:
            await self._start_qq()
            message += "，并已按新配置重启 QQ 端"
        elif self._qq_enabled:
            message += "，点击「启动 QQ」即可生效"
        else:
            message += "，QQ 端已停用"
        logger.info(message)
        return {
            "success": True,
            "message": message,
            "enabled": self._qq_enabled,
            "running": bool(self._qq_running),
        }

    def _setup_gui(self) -> None:
        if not self.window or not self._async_bridge:
            return

        bridge = self._async_bridge

        self.window.start_requested.connect(lambda: bridge.call(self.start))
        self.window.stop_requested.connect(lambda: bridge.call(self.stop))
        self.window.style_change_requested.connect(self._change_style)
        self.window.api_test_requested.connect(lambda: bridge.call(self._test_api))
        self.window.config_save_requested.connect(self._save_config)

        if self.style:
            self.window.style_tab.update_styles(self.style.list_styles())
            self.window.style_tab.set_locked(self.style.is_locked, self.style.locked_name)

        self.window.config_tab.api_url.setText(self.config.get("ai.api_url", ""))
        self.window.config_tab.model.setText(self.config.get("ai.model", ""))
        self.window.config_tab.max_tokens.setValue(self.config.get("ai.max_tokens", 60))
        self.window.config_tab.temperature.setValue(self.config.get("ai.temperature", 0.7))
        self.window.config_tab.target_groups.setText(
            ", ".join(self.config.get("wechat.target_groups", []))
        )
        self.window.config_tab.reply_delay.setValue(
            self.config.get("wechat.reply_delay", 0.0)
        )
        self.window.config_tab.auto_reply.setChecked(
            self.config.get("wechat.auto_reply", True)
        )

        self._status_timer = QTimer()
        self._status_timer.timeout.connect(self._update_gui_status)
        self._status_timer.start(2000)

    def _change_style(self, name: str) -> None:
        if not self.style:
            return

        if self.style.is_locked:
            logger.warning(f"人设已永久锁定为 {self.style.locked_name}，拒绝切换")
            self.window.style_tab.update_styles(self.style.list_styles())
            self.window.style_tab.set_locked(True, self.style.locked_name)
            QMessageBox.information(
                self.window,
                "人设已锁定",
                "傲娇鲸鱼娘人设已永久锁定，不能更改为其他风格。",
            )
            return

        self.style.set_active(name)
        self.window.style_tab.update_styles(self.style.list_styles())

    def _start_sticker_scheduler(self) -> None:
        """启动收藏表情随机发送任务。"""
        if self._sticker_task and not self._sticker_task.done():
            return

        sticker_cfg = self.config.get("wechat.sticker", {})
        if not sticker_cfg.get("enabled", False):
            logger.info("收藏表情随机发送已关闭")
            return
        if not sticker_cfg.get("random_schedule", False):
            logger.info("收藏表情已改为每三次回复发送，随机定时发送已关闭")
            return
        if not self.wechat or not hasattr(self.wechat, "send_random_favorite_sticker"):
            logger.info("当前微信后端不支持随机发送收藏表情")
            return

        self._sticker_task = asyncio.create_task(self._sticker_loop())
        logger.info("收藏表情随机发送任务已启动")

    async def _stop_sticker_scheduler(self) -> None:
        """停止收藏表情随机发送任务。"""
        task = self._sticker_task
        self._sticker_task = None
        if not task:
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("收藏表情随机发送任务已停止")

    async def _sticker_loop(self) -> None:
        """按随机间隔从微信收藏表情中发送一个表情。"""
        sticker_cfg = self.config.get("wechat.sticker", {})
        try:
            min_interval = max(30.0, float(sticker_cfg.get("min_interval", 600)))
            max_interval = max(min_interval, float(sticker_cfg.get("max_interval", 1200)))
        except (TypeError, ValueError):
            logger.warning("收藏表情间隔配置无效，使用默认 600 到 1200 秒")
            min_interval, max_interval = 600.0, 1200.0

        try:
            while self._running:
                delay = random.uniform(min_interval, max_interval)
                logger.debug(f"下次收藏表情发送等待 {delay:.0f} 秒")
                await asyncio.sleep(delay)

                try:
                    if (
                        not self._running
                        or not self.wechat
                        or not self.wechat.is_connected
                    ):
                        continue

                    groups = list(getattr(self.wechat, "target_groups", []) or [])
                    if not groups:
                        continue

                    room = random.choice(groups)
                    success = await self.wechat.send_random_favorite_sticker(room)
                    if success:
                        self._add_recent_message({
                            "direction": "out",
                            "room": room,
                            "sender": "",
                            "content": "[收藏表情]",
                        })
                        self._on_event("sticker_sent", {"room": room})
                    else:
                        logger.warning(f"随机发送收藏表情失败: {room}")
                        self._on_event("sticker_failed", {"room": room})
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"单次收藏表情发送异常，继续下一轮: {e}")
                    await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise

    async def _test_api(self) -> None:
        if self.ai:
            success, msg = await self.ai.test_connection()
            if self.window:
                self.window.config_tab.test_btn.setText(f"{'成功' if success else '失败'}: {msg[:50]}")

    def _save_config(self) -> None:
        if self.config and self.window:
            cfg = self.window.config_tab
            self.config.set("ai.api_url", cfg.api_url.text())
            api_key = cfg.api_key.text().strip()
            if api_key:
                self.config.set("ai.api_key_encrypted", self.config.encrypt_value(api_key))
                self.config.set("ai.api_key", "")
                if self.ai:
                    self.ai.api_key = api_key
            self.config.set("ai.model", cfg.model.text())
            self.config.set("ai.max_tokens", cfg.max_tokens.value())
            self.config.set("ai.temperature", cfg.temperature.value())
            self.config.set("wechat.auto_reply", cfg.auto_reply.isChecked())
            self.config.set("wechat.reply_delay", cfg.reply_delay.value())
            groups = [g.strip() for g in cfg.target_groups.text().split(",") if g.strip()]
            self.config.set("wechat.target_groups", groups)
            self.config.save()

            if self.ai:
                self.ai.api_url = cfg.api_url.text()
                self.ai.model = cfg.model.text()
                self.ai.max_tokens = cfg.max_tokens.value()
                self.ai.temperature = cfg.temperature.value()
                self.ai.api_style = self.ai._resolve_api_style(
                    self.config.get("ai.api_url", ""),
                    self.config.get("ai.api_style", "auto"),
                )
                self.ai.api_url = self.ai._normalize_api_url(
                    self.config.get("ai.api_url", ""),
                    self.ai.api_style,
                )
                self.ai._clients.clear()
            if self.wechat:
                self.wechat.target_groups = groups
                self.wechat.reply_delay = cfg.reply_delay.value()

            logger.info("配置已保存")

    def _update_gui_status(self) -> None:
        if not self.window:
            return
        wx_status = self.wechat.status.value if self.wechat else "unknown"
        wx_color = {"connected": "#4CAF50", "connecting": "#FF9800", "error": "#F44336"}.get(wx_status, "#888")
        self.window.update_status("wechat", wx_status, wx_color)

        if getattr(self, "_qq_enabled", False) and self.qq:
            qq_status = self.qq.status.value
            qq_color = {"connected": "#4CAF50", "connecting": "#FF9800", "error": "#F44336"}.get(qq_status, "#888")
            self.window.update_status("qq", qq_status, qq_color)
        else:
            self.window.update_status("qq", "未启用", "#888")

        ai_ok = self.ai.is_available if self.ai else False
        self.window.update_status("ai", "可用" if ai_ok else "不可用", "#4CAF50" if ai_ok else "#F44336")

        wx_queue = self.handler._queue.qsize() if self.handler else 0
        qq_queue = self.qq_handler._queue.qsize() if self.qq_handler else 0
        q_size = wx_queue + qq_queue
        self.window.update_status("queue", str(q_size), "#FF9800" if q_size > 10 else "#4CAF50")

        style_name = self.style.active_name if self.style else "--"
        self.window.update_status("style", style_name, "#2196F3")


def run_web_server(bot: ChatBot, host: str, port: int) -> None:
    """运行 Web 控制台；端口冲突时短暂重试并记录完整错误。"""
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            uvicorn.run(web_app, host=host, port=port, log_level="warning")
            return
        except KeyboardInterrupt:
            return
        except BaseException as exc:
            last_error = exc
            log_exception("main.run_web_server", exc=exc)
            logger.error(
                f"Web 控制台启动失败（第 {attempt}/3 次）: {exc}"
            )
            time.sleep(2 * attempt)
    if last_error is not None:
        logger.error(f"Web 控制台最终启动失败: {last_error}")


def main():
    mark_startup("main")
    _install_crash_logging()
    _install_qt_message_filter()
    _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _CLEAN_EXIT_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"清理正常退出标记失败: {exc}", file=sys.stderr)
    _write_heartbeat()

    if not acquire_single_instance():
        message = (
            "已经有一个微信机器人在运行了，请勿重复启动。"
            "如需重启，请先运行 restart-clean.bat 清理旧实例。"
        )
        log_error_message(
            context="main.acquire_single_instance",
            error_type="DuplicateInstance",
            error_message=message,
        )
        _report_duplicate_start(message)
        sys.exit(1)

    bot = ChatBot()

    if not bot.initialize():
        print("初始化失败，请检查配置文件")
        sys.exit(1)

    # 创建 Qt 应用
    qt_app = QApplication(sys.argv)
    qt_app.setQuitOnLastWindowClosed(True)

    # 创建 asyncio 事件循环（独立线程）
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    async_thread = threading.Thread(target=loop.run_forever, daemon=True)
    async_thread.start()

    # 创建 Qt-asyncio 桥梁
    bridge = AsyncBridge(loop)
    bot.set_async_bridge(bridge)

    # 创建 GUI
    window = MainWindow()
    bot.window = window
    bot._setup_gui()
    window.show()
    _restore_recent_monitor_text(window)

    heartbeat_timer = QTimer()
    heartbeat_timer.timeout.connect(_write_heartbeat)
    heartbeat_timer.start(5000)

    # 启动 Web 服务器
    web_cfg = bot.config.get("web", {})
    if web_cfg.get("enabled", True):
        host = web_cfg.get("host", "127.0.0.1")
        port = web_cfg.get("port", 8080)
        web_thread = threading.Thread(
            target=run_web_server,
            args=(bot, host, port),
            daemon=True,
        )
        web_thread.start()
        logger.info(f"Web 仪表盘已启动: http://{host}:{port}")

    if bot.config.get("wechat.auto_start", True):
        QTimer.singleShot(800, lambda: bridge.call(bot.start))
        logger.info("已启用启动后自动连接微信")

    # 运行 Qt 事件循环
    exit_code = qt_app.exec()
    heartbeat_timer.stop()
    if exit_code == 0:
        _mark_clean_exit()
    sys.exit(exit_code)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:
        log_exception("main.__main__", exc=exc, level="CRITICAL")
        raise
