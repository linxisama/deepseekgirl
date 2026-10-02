"""REST API 路由（支持微信 / QQ 双平台）"""

import asyncio
import os
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

# 全局引用（由 main.py 注入）
_bot_ref = {}


def init_routes(bot):
    """初始化路由引用"""
    _bot_ref["bot"] = bot


class StyleRequest(BaseModel):
    name: str


class ConfigUpdate(BaseModel):
    key: str
    value: str


class AIConfigUpdate(BaseModel):
    api_url: str
    api_key: str = ""
    model: str
    api_style: str = "auto"


class GroupRequest(BaseModel):
    name: str
    platform: str = "wechat"


class PlatformRequest(BaseModel):
    platform: str = ""


class QQConfigUpdate(BaseModel):
    app_id: str = ""
    client_secret: str = ""
    sandbox: bool = False
    backend: str = "official"
    target_groups: list[str] | None = None
    reply_delay: float | None = None
    enabled: bool = True


def _normalize_platform(value: str) -> str:
    name = str(value or "").strip().lower()
    if name in {"qq", "qqbot"}:
        return "qq"
    return "wechat"


def _normalize_group_name(name: str) -> str:
    """校验并整理用户输入的群聊名称。"""
    value = str(name or "").strip()
    if not value:
        raise ValueError("群聊名称不能为空")
    if "\n" in value or "\r" in value:
        raise ValueError("群聊名称不能包含换行")
    if len(value) > 100:
        raise ValueError("群聊名称不能超过 100 个字符")
    return value


def _platform_config_prefix(platform: str) -> str:
    return "qq.target_groups" if platform == "qq" else "wechat.target_groups"


def _read_groups(bot, platform: str) -> list[str]:
    prefix = _platform_config_prefix(platform)
    return [
        str(group).strip()
        for group in (bot.config.get(prefix, []) or [])
        if str(group).strip()
    ]


def _bridge_of(bot, platform: str):
    return getattr(bot, "qq" if platform == "qq" else "wechat", None)


async def _apply_target_groups(bot, groups: list[str], platform: str = "wechat") -> dict:
    """在主事件循环中热更新对应平台的监听列表。"""
    bridge = getattr(bot, "_async_bridge", None)
    loop = getattr(bridge, "_loop", None)
    if loop and loop.is_running() and hasattr(bot, "apply_target_groups"):
        future = asyncio.run_coroutine_threadsafe(
            bot.apply_target_groups(groups, platform),
            loop,
        )
        return await asyncio.wait_for(
            asyncio.wrap_future(future),
            timeout=8.0,
        )

    target = _bridge_of(bot, platform)
    if target and hasattr(target, "set_target_groups"):
        return await target.set_target_groups(groups)
    return {
        "success": True,
        "live": False,
        "message": "配置已保存，机器人启动后生效",
    }


@router.get("/status")
async def get_status():
    """获取系统状态（含双平台明细，保留旧字段向后兼容）。"""
    bot = _bot_ref.get("bot")
    if not bot:
        return {"error": "机器人未初始化"}

    style_name = bot.style.active_name if bot.style else "unknown"
    style_display = style_name
    if bot.style and bot.style.get(style_name):
        style_display = bot.style.get(style_name).display_name

    platforms = bot.platform_status() if hasattr(bot, "platform_status") else {}

    return {
        "running": bool(getattr(bot, "_running", False)),
        "qq_running": bool(getattr(bot, "_qq_running", False)),
        "wecom_running": bool(getattr(bot, "_wecom_running", False)),
        "wechat": bot.wechat.status_info if bot.wechat else {},
        "qq": bot.qq.status_info if getattr(bot, "qq", None) else {},
        "wecom": bot.wecom.status_info if getattr(bot, "wecom", None) else {},
        "qq_enabled": bool(getattr(bot, "_qq_enabled", False)),
        "wecom_enabled": bool(getattr(bot, "_wecom_enabled", False)),
        "ai": {
            "available": bot.ai.is_available if bot.ai else False,
            "usage": bot.ai.usage.summary if bot.ai else {},
        },
        "handler": bot.handler.status if bot.handler else {},
        "security": bot.security.status if bot.security else {},
        "style": style_name,
        "style_display": style_display,
        "platforms": platforms,
        "voice": (
            bot.voice_status() if hasattr(bot, "voice_status") else {}
        ),
    }


