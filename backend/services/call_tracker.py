"""
AI 调用追踪器
==============
全局记录每一次 AI API 调用，不管来自监测、提取、生成还是自检。
解决"钱在掉却不知道花哪"的问题——所有烧钱的地方统一记账。

用法：在任何真实调用 AI 的地方调用 track_call(platform, ok, scene)。
数据先存内存缓冲区，由 main.py 定期 flush 到数据库。
"""
import threading
from datetime import datetime, timezone, timedelta

# 各平台每次调用的估算成本（元）。便宜的国产模型 vs 贵的海外模型。
COST_PER_CALL = {
    "deepseek": 0.003, "qwen": 0.003, "doubao": 0.003,
    "kimi": 0.003, "wenxin": 0.003,
    "chatgpt": 0.03, "openai": 0.03, "gemini": 0.01,
    "claude": 0.03, "anthropic": 0.03, "perplexity": 0.01,
}

# 场景标签（让你知道钱花在哪个功能上）
SCENE_LABELS = {
    "monitor": "品牌监测",
    "extract": "品牌提取",
    "questions": "生成问题集",
    "content": "生成内容",
    "opportunity": "关键词分析",
    "check_keys": "密钥自检",
    "other": "其他",
}

_lock = threading.Lock()
# 内存缓冲：[(platform, scene, ok)]，由 main.py 定期 flush
_buffer = []


def track_call(platform: str, ok: bool = True, scene: str = "other"):
    """记录一次 AI 调用。线程安全，极轻量。"""
    try:
        with _lock:
            _buffer.append((platform or "unknown", scene, bool(ok)))
    except Exception:
        pass  # 记账失败绝不能影响主流程


def drain():
    """取出并清空缓冲区，返回聚合后的记录列表。供 main.py flush 到数据库。"""
    with _lock:
        items = _buffer[:]
        _buffer.clear()
    return items


def cost_of(platform: str) -> float:
    """单次调用的估算成本。"""
    return COST_PER_CALL.get((platform or "").lower(), 0.01)
