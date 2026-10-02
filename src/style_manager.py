"""语言风格管理：加载/切换/自定义 prompt"""

from pathlib import Path
from dataclasses import dataclass, field

import yaml
from loguru import logger


@dataclass
class ChatStyle:
    """聊天风格定义"""
    name: str                          # 风格标识
    display_name: str                  # 显示名称
    description: str                   # 风格描述
    system_prompt: str                 # 系统提示词
    temperature: float = 0.7           # 创造性参数
    max_tokens: int = 500              # 最大回复长度
    examples: list[dict] = field(default_factory=list)  # 示例对话


class StyleManager:
    """风格管理器"""

    def __init__(
        self,
        styles_dir: str = "styles",
        custom_dir: str = "styles/custom",
        locked_name: str | None = None,
    ):
        self.styles_dir = Path(styles_dir)
        self.custom_dir = Path(custom_dir)
        self._locked_name = locked_name
        self._styles: dict[str, ChatStyle] = {}
        self._active: str = "default"

    def load_all(self) -> None:
        """加载所有风格文件"""
        self._styles.clear()

        # 加载内置风格
        if self.styles_dir.exists():
            for f in self.styles_dir.glob("*.yaml"):
                self._load_style_file(f)

        # 加载自定义风格
        if self.custom_dir.exists():
            for f in self.custom_dir.glob("*.yaml"):
                self._load_style_file(f)

        # 确保至少有默认风格
        if "default" not in self._styles:
            self._styles["default"] = self._default_style()

        if self._locked_name:
            if self._locked_name not in self._styles:
                raise RuntimeError(f"锁定人设文件不存在: {self._locked_name}")
            self._active = self._locked_name

        logger.info(f"已加载 {len(self._styles)} 种风格: {list(self._styles.keys())}")

    def _load_style_file(self, path: Path) -> None:
        """加载单个风格文件"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not data or "name" not in data:
                logger.warning(f"风格文件格式错误: {path}")
                return

            style = ChatStyle(
                name=data["name"],
                display_name=data.get("display_name", data["name"]),
                description=data.get("description", ""),
                system_prompt=data.get("system_prompt", ""),
                temperature=data.get("temperature", 0.7),
                max_tokens=data.get("max_tokens", 500),
                examples=data.get("examples", []),
            )
            self._styles[style.name] = style
        except Exception as e:
            logger.error(f"加载风格文件失败 {path}: {e}")

    def get(self, name: str | None = None) -> ChatStyle:
        """获取指定风格，不存在则返回默认风格"""
        target = self._locked_name or name or self._active
        style = self._styles.get(target)
        if not style:
            logger.warning(f"风格 '{target}' 不存在，使用默认风格")
            style = self._styles.get("default", self._default_style())
        return style

    def get_active(self) -> ChatStyle:
        """获取当前激活的风格"""
        return self.get(self._active)

    def set_active(self, name: str) -> bool:
        """切换激活风格"""
        if self._locked_name and name != self._locked_name:
            logger.warning(f"人设已永久锁定为 '{self._locked_name}'，拒绝切换到 '{name}'")
            return False
        if name in self._styles:
            self._active = name
            logger.info(f"切换风格: {name}")
            return True
        logger.warning(f"风格 '{name}' 不存在")
        return False

    def list_styles(self) -> list[dict]:
        """列出所有可用风格"""
        return [
            {
                "name": s.name,
                "display_name": s.display_name,
                "description": s.description,
                "active": s.name == self._active,
            }
            for s in self._styles.values()
        ]

    def add_style(self, style: ChatStyle) -> None:
        """添加新风格"""
        self._styles[style.name] = style
        # 保存到自定义风格目录
        self.custom_dir.mkdir(parents=True, exist_ok=True)
        path = self.custom_dir / f"{style.name}.yaml"
        data = {
            "name": style.name,
            "display_name": style.display_name,
            "description": style.description,
            "system_prompt": style.system_prompt,
            "temperature": style.temperature,
            "max_tokens": style.max_tokens,
            "examples": style.examples,
        }
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
        logger.info(f"添加风格: {style.name}")

    def remove_style(self, name: str) -> bool:
        """删除自定义风格（内置风格不可删除）"""
        if name == self._locked_name:
            logger.warning(f"不能删除锁定人设: {name}")
            return False
        if name == "default":
            logger.warning("不能删除默认风格")
            return False

        # 检查是否为内置风格
        builtin_path = self.styles_dir / f"{name}.yaml"
        if builtin_path.exists():
            logger.warning(f"不能删除内置风格: {name}")
            return False

        # 删除自定义风格文件
        custom_path = self.custom_dir / f"{name}.yaml"
        if custom_path.exists():
            custom_path.unlink()

        if name in self._styles:
            del self._styles[name]

        # 如果删除的是当前风格，切换到默认
        if self._active == name:
            self._active = "default"

        logger.info(f"删除风格: {name}")
        return True

    @property
    def active_name(self) -> str:
        return self._active

    @property
    def locked_name(self) -> str | None:
        return self._locked_name

    @property
    def is_locked(self) -> bool:
        return bool(self._locked_name)

    @staticmethod
    def _default_style() -> ChatStyle:
        """默认风格（兜底）"""
        return ChatStyle(
            name="default",
            display_name="默认",
            description="平衡、友好的通用风格",
            system_prompt=(
                "你是一个友好的AI助手，在微信群聊中与大家交流。"
                "请用简洁自然的中文回复，语气友好亲切。"
                "回复不要太长，适合群聊场景。"
            ),
            temperature=0.7,
            max_tokens=300,
        )