@router.get("/styles")
async def get_styles():
    """获取所有可用风格"""
    bot = _bot_ref.get("bot")
    if not bot or not bot.style:
        return {"styles": [], "locked": False}
    locked_name = bot.style.locked_name
    locked_display_name = (
        bot.style.get(locked_name).display_name if locked_name else None
    )
    return {
        "styles": bot.style.list_styles(),
        "locked": bot.style.is_locked,
        "locked_name": locked_name,
        "locked_display_name": locked_display_name,
    }


@router.post("/styles/active")
async def set_active_style(req: StyleRequest):
    """切换风格"""
    bot = _bot_ref.get("bot")
    if not bot or not bot.style:
        return {"error": "未初始化"}
    if bot.style.is_locked:
        return {
            "success": False,
            "active": bot.style.active_name,
            "message": "人设已永久锁定，不能更改",
        }

    success = bot.style.set_active(req.name)
    if success and bot.config:
        bot.config.set("style.active", req.name)
        bot.config.save()
    return {"success": success, "active": bot.style.active_name}


@router.get("/messages")
async def get_messages(limit: int = 50, platform: str = ""):
    """获取最近消息，可按平台过滤。"""
    bot = _bot_ref.get("bot")
    if not bot:
        return {"messages": []}
    name = str(platform or "").strip().lower()
    if name in {"wechat", "qq"}:
        return {"messages": bot.get_recent_messages(limit, platform=name)}
    return {"messages": bot.get_recent_messages(limit)}


@router.get("/logs")
async def get_logs(limit: int = 100):
    """获取日志"""
    bot = _bot_ref.get("bot")
    if not bot:
        return {"logs": []}
    return {"logs": bot.get_logs(limit)}


@router.post("/bot/start")
async def start_bot(req: PlatformRequest | None = None):
    """启动机器人；不传 platform 表示启动全部已启用平台。"""
    bot = _bot_ref.get("bot")
    if not bot:
        return {"error": "未初始化"}
    platform = (req.platform if req else "") or ""
    await bot.start(platform or None)

    if platform and _normalize_platform(platform) == "qq":
        running = bool(getattr(bot, "_qq_running", False))
        return {
            "success": running,
            "platform": "qq",
            "message": (
                "QQ 端已启动"
                if running
                else (
                    getattr(getattr(bot, "qq", None), "last_error", "")
                    or "QQ 端启动失败，请检查 AppID / AppSecret 与网络"
                )
            ),
        }

    running = bool(getattr(bot, "_running", False))
    qq_running = bool(getattr(bot, "_qq_running", False))
    return {
        "success": running or qq_running,
        "platform": "all" if not platform else platform,
        "message": (
            "机器人已启动"
            if (running or qq_running)
            else "微信窗口不可用，请由用户先完成登录并保持窗口可见"
        ),
    }


@router.post("/bot/stop")
async def stop_bot(req: PlatformRequest | None = None):
    """停止机器人；不传 platform 表示停止全部平台。"""
    bot = _bot_ref.get("bot")
    if not bot:
        return {"error": "未初始化"}
    platform = (req.platform if req else "") or ""
    await bot.stop(platform or None)

    if platform and _normalize_platform(platform) == "qq":
        running = bool(getattr(bot, "_qq_running", False))
        return {
            "success": not running,
            "platform": "qq",
            "message": "QQ 端已停止" if not running else "QQ 端停止失败",
        }

    running = bool(getattr(bot, "_running", False))
    qq_running = bool(getattr(bot, "_qq_running", False))
    return {
        "success": not running and not qq_running,
        "platform": "all" if not platform else platform,
        "message": "机器人已停止" if not running else "机器人停止失败",
    }


@router.post("/ai/test")
async def test_ai():
    """测试 AI 连接"""
    bot = _bot_ref.get("bot")
    if not bot or not bot.ai:
        return {"error": "未初始化"}

    success, msg = await bot.ai.test_connection()
    return {"success": success, "message": msg}


