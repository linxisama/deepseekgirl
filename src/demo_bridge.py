"""Demo 模式：模拟微信消息收发，无需真实微信连接"""

import asyncio
import random
import time
import threading
from typing import Callable, Awaitable
from enum import Enum

from loguru import logger

from .wechat_bridge import WeChatMessage, ConnectionStatus


# 模拟消息素材
DEMO_MESSAGES = [
    "你好",
    "今天天气怎么样？",
    "推荐个电影呗",
    "有人知道附近哪家餐厅好吃吗？",
    "周末有什么安排？",
    "这个功能怎么用？",
    "大家好，我是新来的",
    "有没有人遇到过这个问题？",
    "分享一篇不错的文章",
    "明天开会记得准时到",
    "谁知道这个软件怎么安装？",
    "最近有什么好看的剧吗？",
    "请问这个 bug 怎么解决？",
    "今天加班到几点？",
    "有人一起吃饭吗？",
]

DEMO_SENDERS = [
    "张三", "李四", "王五", "赵六", "小明",
    "小红", "老王", "阿杰", "小林", "大伟",
]


class DemoBridge:
    """Demo 模式：模拟微信消息收发，用于测试完整管线"""

    def __init__(
        self,
        target_groups: list[str],
        on_message: Callable[[WeChatMessage], Awaitable[None]] | None = None,
        reply_delay: float = 0.0,
    ):
        self.target_groups = target_groups or ["Demo群"]
        self.on_message = on_message
        self.reply_delay = reply_delay
        self._status = ConnectionStatus.DISCONNECTED
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    @property
    def status(self) -> ConnectionStatus:
        return self._status

    @property
    def is_connected(self) -> bool:
        return self._status == ConnectionStatus.CONNECTED

    async def connect(self) -> bool:
        """Demo 模式：立即连接成功"""
        self._status = ConnectionStatus.CONNECTING
        logger.info("[Demo] 模拟连接微信...")
        await asyncio.sleep(0.5)
        self._status = ConnectionStatus.CONNECTED
        logger.info("[Demo] 微信连接成功（模拟）")
        return True

    def start_listening(self, loop: asyncio.AbstractEventLoop) -> None:
        """启动模拟消息生成"""
        if self._status != ConnectionStatus.CONNECTED:
            return

        self._running = True
        self._loop = loop
        self._thread = threading.Thread(
            target=self._generate_messages,
            daemon=True,
            name="demo-message-gen",
        )
        self._thread.start()
        logger.info(f"[Demo] 消息监听已启动，模拟群聊: {self.target_groups}")

    def _generate_messages(self) -> None:
        """定时生成模拟消息"""
        while self._running:
            delay = random.randint(10, 30)
            time.sleep(delay)

            if not self._running:
                break

            group = random.choice(self.target_groups)
            sender = random.choice(DEMO_SENDERS)
            content = random.choice(DEMO_MESSAGES)

            message = WeChatMessage(
                id=f"demo-{time.time():.0f}",
                sender=sender,
                sender_name=sender,
                room_id=group,
                room_name=group,
                content=content,
                type=1,
                timestamp=time.time(),
                is_group=True,
                is_at_me=random.random() < 0.2,
            )

            logger.info(f"[Demo] [{group}] {sender}: {content}")

            if self.on_message and self._loop:
                asyncio.run_coroutine_threadsafe(
                    self.on_message(message), self._loop
                )

    async def send_text(self, room_name: str, content: str) -> bool:
        """Demo 模式：记录回复到日志"""
        if self.reply_delay > 0:
            await asyncio.sleep(self.reply_delay)

        logger.info(f"[Demo] 回复 [{room_name}]: {content[:100]}...")
        return True

    async def disconnect(self) -> None:
        """停止模拟"""
        self._running = False
        self._status = ConnectionStatus.DISCONNECTED
        logger.info("[Demo] 已断开连接")

    @property
    def status_info(self) -> dict:
        return {
            "status": self._status.value,
            "target_groups": self.target_groups,
            "mode": "demo",
        }
