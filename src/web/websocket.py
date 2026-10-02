"""WebSocket 实时推送"""

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

ws_router = APIRouter()

_connections: set[WebSocket] = set()
_event_buffer: list[dict] = []
_BUFFER_MAX = 200


async def broadcast(event: str, data: dict) -> None:
    """向所有连接广播事件"""
    message = json.dumps({
        "event": event,
        "data": data,
        "time": datetime.now().isoformat(),
    }, ensure_ascii=False)

    _event_buffer.append({"event": event, "data": data, "time": datetime.now().isoformat()})
    if len(_event_buffer) > _BUFFER_MAX:
        _event_buffer.pop(0)

    disconnected = set()
    for ws in _connections:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.add(ws)
    _connections.difference_update(disconnected)


@ws_router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    _connections.add(websocket)
    logger.info(f"WebSocket 连接建立 (总数: {len(_connections)})")

    try:
        for event in _event_buffer[-50:]:
            await websocket.send_text(json.dumps(event, ensure_ascii=False))

        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=60)
                if data == "ping":
                    await websocket.send_text(json.dumps({"event": "pong"}))
            except asyncio.TimeoutError:
                # 超时无消息，发送心跳检测连接是否存活
                try:
                    await websocket.send_text(json.dumps({"event": "heartbeat"}))
                except Exception:
                    break

    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as e:
        logger.debug(f"WebSocket 异常: {e}")
    finally:
        _connections.discard(websocket)
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info(f"WebSocket 连接断开 (剩余: {len(_connections)})")