@router.get("/config")
async def get_config():
    """返回可安全展示的 AI 配置，密钥只返回是否已设置。"""
    bot = _bot_ref.get("bot")
    if not bot or not bot.config:
        return {"error": "未初始化"}
    return {
        "api_url": bot.config.get("ai.api_url", ""),
        "api_style": bot.config.get("ai.api_style", "auto"),
        "model": bot.config.get("ai.model", ""),
        "api_key_set": bool(
            bot.config.get("ai.api_key_encrypted")
            or bot.config.get("ai.api_key")
        ),
    }


# ---------------------------------------------------------------------------
# QQ 官方机器人配置
# ---------------------------------------------------------------------------
@router.get("/qq/config")
async def get_qq_config():
    """读取 QQ 机器人配置（AppSecret 只返回是否已设置）。"""
    bot = _bot_ref.get("bot")
    if not bot or not bot.config:
        return {"error": "未初始化"}
    qq_cfg = bot.config.get("qq", {}) or {}
    bridge = getattr(bot, "qq", None)
    return {
        "enabled": bool(qq_cfg.get("enabled", False)),
        "app_id": qq_cfg.get("app_id", ""),
        "sandbox": bool(qq_cfg.get("sandbox", False)),
        "backend": qq_cfg.get("backend", "official"),
        "target_groups": list(qq_cfg.get("target_groups", []) or []),
        "reply_delay": qq_cfg.get("reply_delay", 0.0),
        "secret_set": bool(
            bot.config.get("qq.client_secret_encrypted")
            or bot.config.get("qq.client_secret")
        ),
        "status": bridge.status_info if bridge else {},
    }


@router.post("/qq/config")
async def update_qq_config(req: QQConfigUpdate):
    """保存 QQ 配置并热更新（无需重启进程）。"""
    bot = _bot_ref.get("bot")
    if not bot or not bot.config:
        return {"success": False, "message": "机器人未初始化"}

    try:
        result = await bot.configure_qq(
            app_id=req.app_id,
            client_secret=req.client_secret,
            sandbox=req.sandbox,
            backend=req.backend,
            target_groups=req.target_groups,
            reply_delay=req.reply_delay,
            enabled=req.enabled,
        )
    except Exception as exc:
        return {"success": False, "message": f"保存失败: {exc}"}
    return result


@router.get("/qq/rooms")
async def get_qq_rooms():
    """列出 QQ 端已发现的会话（群 / 单聊）。"""
    bot = _bot_ref.get("bot")
    bridge = getattr(bot, "qq", None) if bot else None
    if not bridge or not hasattr(bridge, "list_rooms"):
        return {"rooms": []}
    return {
        "rooms": bridge.list_rooms(),
        "target_groups": list(getattr(bridge, "target_groups", []) or []),
    }


# ---------------------------------------------------------------------------
# 群聊监控列表（按平台）
# ---------------------------------------------------------------------------
@router.get("/groups")
async def get_groups(platform: str = "wechat"):
    """返回指定平台的监控会话列表和实时监听状态。"""
    bot = _bot_ref.get("bot")
    if not bot or not bot.config:
        return {"error": "未初始化", "groups": []}

    name = _normalize_platform(platform)
    groups = _read_groups(bot, name)
    bridge = _bridge_of(bot, name)

    if name == "qq":
        rooms = bridge.list_rooms() if bridge and hasattr(bridge, "list_rooms") else []
        return {
            "platform": "qq",
            "groups": groups,
            "connected": bool(getattr(bridge, "is_connected", False)),
            "listening": bool(getattr(bot, "_qq_running", False)),
            "active_group_count": len(groups),
            "rooms": rooms,
            "enabled": bool(getattr(bot, "_qq_enabled", False)),
        }

    return {
        "platform": "wechat",
        "groups": groups,
        "connected": bool(getattr(bridge, "is_connected", False)),
        "listening": bool(getattr(bridge, "_running", False)),
        "active_group_count": len(
            getattr(bridge, "_target_group_ids", set()) or set()
        ),
    }


