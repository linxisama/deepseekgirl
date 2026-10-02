"""配置管理模块：YAML 读写 + Fernet 加密敏感字段"""

import os
from pathlib import Path
from typing import Any

import yaml
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64
from loguru import logger

LOCKED_STYLE_NAME = "tsundere_whale_girl"

DEFAULT_CONFIG = {
    "wechat": {
        "backend": "auto",
        "target_groups": [],
        "auto_reply": True,
        "reply_delay": 0.0,
        "context_file": "./data/conversation_contexts.json",
        "context_history": 50,
        "ai_context_messages": 12,
        "trigger_keywords": [
            "大肥鲸",
            "deepseek",
            "鲸鱼娘",
            "吃大米",
            "token不够用了",
            "小深",
            "蓝鲸酱",
            "小鲸鱼",
            "出来",
            "在不在",
            "啥子",
            "做啥子",
            "在干嘛",
            "说啥呢",
            "咋样了",
            "冒个泡",
            "开饭了",
            "大米饭",
            "干饭了",
            "煲仔饭",
            "投喂",
            "帮帮我",
            "救一下",
            "别摸鱼",
            "别睡了",
            "写代码",
            "干活了",
            "摸尾巴",
            "摸摸头",
            "拍水花",
            "变胖了",
            "该减肥",
            "好圆啊",
            "咕嘟嘟",
        ],
        "answered_cache_file": "./data/answered_questions.json",
        "dedupe_window": 21600,
        "computer_issue_timeout": 8,
        "computer_max_tokens": 600,
        "listen_private": True,
        "poke_cooldown": 10,
        "group_reply_limit": 6,
        "group_reply_window": 60,
        "max_message_age": 25,
        "image_search": {
            "enabled": True,
            "timeout": 3.0,
            "cache_dir": "./data/images",
        },
        "sensitivity": "normal",
        "proactive": {
            "enabled": True,
            "interval_hours": 2,
            "start_hour": 8,
            "end_hour": 20,
            "groups": [],
            "timeout": 8.0,
            # 长时间没人说话时的主动问候
            "idle": {
                "enabled": True,
                "idle_minutes": 30,
                "cooldown_minutes": 60,
                "check_interval": 60,
                "min_messages": 0,
            },
        },
        "sticker": {
            "enabled": True,
            "random_schedule": False,
            "reply_interval": 3,
            "reply_sticker_count": 3,
            "min_interval": 600,
            "max_interval": 1200,
        },
        # 微信端是否启用语音 / 点歌（细节见顶层 voice 配置）
        "voice": {
            "enabled": True,
        },
    },
    "ai": {
        "api_url": "https://api.example.com/v1/chat/completions",
        "api_key_encrypted": "",
        "api_style": "auto",
        "model": "gpt-4",
        "max_tokens": 60,
        "temperature": 0.7,
        "timeout": 10,
        "max_retries": 2,
        "fast_reply_timeout": 1.2,
        "fast_reply_fallback": True,
    },
    # QQ 机器人开放平台官方接口（与微信端完全独立运行）
    "qq": {
        "enabled": False,
        "backend": "official",
        "app_id": "",
        "client_secret": "",
        "client_secret_encrypted": "",
        "sandbox": False,
        "target_groups": [],
        "group_aliases": {},
        "listen_private": True,
        "reply_delay": 0.0,
        "send_interval": 1.2,
        "context_file": "./data/qq_conversation_contexts.json",
        "answered_cache_file": "./data/qq_answered_questions.json",
        "dedupe_window": 21600,
        "max_message_age": 600,
        "image_search": {
            "enabled": True,
            "timeout": 3.0,
            "cache_dir": "./data/qq-images",
        },
        "sensitivity": "normal",
        "proactive": {
            "enabled": False,
            "interval_hours": 2,
            "start_hour": 8,
            "end_hour": 20,
            "groups": [],
            "timeout": 8.0,
            # QQ 官方接口对主动推送有配额限制，默认关闭久未说话问候
            "idle": {
                "enabled": False,
                "idle_minutes": 30,
                "cooldown_minutes": 60,
                "check_interval": 60,
                "min_messages": 0,
            },
        },
        # QQ 端是否启用语音 / 点歌（细节见顶层 voice 配置）
        "voice": {
            "enabled": True,
        },
        # QQ 是纯 HTTP 接口，没有微信那种窗口风控，
        # 因此可以调低发送间隔换取更快的回复手感
        "efficiency": {
            "enabled": True,
            "send_interval": 0.3,
            "max_message_age": 30,
            "fast_reply_timeout": 1.0,
        },
    },
    # 语音合成 + 点歌（微信 / QQ 双端共用同一套参数与缓存）
    "voice": {
        "enabled": True,
        # auto = 优先 Edge 在线语音，失败自动回退 Windows 离线语音
        "engine": "auto",
        "default_voice": "zh-CN-XiaoxiaoNeural",
        "rate": "+0%",
        "pitch": "+0Hz",
        "cache_dir": "./data/voice_cache",
        "timeout": 25.0,
        "max_chars": 400,
        "retries": 2,
        # 启动后在后台预热热门歌曲，首点也接近秒回
        "prewarm": True,
        "prewarm_count": 6,
        # 真实歌曲下载源：本地部署的洛雪音乐服务器（LX Music Web）
        "music": {
            "enabled": False,
            "base_url": "",
            "password": "",
            "username": "",
            "user_password": "",
            "quality": "128k",
            "timeout": 30.0,
            "cache_dir": "./data/music_cache",
            "max_size_mb": 50.0,
        },
        "songs": {
            "enabled": True,
            "cache_dir": "./data/song_cache",
            "max_duration": 55.0,
            "sing_rate": "+8%",
            "sing_pitch": "+35Hz",
            "voice": "",
            "max_lyrics_chars": 300,
        },
        # 常见寒暄本地秒回，不占用 AI 调用
        "quick_replies": {
            "enabled": True,
            "max_chars": 10,
        },
    },
    # 收到的语音消息转文字（QQ / 微信双端共用）
    "asr": {
        "enabled": True,
        "model": "base",
        "model_dir": "./data/models/faster-whisper",
        "cache_dir": "./data/voice_inbox",
        "language": "zh",
        "compute_type": "int8",
        "cpu_threads": 4,
        "beam_size": 1,
        "max_audio_mb": 20.0,
        "hf_endpoint": "https://hf-mirror.com",
    },
    "style": {
        "active": LOCKED_STYLE_NAME,
        "locked": True,
        "locked_name": LOCKED_STYLE_NAME,
        "custom_styles_dir": "./styles/custom/",
    },
    "security": {
        "rate_limit": 20,
        "circuit_breaker_threshold": 8,
        "circuit_breaker_timeout": 5,
        "max_queue_size": 100,
    },
    "web": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8080,
    },
    "logging": {
        "level": "INFO",
        "file": "./logs/chatbot.log",
        "rotation": "10 MB",
        "retention": "7 days",
    },
}

