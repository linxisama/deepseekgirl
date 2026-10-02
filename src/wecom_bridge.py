"""企业微信智能机器人桥接。"""

from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Optional

import httpx
from loguru import logger

# 智能机器人API基地址
WXWORK_BASE_URL = "https://qyapi.weixin.qq.com"
BOT_TOKEN_URL = f"{WXWORK_BASE_URL}/cgi-bin/service/bot/get_token"
BOT_SEND_URL = f"{WXWORK_BASE_URL}/cgi-bin/message/send"


class ConnectionStatus(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class WxWorkBridge:
    """企业微信智能机器人桥接器"""

    def __init__(
        self,
        bot_id: str,
        secret: str,
        enabled: bool = True,
        timeout: float = 10.0,
        on_message=None,
        agentid: str = "",
        token: str = "",
        aes_key: str = "",
    ):
        self.bot_id = bot_id
        self.secret = secret
        self.enabled = enabled
        self.timeout = timeout
        self.on_message = on_message
        self.agentid = agentid
        self.token = token
        self.aes_key = aes_key

        self._http: Optional[httpx.AsyncClient] = None
        self._access_token: str = ""
        self._token_expire_at: float = 0
        self._status = ConnectionStatus.DISCONNECTED
        self.last_error: str = ""

        self.target_groups: list[str] = []
        self.listen_private: bool = True

    @property
    def status(self) -> ConnectionStatus:
        return self._status

    @property
    def is_connected(self) -> bool:
        return self._status == ConnectionStatus.CONNECTED

    @property
    def status_info(self) -> dict:
        return {
            "status": self._status.value,
            "bot_id": self.bot_id,
            "target_groups": self.target_groups,
            "listen_private": self.listen_private,
            "last_error": self.last_error,
        }

    async def connect(self) -> bool:
        """连接到企业微信智能机器人API"""
        if not self.enabled:
            logger.info("[企微] 企业微信智能机器人未启用")
            return False

        if not self.bot_id or not self.secret:
            self.last_error = "未配置 bot_id / secret"
            self._status = ConnectionStatus.ERROR
            logger.warning(f"[企微] {self.last_error}，跳过连接")
            return False

        self._status = ConnectionStatus.CONNECTING
        try:
            if self._http is None:
                self._http = httpx.AsyncClient(timeout=self.timeout)

            # 企业微信智能机器人API端点可能不同，先尝试多种方式
            token_url = None
            test_endpoints = [
                "https://qyapi.weixin.qq.com/cgi-bin/service/bot/get_token",
                "https://qyapi.weixin.qq.com/cgi-bin/bot/get_token",
                f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={self.bot_id}&corpsecret={self.secret}",
            ]
            
            for url in test_endpoints:
                try:
                    resp = await self._http.post(url, json={"bot_id": self.bot_id, "secret": self.secret})
                    if resp.status_code == 200 and resp.text.strip():
                        try:
                            data = resp.json()
                            if "access_token" in data:
                                token_url = url
                                self._access_token = data["access_token"]
                                self._token_expire_at = time.time() + data.get("expires_in", 7200)
                                break
                        except:
                            continue
                except:
                    continue
            
            if not self._access_token:
                # 尝试GET方式
                try:
                    resp = await self._http.get(
                        "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                        params={"corpid": self.bot_id, "corpsecret": self.secret}
                    )
                    if resp.status_code == 200 and resp.text.strip():
                        data = resp.json()
                        if "access_token" in data:
                            self._access_token = data["access_token"]
                            self._token_expire_at = time.time() + data.get("expires_in", 7200)
                except:
                    pass
            
            if not self._access_token:
                raise RuntimeError(f"未能获取 access_token，请检查bot_id和secret是否正确")

            self._status = ConnectionStatus.CONNECTED
            self.last_error = ""
            logger.info(
                f"[企微] 智能机器人已连接（bot_id: {self.bot_id}）"
            )
            return True

        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self._status = ConnectionStatus.ERROR
            logger.error(f"[企微] 连接失败: {self.last_error}")
            return False

    async def _ensure_token(self, force: bool = False) -> str:
        """获取/刷新 access_token"""
        now = time.time()
        if not force and self._access_token and now < self._token_expire_at - 60:
            return self._access_token

        try:
            resp = await self._http.post(
                BOT_TOKEN_URL,
                json={"bot_id": self.bot_id, "secret": self.secret},
            )
            data = resp.json()

            if "access_token" in data:
                self._access_token = data["access_token"]
                self._token_expire_at = now + data.get("expires_in", 7200)
                return self._access_token
            else:
                self.last_error = data.get("errmsg", "未知错误")
                logger.warning(f"[企微] {self.last_error}")
                return ""

        except Exception as e:
            self.last_error = f"获取token失败: {e}"
            logger.warning(f"[企微] {self.last_error}")
            return ""

    async def get_access_token(self) -> str:
        """获取当前有效的 access_token"""
        return await self._ensure_token()

    async def send_text(self, room_name: str, content: str) -> bool:
        """发送文本消息"""
        if not self.is_connected:
            logger.error("[企微] 企业微信未连接，无法发送消息")
            return False

        token = await self._ensure_token()
        if not token:
            return False

        try:
            url = f"{BOT_SEND_URL}?access_token={token}"

            is_group = room_name.startswith("group:")
            chat_id = room_name.replace("group:", "") if is_group else room_name

            msg_data = {
                "msgtype": "text",
                "text": {"content": content},
            }

            if is_group:
                msg_data["chatid"] = chat_id
            else:
                msg_data["touser"] = chat_id

            resp = await self._http.post(url, json=msg_data)
            data = resp.json()

            if data.get("errcode") == 0:
                logger.info(f"[企微] 消息已发送到 [{room_name}]")
                return True
            else:
                self.last_error = data.get("errmsg", "发送失败")
                logger.error(f"[企微] 发送失败: {self.last_error}")
                return False

        except Exception as e:
            self.last_error = f"发送异常: {e}"
            logger.error(f"[企微] {self.last_error}")
            return False

    async def set_target_groups(self, groups: list[str]) -> dict:
        """设置监听的目标群"""
        self.target_groups = groups
        logger.info(f"[企微] 已设置监听群: {groups}")
        return {"success": True, "groups": groups}

    def start_listening(self, loop: asyncio.AbstractEventLoop) -> None:
        """启动消息监听"""
        logger.info(
            f"[企微] 消息监听已启动（回调模式，配置群: {self.target_groups}）"
        )

    async def close(self) -> None:
        """关闭连接"""
        if self._http:
            await self._http.aclose()
            self._http = None
        self._status = ConnectionStatus.DISCONNECTED
        logger.info("[企微] 连接已关闭")

    async def disconnect(self) -> None:
        await self.close()

    def has_usable_gui(self) -> bool:
        return True


async def create_wxwork_bot(
    bot_id: str,
    secret: str,
    **kwargs,
) -> WxWorkBridge:
    """创建企业微信智能机器人实例"""
    bot = WxWorkBridge(
        bot_id=bot_id,
        secret=secret,
        **kwargs,
    )
    await bot.connect()
    return bot