@router.post("/groups")
async def add_group(req: GroupRequest):
    """添加一个需要监控的会话（微信群名 / QQ 群 openid）。"""
    bot = _bot_ref.get("bot")
    if not bot or not bot.config:
        return {"success": False, "message": "机器人未初始化"}

    platform = _normalize_platform(req.platform)
    try:
        name = _normalize_group_name(req.name)
    except ValueError as exc:
        return {"success": False, "message": str(exc)}

    groups = _read_groups(bot, platform)
    if name in groups:
        label = "QQ 会话" if platform == "qq" else "群聊"
        return {
            "success": False,
            "message": f"{label} [{name}] 已在监控列表中",
            "groups": groups,
        }

    groups.append(name)
    bot.config.set(_platform_config_prefix(platform), groups)
    bot.config.save()
    try:
        runtime = await _apply_target_groups(bot, groups, platform)
    except Exception as exc:
        runtime = {
            "success": True,
            "live": False,
            "message": f"配置已保存，运行时更新失败，重启后生效: {exc}",
        }

    label = "QQ 会话" if platform == "qq" else "监控群聊"
    message = f"已添加{label} [{name}]"
    if not runtime.get("live", False):
        message += "；机器人当前未运行，启动后生效"
    return {
        "success": True,
        "platform": platform,
        "message": message,
        "groups": groups,
        "runtime": runtime,
    }


@router.post("/groups/remove")
async def remove_group(req: GroupRequest):
    """删除一个不再监控的会话。"""
    bot = _bot_ref.get("bot")
    if not bot or not bot.config:
        return {"success": False, "message": "机器人未初始化"}

    platform = _normalize_platform(req.platform)
    try:
        name = _normalize_group_name(req.name)
    except ValueError as exc:
        return {"success": False, "message": str(exc)}

    groups = _read_groups(bot, platform)
    remaining = [group for group in groups if group != name]
    if len(remaining) == len(groups):
        return {
            "success": True,
            "platform": platform,
            "message": f"[{name}] 不在监控列表中",
            "groups": groups,
        }

    bot.config.set(_platform_config_prefix(platform), remaining)
    bot.config.save()
    try:
        runtime = await _apply_target_groups(bot, remaining, platform)
    except Exception as exc:
        runtime = {
            "success": True,
            "live": False,
            "message": f"配置已保存，运行时更新失败，重启后生效: {exc}",
        }

    message = f"已删除 [{name}]"
    if not runtime.get("live", False):
        message += "；机器人当前未运行，启动后生效"
    return {
        "success": True,
        "platform": platform,
        "message": message,
        "groups": remaining,
        "runtime": runtime,
    }


@router.post("/config")
async def update_config(req: AIConfigUpdate):
    """保存 DeepSeek 或任意 OpenAI 兼容中转站配置，并立即热更新。"""
    bot = _bot_ref.get("bot")
    if not bot or not bot.config or not bot.ai:
        return {"success": False, "message": "机器人未初始化"}

    api_url = req.api_url.strip()
    model = req.model.strip()
    api_style = req.api_style.strip().lower() or "auto"
    if not api_url.startswith(("http://", "https://")):
        return {"success": False, "message": "API 地址必须以 http:// 或 https:// 开头"}
    if not model:
        return {"success": False, "message": "模型名称不能为空"}
    if api_style not in {"auto", "openai", "anthropic"}:
        api_style = "auto"

    new_key = req.api_key.strip()
    if new_key:
        encrypted = bot.config.encrypt_value(new_key)
        bot.config.set("ai.api_key_encrypted", encrypted)
        bot.config.set("ai.api_key", "")
    else:
        new_key = bot.config.get_decrypted("ai.api_key_encrypted")
        if not new_key:
            new_key = str(bot.config.get("ai.api_key", "") or "").strip()

    bot.config.set("ai.api_url", api_url)
    bot.config.set("ai.api_style", api_style)
    bot.config.set("ai.model", model)
    bot.config.save()
    await bot.ai.reconfigure(
        api_url=api_url,
        api_key=new_key,
        model=model,
        api_style=api_style,
    )
    return {
        "success": True,
        "message": "配置已保存并立即生效",
        "api_url": bot.ai.api_url,
        "api_style": bot.ai.api_style,
        "model": bot.ai.model,
        "api_key_set": bot.ai.api_key_set,
    }