ENCRYPTED_FIELDS = {"ai.api_key_encrypted"}


def _derive_key(password: str, salt: bytes) -> bytes:
    """从用户密码派生 Fernet 密钥"""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


class ConfigManager:
    """配置管理器，支持 YAML 读写和敏感字段加密"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = Path(config_path)
        self._config: dict = {}
        self._fernet: Fernet | None = None
        self._salt: bytes = b"wechat_chatbot_salt_v1"  # 固定 salt，生产环境应随机

    def load(self, password: str | None = None) -> dict:
        """加载配置文件，如果不存在则创建默认配置"""
        if password:
            self._fernet = Fernet(_derive_key(password, self._salt))

        if not self.config_path.exists():
            logger.warning(f"配置文件 {self.config_path} 不存在，使用默认配置")
            self._config = self._deep_copy(DEFAULT_CONFIG)
            return self._config

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            self._config = self._merge(DEFAULT_CONFIG, loaded)
            self._enforce_style_lock()
            logger.info(f"配置文件加载成功: {self.config_path}")
        except Exception as e:
            logger.error(f"配置文件加载失败: {e}，使用默认配置")
            self._config = self._deep_copy(DEFAULT_CONFIG)

        return self._config

    def save(self) -> None:
        """保存配置到文件"""
        try:
            self._enforce_style_lock()
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.dump(self._config, f, allow_unicode=True, default_flow_style=False)
            logger.info(f"配置已保存: {self.config_path}")
        except Exception as e:
            logger.error(f"配置保存失败: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值，支持点号分隔的嵌套键"""
        keys = key.split(".")
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        return value

    def set(self, key: str, value: Any) -> None:
        """设置配置值，支持点号分隔的嵌套键"""
        if key == "style.active":
            if value != LOCKED_STYLE_NAME:
                logger.warning(
                    f"人设已永久锁定为 {LOCKED_STYLE_NAME}，忽略切换请求: {value}"
                )
            value = LOCKED_STYLE_NAME
        elif key == "style.locked":
            value = True
        elif key == "style.locked_name":
            value = LOCKED_STYLE_NAME

        keys = key.split(".")
        config = self._config
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]
        config[keys[-1]] = value

    def _enforce_style_lock(self) -> None:
        """强制恢复固定人设，避免旧配置或手工修改绕过锁定。"""
        style = self._config.setdefault("style", {})
        style["active"] = LOCKED_STYLE_NAME
        style["locked"] = True
        style["locked_name"] = LOCKED_STYLE_NAME

    def encrypt_value(self, plaintext: str) -> str:
        """加密字符串值"""
        if not self._fernet:
            raise RuntimeError("未初始化加密模块，请提供密码")
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt_value(self, encrypted: str) -> str:
        """解密字符串值"""
        if not self._fernet:
            raise RuntimeError("未初始化加密模块，请提供密码")
        if not encrypted:
            return ""
        try:
            return self._fernet.decrypt(encrypted.encode()).decode()
        except Exception as e:
            logger.error(f"解密失败: {e}")
            return ""

    def get_decrypted(self, key: str) -> str:
        """获取并解密配置值"""
        encrypted = self.get(key, "")
        return self.decrypt_value(encrypted) if encrypted else ""

    @property
    def config(self) -> dict:
        return self._config

    @staticmethod
    def _deep_copy(d: dict) -> dict:
        """简单深拷贝"""
        result = {}
        for k, v in d.items():
            if isinstance(v, dict):
                result[k] = ConfigManager._deep_copy(v)
            elif isinstance(v, list):
                result[k] = list(v)
            else:
                result[k] = v
        return result

    @staticmethod
    def _merge(default: dict, override: dict) -> dict:
        """递归合并配置，override 覆盖 default"""
        result = ConfigManager._deep_copy(default)
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = ConfigManager._merge(result[key], value)
            else:
                result[key] = value
        return result
