"""安全模块：限流器、熔断器、凭据加密、输入清洗"""

import time
import re
import threading
from enum import Enum
from dataclasses import dataclass, field
from loguru import logger


class CircuitState(Enum):
    CLOSED = "closed"      # 正常
    OPEN = "open"          # 熔断中
    HALF_OPEN = "half_open"  # 半开（试探）


@dataclass
class TokenBucket:
    """令牌桶限流器"""
    capacity: int = 20       # 桶容量
    refill_rate: float = 1.0  # 每秒补充令牌数
    tokens: float = field(init=False)
    last_refill: float = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self):
        self.tokens = float(self.capacity)
        self.last_refill = time.monotonic()

    def acquire(self) -> bool:
        """尝试获取一个令牌，成功返回 True"""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now

            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False

    @property
    def available_tokens(self) -> int:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            current = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            return int(current)


class CircuitBreaker:
    """熔断器：连续失败 N 次后短暂暂停，并快速恢复试探"""

    def __init__(self, failure_threshold: int = 8, recovery_timeout: float = 5):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = max(1.0, min(float(recovery_timeout), 5.0))
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    logger.info("熔断器进入半开状态，尝试恢复")
            return self._state

    def record_success(self) -> None:
        """记录成功调用"""
        with self._lock:
            self._failure_count = 0
            if self._state != CircuitState.CLOSED:
                self._state = CircuitState.CLOSED
                logger.info("熔断器恢复正常")

    def record_failure(self) -> None:
        """记录失败调用"""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._failure_count >= self.failure_threshold:
                if self._state != CircuitState.OPEN:
                    self._state = CircuitState.OPEN
                    logger.warning(
                        f"熔断器触发：连续 {self._failure_count} 次失败，"
                        f"暂停 {self.recovery_timeout} 秒"
                    )

    def record_transient_failure(self) -> None:
        """记录超时等瞬时故障，但不把机器人长时间锁死。"""
        with self._lock:
            self._last_failure_time = time.monotonic()
            if self._state == CircuitState.OPEN:
                self._state = CircuitState.HALF_OPEN
                self._failure_count = 0
                logger.info("检测到瞬时超时，熔断器立即进入恢复试探")

    def allow_request(self) -> bool:
        """是否允许发起请求"""
        state = self.state
        if state == CircuitState.CLOSED:
            return True
        if state == CircuitState.HALF_OPEN:
            return True  # 半开状态允许试探
        return False

    def reset(self) -> None:
        """手动重置熔断器"""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            logger.info("熔断器已手动重置")


class InputSanitizer:
    """输入清洗：防注入、防超长"""

    MAX_LENGTH = 4000  # 单条消息最大字符数

    # 危险模式列表
    DANGEROUS_PATTERNS = [
        r"(?i)(system|assistant|user)\s*:",  # prompt 注入
        r"(?i)ignore\s+(previous|above)\s+instructions",  # 指令覆盖
        r"(?i)you\s+are\s+now\s+",  # 角色篡改
    ]

    @classmethod
    def sanitize(cls, text: str) -> str:
        """清洗输入文本"""
        if not text:
            return ""

        # 去除首尾空白
        text = text.strip()

        # 截断超长内容
        if len(text) > cls.MAX_LENGTH:
            text = text[:cls.MAX_LENGTH]
            logger.warning(f"消息被截断至 {cls.MAX_LENGTH} 字符")

        # 移除控制字符（保留换行和制表符）
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)

        return text

    @classmethod
    def check_injection(cls, text: str) -> bool:
        """检查是否包含注入模式，返回 True 表示检测到危险"""
        for pattern in cls.DANGEROUS_PATTERNS:
            if re.search(pattern, text):
                logger.warning(f"检测到潜在注入: {pattern}")
                return True
        return False

    @classmethod
    def sanitize_for_display(cls, text: str) -> str:
        """清洗用于显示的文本（防 XSS）"""
        text = text.replace("&", "&amp;")
        text = text.replace("<", "&lt;")
        text = text.replace(">", "&gt;")
        text = text.replace('"', "&quot;")
        text = text.replace("'", "&#x27;")
        return text


class SecurityManager:
    """安全管理器：组合限流、熔断、清洗"""

    def __init__(
        self,
        rate_limit: int = 20,
        circuit_breaker_threshold: int = 8,
        circuit_breaker_timeout: float = 5,
    ):
        self.rate_limiter = TokenBucket(
            capacity=rate_limit,
            refill_rate=rate_limit / 60.0,  # 每秒补充
        )
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=circuit_breaker_threshold,
            recovery_timeout=circuit_breaker_timeout,
        )
        self.sanitizer = InputSanitizer()

    def can_proceed(self) -> bool:
        """检查是否可以发起 AI 调用"""
        if not self.circuit_breaker.allow_request():
            logger.debug("熔断器阻止请求")
            return False
        if not self.rate_limiter.acquire():
            logger.debug("限流器阻止请求")
            return False
        return True

    def record_success(self) -> None:
        self.circuit_breaker.record_success()

    def record_failure(self) -> None:
        self.circuit_breaker.record_failure()

    def record_transient_failure(self) -> None:
        self.circuit_breaker.record_transient_failure()

    def process_input(self, text: str) -> tuple[str, bool]:
        """处理输入，返回 (清洗后文本, 是否安全)"""
        sanitized = self.sanitizer.sanitize(text)
        is_safe = not self.sanitizer.check_injection(sanitized)
        return sanitized, is_safe

    @property
    def status(self) -> dict:
        return {
            "circuit_state": self.circuit_breaker.state.value,
            "available_tokens": self.rate_limiter.available_tokens,
        }