@router.post("/context/clear")
async def clear_context(req: PlatformRequest | None = None):
    """清除聊天上下文；不传 platform 时清除两端。"""
    bot = _bot_ref.get("bot")
    if not bot or not bot.handler:
        return {"error": "未初始化"}

    platform = (req.platform if req else "") or ""
    name = str(platform).strip().lower()
    cleared = []
    if name in {"", "wechat"} and bot.handler:
        bot.handler.clear_context()
        cleared.append("wechat")
    if name in {"", "qq"} and getattr(bot, "qq_handler", None):
        bot.qq_handler.clear_context()
        cleared.append("qq")
    return {"success": True, "cleared": cleared}


# ---------------------------------------------------------------------------
# 语音 / 点歌
# ---------------------------------------------------------------------------
class VoiceConfigUpdate(BaseModel):
    enabled: bool | None = None
    engine: str | None = None
    default_voice: str | None = None
    rate: str | None = None
    pitch: str | None = None
    songs_enabled: bool | None = None
    sing_rate: str | None = None
    sing_pitch: str | None = None
    quick_replies: bool | None = None
    prewarm: bool | None = None


class VoicePreviewRequest(BaseModel):
    text: str
    voice: str = ""
    singing: bool = True


class SongPlayRequest(BaseModel):
    song: str = ""
    text: str = ""
    platform: str = "qq"
    room: str = ""


class MusicConfigUpdate(BaseModel):
    enabled: bool | None = None
    base_url: str | None = None
    password: str | None = None
    username: str | None = None
    user_password: str | None = None
    quality: str | None = None
    timeout: float | None = None


class MusicTestRequest(BaseModel):
    keyword: str = "青花瓷"


@router.get("/voice/config")
async def get_voice_config():
    """读取语音 / 点歌配置与运行状态。"""
    bot = _bot_ref.get("bot")
    if not bot or not bot.config:
        return {"error": "未初始化"}
    voice_cfg = bot.config.get("voice", {}) or {}
    songs_cfg = voice_cfg.get("songs", {}) or {}
    quick_cfg = voice_cfg.get("quick_replies", {}) or {}
    return {
        "enabled": bool(voice_cfg.get("enabled", True)),
        "engine": voice_cfg.get("engine", "auto"),
        "default_voice": voice_cfg.get("default_voice", ""),
        "rate": voice_cfg.get("rate", "+0%"),
        "pitch": voice_cfg.get("pitch", "+0Hz"),
        "songs_enabled": bool(songs_cfg.get("enabled", True)),
        "sing_rate": songs_cfg.get("sing_rate", "+8%"),
        "sing_pitch": songs_cfg.get("sing_pitch", "+35Hz"),
        "quick_replies": bool(quick_cfg.get("enabled", True)),
        "prewarm": bool(voice_cfg.get("prewarm", True)),
        "status": bot.voice_status(),
    }


@router.post("/voice/config")
async def update_voice_config(req: VoiceConfigUpdate):
    """保存语音配置，并热更新到双端处理管线（无需重启）。"""
    bot = _bot_ref.get("bot")
    if not bot or not bot.config:
        return {"success": False, "message": "机器人未初始化"}

    mapping = {
        "enabled": "voice.enabled",
        "engine": "voice.engine",
        "default_voice": "voice.default_voice",
        "rate": "voice.rate",
        "pitch": "voice.pitch",
        "songs_enabled": "voice.songs.enabled",
        "sing_rate": "voice.songs.sing_rate",
        "sing_pitch": "voice.songs.sing_pitch",
        "quick_replies": "voice.quick_replies.enabled",
        "prewarm": "voice.prewarm",
    }
    changed = []
    for field, key in mapping.items():
        value = getattr(req, field)
        if value is None:
            continue
        bot.config.set(key, value)
        changed.append(key)

    if not changed:
        return {"success": False, "message": "没有需要保存的改动"}

    try:
        bot.config.save()
        status = await bot.rebuild_voice()
    except Exception as exc:
        return {"success": False, "message": f"保存失败: {exc}"}

    return {
        "success": True,
        "message": "语音配置已保存并立即生效",
        "changed": changed,
        "status": status,
    }


