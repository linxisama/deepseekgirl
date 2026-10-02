"""QQ 机器人桥接：对接 QQ 机器人开放平台官方 API。

与 WeChatBridge 保持同一套接口（connect / start_listening / send_text /
send_image / set_target_groups / status_info），因此可以直接挂到同一套
MessageHandler 管线上，而不需要改动消息处理逻辑。

设计要点
--------
1. **纯接口接入**：全部通过 HTTP OpenAPI + WebSocket 网关通信，
   不做任何窗口 UI 自动化，因此与微信端的 wxauto 完全隔离，
   不存在抢窗口焦点、抢鼠标键盘的冲突。
2. **两种后端**：
   - ``official``：真实 QQ 官方机器人（AppID + AppSecret）
   - ``simulate``：模拟后端，无需任何密钥即可跑通完整链路（用于测试）
3. **被动回复优先**：发送时带上收到的 msg_id，走被动回复通道，
   既稳定又不消耗主动推送配额。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
from loguru import logger

from .wechat_bridge import ConnectionStatus, WeChatMessage

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

# 正式 / 沙箱环境 API 基地址
API_BASE_PROD = "https://api.sgroup.qq.com"
API_BASE_SANDBOX = "https://sandbox.api.sgroup.qq.com"

# WebSocket 操作码
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# 事件订阅位（intents）
INTENT_GUILDS = 1 << 0
INTENT_GUILD_MESSAGES = 1 << 9
INTENT_DIRECT_MESSAGE = 1 << 12
INTENT_GROUP_AND_C2C = 1 << 25
INTENT_INTERACTION = 1 << 26
INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30

# 群聊 / 单聊事件（需要 GROUP_AND_C2C 位）
DEFAULT_INTENTS = (
    INTENT_GROUP_AND_C2C
    | INTENT_PUBLIC_GUILD_MESSAGES
    | INTENT_DIRECT_MESSAGE
    | INTENT_GUILDS
)

# 消息类别
KIND_GROUP = "group"
KIND_C2C = "c2c"
KIND_CHANNEL = "channel"
KIND_DM = "dm"

# 富媒体文件类型（官方 /v2/.../files 接口）
FILE_TYPE_IMAGE = 1
FILE_TYPE_VIDEO = 2
FILE_TYPE_VOICE = 3
FILE_TYPE_FILE = 4

#: 语音消息允许的最长时长（秒），超出后退化为"文件"形式发送
VOICE_MAX_SECONDS = 60.0
VOICE_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024

# 群消息事件（必须 @ 机器人）
EVENT_GROUP_AT = "GROUP_AT_MESSAGE_CREATE"
EVENT_C2C = "C2C_MESSAGE_CREATE"
EVENT_GUILD_AT = "AT_MESSAGE_CREATE"
EVENT_GUILD_MSG = "MESSAGE_CREATE"
EVENT_DIRECT_MSG = "DIRECT_MESSAGE_CREATE"

MESSAGE_EVENTS = {
    EVENT_GROUP_AT,
    EVENT_C2C,
    EVENT_GUILD_AT,
    EVENT_GUILD_MSG,
    EVENT_DIRECT_MSG,
}

# 仅日志记录的事件
NOTICE_EVENTS = {
    "GROUP_ADD_ROBOT",
    "GROUP_DEL_ROBOT",
    "GROUP_MSG_REJECT",
    "GROUP_MSG_RECEIVE",
    "FRIEND_ADD",
    "FRIEND_DEL",
    "C2C_MSG_REJECT",
    "C2C_MSG_RECEIVE",
}

# 清洗 content 中的提及 / 表情 / 频道链接标记
_MENTION_RE = re.compile(r"<@!?\d+>|<@[^>]{0,64}>")
_EMOJI_RE = re.compile(r"<emoji:\d+>")
_CHANNEL_LINK_RE = re.compile(r"<#\d+>")
# 官方文本里用于占位换行的标记
_BR_RE = re.compile(r"\\n|\r\n")

SIMULATE_GROUPS = [
    ("群聊A", "张三", ["你好呀", "在吗", "帮我看看这个东西", "今天天气不错"]),
    ("群聊B", "李四", ["机器人测试一下", "1+1等于几", "讲个笑话", "晚上吃什么"]),
]


@dataclass
class _RoomTarget:
    """房间寻址信息：把展示名映射回官方 API 需要的 openid。"""

    kind: str
    openid: str
    display: str
    last_msg_id: str = ""
    msg_seq: int = 0
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class QQBridge:
    """QQ 官方机器人桥接器（接口与 WeChatBridge 对齐）。"""

    def __init__(
        self,
        app_id: str = "",
        client_secret: str = "",
        sandbox: bool = False,
        target_groups: list[str] | None = None,
        on_message: Callable[[WeChatMessage], Awaitable[None]] | None = None,
        reply_delay: float = 0.0,
        backend: str = "official",
        intents: int = DEFAULT_INTENTS,
        aliases: dict[str, str] | None = None,
        listen_private: bool = True,
        send_interval: float = 1.2,
        group_aliases: dict[str, str] | None = None,
    ):
        self.app_id = str(app_id or "").strip()
        self.client_secret = str(client_secret or "").strip()
        self.sandbox = bool(sandbox)
        self.backend = (backend or "official").strip().lower()
        self.intents = int(intents or DEFAULT_INTENTS)
        self.on_message = on_message
        self.reply_delay = max(0.0, float(reply_delay or 0.0))
        self.listen_private = bool(listen_private)
        self.send_interval = max(0.0, float(send_interval or 0.0))

        # target_groups 既支持 ["openid"] 也支持 ["openid:别名"]
        self.target_groups: list[str] = list(target_groups or [])
        self._aliases: dict[str, str] = dict(group_aliases or aliases or {})

        self._status = ConnectionStatus.DISCONNECTED
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._http: httpx.AsyncClient | None = None
        self._ws: Any = None
        self._ws_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._sim_task: asyncio.Task | None = None

        self._token = ""
        self._token_expire_at = 0.0
        self._session_id = ""
        self._last_seq: int | None = None
        self._heartbeat_interval = 45.0
        self._heartbeat_acked = True
        self._gateway_url = ""

        # 展示名 -> 寻址信息
        self._rooms: dict[str, _RoomTarget] = {}
        self._bot_id = ""
        self._send_counter = 0
        self.last_error = ""

    # ------------------------------------------------------------------
    # 基础属性
    # ------------------------------------------------------------------
    @property
    def status(self) -> ConnectionStatus:
        return self._status

    @property
    def is_connected(self) -> bool:
        return self._status == ConnectionStatus.CONNECTED

    @property
    def api_base(self) -> str:
        return API_BASE_SANDBOX if self.sandbox else API_BASE_PROD

    @property
    def status_info(self) -> dict:
        return {
            "status": self._status.value,
            "mode": self.backend,
            "sandbox": self.sandbox,
            "app_id": self.app_id,
            "configured": bool(self.app_id and self.client_secret),
            "target_groups": list(self.target_groups),
            "rooms": len(self._rooms),
            "bot_id": self._bot_id,
            "last_error": self.last_error,
            "unavailable": self._unavailable_reason(),
        }

    def _unavailable_reason(self) -> str:
        if self.backend == "simulate":
            return ""
        if not self.app_id or not self.client_secret:
            return "尚未填写 AppID / AppSecret"
        return ""

    def _room_key(self, kind: str, openid: str) -> str:
        return f"{kind}:{openid}"

    def _display_name(self, kind: str, openid: str) -> str:
        alias = self._aliases.get(openid) or self._aliases.get(self._room_key(kind, openid))
        if alias:
            return str(alias).strip()
        suffix = openid[-8:] if len(openid) > 8 else openid
        prefix = {
            KIND_GROUP: "QQ群",
            KIND_C2C: "QQ好友",
            KIND_CHANNEL: "QQ频道",
            KIND_DM: "QQ私信",
        }.get(kind, "QQ会话")
        return f"{prefix}·{suffix}"

    def _remember_room(
        self,
        kind: str,
        openid: str,
        last_msg_id: str = "",
    ) -> _RoomTarget:
        """登记（或更新）房间寻址信息，返回可复用的对象。"""
        display = self._display_name(kind, openid)
        target = self._rooms.get(display)
        if target is None:
            target = _RoomTarget(kind=kind, openid=openid, display=display)
            self._rooms[display] = target
            logger.info(f"[QQ] 已登记会话 {display} ({kind})")
        if last_msg_id:
            target.last_msg_id = last_msg_id
        # 同时用 room_key 作为备用键，便于外部按 openid 直接寻址
        self._rooms.setdefault(self._room_key(kind, openid), target)
        return target

    def _resolve_room(self, room_name: str) -> _RoomTarget | None:
        target = self._rooms.get(room_name)
        if target is not None:
            return target
        text = str(room_name or "").strip()
        if ":" in text:
            kind, _, openid = text.partition(":")
            if kind in {KIND_GROUP, KIND_C2C, KIND_CHANNEL, KIND_DM} and openid:
                return self._remember_room(kind, openid)
        return None

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    async def connect(self) -> bool:
        if self.backend == "simulate":
            return await self._connect_simulate()

        if not self.app_id or not self.client_secret:
            self.last_error = "未配置 AppID / AppSecret"
            self._status = ConnectionStatus.ERROR
            logger.warning(f"[QQ] {self.last_error}，跳过连接")
            return False

        self._status = ConnectionStatus.CONNECTING
        try:
            if self._http is None:
                self._http = httpx.AsyncClient(timeout=15.0)

            await self._ensure_token(force=True)
            self._gateway_url = await self._fetch_gateway()
            if not self._gateway_url:
                raise RuntimeError("未能获取 WebSocket 网关地址")

            await self._open_ws()
            self._register_configured_groups()
            self._status = ConnectionStatus.CONNECTED
            self.last_error = ""
            logger.info(
                f"[QQ] 官方机器人已连接（{'沙箱' if self.sandbox else '正式'}环境）"
            )
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._status = ConnectionStatus.ERROR
            logger.error(f"[QQ] 连接失败: {self.last_error}")
            await self._close_ws()
            return False

    async def _connect_simulate(self) -> bool:
        self._status = ConnectionStatus.CONNECTING
        await asyncio.sleep(0.2)
        for name, sender, _messages in SIMULATE_GROUPS:
            self._remember_room(KIND_GROUP, f"sim-{name}", f"simmsg-{name}")
        self._status = ConnectionStatus.CONNECTED
        logger.info("[QQ] 模拟后端已连接（无需真实 QQ 机器人）")
        return True

    def _register_configured_groups(self) -> None:
        """把配置里的目标群提前登记，保证 Web 端能先看到列表。"""
        for entry in self.target_groups:
            text = str(entry or "").strip()
            if not text:
                continue
            kind, openid = KIND_GROUP, text
            if ":" in text:
                head, _, tail = text.partition(":")
                if head in {KIND_GROUP, KIND_C2C, KIND_CHANNEL, KIND_DM} and tail:
                    kind, openid = head, tail
            self._remember_room(kind, openid)

    async def _ensure_token(self, force: bool = False) -> str:
        """获取 / 刷新 AccessToken（提前 5 分钟刷新）。"""
        now = time.time()
        if not force and self._token and now < self._token_expire_at - 300:
            return self._token

        if self._http is None:
            self._http = httpx.AsyncClient(timeout=15.0)

        resp = await self._http.post(
            TOKEN_URL,
            json={"appId": self.app_id, "clientSecret": self.client_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        token = str(data.get("access_token") or "").strip()
        if not token:
            raise RuntimeError(f"获取 AccessToken 失败: {data}")
        try:
            expires_in = float(data.get("expires_in") or 7200)
        except (TypeError, ValueError):
            expires_in = 7200.0
        self._token = token
        self._token_expire_at = now + expires_in
        logger.debug(f"[QQ] AccessToken 已刷新，有效期 {expires_in:.0f} 秒")
        return token

    async def _api_headers(self) -> dict[str, str]:
        token = await self._ensure_token()
        return {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
            "X-Union-Appid": self.app_id,
        }

    async def _fetch_gateway(self) -> str:
        headers = await self._api_headers()
        resp = await self._http.get(f"{self.api_base}/gateway", headers=headers)
        resp.raise_for_status()
        return str(resp.json().get("url") or "")

    async def _open_ws(self) -> None:
        import websockets

        self._ws = await websockets.connect(
            self._gateway_url,
            max_size=4 * 1024 * 1024,
            ping_interval=None,
        )
        hello_raw = await asyncio.wait_for(self._ws.recv(), timeout=15)
        hello = json.loads(hello_raw)
        if hello.get("op") != OP_HELLO:
            raise RuntimeError(f"网关握手异常: {hello}")
        interval_ms = (hello.get("d") or {}).get("heartbeat_interval", 45000)
        try:
            self._heartbeat_interval = max(5.0, float(interval_ms) / 1000.0)
        except (TypeError, ValueError):
            self._heartbeat_interval = 45.0

        await self._send_ws(
            {
                "op": OP_IDENTIFY,
                "d": {
                    "token": f"QQBot {self._token}",
                    "intents": self.intents,
                    "shard": [0, 1],
                },
            }
        )
        self._heartbeat_acked = True
        self._start_heartbeat()

    async def _send_ws(self, payload: dict) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    def _start_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        loop = asyncio.get_event_loop()
        self._heartbeat_task = loop.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        try:
            while self._running or self._status == ConnectionStatus.CONNECTED:
                await asyncio.sleep(self._heartbeat_interval)
                if self._ws is None:
                    return
                if not self._heartbeat_acked:
                    logger.warning("[QQ] 心跳未收到 ACK，准备重连")
                    await self._trigger_reconnect("心跳超时")
                    return
                self._heartbeat_acked = False
                try:
                    await self._send_ws({"op": OP_HEARTBEAT, "d": self._last_seq})
                except Exception as exc:
                    logger.warning(f"[QQ] 发送心跳失败: {exc}")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[QQ] 心跳循环异常: {exc}")

    async def _close_ws(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
        self._heartbeat_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None

    def start_listening(self, loop: asyncio.AbstractEventLoop) -> None:
        """启动事件接收循环（模拟后端改为定时生成假消息）。"""
        if self._status != ConnectionStatus.CONNECTED:
            return
        self._running = True
        self._loop = loop

        if self.backend == "simulate":
            if self._sim_task and not self._sim_task.done():
                return
            self._sim_task = loop.create_task(self._simulate_loop())
            logger.info(
                f"[QQ] 模拟消息监听已启动，模拟会话: {[g[0] for g in SIMULATE_GROUPS]}"
            )
            return

        if self._ws_task and not self._ws_task.done():
            return
        self._ws_task = loop.create_task(self._gateway_loop())
        logger.info("[QQ] WebSocket 事件监听已启动")

    async def _gateway_loop(self) -> None:
        """接收网关事件；异常时自动重连。"""
        while self._running:
            if self._ws is None:
                if not await self._reconnect():
                    await asyncio.sleep(5)
                    continue
            try:
                raw = await self._ws.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[QQ] WebSocket 断开: {exc}")
                self._status = ConnectionStatus.CONNECTING
                await self._close_ws()
                await asyncio.sleep(3)
                continue

            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if await self._handle_payload(payload):
                return

    async def _handle_payload(self, payload: dict) -> bool:
        """处理一条网关消息；返回 True 表示需要退出接收循环。"""
        op = payload.get("op")
        if op == OP_DISPATCH:
            seq = payload.get("s")
            if isinstance(seq, int):
                self._last_seq = seq
            event = str(payload.get("t") or "")
            data = payload.get("d") or {}
            if event == "READY":
                self._session_id = str(data.get("session_id") or "")
                user = data.get("user") or {}
                self._bot_id = str(user.get("id") or "")
                logger.info(f"[QQ] 网关就绪，机器人 ID: {self._bot_id or '未知'}")
            else:
                await self._dispatch_event(event, data)
            return False

        if op == OP_HEARTBEAT_ACK:
            self._heartbeat_acked = True
            return False

        if op == OP_RECONNECT:
            logger.warning("[QQ] 服务端要求重连")
            await self._trigger_reconnect("服务端要求重连")
            return False

        if op == OP_INVALID_SESSION:
            logger.warning("[QQ] 会话失效，重新鉴权")
            self._session_id = ""
            self._last_seq = None
            await self._trigger_reconnect("会话失效")
            return False

        if op == OP_HELLO:
            return False

        logger.debug(f"[QQ] 未处理的网关消息: {op}")
        return False

    async def _trigger_reconnect(self, reason: str) -> None:
        logger.warning(f"[QQ] 准备重连：{reason}")
        self._status = ConnectionStatus.CONNECTING
        await self._close_ws()

    async def _reconnect(self) -> bool:
        try:
            await self._ensure_token()
            if not self._gateway_url:
                self._gateway_url = await self._fetch_gateway()
            await self._open_ws()
            self._status = ConnectionStatus.CONNECTED
            self.last_error = ""
            logger.info("[QQ] 连接已恢复")
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._status = ConnectionStatus.ERROR
            logger.error(f"[QQ] 重连失败: {self.last_error}")
            return False

    async def recover_connection(self) -> bool:
        """供主控制器看门狗调用。"""
        if self.backend == "simulate":
            return True
        await self._close_ws()
        return await self._reconnect()

    async def disconnect(self) -> None:
        self._running = False
        for task_attr in ("_ws_task", "_sim_task"):
            task = getattr(self, task_attr)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            setattr(self, task_attr, None)
        await self._close_ws()
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
        self._status = ConnectionStatus.DISCONNECTED
        logger.info("[QQ] 已断开连接")

    # 供主控制器看门狗接口对齐（QQ 不使用 UI 自动化）
    def window_process_changed(self) -> bool:
        return False

    def is_sender_stuck(self, timeout: float = 20.0) -> bool:
        del timeout
        return False

    # ------------------------------------------------------------------
    # 事件解析
    # ------------------------------------------------------------------
    async def _dispatch_event(self, event: str, data: dict) -> None:
        if event in NOTICE_EVENTS:
            logger.info(f"[QQ] 收到通知事件 {event}")
            return
        if event not in MESSAGE_EVENTS:
            logger.debug(f"[QQ] 忽略事件 {event}")
            return

        message = self._build_message(event, data)
        if message is None:
            return

        logger.info(
            f"[QQ] [{message.room_name}] {message.sender_name}: "
            f"{message.content[:60]}"
        )
        if self.on_message is None:
            return
        try:
            await self.on_message(message)
        except Exception as exc:
            logger.error(f"[QQ] 消息回调处理失败: {exc}")

    def _build_message(self, event: str, data: dict) -> WeChatMessage | None:
        author = data.get("author") or {}
        content = self._clean_content(str(data.get("content") or ""))
        msg_id = str(data.get("id") or data.get("message_id") or "")
        timestamp = self._parse_timestamp(data)
        voice_url, voice_mime, voice_filename = self._extract_voice_attachment(
            data
        )
        message_type = "voice" if voice_url else "text"
        display_content = content or ("[语音]" if voice_url else "[非文本消息]")

        if event == EVENT_GROUP_AT:
            openid = str(data.get("group_openid") or "")
            if not openid:
                return None
            sender = str(
                author.get("username")
                or author.get("member_openid")
                or author.get("id")
                or "群成员"
            )
            room = self._remember_room(KIND_GROUP, openid, msg_id)
            return WeChatMessage(
                id=msg_id or uuid.uuid4().hex,
                sender=sender,
                sender_name=sender,
                room_id=self._room_key(KIND_GROUP, openid),
                room_name=room.display,
                content=display_content,
                type=2 if voice_url else 1,
                timestamp=timestamp,
                is_group=True,
                # 官方群里只有 @ 机器人才能收到消息
                is_at_me=True,
                message_type=message_type,
                is_private=False,
                is_target_group=self._is_target(KIND_GROUP, openid),
                is_self=self._is_self_author(author),
                voice_url=voice_url,
                voice_mime=voice_mime,
                voice_filename=voice_filename,
            )

        if event == EVENT_C2C:
            openid = str(author.get("user_openid") or author.get("id") or "")
            if not openid:
                return None
            sender = str(author.get("username") or openid)
            room = self._remember_room(KIND_C2C, openid, msg_id)
            return WeChatMessage(
                id=msg_id or uuid.uuid4().hex,
                sender=sender,
                sender_name=sender,
                room_id=self._room_key(KIND_C2C, openid),
                room_name=room.display,
                content=display_content,
                type=2 if voice_url else 1,
                timestamp=timestamp,
                is_group=False,
                is_at_me=True,
                message_type=message_type,
                is_private=True,
                is_target_group=self.listen_private,
                is_self=self._is_self_author(author),
                voice_url=voice_url,
                voice_mime=voice_mime,
                voice_filename=voice_filename,
            )

        # 频道消息 / 频道私信
        openid = str(data.get("channel_id") or "")
        if not openid:
            return None
        kind = KIND_DM if event == EVENT_DIRECT_MSG else KIND_CHANNEL
        sender = str(author.get("username") or author.get("id") or "频道用户")
        room = self._remember_room(kind, openid, msg_id)
        return WeChatMessage(
            id=msg_id or uuid.uuid4().hex,
            sender=sender,
            sender_name=sender,
            room_id=self._room_key(kind, openid),
            room_name=room.display,
            content=display_content,
            type=2 if voice_url else 1,
            timestamp=timestamp,
            is_group=(kind == KIND_CHANNEL),
            is_at_me=(event == EVENT_GUILD_AT),
            message_type=message_type,
            is_private=(kind == KIND_DM),
            is_target_group=True,
            is_self=self._is_self_author(author),
            voice_url=voice_url,
            voice_mime=voice_mime,
            voice_filename=voice_filename,
        )

    @staticmethod
    def _extract_voice_attachment(data: dict) -> tuple[str, str, str]:
        """Return (url, mime, filename) for the first voice attachment."""
        attachments = data.get("attachments") or []
        if not isinstance(attachments, list):
            return "", "", ""
        audio_suffixes = {
            ".silk",
            ".slk",
            ".amr",
            ".mp3",
            ".m4a",
            ".aac",
            ".wav",
            ".ogg",
            ".opus",
        }
        for item in attachments:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            mime = str(item.get("content_type") or "").strip().lower()
            filename = str(item.get("filename") or "").strip()
            suffix = Path(filename).suffix.casefold()
            if (
                not url
                or (
                    not mime.startswith("audio/")
                    and "voice" not in mime
                    and suffix not in audio_suffixes
                )
            ):
                continue
            return url, mime, filename
        return "", "", ""

    async def download_voice(
        self,
        url: str,
        filename: str = "",
        mime: str = "",
    ) -> str:
        """Download a QQ voice attachment into the local voice inbox."""
        parsed = urlparse(str(url or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("QQ 语音附件地址无效")
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=15.0, follow_redirects=True)

        mime = str(mime or "").casefold()
        suffix = Path(str(filename or "")).suffix.casefold()
        if suffix not in {".silk", ".slk", ".amr", ".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus"}:
            suffix = {
                "audio/silk": ".silk",
                "audio/amr": ".amr",
                "audio/mpeg": ".mp3",
                "audio/mp4": ".m4a",
                "audio/wav": ".wav",
                "audio/x-wav": ".wav",
                "audio/ogg": ".ogg",
                "audio/opus": ".opus",
            }.get(mime, ".silk")

        root = Path(__file__).resolve().parents[1]
        save_dir = root / "data" / "voice_inbox"
        save_dir.mkdir(parents=True, exist_ok=True)
        target = save_dir / f"qq_{uuid.uuid4().hex}{suffix}"
        total = 0
        try:
            async with self._http.stream("GET", parsed.geturl()) as response:
                response.raise_for_status()
                with target.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > VOICE_DOWNLOAD_MAX_BYTES:
                            raise ValueError("QQ 语音附件超过 20MB")
                        handle.write(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        if total <= 0:
            target.unlink(missing_ok=True)
            raise RuntimeError("QQ 语音附件为空")
        detected_suffix = self._detect_voice_suffix(target)
        if detected_suffix != target.suffix.casefold():
            actual_target = target.with_suffix(detected_suffix)
            os.replace(target, actual_target)
            target = actual_target
        logger.info(f"[QQ] 已下载语音附件: {target} ({total} 字节)")
        return str(target)

    @staticmethod
    def _detect_voice_suffix(path: Path) -> str:
        """QQ 的 MIME 经常不可靠，按文件头纠正 SILK / AMR 扩展名。"""
        try:
            with path.open("rb") as handle:
                header = handle.read(16)
        except OSError:
            return path.suffix.casefold()
        if b"#!SILK_V3" in header:
            return ".silk"
        if header.startswith(b"#!AMR"):
            return ".amr"
        return path.suffix.casefold()

    def _is_self_author(self, author: dict) -> bool:
        if not self._bot_id:
            return False
        for key in ("id", "user_openid", "member_openid", "union_openid"):
            if str(author.get(key) or "") == self._bot_id:
                return True
        return False

    def _is_target(self, kind: str, openid: str) -> bool:
        """目标群校验：配置为空时接受该类型的所有会话。"""
        if not self.target_groups:
            return True
        keys = {str(item).strip() for item in self.target_groups if str(item).strip()}
        return (
            openid in keys
            or f"{kind}:{openid}" in keys
            or self._display_name(kind, openid) in keys
        )

    @staticmethod
    def _parse_timestamp(data: dict) -> float:
        raw = data.get("timestamp")
        if isinstance(raw, (int, float)):
            value = float(raw)
            # 官方事件里 timestamp 常见为毫秒
            return value / 1000.0 if value > 1e11 else value
        if isinstance(raw, str) and raw.isdigit():
            value = float(raw)
            return value / 1000.0 if value > 1e11 else value
        return time.time()

    @staticmethod
    def _clean_content(content: str) -> str:
        """清洗官方文本中的提及、表情、频道链接等标记。"""
        text = _MENTION_RE.sub("", content)
        text = _EMOJI_RE.sub("", text)
        text = _CHANNEL_LINK_RE.sub("", text)
        text = _BR_RE.sub("\n", text)
        # 官方接口禁止某些字符（如首尾空格 / 连续空白），统一压缩
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    async def send_text(self, room_name: str, content: str) -> bool:
        text = str(content or "").strip()
        if not text:
            logger.warning(f"[QQ] 跳过空消息发送: {room_name}")
            return False

        target = self._resolve_room(room_name)
        if target is None:
            logger.warning(f"[QQ] 未识别的会话，无法发送: {room_name}")
            return False

        if self.backend == "simulate":
            if self.reply_delay > 0:
                await asyncio.sleep(self.reply_delay)
            await self._simulate_delay()
            logger.info(f"[QQ-SIM] 回复 [{target.display}]: {text[:80]}")
            return True

        if self._status != ConnectionStatus.CONNECTED:
            logger.warning(f"[QQ] 未连接，暂不发送: {target.display}")
            return False

        async with target.send_lock:
            if self.reply_delay > 0:
                await asyncio.sleep(self.reply_delay)
            ok = await self._send_to_target(target, text)
            if ok and self.send_interval > 0:
                await asyncio.sleep(self.send_interval)
            return ok

    async def _send_to_target(self, target: _RoomTarget, text: str) -> bool:
        """按会话类型选择官方接口发送，优先走被动回复。"""
        if target.kind == KIND_GROUP:
            path = f"/v2/groups/{target.openid}/messages"
        elif target.kind == KIND_C2C:
            path = f"/v2/users/{target.openid}/messages"
        elif target.kind == KIND_DM:
            path = f"/dms/{target.openid}/messages"
        else:
            path = f"/channels/{target.openid}/messages"

        target.msg_seq += 1
        payload: dict[str, Any] = {
            "content": text,
            "msg_type": 0,
            "msg_seq": target.msg_seq,
        }
        # 带上被动回复的 msg_id，避免消耗主动推送配额
        if target.last_msg_id:
            payload["msg_id"] = target.last_msg_id

        try:
            headers = await self._api_headers()
            resp = await self._http.post(
                f"{self.api_base}{path}",
                headers=headers,
                json=payload,
            )
            if resp.status_code >= 400:
                body = resp.text[:300]
                logger.error(
                    f"[QQ] 发送失败 [{target.display}] HTTP {resp.status_code}: {body}"
                )
                # 被动回复失败时退回主动消息再试一次
                if target.last_msg_id:
                    logger.info("[QQ] 退回主动消息重试一次")
                    target.last_msg_id = ""
                    return await self._send_to_target(target, text)
                return False
            logger.info(f"[QQ] 已发送 [{target.display}]: {text[:60]}")
            return True
        except Exception as exc:
            logger.error(f"[QQ] 发送异常 [{target.display}]: {exc}")
            return False

    async def send_image(self, room_name: str, image_path: str) -> bool:
        """上传富媒体并发送图片（群 / 单聊）。"""
        target = self._resolve_room(room_name)
        if target is None:
            logger.warning(f"[QQ] 未识别的会话，无法发图: {room_name}")
            return False
        if self.backend == "simulate":
            logger.info(f"[QQ-SIM] 发送图片 [{target.display}]: {image_path}")
            return True
        if self._status != ConnectionStatus.CONNECTED:
            return False

        try:
            if target.kind == KIND_GROUP:
                upload_path = f"/v2/groups/{target.openid}/files"
            elif target.kind == KIND_C2C:
                upload_path = f"/v2/users/{target.openid}/files"
            else:
                logger.warning("[QQ] 当前会话类型暂不支持发图")
                return False

            headers = await self._api_headers()
            headers.pop("Content-Type", None)
            with open(image_path, "rb") as handle:
                resp = await self._http.post(
                    f"{self.api_base}{upload_path}",
                    headers=headers,
                    data={"file_type": "1", "srv_send_msg": "false"},
                    files={"file_data": handle},
                )
            if resp.status_code >= 400:
                logger.error(
                    f"[QQ] 图片上传失败 HTTP {resp.status_code}: {resp.text[:200]}"
                )
                return False
            file_info = str((resp.json() or {}).get("file_info") or "")
            if not file_info:
                logger.error("[QQ] 图片上传未返回 file_info")
                return False

            async with target.send_lock:
                target.msg_seq += 1
                payload: dict[str, Any] = {
                    "content": " ",
                    "msg_type": 7,
                    "media": {"file_info": file_info},
                    "msg_seq": target.msg_seq,
                }
                if target.last_msg_id:
                    payload["msg_id"] = target.last_msg_id
                send_path = (
                    f"/v2/groups/{target.openid}/messages"
                    if target.kind == KIND_GROUP
                    else f"/v2/users/{target.openid}/messages"
                )
                resp = await self._http.post(
                    f"{self.api_base}{send_path}",
                    headers=await self._api_headers(),
                    json=payload,
                )
            if resp.status_code >= 400:
                logger.error(
                    f"[QQ] 图片发送失败 HTTP {resp.status_code}: {resp.text[:200]}"
                )
                return False
            logger.info(f"[QQ] 图片已发送 [{target.display}]")
            return True
        except FileNotFoundError:
            logger.error(f"[QQ] 图片不存在: {image_path}")
            return False
        except Exception as exc:
            logger.error(f"[QQ] 发送图片异常: {exc}")
            return False

    # ------------------------------------------------------------------
    # 富媒体发送（音频 / 语音 / 文件）
    # ------------------------------------------------------------------
    async def send_audio(
        self,
        room_name: str,
        audio_path: str,
        duration: float | None = None,
        as_file: bool = False,
        voice_max_seconds: float = VOICE_MAX_SECONDS,
        prefer_voice: bool = False,
    ) -> bool:
        """发送音频（唱歌 / 语音）。

        时长不超过 ``voice_max_seconds`` 时按"语音条"发送（file_type=3），
        否则退化为"文件"（file_type=4）。任一形式上传失败会自动换另一种重试，
        保证"点歌"不会因为平台格式限制而静默失败。

        ``prefer_voice=True`` 用于点歌：无论源歌曲多大，都先转换成
        最多 ``voice_max_seconds`` 秒的 AMR 语音条，发送失败才降级成文件。
        """
        target = self._resolve_room(room_name)
        if target is None:
            logger.warning(f"[QQ] 未识别的会话，无法发语音: {room_name}")
            return False
        if self.backend == "simulate":
            logger.info(f"[QQ-SIM] 发送音频 [{target.display}]: {audio_path}")
            return True
        if self._status != ConnectionStatus.CONNECTED:
            logger.warning(f"[QQ] 未连接，暂不发语音: {target.display}")
            return False

        if target.kind == KIND_GROUP:
            upload_path = f"/v2/groups/{target.openid}/files"
            send_path = f"/v2/groups/{target.openid}/messages"
        elif target.kind == KIND_C2C:
            upload_path = f"/v2/users/{target.openid}/files"
            send_path = f"/v2/users/{target.openid}/messages"
        else:
            logger.warning("[QQ] 当前会话类型暂不支持发语音")
            return False

        try:
            size = os.path.getsize(audio_path)
        except OSError:
            logger.error(f"[QQ] 音频文件不存在: {audio_path}")
            return False
        if size <= 0:
            logger.error(f"[QQ] 音频文件为空: {audio_path}")
            return False

        # 优先尝试"语音条"，失败后降级为"文件"
        if as_file or (size > 2 * 1024 * 1024 and not prefer_voice):
            plans = [FILE_TYPE_FILE]
        else:
            # 本地歌曲统一转成 AMR，QQ 才会按语音条而不是附件处理。
            suffix = Path(audio_path).suffix.casefold()
            if suffix in {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus"}:
                converted = await self._convert_to_amr(
                    audio_path,
                    max_seconds=voice_max_seconds,
                )
                if converted:
                    audio_path = converted
                    size = os.path.getsize(audio_path)

            too_long = (
                duration is not None
                and float(duration) > float(voice_max_seconds)
                and not prefer_voice
            )
            plans = [FILE_TYPE_FILE] if too_long else [
                FILE_TYPE_VOICE,
                FILE_TYPE_FILE,
            ]

        for index, file_type in enumerate(plans):
            file_info = await self._upload_media(
                upload_path,
                audio_path,
                file_type,
            )
            if not file_info:
                logger.warning(
                    f"[QQ] 音频上传失败（file_type={file_type}），"
                    f"{'降级为文件重试' if index + 1 < len(plans) else '放弃'}"
                )
                continue
            ok = await self._send_media_message(
                target,
                send_path,
                file_info,
            )
            if ok:
                logger.info(
                    f"[QQ] 音频已发送 [{target.display}] "
                    f"file_type={file_type} {size} 字节"
                )
                return True
            logger.warning(f"[QQ] 音频消息发送失败（file_type={file_type}）")

        logger.error(f"[QQ] 音频发送最终失败 [{target.display}]: {audio_path}")
        return False

    async def _upload_media(
        self,
        upload_path: str,
        file_path: str,
        file_type: int,
    ) -> str:
        """上传富媒体并返回 ``file_info``，失败返回空串。"""
        try:
            headers = await self._api_headers()
            headers.pop("Content-Type", None)
            with open(file_path, "rb") as handle:
                resp = await self._http.post(
                    f"{self.api_base}{upload_path}",
                    headers=headers,
                    data={
                        "file_type": str(int(file_type)),
                        "srv_send_msg": "false",
                    },
                    files={
                        "file_data": (
                            os.path.basename(file_path),
                            handle,
                            "application/octet-stream",
                        )
                    },
                )
            if resp.status_code >= 400:
                logger.error(
                    f"[QQ] 富媒体上传失败 HTTP {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
                return ""
            return str((resp.json() or {}).get("file_info") or "")
        except Exception as exc:
            logger.error(f"[QQ] 富媒体上传异常: {exc}")
            return ""

    async def _send_media_message(
        self,
        target: "_RoomTarget",
        send_path: str,
        file_info: str,
    ) -> bool:
        """发送 ``msg_type=7`` 的富媒体消息。"""
        async with target.send_lock:
            target.msg_seq += 1
            payload: dict[str, Any] = {
                "content": " ",
                "msg_type": 7,
                "media": {"file_info": file_info},
                "msg_seq": target.msg_seq,
            }
            if target.last_msg_id:
                payload["msg_id"] = target.last_msg_id
            try:
                resp = await self._http.post(
                    f"{self.api_base}{send_path}",
                    headers=await self._api_headers(),
                    json=payload,
                )
            except Exception as exc:
                logger.error(f"[QQ] 富媒体发送异常 [{target.display}]: {exc}")
                return False
        if resp.status_code >= 400:
            logger.error(
                f"[QQ] 富媒体发送失败 [{target.display}] "
                f"HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return False
        return True

    # ------------------------------------------------------------------
    # 群列表管理
    # ------------------------------------------------------------------
    async def set_target_groups(self, target_groups: list[str]) -> dict:
        self.target_groups = [
            str(item).strip() for item in (target_groups or []) if str(item).strip()
        ]
        self._register_configured_groups()
        return {
            "success": True,
            "live": self._status == ConnectionStatus.CONNECTED,
            "message": "QQ 监控列表已更新",
            "groups": list(self.target_groups),
        }

    def list_rooms(self) -> list[dict]:
        seen: set[int] = set()
        result = []
        for target in self._rooms.values():
            if id(target) in seen:
                continue
            seen.add(id(target))
            result.append(
                {
                    "display": target.display,
                    "kind": target.kind,
                    "openid": target.openid,
                    "key": self._room_key(target.kind, target.openid),
                    "target": self._is_target(target.kind, target.openid),
                }
            )
        return result

    # ------------------------------------------------------------------
    # 模拟后端
    # ------------------------------------------------------------------
    async def _simulate_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(random.uniform(12, 25))
                if not self._running:
                    break
                name, sender, messages = random.choice(SIMULATE_GROUPS)
                target = self._remember_room(KIND_GROUP, f"sim-{name}", "")
                content = random.choice(messages)
                message = WeChatMessage(
                    id=f"qqsim-{int(time.time() * 1000)}",
                    sender=sender,
                    sender_name=sender,
                    room_id=self._room_key(KIND_GROUP, f"sim-{name}"),
                    room_name=target.display,
                    content=content,
                    type=1,
                    timestamp=time.time(),
                    is_group=True,
                    is_at_me=True,
                    is_target_group=True,
                )
                logger.info(f"[QQ-SIM] [{target.display}] {sender}: {content}")
                if self.on_message and self._loop:
                    await self.on_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[QQ] 模拟消息循环异常: {exc}")

    async def _simulate_delay(self) -> None:
        await asyncio.sleep(random.uniform(0.1, 0.4))

    async def _convert_to_amr(
        self,
        audio_path: str,
        max_size: int = 3 * 1024 * 1024,
        max_seconds: float = VOICE_MAX_SECONDS,
    ) -> str | None:
        """把本地音频转换成 QQ 语音条可用的 AMR-NB（最多 60 秒）。"""
        return await asyncio.to_thread(
            self._convert_to_amr_sync,
            str(audio_path),
            int(max_size),
            float(max_seconds),
        )

    def _convert_to_amr_sync(
        self,
        audio_path: str,
        max_size: int,
        max_seconds: float,
    ) -> str | None:
        import hashlib
        import shutil
        import subprocess

        source = Path(audio_path).expanduser().resolve()
        if not source.is_file():
            return None

        ffmpeg_exe = os.environ.get("SONG_FFMPEG", "").strip()
        if not ffmpeg_exe or not Path(ffmpeg_exe).is_file():
            try:
                import imageio_ffmpeg

                ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                ffmpeg_exe = shutil.which("ffmpeg") or ""
        if not ffmpeg_exe:
            logger.warning("[QQ] 未找到 ffmpeg，无法把本地歌曲转换成语音")
            return None

        try:
            stat = source.stat()
            digest = hashlib.sha256(
                (
                    f"{source}|{stat.st_mtime_ns}|{stat.st_size}|"
                    f"{max_seconds:.0f}"
                ).encode("utf-8")
            ).hexdigest()[:24]
            cache_dir = (
                Path(__file__).resolve().parents[1]
                / "data"
                / "voice_cache"
                / "song_voice"
            )
            cache_dir.mkdir(parents=True, exist_ok=True)
            target = cache_dir / f"{digest}.amr"
            if target.is_file() and target.stat().st_size > 0:
                return str(target)

            partial = cache_dir / f"{digest}.part.amr"
            partial.unlink(missing_ok=True)
            cmd = [
                ffmpeg_exe,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-vn",
                "-map_metadata",
                "-1",
                "-ar",
                "8000",
                "-ac",
                "1",
                "-acodec",
                "libopencore_amrnb",
                "-b:a",
                "12k",
                "-t",
                str(max(1.0, min(60.0, max_seconds))),
                "-f",
                "amr",
                "-y",
                str(partial),
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=45,
            )
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", errors="ignore")[:160]
                logger.warning(f"[QQ] 本地歌曲转语音失败: {detail}")
                partial.unlink(missing_ok=True)
                return None

            final_size = partial.stat().st_size
            if final_size <= 0 or final_size > max_size:
                logger.warning(
                    f"[QQ] 转换后的语音大小异常: {final_size / 1024:.1f}KB"
                )
                partial.unlink(missing_ok=True)
                return None
            os.replace(partial, target)
            logger.info(
                f"[QQ] 本地歌曲已转换为语音: {source.name} -> "
                f"{final_size / 1024:.1f}KB"
            )
            return str(target)
        except Exception as exc:
            logger.error(f"[QQ] 本地歌曲转语音异常: {exc}")
            return None
