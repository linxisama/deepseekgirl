"""语音合成服务（微信 / QQ 双端共用）。

设计目标：**永远能出声，且重复请求接近零延迟**。

1. 首选 ``edge-tts``（微软 Edge 在线语音）：音质自然，支持语速 / 音调微调，
   用来做"唱歌"这种需要抬高音调的玩法。
2. 网络抖动或服务端偶发 ``NoAudioReceived``（实测为概率性问题）时自动重试，
   并在重试仍失败后回退 Windows 本地 SAPI5（pyttsx3）离线引擎。
3. 合成结果按"内容 + 参数"哈希落盘缓存，重复点同一首歌时直接命中缓存，
   把数秒的合成耗时压成毫秒级的文件读取。

所有公开方法都是协程，内部把阻塞调用丢到线程池，绝不阻塞消息处理循环。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import struct
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 常量

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

#: 允许使用的中文音色（其余一律回落到默认音色，避免用户填错导致无法出声）
KNOWN_VOICES = (
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunyangNeural",
    "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural",
    "zh-HK-HiuMaanNeural",
    "zh-TW-HsiaoChenNeural",
)

#: 音色别名，方便在配置 / 指令里用中文指定
VOICE_PRESETS = {
    "default": DEFAULT_VOICE,
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "xiaoyi": "zh-CN-XiaoyiNeural",
    "yunxi": "zh-CN-YunxiNeural",
    "yunjian": "zh-CN-YunjianNeural",
    "云希": "zh-CN-YunxiNeural",
    "云健": "zh-CN-YunjianNeural",
    "晓晓": "zh-CN-XiaoxiaoNeural",
    "晓伊": "zh-CN-XiaoyiNeural",
    "女声": "zh-CN-XiaoxiaoNeural",
    "女生": "zh-CN-XiaoxiaoNeural",
    "男声": "zh-CN-YunxiNeural",
    "男生": "zh-CN-YunxiNeural",
    "东北": "zh-CN-liaoning-XiaobeiNeural",
    "陕西": "zh-CN-shaanxi-XiaoniNeural",
}

#: edge-tts 默认输出 audio-24khz-48kbitrate-mono-mp3
EDGE_BITRATE_BPS = 48000

#: 明显小于该字节数的产物视为合成失败（空音频）
_MIN_AUDIO_BYTES = 512

_SAPI_LOCK = threading.Lock()

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _percent_to_wpm(value: str, base: int = 200) -> int:
    """把 ``"+8%"`` / ``"-10%"`` 这类语速换算成 SAPI 的 words-per-minute。"""
    text = str(value or "").strip().replace("%", "")
    try:
        pct = float(text)
    except (TypeError, ValueError):
        pct = 0.0
    return int(max(60, min(420, base * (1.0 + pct / 100.0))))


def _wav_duration(path: Path) -> float:
    """解析 WAV 头部拿到真实时长，失败时返回 0。"""
    try:
        with open(path, "rb") as handle:
            header = handle.read(64)
        if len(header) < 44 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            return 0.0
        byte_rate = struct.unpack_from("<I", header, 28)[0]
        data_size = struct.unpack_from("<I", header, 40)[0]
        if byte_rate <= 0 or data_size <= 0:
            return 0.0
        return data_size / float(byte_rate)
    except Exception:
        return 0.0


class TTSService:
    """语音合成服务。线程安全，可被双端 handler 并发调用。"""

    def __init__(
        self,
        cache_dir: str | os.PathLike = "./data/voice_cache",
        enabled: bool = True,
        engine: str = "auto",
        default_voice: str = DEFAULT_VOICE,
        rate: str = "+0%",
        pitch: str = "+0Hz",
        timeout: float = 25.0,
        max_chars: int = 400,
        retries: int = 2,
        backoff: float = 0.6,
    ) -> None:
        self.enabled = bool(enabled)
        self.engine = str(engine or "auto").strip().lower()
        if self.engine not in {"auto", "edge", "sapi", "off"}:
            self.engine = "auto"
        self.default_voice = self.normalize_voice(default_voice)
        self.rate = str(rate or "+0%")
        self.pitch = str(pitch or "+0Hz")
        self.timeout = max(5.0, float(timeout))
        self.max_chars = max(20, int(max_chars))
        self.retries = max(0, int(retries))
        self.backoff = max(0.1, float(backoff))

        self.cache_dir = Path(cache_dir)
        self._tmp_dir = self.cache_dir / ".tmp"
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._tmp_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover - 磁盘异常
            logger.error(f"语音缓存目录创建失败: {exc}")

        self._lock = asyncio.Lock()
        self._engine_cache: str | None = None
        self.stats: dict = {
            "synthesis": 0,
            "cache_hits": 0,
            "failures": 0,
            "last_seconds": 0.0,
            "total_seconds": 0.0,
        }

    # ------------------------------------------------------------ 基础能力

    @staticmethod
    def normalize_voice(voice: str | None) -> str:
        """把别名 / 错误音色统一成可用的 edge 音色名。"""
        raw = str(voice or "").strip()
        if not raw:
            return DEFAULT_VOICE
        mapped = VOICE_PRESETS.get(raw) or VOICE_PRESETS.get(raw.casefold())
        if mapped:
            return mapped
        if raw in KNOWN_VOICES:
            return raw
        # 允许用户直接写完整音色名，只要在已知列表里
        for known in KNOWN_VOICES:
            if known.casefold() == raw.casefold():
                return known
        return DEFAULT_VOICE

    @staticmethod
    def _edge_importable() -> bool:
        try:
            import edge_tts  # noqa: F401
        except Exception:
            return False
        return True

    @staticmethod
    def _sapi_importable() -> bool:
        try:
            import pyttsx3  # noqa: F401
        except Exception:
            return False
        return True

    def resolve_engine(self) -> str:
        """返回当前实际可用的引擎：``edge`` / ``sapi`` / ``off``。"""
        if not self.enabled or self.engine == "off":
            return "off"
        if self._engine_cache:
            return self._engine_cache
        if self.engine == "edge":
            resolved = "edge" if self._edge_importable() else "off"
        elif self.engine == "sapi":
            resolved = "sapi" if self._sapi_importable() else "off"
        else:  # auto
            if self._edge_importable():
                resolved = "edge"
            elif self._sapi_importable():
                resolved = "sapi"
            else:
                resolved = "off"
        self._engine_cache = resolved
        return resolved

    def available(self) -> bool:
        return self.resolve_engine() != "off"

    def _clean_text(self, text: str) -> str:
        cleaned = _CONTROL_CHARS.sub("", str(text or ""))
        cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        if len(cleaned) > self.max_chars:
            cleaned = cleaned[: self.max_chars]
        return cleaned

    @staticmethod
    def cache_key(
        text: str,
        voice: str,
        rate: str,
        pitch: str,
        salt: str = "",
    ) -> str:
        payload = "|".join([salt, voice, rate, pitch, text])
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def estimate_duration(path: str | os.PathLike) -> float:
        """估算音频时长（秒）。mp3 按 48kbps 折算，wav 读头部精确计算。"""
        target = Path(path)
        try:
            size = target.stat().st_size
        except OSError:
            return 0.0
        if target.suffix.casefold() == ".wav":
            parsed = _wav_duration(target)
            if parsed > 0:
                return parsed
            # 32kHz / 16bit / 单声道 估算
            return size / float(32000 * 2)
        return size * 8.0 / float(EDGE_BITRATE_BPS)

    # ------------------------------------------------------------ 合成入口

    async def synthesize(
        self,
        text: str,
        out_path: str | os.PathLike | None = None,
        voice: str | None = None,
        rate: str | None = None,
        pitch: str | None = None,
        salt: str = "",
    ) -> Path | None:
        """把文本合成为音频文件，返回文件路径；失败返回 ``None``。

        ``out_path`` 为空时写入内容寻址缓存目录，命中缓存直接返回。
        ``salt`` 用于区分同一文本的不同用途（例如"念"与"唱"）。
        """
        cleaned = self._clean_text(text)
        if not cleaned:
            return None
        engine = self.resolve_engine()
        if engine == "off":
            logger.debug("语音合成已关闭或引擎不可用，跳过")
            return None

        voice = self.normalize_voice(voice or self.default_voice)
        rate = str(rate or self.rate)
        pitch = str(pitch or self.pitch)
        key = self.cache_key(cleaned, voice, rate, pitch, salt)

        if out_path:
            target = Path(out_path)
            target.parent.mkdir(parents=True, exist_ok=True)
        else:
            # 缓存命中时后缀由实际产物决定，这里先探测两种可能
            cached = self._lookup_cache(key)
            if cached is not None:
                self.stats["cache_hits"] += 1
                return cached
            target = self.cache_dir / f"{key}.mp3"

        if out_path and target.is_file() and target.stat().st_size > _MIN_AUDIO_BYTES:
            self.stats["cache_hits"] += 1
            return target

        started = time.perf_counter()
        produced = await self._render_with_fallback(
            cleaned,
            voice,
            rate,
            pitch,
            key,
            engine,
        )
        if produced is None:
            self.stats["failures"] += 1
            return None

        try:
            if produced != target:
                if target.exists():
                    target.unlink(missing_ok=True)
                shutil.move(str(produced), str(target))
        except Exception as exc:
            logger.error(f"语音文件落盘失败: {exc}")
            return produced if produced.is_file() else None

        elapsed = time.perf_counter() - started
        self.stats["synthesis"] += 1
        self.stats["last_seconds"] = round(elapsed, 2)
        self.stats["total_seconds"] = round(
            float(self.stats["total_seconds"]) + elapsed,
            2,
        )
        logger.info(
            f"语音合成完成: {target.name} "
            f"{target.stat().st_size} 字节 / {elapsed:.2f} 秒"
        )
        return target

    def _lookup_cache(self, key: str) -> Path | None:
        for suffix in (".mp3", ".wav"):
            candidate = self.cache_dir / f"{key}{suffix}"
            try:
                if candidate.is_file() and candidate.stat().st_size > _MIN_AUDIO_BYTES:
                    return candidate
            except OSError:
                continue
        return None

    def cache_path_for(
        self,
        text: str,
        voice: str | None = None,
        rate: str | None = None,
        pitch: str | None = None,
        salt: str = "",
    ) -> Path | None:
        """按与 :meth:`synthesize` 完全一致的规则查找已有缓存。

        供上层（如点歌服务）在不解码音频的前提下判断"是否已缓存"。
        """
        cleaned = self._clean_text(text)
        if not cleaned:
            return None
        key = self.cache_key(
            cleaned,
            self.normalize_voice(voice or self.default_voice),
            str(rate or self.rate),
            str(pitch or self.pitch),
            salt,
        )
        return self._lookup_cache(key)

    async def _render_with_fallback(
        self,
        text: str,
        voice: str,
        rate: str,
        pitch: str,
        key: str,
        engine: str,
    ) -> Path | None:
        """在线引擎重试，全部失败后回退离线引擎。"""
        attempts = self.retries + 1
        for attempt in range(attempts):
            if engine == "edge":
                candidate = self._tmp_dir / f"{key}-{attempt}.mp3"
                ok = await asyncio.to_thread(
                    self._synth_edge, text, voice, rate, pitch, candidate
                )
            else:
                candidate = self._tmp_dir / f"{key}-{attempt}.wav"
                ok = await asyncio.to_thread(
                    self._synth_sapi, text, rate, pitch, candidate
                )
            if ok:
                return candidate
            if attempt < attempts - 1:
                wait = self.backoff * (attempt + 1)
                logger.warning(
                    f"语音合成第 {attempt + 1} 次失败，{wait:.1f} 秒后重试"
                )
                await asyncio.sleep(wait)

        if engine == "edge" and self._sapi_importable():
            logger.warning("在线语音引擎不可用，回退 Windows 离线语音")
            candidate = self._tmp_dir / f"{key}-sapi.wav"
            ok = await asyncio.to_thread(
                self._synth_sapi, text, rate, pitch, candidate
            )
            if ok:
                return candidate
        return None

    # ------------------------------------------------------------ 各引擎实现

    def _synth_edge(
        self,
        text: str,
        voice: str,
        rate: str,
        pitch: str,
        out: Path,
    ) -> bool:
        try:
            import edge_tts
        except Exception as exc:
            logger.warning(f"edge-tts 不可用: {exc}")
            return False

        async def _run() -> None:
            communicate = edge_tts.Communicate(
                text,
                voice,
                rate=rate,
                pitch=pitch,
            )
            await asyncio.wait_for(
                communicate.save(str(out)),
                timeout=self.timeout,
            )

        try:
            asyncio.run(_run())
        except Exception as exc:
            logger.warning(f"edge-tts 合成失败（{voice}）: {exc}")
            self._safe_unlink(out)
            return False

        if out.is_file() and out.stat().st_size > _MIN_AUDIO_BYTES:
            return True
        self._safe_unlink(out)
        return False

    def _synth_sapi(
        self,
        text: str,
        rate: str,
        pitch: str,
        out: Path,
    ) -> bool:
        del pitch  # SAPI5 不支持音调微调，只保证语速可调
        try:
            import pyttsx3
        except Exception as exc:
            logger.warning(f"pyttsx3 不可用: {exc}")
            return False

        engine = None
        with _SAPI_LOCK:  # pyttsx3 非线程安全，串行化
            try:
                engine = pyttsx3.init("sapi5")
                engine.setProperty("rate", _percent_to_wpm(rate))
                engine.setProperty("volume", 1.0)
                engine.save_to_file(text, str(out))
                engine.runAndWait()
            except Exception as exc:
                logger.warning(f"SAPI 离线合成失败: {exc}")
                self._safe_unlink(out)
                return False
            finally:
                if engine is not None:
                    try:
                        engine.stop()
                    except Exception:
                        pass

        if out.is_file() and out.stat().st_size > _MIN_AUDIO_BYTES:
            return True
        self._safe_unlink(out)
        return False

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------ 维护

    def purge_tmp(self, max_age: float = 3600.0) -> int:
        """清理合成过程中的临时文件，返回删除数量。"""
        removed = 0
        now = time.time()
        try:
            for item in self._tmp_dir.iterdir():
                try:
                    if not item.is_file():
                        continue
                    if now - item.stat().st_mtime > max_age:
                        item.unlink(missing_ok=True)
                        removed += 1
                except OSError:
                    continue
        except Exception:
            return removed
        return removed

    def cache_info(self) -> dict:
        count = 0
        size = 0
        try:
            for item in self.cache_dir.iterdir():
                if item.is_file() and item.suffix.casefold() in {".mp3", ".wav"}:
                    count += 1
                    size += item.stat().st_size
        except Exception:
            pass
        return {
            "count": count,
            "size_mb": round(size / 1024.0 / 1024.0, 2),
        }

    def status(self) -> dict:
        engine = self.resolve_engine()
        total = int(self.stats["synthesis"]) + int(self.stats["cache_hits"])
        hit_rate = (
            round(int(self.stats["cache_hits"]) * 100.0 / total, 1)
            if total
            else 0.0
        )
        return {
            "enabled": self.enabled,
            "engine": engine,
            "engine_label": {
                "edge": "Edge 在线语音",
                "sapi": "Windows 离线语音",
                "off": "未启用",
            }.get(engine, engine),
            "default_voice": self.default_voice,
            "rate": self.rate,
            "pitch": self.pitch,
            "synthesis": int(self.stats["synthesis"]),
            "cache_hits": int(self.stats["cache_hits"]),
            "failures": int(self.stats["failures"]),
            "hit_rate": hit_rate,
            "last_seconds": float(self.stats["last_seconds"]),
            "avg_seconds": (
                round(
                    float(self.stats["total_seconds"])
                    / max(1, int(self.stats["synthesis"])),
                    2,
                )
            ),
            "cache": self.cache_info(),
        }