@router.get("/songs")
async def list_songs():
    """列出点歌曲库。"""
    bot = _bot_ref.get("bot")
    songs = []
    if bot is not None:
        try:
            songs = bot.song.list_songs() if getattr(bot, "song", None) else []
        except Exception:
            songs = []
    return {"songs": songs, "count": len(songs)}


@router.post("/voice/preview")
async def preview_voice(req: VoicePreviewRequest):
    """本地试听：把一句话用朗读或唱腔合成出来，返回可播放的地址。"""
    bot = _bot_ref.get("bot")
    if not bot or not getattr(bot, "song", None):
        return {"success": False, "message": "语音服务未启用"}

    text = str(req.text or "").strip()
    if not text:
        return {"success": False, "message": "请先输入要试听的内容"}

    try:
        if req.singing:
            audio = await bot.song.preview(text, voice=req.voice)
        else:
            audio = await bot.song.render_speech(text, voice=req.voice)
    except Exception as exc:
        return {"success": False, "message": f"合成失败: {exc}"}

    if not audio:
        return {"success": False, "message": "合成失败，请稍后再试"}

    name = os.path.basename(str(audio))
    duration = 0.0
    try:
        duration = bot.song.tts.estimate_duration(audio)
    except Exception:
        duration = 0.0
    return {
        "success": True,
        "url": f"/api/voice/file?name={quote(name)}",
        "duration": round(duration, 1),
    }


@router.get("/voice/file")
async def voice_file(name: str):
    """播放已合成的音频（仅允许读取语音缓存目录内的文件）。"""
    from fastapi.responses import FileResponse

    bot = _bot_ref.get("bot")
    tts = getattr(getattr(bot, "song", None), "tts", None) if bot else None
    if tts is None:
        return {"success": False, "message": "语音服务未启用"}

    safe_name = os.path.basename(str(name or ""))
    if not safe_name:
        return {"success": False, "message": "缺少文件名"}

    cache_dir = Path(tts.cache_dir).resolve()
    target = (cache_dir / safe_name).resolve()
    try:
        target.relative_to(cache_dir)
    except ValueError:
        return {"success": False, "message": "非法的文件路径"}
    if not target.is_file():
        return {"success": False, "message": "音频文件不存在"}

    media = "audio/mpeg" if target.suffix.casefold() == ".mp3" else "audio/wav"
    return FileResponse(str(target), media_type=media, filename=safe_name)


@router.post("/songs/play")
async def play_song(req: SongPlayRequest):
    """手动把一首歌（或自定义歌词）发到指定会话，用于测试。"""
    bot = _bot_ref.get("bot")
    if not bot or not getattr(bot, "song", None):
        return {"success": False, "message": "语音服务未启用"}

    platform = _normalize_platform(req.platform)
    bridge = _bridge_of(bot, platform)
    if bridge is None:
        return {"success": False, "message": f"{platform} 端未启用"}

    room = str(req.room or "").strip()
    if not room:
        return {"success": False, "message": "请选择目标会话"}

    song = bot.song.get_song(req.song) if req.song else None
    title = song["title"] if song else ""
    try:
        if song is not None:
            audio = await bot.song.render_song(song)
        elif req.text.strip():
            audio = await bot.song.render_lyrics(req.text)
            title = "自定义歌词"
        else:
            song = bot.song.random_song()
            title = song["title"]
            audio = await bot.song.render_song(song)
    except Exception as exc:
        return {"success": False, "message": f"合成失败: {exc}"}

    if not audio:
        return {"success": False, "message": "合成失败，请稍后再试"}

    duration = 0.0
    try:
        duration = bot.song.tts.estimate_duration(audio)
    except Exception:
        duration = 0.0

    sender = getattr(bridge, "send_audio", None)
    try:
        if callable(sender):
            ok = await sender(room, str(audio), duration=duration)
        else:
            ok = await bridge.send_file(room, str(audio))
    except Exception as exc:
        return {"success": False, "message": f"发送失败: {exc}"}

    return {
        "success": bool(ok),
        "message": (
            f"已把《{title}》发到 {room}" if ok else f"发送失败（{room}）"
        ),
        "song": title,
        "seconds": round(duration, 1),
    }


