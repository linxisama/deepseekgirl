"""点歌服务：曲库 + 意图解析 + 唱腔合成 + 缓存。

工作原理
--------
本机没有歌声合成模型，所以"唱歌"采用**唱腔念唱**方案：把歌词交给语音引擎，
抬升音调（pitch）、加快语速（rate），并用"～／，／。"控制断句节奏，
听起来接近"跟着调子哼唱"的效果。

为了让点歌本身不影响回复速度：
1. 曲库内置，解析纯本地字符串匹配，零网络开销；
2. 合成结果落盘缓存，同一首歌第二次点播直接命中文件；
3. 机器人启动后可在后台预热热门歌曲，用户点歌时几乎瞬间发出。
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from pathlib import Path

from .music_provider import LXMusicProvider, mp3_duration
from .tts_service import TTSService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 触发词

#: 点歌意图
PLAY_TRIGGERS = (
    "点歌",
    "点一首",
    "点首",
    "来一首",
    "来首",
    "来一个",
    "来一段",
    "唱首歌",
    "唱一首",
    "唱首",
    "唱一个",
    "唱歌",
    "唱给我",
    "给我唱",
    "帮我唱",
    "献唱",
    "整一首",
    "整首",
    "我要听",
    "想听",
)

#: 歌单意图
LIST_TRIGGERS = (
    "歌单",
    "有哪些歌",
    "有什么歌",
    "能唱什么",
    "会唱什么",
    "会什么歌",
    "有什么曲子",
    "歌曲列表",
    "曲库",
    "能点哪些歌",
    "怎么点歌",
)

#: 随机意图
RANDOM_TRIGGERS = (
    "随便",
    "随机",
    "任意",
    "啥都行",
    "都行",
    "你选",
    "你来选",
    "你决定",
    "随便唱",
)

#: 需要从歌名里剥离的语气词 / 量词
_FILLER_WORDS = (
    "一下",
    "一首",
    "一个",
    "一段",
    "的",
    "歌",
    "曲子",
    "吧",
    "啊",
    "呀",
    "嘛",
    "哦",
    "噢",
    "呗",
    "呢",
    "来",
    "唱",
    "点",
    "给我",
    "帮我",
    "谢谢",
    "好听的",
    "好听点的",
    "please",
)

#: 明确表达"唱我写的词"的触发词（区别于"点一首歌"）
SING_LYRIC_TRIGGERS = (
    "唱歌",
    "唱给我",
    "给我唱",
    "帮我唱",
    "献唱",
)

#: 每首歌最短需要匹配到的别名长度（避免"小"这种字误命中）
_MIN_ALIAS_LEN = 2


# ---------------------------------------------------------------- 曲库

#: 内置曲库。歌词只截取副歌片段（2~4 句），用于唱腔演示。
SONG_LIBRARY: list[dict] = [
    {
        "id": "qinghuaci",
        "title": "青花瓷",
        "artist": "周杰伦",
        "aliases": ["青花瓷"],
        "tags": ["国风", "经典"],
        "lyrics": (
            "素胚勾勒出青花笔锋浓转淡，"
            "瓶身描绘的牡丹一如你初妆，"
            "冉冉檀香透过窗心事我了然，"
            "宣纸上走笔至此搁一半"
        ),
    },
    {
        "id": "daoxiang",
        "title": "稻香",
        "artist": "周杰伦",
        "aliases": ["稻香"],
        "tags": ["励志", "怀旧"],
        "lyrics": (
            "还记得你说家是唯一的城堡，"
            "随着稻香河流继续奔跑，"
            "微微笑 小时候的梦我知道，"
            "不要哭 让萤火虫带着你逃跑"
        ),
    },
    {
        "id": "qingtian",
        "title": "晴天",
        "artist": "周杰伦",
        "aliases": ["晴天"],
        "tags": ["青春"],
        "lyrics": (
            "故事的小黄花 从出生那年就飘着，"
            "童年的荡秋千 随记忆一直晃到现在，"
            "吹着前奏望着天空 我想起花瓣试着掉落"
        ),
    },
    {
        "id": "qilixiang",
        "title": "七里香",
        "artist": "周杰伦",
        "aliases": ["七里香"],
        "tags": ["夏天"],
        "lyrics": (
            "雨下整夜 我的爱溢出就像雨水，"
            "院子落叶 跟我的思念厚厚一叠，"
            "窗外的麻雀 在电线杆上多嘴"
        ),
    },
    {
        "id": "juhuatai",
        "title": "菊花台",
        "artist": "周杰伦",
        "aliases": ["菊花台"],
        "tags": ["国风"],
        "lyrics": (
            "菊花残 满地伤 你的笑容已泛黄，"
            "花落人断肠 我心事静静躺"
        ),
    },
    {
        "id": "guyongzhe",
        "title": "孤勇者",
        "artist": "陈奕迅",
        "aliases": ["孤勇者"],
        "tags": ["热血"],
        "lyrics": (
            "爱你不跪的模样，爱你对峙过绝望，"
            "致那黑夜中的呜咽与怒吼，"
            "谁说站在光里的才算英雄"
        ),
    },
    {
        "id": "houlaidian",
        "title": "后来",
        "artist": "刘若英",
        "aliases": ["后来"],
        "tags": ["怀旧", "遗憾"],
        "lyrics": (
            "后来 我总算学会了如何去爱，"
            "可惜你早已远去 消失在人海，"
            "后来 终于在眼泪中明白，"
            "有些人 一旦错过就不再"
        ),
    },
    {
        "id": "tonghua",
        "title": "童话",
        "artist": "光良",
        "aliases": ["童话"],
        "tags": ["纯爱"],
        "lyrics": (
            "你说爱我 你要变成童话里 我爱的那个天使，"
            "张开双手 变成翅膀守护你，"
            "你要相信 相信我们会像童话故事里 幸福和快乐是结局"
        ),
    },
    {
        "id": "xiaoxingyun",
        "title": "小幸运",
        "artist": "田馥甄",
        "aliases": ["小幸运"],
        "tags": ["青春"],
        "lyrics": (
            "我听见雨滴落在青青草地，"
            "我听见远方下课钟声响起，"
            "原来你是我最想留住的幸运"
        ),
    },
    {
        "id": "yanyuan",
        "title": "演员",
        "artist": "薛之谦",
        "aliases": ["演员"],
        "tags": ["伤感"],
        "lyrics": (
            "该配合你演出的我演视而不见，"
            "在逼一个最爱你的人即兴表演，"
            "简单点 说话的方式简单点"
        ),
    },
    {
        "id": "chengdu",
        "title": "成都",
        "artist": "赵雷",
        "aliases": ["成都"],
        "tags": ["民谣"],
        "lyrics": (
            "和我在成都的街头走一走，"
            "直到所有的灯都熄灭了也不停留，"
            "你会挽着我的衣袖 我会把手揣进裤兜"
        ),
    },
    {
        "id": "nanshannan",
        "title": "南山南",
        "artist": "马頔",
        "aliases": ["南山南"],
        "tags": ["民谣"],
        "lyrics": (
            "你在南方的艳阳里大雪纷飞，"
            "我在北方的寒夜里四季如春，"
            "如果天黑之前来得及 我要忘了你的眼睛"
        ),
    },
    {
        "id": "qifengle",
        "title": "起风了",
        "artist": "买辣椒也用券",
        "aliases": ["起风了"],
        "tags": ["治愈"],
        "lyrics": (
            "我曾难自拔于世界之大，也沉溺于其中梦话，"
            "不得真假 不做挣扎 不惧笑话，"
            "如今我能笑着说出 那些过往"
        ),
    },
    {
        "id": "yehangzhong",
        "title": "夜空中最亮的星",
        "artist": "逃跑计划",
        "aliases": ["夜空中最亮的星", "最亮的星"],
        "tags": ["热血", "治愈"],
        "lyrics": (
            "夜空中最亮的星 请指引我走出去，"
            "照亮我前行 夜空中最亮的星，"
            "是否在意 等太阳升起还是意外先来临"
        ),
    },
    {
        "id": "pingfanzhilu",
        "title": "平凡之路",
        "artist": "朴树",
        "aliases": ["平凡之路"],
        "tags": ["励志"],
        "lyrics": (
            "我曾经跨过山和大海 也穿过人山人海，"
            "我曾经拥有着的一切 转眼都飘散如烟，"
            "我曾经失落失望失掉所有方向"
        ),
    },
    {
        "id": "yishengyouni",
        "title": "一生有你",
        "artist": "水木年华",
        "aliases": ["一生有你"],
        "tags": ["校园"],
        "lyrics": (
            "多少人曾爱慕你年轻时的容颜，"
            "可知谁愿承受岁月无情的变迁，"
            "多少人曾在你生命中来了又还"
        ),
    },
    {
        "id": "guanghuisuivue",
        "title": "光辉岁月",
        "artist": "Beyond",
        "aliases": ["光辉岁月"],
        "tags": ["励志", "经典"],
        "lyrics": (
            "风雨中抱紧自由，一生经过彷徨的挣扎，"
            "自信可改变未来，问谁又能做到"
        ),
    },
    {
        "id": "haikuotiankong",
        "title": "海阔天空",
        "artist": "Beyond",
        "aliases": ["海阔天空"],
        "tags": ["励志", "经典"],
        "lyrics": (
            "原谅我这一生不羁放纵爱自由，"
            "也会怕有一天会跌倒，"
            "背弃了理想 谁人都可以"
        ),
    },
    {
        "id": "yueliangdaibiao",
        "title": "月亮代表我的心",
        "artist": "邓丽君",
        "aliases": ["月亮代表我的心", "代表我的心"],
        "tags": ["经典", "老歌"],
        "lyrics": (
            "你问我爱你有多深，我爱你有几分，"
            "我的情也真 我的爱也真，"
            "月亮代表我的心"
        ),
    },
    {
        "id": "tianmimi",
        "title": "甜蜜蜜",
        "artist": "邓丽君",
        "aliases": ["甜蜜蜜"],
        "tags": ["经典", "老歌"],
        "lyrics": (
            "甜蜜蜜 你笑得甜蜜蜜，"
            "好像花儿开在春风里，"
            "在哪里 在哪里见过你"
        ),
    },
    {
        "id": "pengyou",
        "title": "朋友",
        "artist": "周华健",
        "aliases": ["朋友"],
        "tags": ["友情"],
        "lyrics": (
            "朋友一生一起走 那些日子不再有，"
            "一句话 一辈子 一生情 一杯酒"
        ),
    },
    {
        "id": "tongnian",
        "title": "童年",
        "artist": "罗大佑",
        "aliases": ["童年"],
        "tags": ["怀旧"],
        "lyrics": (
            "池塘边的榕树上 知了在声声叫着夏天，"
            "操场边的秋千上 只有蝴蝶停在上面"
        ),
    },
    {
        "id": "dashengzai_dongji",
        "title": "大约在冬季",
        "artist": "齐秦",
        "aliases": ["大约在冬季"],
        "tags": ["老歌", "离别"],
        "lyrics": (
            "轻轻地 我将离开你 请将眼角的泪拭去，"
            "不是在此时 不知在何时，"
            "我想大约会是在冬季"
        ),
    },
    {
        "id": "songbie",
        "title": "送别",
        "artist": "李叔同",
        "aliases": ["送别", "长亭外"],
        "tags": ["经典", "民谣"],
        "lyrics": (
            "长亭外 古道边 芳草碧连天，"
            "晚风拂柳笛声残 夕阳山外山"
        ),
    },
    {
        "id": "molihua",
        "title": "茉莉花",
        "artist": "江苏民歌",
        "aliases": ["茉莉花"],
        "tags": ["民歌"],
        "lyrics": (
            "好一朵美丽的茉莉花，"
            "芬芳美丽满枝桠 又香又白人人夸"
        ),
    },
    {
        "id": "kangdingqingge",
        "title": "康定情歌",
        "artist": "四川民歌",
        "aliases": ["康定情歌"],
        "tags": ["民歌"],
        "lyrics": (
            "跑马溜溜的山上 一朵溜溜的云哟，"
            "端端溜溜的照在 康定溜溜的城哟"
        ),
    },
    {
        "id": "zhu_ni_shengrikuaile",
        "title": "生日快乐",
        "artist": "经典",
        "aliases": ["生日快乐", "生日歌", "生日快乐歌"],
        "tags": ["祝福"],
        "lyrics": (
            "祝你生日快乐，祝你生日快乐，"
            "祝你生日快乐 祝你永远快乐"
        ),
    },
    {
        "id": "tingshuo_xiexie",
        "title": "听我说谢谢你",
        "artist": "李昕融",
        "aliases": ["听我说谢谢你", "谢谢你"],
        "tags": ["温暖"],
        "lyrics": (
            "听我说谢谢你 因为有你 温暖了四季，"
            "谢谢你 感谢有你 世界更美丽"
        ),
    },
    {
        "id": "chongerfei",
        "title": "虫儿飞",
        "artist": "儿歌",
        "aliases": ["虫儿飞"],
        "tags": ["儿歌", "治愈"],
        "lyrics": (
            "黑黑的天空低垂 亮亮的繁星相随，"
            "虫儿飞 虫儿飞 你在思念谁"
        ),
    },
    {
        "id": "xiaoxingxing",
        "title": "小星星",
        "artist": "儿歌",
        "aliases": ["小星星", "一闪一闪亮晶晶"],
        "tags": ["儿歌"],
        "lyrics": (
            "一闪一闪亮晶晶 满天都是小星星，"
            "挂在天上放光明 好像许多小眼睛"
        ),
    },
    {
        "id": "liangzhilaohu",
        "title": "两只老虎",
        "artist": "儿歌",
        "aliases": ["两只老虎"],
        "tags": ["儿歌"],
        "lyrics": "两只老虎 两只老虎 跑得快 跑得快，一只没有耳朵 一只没有尾巴 真奇怪 真奇怪",
    },
    {
        "id": "xiaobaiuchuan",
        "title": "小白船",
        "artist": "儿歌",
        "aliases": ["小白船"],
        "tags": ["儿歌"],
        "lyrics": "蓝蓝的天空银河里 有只小白船，船上有棵桂花树 白兔在游玩",
    },
    {
        "id": "shishangzhiyou_mama",
        "title": "世上只有妈妈好",
        "artist": "儿歌",
        "aliases": ["世上只有妈妈好"],
        "tags": ["儿歌", "亲情"],
        "lyrics": "世上只有妈妈好，有妈的孩子像块宝，投进妈妈的怀抱 幸福享不了",
    },
    {
        "id": "yilushenghua",
        "title": "一路生花",
        "artist": "温奕心",
        "aliases": ["一路生花"],
        "tags": ["温暖"],
        "lyrics": "我希望许过的愿望一路生花，护送那时的梦抵挡过风沙",
    },
    {
        "id": "mohewu_ting",
        "title": "漠河舞厅",
        "artist": "柳爽",
        "aliases": ["漠河舞厅"],
        "tags": ["民谣"],
        "lyrics": "如果有时间 你会来看一看我吧，看大雪如何衰老 我的眼睛如何融化",
    },
]


# ---------------------------------------------------------------- 解析结果


class SongIntent:
    """点歌意图解析结果。"""

    __slots__ = ("action", "song", "text")

    def __init__(
        self,
        action: str | None,
        song: dict | None = None,
        text: str = "",
    ) -> None:
        self.action = action
        self.song = song
        self.text = text

    def __bool__(self) -> bool:
        return self.action is not None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"SongIntent(action={self.action!r}, "
            f"song={self.song.get('title') if self.song else None!r}, "
            f"text={self.text[:20]!r})"
        )


# ---------------------------------------------------------------- 服务


class SongService:
    """点歌服务：本地曲库 + 唱腔合成 + 内容缓存。"""

    def __init__(
        self,
        tts: TTSService,
        cache_dir: str | os.PathLike = "./data/song_cache",
        enabled: bool = True,
        max_duration: float = 55.0,
        sing_rate: str = "+8%",
        sing_pitch: str = "+35Hz",
        voice: str = "",
        max_lyrics_chars: int = 300,
        music: LXMusicProvider | None = None,
        music_timeout: float = 30.0,
    ) -> None:
        self.tts = tts
        self.enabled = bool(enabled)
        self.max_duration = max(5.0, float(max_duration))
        self.sing_rate = str(sing_rate or "+8%")
        self.sing_pitch = str(sing_pitch or "+35Hz")
        self.voice = str(voice or "")
        self.max_lyrics_chars = max(40, int(max_lyrics_chars))
        self.music = music
        self.music_timeout = max(5.0, float(music_timeout))

        self.cache_dir = Path(cache_dir)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover
            logger.error(f"点歌缓存目录创建失败: {exc}")

        self._index: dict[str, dict] = {}
        self._aliases: list[tuple[str, dict]] = []
        # 本地导入的歌曲列表
        self._local_songs: list[dict] = []
        self._build_index()
        # 自动加载本地音乐
        self._load_local_songs()
        self._prewarm_task: asyncio.Task | None = None
        self.stats: dict = {
            "requests": 0,
            "rendered": 0,
            "cache_hits": 0,
            "failures": 0,
            "music_downloads": 0,
            "last_seconds": 0.0,
        }

    # ------------------------------------------------------------ 曲库索引

    def _build_index(self) -> None:
        self._index = {song["id"]: song for song in SONG_LIBRARY}
        pairs: list[tuple[str, dict]] = []

        # 合并本地导入的歌曲
        for song in self._local_songs:
            if song["id"] not in self._index:
                self._index[song["id"]] = song
                pairs.append((song["title"].lower(), song))
                for alias in song.get("aliases", [song["title"]]):
                    pairs.append((alias.lower(), song))

        for song in SONG_LIBRARY:
            names = [song["title"], *song.get("aliases", [])]
            for name in names:
                normalized = str(name).strip().casefold()
                if normalized:
                    pairs.append((normalized, song))
        # 长别名优先，避免"小星星"被"星星"之类的短别名截胡
        pairs.sort(key=lambda item: len(item[0]), reverse=True)
        self._aliases = pairs

    def _load_local_songs(self) -> None:
        """自动加载本地音乐缓存目录中的歌曲。"""
        import os
        music_cache_dir = Path("./data/music_cache")
        if not music_cache_dir.exists():
            return

        for filename in os.listdir(music_cache_dir):
            if filename.lower().endswith(('.mp3', '.m4a', '.aac', '.flac', '.wav')):
                # 提取歌名（去掉扩展名和哈希后缀）
                name = os.path.splitext(filename)[0]
                if '.' in name:
                    name = name.rsplit('.', 1)[0]

                # 生成唯一ID
                import hashlib
                song_id = f"local_{hashlib.md5(filename.encode()).hexdigest()[:8]}"

                # 检测时长
                duration = 0.0
                if filename.lower().endswith('.mp3'):
                    try:
                        duration = mp3_duration(music_cache_dir / filename)
                    except:
                        duration = 0.0

                song = {
                    "id": song_id,
                    "title": name,
                    "artist": "本地导入",
                    "audio_path": str(music_cache_dir / filename),
                    "duration": duration,
                    "tags": ["本地导入"],
                    "aliases": [name, filename],
                    "is_local": True,
                }

                # 添加到本地歌曲列表
                if not any(s["id"] == song_id for s in self._local_songs):
                    self._local_songs.append(song)
                    logger.info(f"[音乐] 已加载本地歌曲: {name}")

        # 重新构建索引
        if self._local_songs:
            self._build_index()
            logger.info(f"[音乐] 已加载 {len(self._local_songs)} 首本地歌曲")

    def list_songs(self) -> list[dict]:
        all_songs = list(SONG_LIBRARY) + [s for s in self._local_songs if s["id"] not in {x["id"] for x in SONG_LIBRARY}]
        return [
            {
                "id": song["id"],
                "title": song["title"],
                "artist": song.get("artist", "本地导入"),
                "tags": list(song.get("tags", [])),
                "cached": self._is_cached(song),
                "is_local": song.get("is_local", False),
            }
            for song in all_songs
        ]

    def song_count(self) -> int:
        all_songs = list(SONG_LIBRARY) + [s for s in self._local_songs if s["id"] not in {x["id"] for x in SONG_LIBRARY}]
        return len(all_songs)

    def get_song(self, song_id: str) -> dict | None:
        return self._index.get(str(song_id or "").strip())

    def random_song(self, exclude_id: str = "") -> dict:
        all_songs = list(SONG_LIBRARY) + [s for s in self._local_songs if s["id"] not in {x["id"] for x in SONG_LIBRARY}]
        pool = [
            song for song in all_songs if song["id"] != str(exclude_id or "")
        ] or all_songs
        return random.choice(pool)

    def add_local_song(
        self,
        title: str,
        audio_path: str | Path,
        artist: str = "本地导入",
        duration: float | None = None,
    ) -> dict | None:
        """导入本地音频文件到点歌曲库。"""
        path = Path(audio_path)
        if not path.exists():
            logger.error(f"本地歌曲文件不存在: {path}")
            return None

        # 生成唯一ID
        import hashlib
        song_id = f"local_{hashlib.md5(path.name.encode()).hexdigest()[:8]}"

        # 自动检测时长
        if duration is None or duration <= 0:
            duration = mp3_duration(path) if path.suffix.lower() in ('.mp3', '.m4a', '.aac', '.flac', '.wav') else 0.0

        song = {
            "id": song_id,
            "title": title,
            "artist": artist,
            "audio_path": str(path),
            "duration": duration,
            "tags": ["本地导入"],
            "aliases": [title],
            "is_local": True,
        }

        # 添加到本地歌曲列表
        if not any(s["id"] == song_id for s in self._local_songs):
            self._local_songs.append(song)
            # 重新构建索引
            self._build_index()
            logger.info(f"已导入本地歌曲: {title} ({path.name})")
            return song

        logger.warning(f"歌曲已存在: {title}")
        return None

    def find_song(self, text: str) -> dict | None:
        """在文本中查找曲库歌曲（最长别名优先，支持部分匹配和繁简转换）。"""
        haystack = str(text or "").casefold()
        if not haystack:
            return None

        # 简繁/日文汉字映射
        # 格式: {原字符: [替换字符列表]}
        char_map = {
            '何': ['何', '为'],      # 日文何 = 中文为
            '為': ['为', '何'],      # 繁体为
            '爲': ['为', '何'],      # 古体为
            'う': ['wu', 'u'],
            'を': ['wo'],
            'が': ['ga'],
            'の': ['no', 'de'],
            'に': ['ni'],
            'へ': ['he'],
            'と': ['to'],
            'も': ['mo'],
            'は': ['wa', 'ha'],
            'し': ['shi'],
            'す': ['su'],
            'せ': ['se'],
            'そ': ['so'],
            'さ': ['sa'],
            'ま': ['ma'],
            'み': ['mi'],
            'む': ['mu'],
            'め': ['me'],
            'や': ['ya'],
            'ゆ': ['yu'],
            'よ': ['yo'],
            'ら': ['ra'],
            'り': ['ri'],
            'る': ['ru'],
            'れ': ['re'],
            'ろ': ['ro'],
            'わ': ['wa'],
            'ん': ['n'],
        }

        # 构建扩展搜索词
        expanded_haystack = haystack
        for cn_char, variants in char_map.items():
            if cn_char in haystack:
                for variant in variants:
                    expanded_haystack += f' {variant}'

        # 先尝试精确匹配
        for alias, song in self._aliases:
            if len(alias) < _MIN_ALIAS_LEN:
                continue
            if alias in haystack or alias in expanded_haystack:
                return song

        # 部分匹配
        for alias, song in self._aliases:
            if len(alias) < 2:
                continue
            if alias in haystack or haystack in alias:
                return song
            if alias in expanded_haystack or expanded_haystack in alias:
                return song

        # 从本地MP3文件名匹配
        import os
        music_cache_dir = Path("./data/music_cache")
        if music_cache_dir.exists():
            for filename in os.listdir(music_cache_dir):
                if filename.lower().endswith(('.mp3', '.m4a', '.aac', '.flac', '.wav')):
                    name = os.path.splitext(filename)[0]
                    if '.' in name:
                        name = name.rsplit('.', 1)[0]
                    name_lower = name.casefold()

                    # 直接匹配
                    if name_lower in haystack or haystack in name_lower:
                        return self._create_local_song_dict(filename, name, music_cache_dir)

                    # 扩展匹配
                    if name_lower in expanded_haystack or expanded_haystack in name_lower:
                        return self._create_local_song_dict(filename, name, music_cache_dir)

                    # 逐字符匹配（处理单字差异）
                    for cn_char, variants in char_map.items():
                        if cn_char in name_lower and any(v in haystack for v in variants):
                            return self._create_local_song_dict(filename, name, music_cache_dir)

        return None

    def _create_local_song_dict(self, filename: str, name: str, music_cache_dir: Path) -> dict:
        """创建本地歌曲字典。"""
        import hashlib
        song_id = f"local_{hashlib.md5(filename.encode()).hexdigest()[:8]}"
        duration = 0.0
        try:
            if filename.lower().endswith('.mp3'):
                duration = mp3_duration(music_cache_dir / filename)
        except:
            duration = 0.0

        return {
            "id": song_id,
            "title": name,
            "artist": "本地导入",
            "audio_path": str(music_cache_dir / filename),
            "duration": duration,
            "tags": ["本地导入"],
            "aliases": [name, filename],
            "is_local": True,
        }

    # ------------------------------------------------------------ 意图解析

    @staticmethod
    def _strip_fillers(text: str) -> str:
        result = str(text or "").strip()
        changed = True
        while changed and result:
            changed = False
            for filler in _FILLER_WORDS:
                if not filler:
                    continue
                if result == filler:
                    result = ""
                    changed = True
                    break
                if result.startswith(filler):
                    result = result[len(filler):]
                    changed = True
                if result.endswith(filler):
                    result = result[: -len(filler)]
                    changed = True
            result = result.strip(" 　,，.。!！?？~～:：、'\"“”‘’《》[]【】()（）")
        return result.strip()

    def resolve(self, content: str) -> SongIntent:
        """把一条用户消息解析成点歌意图。

        返回的 ``SongIntent.action`` 取值：
        ``"list"`` 查看歌单、``"play"`` 点指定歌、``"random"`` 随便来一首、
        ``"sing_text"`` 唱用户给的词、``None`` 不是点歌。
        """
        if not self.enabled:
            return SongIntent(None)

        text = str(content or "").strip()
        if not text:
            return SongIntent(None)
        lowered = text.casefold()

        list_hit = any(trigger in lowered for trigger in LIST_TRIGGERS)
        play_hit = any(trigger in lowered for trigger in PLAY_TRIGGERS)

        # "怎么点歌""有什么歌"这类是在问歌单，优先按歌单处理
        if list_hit:
            return SongIntent("list")

        # "唱《青花瓷》" 这类只带歌名不带触发词的说法
        direct_song = self.find_song(text)

        if not (play_hit or direct_song):
            return SongIntent(None)

        # 1) 文本里能直接找到歌名 -> 点这首歌
        if direct_song is not None:
            return SongIntent("play", song=direct_song)

        # 2) 去掉触发词与语气词，看剩下的内容
        remainder = text
        matched_trigger = ""
        for trigger in sorted(PLAY_TRIGGERS, key=len, reverse=True):
            if trigger.casefold() in remainder.casefold():
                idx = remainder.casefold().index(trigger.casefold())
                remainder = remainder[:idx] + remainder[idx + len(trigger):]
                matched_trigger = trigger
        remainder = self._strip_fillers(remainder)

        if not remainder or any(
            key in remainder.casefold() for key in RANDOM_TRIGGERS
        ):
            return SongIntent("random")

        # 3) 剩下的内容：
        #    - "唱歌/给我唱"这类 = 用户自填歌词 -> TTS 哼唱
        #    - 其余（点歌/来一首/想听）= 找真实歌曲 -> 音乐源在线下载
        #      （音乐源未启用时退回 TTS 哼唱兜底）
        if len(remainder) >= 2:
            lyric_trigger = matched_trigger in SING_LYRIC_TRIGGERS
            if not lyric_trigger and self.music is not None and self.music.enabled:
                return SongIntent("play_online", text=remainder)
            return SongIntent("sing_text", text=remainder)

        return SongIntent("random")

    # ------------------------------------------------------------ 合成

    def _cache_path(self, song: dict) -> Path | None:
        """按 TTS 的缓存规则查找这首歌是否已经合成过。"""
        return self.tts.cache_path_for(
            song.get("lyrics", ""),
            voice=self.voice or None,
            rate=self.sing_rate,
            pitch=self.sing_pitch,
            salt=f"song:{song['id']}",
        )

    def _is_cached(self, song: dict) -> bool:
        try:
            return self._cache_path(song) is not None
        except Exception:
            return False

    async def render_song(self, song: dict) -> Path | None:
        """合成（或下载）一首歌，返回音频路径。

        优先从音乐源下载真实歌曲（受超时预算约束），失败自动回退 TTS 哼唱，
        保证用户点歌永远有结果。
        本地导入的歌曲直接返回文件路径。
        """
        if not self.enabled or not song:
            return None
        self.stats["requests"] += 1

        # 1) 本地导入的歌曲直接返回
        if song.get("is_local") and song.get("audio_path"):
            path = Path(song["audio_path"])
            if path.exists():
                self.stats["cache_hits"] += 1
                duration = song.get("duration", 0.0)
                self.stats["last_seconds"] = round(float(duration), 2)
                logger.info(f"[音乐] 使用本地歌曲: {song.get('title')}")
                return path

        # 2) 音乐源真实歌曲（超时预算内）
        if self.music is not None and self.music.enabled:
            try:
                result = await asyncio.wait_for(
                    self.music.fetch(
                        song.get("title", ""),
                        song.get("artist", ""),
                    ),
                    timeout=self.music_timeout,
                )
            except asyncio.TimeoutError:
                result = None
                logger.warning(
                    f"[音乐] 下载《{song.get('title')}》超时，回退 TTS 哼唱"
                )
            except Exception as exc:
                result = None
                logger.warning(
                    f"[音乐] 下载《{song.get('title')}》失败，回退 TTS 哼唱: {exc}"
                )
            if result is not None:
                self.stats["music_downloads"] += 1
                self.stats["last_seconds"] = round(
                    float(result.get("seconds") or 0.0),
                    2,
                )
                return result["path"]

        # 2) TTS 哼唱兜底（先查缓存）
        cached = self._cache_path(song)
        if cached is not None:
            self.stats["cache_hits"] += 1
            return cached

        started = time.perf_counter()
        audio = await self.tts.synthesize(
            song.get("lyrics", ""),
            voice=self.voice or None,
            rate=self.sing_rate,
            pitch=self.sing_pitch,
            salt=f"song:{song['id']}",
        )
        if audio is None:
            self.stats["failures"] += 1
            return None

        elapsed = time.perf_counter() - started
        self.stats["rendered"] += 1
        self.stats["last_seconds"] = round(elapsed, 2)
        if self.tts.estimate_duration(audio) > self.max_duration + 15.0:
            logger.warning(
                f"歌曲音频偏长（{self.tts.estimate_duration(audio):.0f} 秒），"
                "发送时将自动改用文件形式"
            )
        return audio

    async def fetch_online(self, query: str, artist: str = "") -> dict | None:
        """在线下载任意歌名的真实歌曲（用于"点歌 XX"非曲库歌曲）。"""
        if self.music is None or not self.music.enabled:
            return None
        self.stats["requests"] += 1
        try:
            result = await asyncio.wait_for(
                self.music.fetch(query, artist),
                timeout=self.music_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[音乐] 下载《{query}》超时")
            return None
        except Exception as exc:
            logger.error(f"[音乐] 下载《{query}》异常: {exc}")
            return None
        if result is not None:
            self.stats["music_downloads"] += 1
        return result

    def estimate_duration(self, path) -> float:
        """估算任意音频时长：先按真实码率解析 MP3，失败再按 TTS 默认码率。"""
        real = mp3_duration(path)
        if real > 0:
            return real
        return self.tts.estimate_duration(path)

    async def render_lyrics(self, lyrics: str) -> Path | None:
        """把用户自填的歌词用唱腔唱出来。"""
        text = str(lyrics or "").strip()[: self.max_lyrics_chars]
        if not self.enabled or not text:
            return None
        self.stats["requests"] += 1
        started = time.perf_counter()
        audio = await self.tts.synthesize(
            text,
            voice=self.voice or None,
            rate=self.sing_rate,
            pitch=self.sing_pitch,
            salt="song:custom",
        )
        if audio is None:
            self.stats["failures"] += 1
            return None
        self.stats["rendered"] += 1
        self.stats["last_seconds"] = round(time.perf_counter() - started, 2)
        return audio

    async def render_speech(self, text: str, voice: str = "") -> Path | None:
        """普通朗读（用于语音回复 / 试听）。"""
        if not self.enabled:
            return None
        return await self.tts.synthesize(
            text,
            voice=voice or self.voice or None,
            salt="song:speech",
        )

    async def preview(self, text: str, voice: str = "") -> Path | None:
        """Web 试听：直接用唱腔合成一小段。"""
        if not text:
            return None
        if not self.tts.enabled:
            return None
        return await self.tts.synthesize(
            text,
            voice=voice or None,
            rate=self.sing_rate,
            pitch=self.sing_pitch,
            salt="song:preview",
        )

    # ------------------------------------------------------------ 预热

    def start_prewarm(self, limit: int = 6) -> None:
        """后台预热前 N 首热门歌曲，让首次点歌也接近秒回。"""
        if not self.enabled or not self.tts.available():
            return
        if self._prewarm_task and not self._prewarm_task.done():
            return
        self._prewarm_task = asyncio.create_task(
            self._prewarm_loop(max(1, int(limit))),
            name="song-prewarm",
        )

    async def _prewarm_loop(self, limit: int) -> None:
        done = 0
        for song in SONG_LIBRARY:
            if done >= limit:
                break
            if self._is_cached(song):
                continue
            try:
                await self.render_song(song)
                done += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"预热歌曲失败 {song.get('title')}: {exc}")
            await asyncio.sleep(0.5)  # 错峰，避免和正常回复抢网络
        if done:
            logger.info(f"点歌预热完成，新合成 {done} 首")

    async def stop(self) -> None:
        if self._prewarm_task and not self._prewarm_task.done():
            self._prewarm_task.cancel()
            try:
                await self._prewarm_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.music is not None:
            try:
                await self.music.close()
            except Exception:
                pass

    # ------------------------------------------------------------ 状态

    def status(self) -> dict:
        total = int(self.stats["requests"])
        hit_rate = (
            round(int(self.stats["cache_hits"]) * 100.0 / total, 1)
            if total
            else 0.0
        )
        cached_count = sum(1 for song in SONG_LIBRARY if self._is_cached(song))
        cache_size = 0
        try:
            cache_size = int(self.tts.cache_info().get("size_mb", 0) * 1024 * 1024)
        except Exception:
            cache_size = 0
        return {
            "enabled": self.enabled,
            "song_count": self.song_count(),
            "cached_count": cached_count,
            "cache_size_mb": round(cache_size / 1024.0 / 1024.0, 2),
            "sing_rate": self.sing_rate,
            "sing_pitch": self.sing_pitch,
            "max_duration": self.max_duration,
            "requests": total,
            "rendered": int(self.stats["rendered"]),
            "cache_hits": int(self.stats["cache_hits"]),
            "failures": int(self.stats["failures"]),
            "hit_rate": hit_rate,
            "last_seconds": float(self.stats["last_seconds"]),
            "music_downloads": int(self.stats["music_downloads"]),
            "music": (
                self.music.status()
                if self.music is not None
                else {"enabled": False}
            ),
            "prewarm_running": bool(
                self._prewarm_task and not self._prewarm_task.done()
            ),
        }
