"""WeChat Work channel for LeAgent."""

from .channel import (
    WECHAT_WORK_API_BASE,
    WECHAT_WORK_MAX_TEXT_LENGTH,
    WECHAT_WORK_TOKEN_TTL_SECONDS,
    WeChatWorkChannel,
)

__all__ = [
    "WeChatWorkChannel",
    "WECHAT_WORK_API_BASE",
    "WECHAT_WORK_MAX_TEXT_LENGTH",
    "WECHAT_WORK_TOKEN_TTL_SECONDS",
]
