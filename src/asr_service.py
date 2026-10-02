"""Local speech recognition for WeChat and QQ voice messages."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from loguru import logger


@dataclass
class ASRResult:
    text: str
    elapsed: float
    audio_path: str = ""
    language: str = ""


class ASRService:
    """Thread-safe, lazily initialised faster-whisper wrapper."""

    SILK_HEADER = b"#!SILK_V3"
    SUPPORTED_SUFFIXES = {
        ".wav",
        ".mp3",
        ".m4a",
        ".aac",
        ".flac",
        ".ogg",
        ".opus",
        ".webm",
        ".amr",
        ".silk",
        ".slk",
    }

    def __init__(
        self,
        *,
        enabled: bool = True,
        model: str = "base",
        model_dir: str = "./data/models/faster-whisper",
        cache_dir: str = "./data/voice_inbox",
        language: str = "zh",
        compute_type: str = "int8",
        cpu_threads: int = 4,
        beam_size: int = 1,
        max_audio_mb: float = 20.0,
        hf_endpoint: str = "https://hf-mirror.com",
    ) -> None:
        self.enabled = bool(enabled)
        self.model_name = str(model or "base").strip() or "base"
        self.model_dir = Path(model_dir).expanduser().resolve()
        self.cache_dir = Path(cache_dir).expanduser().resolve()
        self.language = str(language or "zh").strip() or "zh"
        self.compute_type = str(compute_type or "int8").strip() or "int8"
        self.cpu_threads = max(1, min(16, int(cpu_threads or 4)))
        self.beam_size = max(1, min(5, int(beam_size or 1)))
        self.max_audio_bytes = max(
            1,
            int(float(max_audio_mb or 20.0) * 1024 * 1024),
        )
        self.hf_endpoint = str(hf_endpoint or "").strip()

        self._model = None
        self._model_error = ""
        self._model_lock = threading.Lock()
        self._run_lock = asyncio.Lock()
        self._warmup_task: asyncio.Task | None = None
        self._count = 0
        self._failures = 0
        self._last_seconds = 0.0
        self._avg_seconds = 0.0
        self._last_text = ""

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.model_dir.mkdir(parents=True, exist_ok=True)

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "model": self.model_name,
            "loaded": self.loaded,
            "language": self.language,
            "compute_type": self.compute_type,
            "cpu_threads": self.cpu_threads,
            "count": self._count,
            "failures": self._failures,
            "last_seconds": round(self._last_seconds, 2),
            "avg_seconds": round(self._avg_seconds, 2),
            "last_text": self._last_text[:80],
            "last_error": self._model_error,
        }

    async def start(self) -> None:
        """Load the model in the background so first voice reply is faster."""
        if not self.enabled or self._warmup_task is not None:
            return

        async def warmup() -> None:
            try:
                await asyncio.to_thread(self._ensure_model)
            except Exception as exc:
                self._model_error = f"{type(exc).__name__}: {exc}"
                logger.warning(f"[语音识别] 模型预热失败: {self._model_error}")

        self._warmup_task = asyncio.create_task(warmup())

    async def close(self) -> None:
        task = self._warmup_task
        self._warmup_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def transcribe(self, audio_path: str) -> ASRResult:
        if not self.enabled:
            raise RuntimeError("语音识别已关闭")

        path = Path(str(audio_path or "")).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"语音文件不存在: {path}")
        if path.suffix.casefold() not in self.SUPPORTED_SUFFIXES:
            raise ValueError(f"不支持的音频格式: {path.suffix}")
        if path.stat().st_size > self.max_audio_bytes:
            raise ValueError("语音文件过大")

        async with self._run_lock:
            started = time.perf_counter()
            try:
                text = await asyncio.to_thread(self._transcribe_sync, path)
            except Exception as exc:
                self._model_error = f"{type(exc).__name__}: {exc}"
                self._failures += 1
                raise
            elapsed = time.perf_counter() - started
            self._count += 1
            self._model_error = ""
            self._last_seconds = elapsed
            self._avg_seconds = (
                (self._avg_seconds * (self._count - 1) + elapsed) / self._count
            )
            self._last_text = text
            logger.info(
                f"[语音识别] 转写完成（{elapsed:.2f} 秒）: {text[:80]}"
            )
            return ASRResult(
                text=text,
                elapsed=elapsed,
                audio_path=str(path),
                language=self.language,
            )

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            if self.hf_endpoint:
                os.environ.setdefault("HF_ENDPOINT", self.hf_endpoint)
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

            from faster_whisper import WhisperModel

            logger.info(
                f"[语音识别] 正在加载本地模型 {self.model_name} "
                f"({self.compute_type}, {self.cpu_threads} 线程)"
            )
            started = time.perf_counter()
            local_dir = self.model_dir / self.model_name
            local_model = (
                local_dir.is_dir()
                and (local_dir / "model.bin").is_file()
            )
            model_ref = str(local_dir) if local_model else self.model_name
            self._model = WhisperModel(
                model_ref,
                device="cpu",
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
                num_workers=1,
                download_root=str(self.model_dir),
                local_files_only=local_model,
            )
            self._model_error = ""
            logger.info(
                f"[语音识别] 模型已加载（{time.perf_counter() - started:.2f} 秒）"
            )
            return self._model

    def _transcribe_sync(self, audio_path: Path) -> str:
        model = self._ensure_model()
        prepared = self._prepare_audio(audio_path)
        cleanup: list[Path] = []
        if prepared != audio_path:
            cleanup.append(prepared)
        try:
            segments, info = model.transcribe(
                str(prepared),
                language=self.language,
                beam_size=self.beam_size,
                best_of=1,
                temperature=0.0,
                vad_filter=True,
                vad_parameters={
                    "min_silence_duration_ms": 350,
                    "speech_pad_ms": 200,
                },
                initial_prompt=(
                    "以下是普通话简体中文对话，请准确转写为简体中文。"
                ),
                condition_on_previous_text=False,
                word_timestamps=False,
                without_timestamps=True,
            )
            text = "".join(
                str(getattr(segment, "text", "") or "")
                for segment in segments
            )
            text = " ".join(text.split()).strip()
            if not text:
                raise RuntimeError("没有识别到有效语音内容")
            logger.debug(
                f"[语音识别] 检测语言={getattr(info, 'language', '')}, "
                f"时长={getattr(info, 'duration', 0.0):.2f}s"
            )
            return text
        finally:
            for path in cleanup:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _prepare_audio(self, source: Path) -> Path:
        suffix = source.suffix.casefold()
        silk_header = self._looks_like_silk(source)
        if suffix not in {".silk", ".slk", ".amr"} and not silk_header:
            return source

        digest = hashlib.sha256(
            f"{source}:{source.stat().st_mtime_ns}:{source.stat().st_size}".encode()
        ).hexdigest()[:24]
        target = self.cache_dir / f"{digest}.wav"
        if target.is_file() and target.stat().st_size > 44:
            return target

        # QQ 有时把 SILK 内容命名为 .amr；必须以文件头为准，不能只看扩展名。
        if suffix in {".silk", ".slk"} or silk_header:
            self._silk_to_wav(source, target)
        else:
            self._ffmpeg_to_wav(source, target)
        if not target.is_file() or target.stat().st_size <= 44:
            raise RuntimeError("音频转换失败")
        return target

    @classmethod
    def _looks_like_silk(cls, source: Path) -> bool:
        try:
            with source.open("rb") as handle:
                header = handle.read(16)
        except OSError:
            return False
        return cls.SILK_HEADER in header

    @staticmethod
    def _silk_to_wav(source: Path, target: Path) -> None:
        try:
            import pysilk
        except Exception as exc:
            raise RuntimeError(
                "缺少 silk-python，无法解码 SILK 语音"
            ) from exc

        pcm_path = target.with_suffix(".pcm")
        try:
            with source.open("rb") as src, pcm_path.open("wb") as pcm:
                pysilk.decode(src, pcm, 24000)
            with pcm_path.open("rb") as pcm, wave.open(str(target), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                while True:
                    chunk = pcm.read(1024 * 1024)
                    if not chunk:
                        break
                    wav.writeframesraw(chunk)
        finally:
            try:
                pcm_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _ffmpeg_to_wav(source: Path, target: Path) -> None:
        ffmpeg = os.environ.get("ASR_FFMPEG", "").strip()
        if not ffmpeg:
            try:
                import imageio_ffmpeg

                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                ffmpeg = shutil.which("ffmpeg") or ""
        if not ffmpeg:
            raise RuntimeError("未找到 ffmpeg，无法转换音频")

        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-ac",
                "1",
                "-ar",
                "16000",
                str(target),
            ],
            capture_output=True,
            timeout=20,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "ffmpeg 转码失败: "
                + result.stderr.decode("utf-8", errors="ignore")[:160]
            )
