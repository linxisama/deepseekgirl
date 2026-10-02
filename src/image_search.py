"""联网图片搜索与本地缓存。"""

from __future__ import annotations

import asyncio
import hashlib
import html
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from loguru import logger

from .error_logger import log_error_message, log_exception


@dataclass(frozen=True, slots=True)
class ImageCandidate:
    """A ranked image result with enough source data for safe fallback."""

    urls: tuple[str, ...]
    score: int
    source_index: int


class ImageSearchService:
    """Search an image request, download one candidate, and cache it locally."""

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0 Safari/537.36"
    )
    MAX_IMAGE_BYTES = 10 * 1024 * 1024
    CATALOG_TTL_SECONDS = 1800.0
    MAX_SEARCH_SECONDS = 3.0
    RANDOM_TOP_N = 5
    IMAGE_CONTENT_TYPES = {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "image/bmp",
    }

    def __init__(
        self,
        cache_dir: str = "./data/images",
        timeout: float = 3.0,
    ):
        self.cache_dir = Path(cache_dir)
        self.timeout = max(
            0.5,
            min(float(timeout), self.MAX_SEARCH_SECONDS),
        )
        self._catalogs: dict[
            str,
            tuple[float, list[ImageCandidate]],
        ] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def find_image(self, query: str) -> Path | None:
        """Return a downloaded image path for a natural-language query."""
        cleaned = self._clean_query(query)
        if not cleaned:
            return None

        key = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:20]
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.timeout):
                async with self._locks[key]:
                    candidates = await self._catalog(cleaned, key)
                    if not candidates:
                        return None

                    # The source does not expose a consistent "favorite" field.
                    # Prefer the strongest available popularity signals, then
                    # randomize within the five best-ranked results.
                    top_candidates = candidates[: self.RANDOM_TOP_N]
                    random.SystemRandom().shuffle(top_candidates)
                    for candidate in top_candidates:
                        for url_index, url in enumerate(candidate.urls):
                            path = await self._download(
                                url,
                                (
                                    f"{key}-{candidate.source_index}"
                                    f"-{url_index}"
                                ),
                            )
                            if path:
                                logger.info(
                                    "图片随机选取完成: "
                                    f"{cleaned}，候选热度前 "
                                    f"{len(top_candidates)}，耗时 "
                                    f"{time.monotonic() - started:.2f} 秒"
                                )
                                return path
        except TimeoutError:
            logger.warning(
                f"图片搜索超过 {self.timeout:.1f} 秒，已中止: {cleaned}"
            )
            log_error_message(
                context="image_search.timeout",
                error_type="ImageSearchTimeout",
                error_message=(
                    f"图片搜索超过 {self.timeout:.1f} 秒: {cleaned}"
                ),
            )
            return None
        except Exception as exc:
            log_exception("image_search.find_image", exc=exc)
            logger.warning(f"图片搜索处理失败: {cleaned} -> {exc}")
            return None
        return None

    async def _catalog(
        self,
        query: str,
        key: str,
    ) -> list[ImageCandidate]:
        cached = self._catalogs.get(key)
        if cached and time.monotonic() - cached[0] <= self.CATALOG_TTL_SECONDS:
            return cached[1]

        try:
            candidates = await self._search_360(query)
            if not candidates:
                candidates = await self._search_bing(query)
        except Exception as exc:
            log_exception("image_search.search", exc=exc)
            logger.warning(f"联网图片搜索失败: {exc}")
            return []

        if candidates:
            self._catalogs[key] = (time.monotonic(), candidates)
            logger.info(
                f"图片搜索完成: {query}，候选 {len(candidates)} 张"
            )
        else:
            log_error_message(
                context="image_search.search",
                error_type="NoImageResult",
                error_message=f"没有找到图片: {query}",
            )
        return candidates

    async def _search_360(self, query: str) -> list[ImageCandidate]:
        """360 图片搜索对中文关键词的匹配通常比 Bing 中站更稳定。"""
        params = {
            "q": query,
            "src": "srp",
            "correct": query,
            "sn": "0",
            "pn": "30",
        }
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            verify=False,
            headers={
                "User-Agent": self.USER_AGENT,
                "Referer": "https://image.so.com/",
            },
        ) as client:
            response = await client.get(
                "https://image.so.com/j",
                params=params,
            )
            response.raise_for_status()
            data = response.json()

        candidates: list[ImageCandidate] = []
        seen: set[str] = set()
        for index, item in enumerate(data.get("list", [])):
            if not isinstance(item, dict):
                continue
            urls: list[str] = []
            for key in ("img", "thumb_bak", "thumb"):
                url = str(item.get(key) or "").strip()
                if (
                    self._looks_like_image_url(url)
                    and url not in urls
                    and url not in seen
                ):
                    urls.append(url)
            if not urls:
                continue
            seen.update(urls)
            fnum = self._as_int(item.get("fnum"))
            grpcnt = self._as_int(item.get("grpcnt"))
            # fnum/grpcnt are the available popularity hints. When both are
            # absent, the original search order remains the tie-breaker.
            score = fnum * 1000 + grpcnt * 100
            candidates.append(
                ImageCandidate(
                    urls=tuple(urls),
                    score=score,
                    source_index=index,
                )
            )

        candidates.sort(
            key=lambda candidate: (
                candidate.score,
                -candidate.source_index,
            ),
            reverse=True,
        )
        return candidates[:20]

    async def _search_bing(self, query: str) -> list[ImageCandidate]:
        params = {
            "q": f"{query} 图片",
            "form": "HDRSC2",
            "first": "1",
            "count": "30",
            "adlt": "moderate",
        }
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            verify=False,
            headers={"User-Agent": self.USER_AGENT},
        ) as client:
            response = await client.get(
                "https://www.bing.com/images/search",
                params=params,
            )
            response.raise_for_status()
            text = response.text

        candidates: list[str] = []
        patterns = (
            r'murl&quot;:&quot;(.*?)&quot;',
            r'"murl"\s*:\s*"(.*?)"',
            r"murl%3D(.*?)%26",
        )
        for pattern in patterns:
            for raw in re.findall(pattern, text, flags=re.IGNORECASE):
                url = html.unescape(raw).replace("\\/", "/").strip()
                if self._looks_like_image_url(url):
                    candidates.append(url)

        return [
            ImageCandidate(
                urls=(url,),
                score=0,
                source_index=index,
            )
            for index, url in enumerate(self._dedupe(candidates)[:20])
        ]

    @staticmethod
    def _as_int(value) -> int:
        try:
            return max(0, int(float(value)))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _dedupe(urls: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            unique.append(url)
        return unique

    async def _download(self, url: str, name: str) -> Path | None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                verify=False,
                headers={
                    "User-Agent": self.USER_AGENT,
                    "Referer": "https://www.bing.com/",
                },
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if (
                        content_type
                        and content_type not in self.IMAGE_CONTENT_TYPES
                    ):
                        return None

                    suffix = self._suffix_for(content_type, url)
                    destination = self.cache_dir / f"{name}{suffix}"
                    total = 0
                    with destination.open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > self.MAX_IMAGE_BYTES:
                                raise ValueError("图片超过 10MB，已放弃")
                            handle.write(chunk)

            if total < 1024:
                destination.unlink(missing_ok=True)
                return None
            logger.info(f"图片已下载: {destination}（{total} 字节）")
            return destination
        except Exception as exc:
            logger.debug(f"图片下载失败: {url} -> {exc}")
            return None

    @classmethod
    def _looks_like_image_url(cls, url: str) -> bool:
        if not url.startswith(("http://", "https://")):
            return False
        parsed = urlparse(url)
        if not parsed.netloc:
            return False
        path = parsed.path.lower()
        if path.endswith(
            (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
        ):
            return True
        return any(
            token in url.lower()
            for token in ("image", "photo", "img", "pic")
        )

    @staticmethod
    def _suffix_for(content_type: str, url: str) -> str:
        mapping = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "image/bmp": ".bmp",
        }
        if content_type in mapping:
            return mapping[content_type]
        path = urlparse(url).path.lower()
        for suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
            if path.endswith(suffix):
                return ".jpg" if suffix == ".jpeg" else suffix
        return ".jpg"

    @staticmethod
    def _clean_query(query: str) -> str:
        cleaned = re.sub(r"\s+", " ", str(query or "")).strip()
        cleaned = re.sub(
            r"^(?:帮我|请|麻烦|给我|发|找|搜索|搜一下)+",
            "",
            cleaned,
        ).strip(" ：:，,。.!！？?")
        return cleaned[:80]