@router.get("/voice/songs/qq")
async def qq_song_options():
    """QQ 页的下拉选项：歌曲列表 + 已发现会话。"""
    bot = _bot_ref.get("bot")
    songs = []
    rooms = []
    if bot is not None:
        try:
            songs = bot.song.list_songs() if getattr(bot, "song", None) else []
        except Exception:
            songs = []
        bridge = getattr(bot, "qq", None)
        if bridge is not None and hasattr(bridge, "list_rooms"):
            try:
                rooms = bridge.list_rooms()
            except Exception:
                rooms = []
    return {"songs": songs, "rooms": rooms}


# ---------------------------------------------------------------------------
# 音乐源（洛雪音乐服务器）
# ---------------------------------------------------------------------------
@router.get("/music/config")
async def get_music_config():
    """读取音乐源配置（访问密码只回传是否已设置）。"""
    bot = _bot_ref.get("bot")
    if not bot or not bot.config:
        return {"error": "未初始化"}
    music_cfg = (bot.config.get("voice", {}) or {}).get("music", {}) or {}
    return {
        "enabled": bool(music_cfg.get("enabled", False)),
        "base_url": music_cfg.get("base_url", ""),
        "username": music_cfg.get("username", ""),
        "quality": music_cfg.get("quality", "128k"),
        "timeout": music_cfg.get("timeout", 30.0),
        "password_set": bool(music_cfg.get("password")),
        "user_password_set": bool(music_cfg.get("user_password")),
        "status": (
            bot.music.status() if getattr(bot, "music", None) else {}
        ),
    }


@router.post("/music/config")
async def update_music_config(req: MusicConfigUpdate):
    """保存音乐源配置并热更新（无需重启进程）。"""
    bot = _bot_ref.get("bot")
    if not bot or not bot.config:
        return {"success": False, "message": "机器人未初始化"}

    mapping = {
        "enabled": "voice.music.enabled",
        "base_url": "voice.music.base_url",
        "username": "voice.music.username",
        "quality": "voice.music.quality",
        "timeout": "voice.music.timeout",
    }
    changed = []
    for field, key in mapping.items():
        value = getattr(req, field)
        if value is None:
            continue
        bot.config.set(key, value)
        changed.append(key)

    # 密码类字段：留空表示不修改
    if req.password:
        bot.config.set("voice.music.password", req.password)
        changed.append("voice.music.password")
    if req.user_password:
        bot.config.set("voice.music.user_password", req.user_password)
        changed.append("voice.music.user_password")

    if not changed:
        return {"success": False, "message": "没有需要保存的改动"}

    try:
        bot.config.save()
        status = await bot.rebuild_voice()
    except Exception as exc:
        return {"success": False, "message": f"保存失败: {exc}"}

    enabled = bool(bot.config.get("voice.music.enabled", False))
    return {
        "success": True,
        "message": (
            "音乐源已保存并立即生效"
            if enabled
            else "音乐源已保存（当前为关闭状态）"
        ),
        "changed": changed,
        "status": status.get("music", {}),
    }


@router.post("/music/test")
async def test_music(req: MusicTestRequest | None = None):
    """连通性自检：认证 → 搜索 → 解析（不下载文件）。"""
    bot = _bot_ref.get("bot")
    music = getattr(bot, "music", None) if bot else None
    if music is None or not music.enabled:
        return {"success": False, "message": "音乐源未启用，请先保存配置并开启"}
    keyword = (req.keyword if req else "") or "青花瓷"
    try:
        return await asyncio.wait_for(
            music.test_connection(keyword),
            timeout=max(10.0, music.timeout + 10.0),
        )
    except asyncio.TimeoutError:
        return {"success": False, "message": "连接超时，请检查服务器是否在线"}
    except Exception as exc:
        return {"success": False, "message": f"测试异常: {exc}"}


# =============================================================================
# 本地音乐导入 API
# =============================================================================

class LocalSongImport(BaseModel):
    title: str
    artist: str = "本地导入"
    file_path: str  # 相对于 data/music_cache/ 的文件名


