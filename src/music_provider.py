"""真实歌曲下载提供者：对接本地部署的 LX Music Web（洛雪音乐同步服务器）。

接口契约（实测于 lxserver v2.0.0，/music 播放器路径）：
1. ``POST {base}/api/music/auth``     {"password": 访问密码}
   → 响应头 ``Set-Cookie: lx_player_session=<token>``（24 小时有效）
2. ``GET  {base}/api/music/search?name=<歌名>``
   → JSON 数组，元素含 name/singer/source/songmid/types 等
3. ``POST {base}/api/music/url``      {"songInfo": 搜索结果, "quality": "128k"}
   → {"url": 真实下载地址, "type": 实际音质}
4. ``GET  {base}/api/music/download?url=<enc>&filename=<enc>&tag=1``
   → 流式返回音频文件（服务端已嵌入 ID3 标签与歌词）

设计原则（对应"回复效率与稳定性"的要求）：
- **永不阻塞**：搜索 / 解析 / 下载各自带超时，总耗时受 ``timeout`` 预算约束；
- **命中即秒回**：按"歌名+歌手+音质"哈希落盘缓存，重复点歌只读文件；
- **失败有兜底**：任何一步失败都返回 ``None``，由上层回退到 TTS 哼唱；
- **会话自愈**：401/403 时自动重新认证并重试一次。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_QUALITIES = ("128k", "320k", "flac", "flac24bit")

#: 文件名里不安全的字符
_BAD_CHARS = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")


def _safe_filename(text: str, limit: int = 80) -> str:
    cleaned = _BAD_CHARS.sub(" ", str(text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit] or "song"


def _parse_interval(value: str) -> float:
    """把 ``03:59`` 换算成秒。"""
    match = re.match(r"^\s*(\d+):(\d+)(?::(\d+))?\s*$", str(value or ""))
    if not match:
        return 0.0
    parts = [int(p) for p in match.groups() if p is not None]
    total = 0
    for part in parts:
        total = total * 60 + part
    return float(total)


#: MPEG1 Layer III 码率表（kbps）
_MP3_BITRATES = {
    0b0001: 32,
    0b0010: 40,
    0b0011: 48,
    0b0100: 56,
    0b0101: 64,
    0b0110: 80,
    0b0111: 96,
    0b1000: 112,
    0b1001: 128,
    0b1010: 160,
    0b1011: 192,
    0b1100: 224,
    0b1101: 256,
    0b1110: 320,
}


def mp3_duration(path: str | os.PathLike) -> float:
    """解析 MP3 首个合法帧头拿到码率，按文件大小折算时长；读不到返回 0。"""
    target = Path(path)
    if target.suffix.casefold() != ".mp3":
        return 0.0
    try:
        size = target.stat().st_size
        with open(target, "rb") as handle:
            data = handle.read(64 * 1024)
    except OSError:
        return 0.0
    if size <= 0 or len(data) < 4:
        return 0.0

    # 跳过 ID3v2 头
    idx = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        sync = (
            ((data[6] & 0x7F) << 21)
            | ((data[7] & 0x7F) << 14)
            | ((data[8] & 0x7F) << 7)
            | (data[9] & 0x7F)
        )
        idx = 10 + sync

    def _scan(start: int) -> float:
        for i in range(start, len(data) - 3):
            if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
                bitrate_idx = (data[i + 2] >> 4) & 0x0F
                kbps = _MP3_BITRATES.get(bitrate_idx)
                if kbps:
                    return size * 8.0 / (kbps * 1000.0)
                return 0.0
        return -1.0

    result = _scan(idx)
    if result >= 0.0:
        return result
    if idx > 0:
        # ID3 头与实际布局不符（罕见），回退为从头扫描
        result = _scan(0)
        if result >= 0.0:
            return result
    return 0.0


class LXMusicProvider:
    """LX Music Web 下载提供者。线程安全，支持并发调用。"""

    def __init__(
        self,
        base_url: str = "",
        password: str = "",
        username: str = "",
        user_password: str = "",
        quality: str = "128k",
        enabled: bool = False,
        timeout: float = 30.0,
        cache_dir: str | os.PathLike = "./data/music_cache",
        max_size_mb: float = 50.0,
        search_limit: int = 5,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.password = str(password or "")
        self.username = str(username or "")
        self.user_password = str(user_password or "")
        self.quality = quality if quality in _QUALITIES else "128k"
        self.enabled = bool(enabled) and bool(self.base_url)
        self.timeout = max(5.0, float(timeout))
        self.max_size_bytes = int(max(1.0, float(max_size_mb)) * 1024 * 1024)
        self.search_limit = max(1, int(search_limit))

        self.cache_dir = Path(cache_dir)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover
            logger.error(f"音乐缓存目录创建失败: {exc}")

        self._http: httpx.AsyncClient | None = None
        self._session_cookie = ""
        self._session_expire_at = 0.0
        self._auth_lock = asyncio.Lock()
        self._fetch_locks: dict[str, asyncio.Lock] = {}
        self.stats: dict = {
            "requests": 0,
            "hits": 0,
            "downloads": 0,
            "failures": 0,
            "bytes": 0,
            "last_seconds": 0.0,
        }

    # ------------------------------------------------------------ 基础

    def available(self) -> bool:
        return self.enabled

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(
                    connect=6.0,
                    read=min(60.0, self.timeout + 20.0),
                    write=15.0,
                    pool=6.0,
                ),
                limits=httpx.Limits(
                    max_connections=6,
                    max_keepalive_connections=3,
                ),
                headers={"Accept": "application/json"},
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None and not self._http.is_closed:
            try:
                await self._http.aclose()
            except Exception:
                pass
        self._http = None

    async def _ensure_session(self, force: bool = False) -> bool:
        """保证持有有效的 lx_player_session Cookie。"""
        if not force and self._session_cookie and time.time() < self._session_expire_at:
            return True
        if not self.password:
            logger.debug("[音乐] 未配置访问密码，跳过认证")
            return False
        async with self._auth_lock:
            if not force and self._session_cookie and time.time() < self._session_expire_at:
                return True
            try:
                resp = await self._client().post(
                    "/api/music/auth",
                    json={"password": self.password},
                )
                if resp.status_code != 200 or not resp.json().get("success"):
                    logger.warning(f"[音乐] 认证失败 HTTP {resp.status_code}: {resp.text[:120]}")
                    return False
                cookie = resp.cookies.get("lx_player_session", "")
                if not cookie:
                    logger.warning("[音乐] 认证成功但未下发会话 Cookie")
                    return False
                self._session_cookie = cookie
                # Cookie 有效期 24h，提前 1 小时刷新
                self._session_expire_at = time.time() + 23 * 3600
                logger.info("[音乐] 已登录 LX Music 服务器")
                return True
            except Exception as exc:
                logger.warning(f"[音乐] 认证异常: {exc}")
                return False

    def _authed_client(self) -> httpx.AsyncClient:
        client = self._client()
        if self._session_cookie:
            client.cookies.set("lx_player_session", self._session_cookie)
        return client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        retry_auth: bool = True,
        **kwargs,
    ) -> httpx.Response | None:
        """带认证自愈的请求封装。"""
        if not await self._ensure_session():
            return None
        try:
            resp = await self._authed_client().request(method, path, **kwargs)
            if resp.status_code in (401, 403) and retry_auth:
                logger.info("[音乐] 会话过期，重新认证后重试")
                if await self._ensure_session(force=True):
                    resp = await self._authed_client().request(method, path, **kwargs)
            return resp
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning(f"[音乐] 请求超时/连接失败 {path}: {exc}")
            return None
        except Exception as exc:
            logger.error(f"[音乐] 请求异常 {path}: {exc}")
            return None

    # ------------------------------------------------------------ 搜索 / 解析

    async def search(self, keyword: str) -> list[dict]:
        """搜索歌曲，返回候选列表。"""
        keyword = str(keyword or "").strip()
        if not keyword:
            return []
        resp = await self._request(
            "GET",
            f"/api/music/search?name={quote(keyword)}",
        )
        if resp is None or resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        return data[: self.search_limit]

    @staticmethod
    def pick_best(candidates: list[dict], keyword: str, artist: str = "") -> dict | None:
        """从候选里挑最匹配的一首：优先歌名完全匹配，其次歌手匹配。"""
        if not candidates:
            return None
        key = str(keyword or "").strip().casefold()
        artist_key = str(artist or "").strip().casefold()

        exact = [
            item
            for item in candidates
            if str(item.get("name", "")).strip().casefold() == key
        ]
        pool = exact or candidates
        if artist_key:
            with_artist = [
                item
                for item in pool
                if artist_key in str(item.get("singer", "")).casefold()
            ]
            if with_artist:
                pool = with_artist
        return pool[0]

    async def resolve(self, song: dict, quality: str | None = None) -> str:
        """把搜索结果解析成真实下载地址，失败返回空串。"""
        if not song:
            return ""
        payload = {
            "songInfo": song,
            "quality": quality or self.quality,
            "enableAutoSwitchApiSource": True,
        }
        resp = await self._request("POST", "/api/music/url", json=payload)
        if resp is None or resp.status_code != 200:
            return ""
        try:
            data = resp.json()
        except Exception:
            return ""
        url = str(data.get("url") or "").strip()
        return url if url.startswith("http") else ""

    # ------------------------------------------------------------ 下载

    def _cache_path(self, title: str, artist: str, quality: str, ext: str = "mp3") -> Path:
        key = hashlib.sha1(
            f"{title}|{artist}|{quality}".encode("utf-8")
        ).hexdigest()[:20]
        safe = _safe_filename(f"{artist} - {title}" if artist else title)
        return self.cache_dir / f"{safe}.{key}.{ext}"

    def _lookup_cache(self, title: str, artist: str, quality: str) -> Path | None:
        prefix_title = _safe_filename(f"{artist} - {title}" if artist else title)
        key = hashlib.sha1(
            f"{title}|{artist}|{quality}".encode("utf-8")
        ).hexdigest()[:20]
        for ext in ("mp3", "flac", "m4a"):
            candidate = self.cache_dir / f"{prefix_title}.{key}.{ext}"
            try:
                if candidate.is_file() and candidate.stat().st_size > 64 * 1024:
                    return candidate
            except OSError:
                continue
        return None

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._fetch_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._fetch_locks[key] = lock
        return lock

    async def fetch(
        self,
        title: str,
        artist: str = "",
        quality: str | None = None,
    ) -> dict | None:
        """搜索 → 解析 → 下载一首歌。

        返回 ``{"path": Path, "title": str, "singer": str, "source": str,
        "duration": float, "cached": bool}``，失败返回 ``None``。
        并发点同一首歌时只下载一次（其余等待并复用结果）。
        """
        if not self.enabled:
            return None
        title = str(title or "").strip()
        if not title:
            return None
        artist = str(artist or "").strip()
        quality = quality if quality in _QUALITIES else self.quality

        self.stats["requests"] += 1
        lock = self._lock_for(f"{title}|{artist}|{quality}".casefold())
        async with lock:
            cached = self._lookup_cache(title, artist, quality)
            if cached is not None:
                self.stats["hits"] += 1
                return {
                    "path": cached,
                    "title": title,
                    "singer": artist,
                    "source": "cache",
                    "duration": 0.0,
                    "cached": True,
                }

            started = time.perf_counter()
            try:
                return await self._fetch_online(title, artist, quality, started)
            except Exception as exc:
                self.stats["failures"] += 1
                logger.error(f"[音乐] 下载《{title}》异常: {exc}")
                return None

    async def _fetch_online(
        self,
        title: str,
        artist: str,
        quality: str,
        started: float,
    ) -> dict | None:
        # 1. 搜索（优先带歌手提高命中率）
        keyword = f"{artist} {title}".strip() if artist else title
        candidates = await self.search(keyword)
        if not candidates and artist:
            candidates = await self.search(title)
        song = self.pick_best(candidates, title, artist)
        if not song:
            logger.warning(f"[音乐] 未找到《{title}》")
            self.stats["failures"] += 1
            return None

        real_title = str(song.get("name") or title)
        real_singer = str(song.get("singer") or artist)
        duration = _parse_interval(song.get("interval", ""))

        # 2. 解析下载地址
        url = await self.resolve(song, quality)
        if not url:
            logger.warning(f"[音乐] 《{real_title}》解析下载地址失败")
            self.stats["failures"] += 1
            return None

        # 3. 下载（走服务端代理可顺带嵌入 ID3 标签）
        ext = "flac" if "flac" in quality else "mp3"
        target = self._cache_path(real_title, real_singer, quality, ext)
        tmp = target.with_suffix(target.suffix + ".part")
        ok = await self._download(song, url, tmp)
        if not ok:
            self.stats["failures"] += 1
            tmp.unlink(missing_ok=True)
            return None

        try:
            target.unlink(missing_ok=True)
            tmp.replace(target)
        except OSError as exc:
            logger.error(f"[音乐] 歌曲落盘失败: {exc}")
            tmp.unlink(missing_ok=True)
            return None

        size = target.stat().st_size
        elapsed = time.perf_counter() - started
        self.stats["downloads"] += 1
        self.stats["bytes"] += size
        self.stats["last_seconds"] = round(elapsed, 2)
        logger.info(
            f"[音乐] 《{real_singer} - {real_title}》下载完成 "
            f"{size / 1024 / 1024:.1f}MB / {elapsed:.1f}s"
        )
        return {
            "path": target,
            "title": real_title,
            "singer": real_singer,
            "source": str(song.get("source") or ""),
            "duration": duration,
            "cached": False,
        }

    async def _download(self, song: dict, url: str, target: Path) -> bool:
        """优先走服务端代理下载（嵌入标签），失败则直连下载。"""
        filename = _safe_filename(
            f"{song.get('singer', '')} - {song.get('name', '')}"
        )
        proxy_path = (
            "/api/music/download"
            f"?url={quote(url, safe='')}"
            f"&filename={quote(filename + target.suffix, safe='')}"
            f"&taskId=bot-{int(time.time() * 1000)}"
            f"&tag=1"
            f"&name={quote(str(song.get('name') or ''), safe='')}"
            f"&singer={quote(str(song.get('singer') or ''), safe='')}"
        )
        for candidate in (proxy_path, url):
            try:
                if await self._stream_to_file(candidate, target):
                    return True
            except Exception as exc:
                logger.warning(f"[音乐] 下载通道失败 {str(candidate)[:60]}: {exc}")
        return False

    async def _stream_to_file(self, url_or_path: str, target: Path) -> bool:
        is_proxy = str(url_or_path).startswith("/")
        client = self._authed_client()
        request = client.build_request(
            "GET",
            url_or_path if is_proxy else str(url_or_path),
        )
        resp = await client.send(request, stream=True)
        if resp.status_code in (401, 403) and is_proxy:
            await resp.aclose()
            if not await self._ensure_session(force=True):
                return False
            client = self._authed_client()
            request = client.build_request("GET", url_or_path)
            resp = await client.send(request, stream=True)

        if resp.status_code != 200:
            logger.warning(f"[音乐] 下载失败 HTTP {resp.status_code}")
            await resp.aclose()
            return False

        length = resp.headers.get("Content-Length")
        if length and int(length) > self.max_size_bytes:
            logger.warning(
                f"[音乐] 文件过大（{int(length) / 1024 / 1024:.0f}MB），放弃下载"
            )
            await resp.aclose()
            return False

        written = 0
        try:
            with open(target, "wb") as handle:
                async for chunk in resp.aiter_bytes(chunk_size=256 * 1024):
                    written += len(chunk)
                    if written > self.max_size_bytes:
                        raise ValueError("文件超出大小上限")
                    handle.write(chunk)
        finally:
            await resp.aclose()
        return written > 64 * 1024

    # ------------------------------------------------------------ 维护

    def status(self) -> dict:
        count = 0
        size = 0
        try:
            for item in self.cache_dir.iterdir():
                if item.is_file() and item.suffix in (".mp3", ".flac", ".m4a"):
                    count += 1
                    size += item.stat().st_size
        except Exception:
            pass
        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "quality": self.quality,
            "logged_in": bool(self._session_cookie)
            and time.time() < self._session_expire_at,
            "cached_count": count,
            "cache_size_mb": round(size / 1024 / 1024, 1),
            "requests": int(self.stats["requests"]),
            "hits": int(self.stats["hits"]),
            "downloads": int(self.stats["downloads"]),
            "failures": int(self.stats["failures"]),
            "downloaded_mb": round(int(self.stats["bytes"]) / 1024 / 1024, 1),
            "last_seconds": float(self.stats["last_seconds"]),
        }

    async def test_connection(self, keyword: str = "青花瓷") -> dict:
        """连通性自检：认证 → 搜索 → 解析（不下载文件）。"""
        if not self.enabled:
            return {"success": False, "message": "音乐源未启用"}
        if not await self._ensure_session():
            return {"success": False, "message": "认证失败：请检查访问密码与网络"}
        started = time.perf_counter()
        candidates = await self.search(keyword)
        if not candidates:
            return {
                "success": False,
                "message": f"搜索《{keyword}》没有结果",
                "seconds": round(time.perf_counter() - started, 2),
            }
        song = candidates[0]
        url = await self.resolve(song, self.quality)
        seconds = round(time.perf_counter() - started, 2)
        if not url:
            return {
                "success": False,
                "message": f"找到《{song.get('name')}》但解析下载地址失败",
                "seconds": seconds,
            }
        return {
            "success": True,
            "message": (
                f"搜索/解析正常：《{song.get('singer')} - {song.get('name')}》"
                f"（{song.get('source')} · {song.get('interval')}）"
            ),
            "seconds": seconds,
            "song": f"{song.get('singer')} - {song.get('name')}",
            "source": song.get("source"),
            "url_prefix": url[:60],
        }