@router.post("/songs/local")
async def import_local_song(req: LocalSongImport):
    """导入本地音频文件到点歌曲库"""
    bot = _bot_ref.get("bot")
    if not bot or not getattr(bot, "song", None):
        return {"success": False, "message": "语音服务未启用"}

    song_service = bot.song
    music_cache_dir = Path("./data/music_cache")
    audio_path = music_cache_dir / req.file_path

    if not audio_path.exists():
        return {"success": False, "message": f"文件不存在: {audio_path}"}

    try:
        song = song_service.add_local_song(
            title=req.title,
            audio_path=str(audio_path),
            artist=req.artist,
        )
        if song:
            return {
                "success": True,
                "message": f"已导入: {req.title}",
                "song": song,
            }
        else:
            return {"success": False, "message": "导入失败，歌曲可能已存在"}
    except Exception as e:
        return {"success": False, "message": f"导入失败: {e}"}


@router.get("/songs/local/list")
async def list_local_songs():
    """列出所有本地导入的歌曲"""
    bot = _bot_ref.get("bot")
    if not bot or not getattr(bot, "song", None):
        return {"songs": []}

    all_songs = bot.song.list_songs()
    local_songs = [s for s in all_songs if s.get("is_local")]
    return {"songs": local_songs}


@router.post("/songs/local/remove")
async def remove_local_song(req: LocalSongImport):
    """移除本地导入的歌曲"""
    bot = _bot_ref.get("bot")
    if not bot or not getattr(bot, "song", None):
        return {"success": False}

    song_service = bot.song
    # 从本地歌曲列表中移除
    song_service._local_songs = [
        s for s in song_service._local_songs
        if s.get("title") != req.title
    ]
    song_service._build_index()
    return {"success": True, "message": f"已移除: {req.title}"}


# =============================================================================
# 企业微信配置 API
# =============================================================================

class WecomConfigUpdate(BaseModel):
    bot_id: str = ""
    secret: str = ""
    corp_id: str = ""
    agent_id: str = ""
    enabled: bool = False


@router.get("/wecom/config")
async def get_wecom_config():
    """读取企业微信机器人配置"""
    bot = _bot_ref.get("bot")
    if not bot or not bot.config:
        return {"error": "未初始化"}
    wecom_cfg = bot.config.get("wecom", {}) or {}
    bridge = getattr(bot, "wecom", None)
    return {
        "enabled": bool(wecom_cfg.get("enabled", False)),
        "bot_id": wecom_cfg.get("bot_id", ""),
        "corp_id": wecom_cfg.get("corpid", ""),
        "agent_id": wecom_cfg.get("agentid", ""),
        "secret_set": bool(
            bot.config.get("wecom.corpsecret_encrypted")
            or bot.config.get("wecom.corpsecret")
        ),
        "status": bridge.status_info if bridge else {},
    }


@router.post("/wecom/config")
async def update_wecom_config(req: WecomConfigUpdate):
    """保存企业微信配置并热更新"""
    bot = _bot_ref.get("bot")
    if not bot or not bot.config:
        return {"success": False, "message": "机器人未初始化"}

    try:
        # 保存配置
        bot.config.set("wecom.bot_id", req.bot_id)
        bot.config.set("wecom.corpid", req.corp_id)
        bot.config.set("wecom.agentid", req.agent_id)
        bot.config.set("wecom.enabled", req.enabled)

        if req.secret:
            encrypted = bot.config.encrypt_value(req.secret)
            bot.config.set("wecom.corpsecret_encrypted", encrypted)
            bot.config.set("wecom.corpsecret", "")
        elif not bot.config.get("wecom.corpsecret_encrypted"):
            # 如果没有加密的secret，尝试从明文读取
            plain_secret = bot.config.get("wecom.corpsecret", "")
            if plain_secret:
                bot.config.set("wecom.corpsecret_encrypted", bot.config.encrypt_value(plain_secret))
                bot.config.set("wecom.corpsecret", "")

        bot.config.save()

        # 更新运行时状态
        if req.enabled:
            await bot._start_wecom()
        else:
            await bot._stop_wecom()

        return {
            "success": True,
            "message": "企业微信配置已保存" if req.enabled else "企业微信已禁用",
        }
    except Exception as exc:
        return {"success": False, "message": f"保存失败: {exc}"}
