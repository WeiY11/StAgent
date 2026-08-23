"""Agent defaults and model tier trigger constants."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Agent settings
# ---------------------------------------------------------------------------

AGENT_MAX_ITERATIONS = 15
AGENT_DEFAULT_TIMEOUT_SEC = 300
AGENT_MAX_TOOL_CALLS_PER_TURN = 10
AGENT_CONTEXT_WINDOW_TOKENS = 128_000
AGENT_MAX_OUTPUT_TOKENS = 4096
AGENT_BROWSER_POOL_SIZE = 5

# ---------------------------------------------------------------------------
# Model tier triggers (keywords → tier routing)
# ---------------------------------------------------------------------------

TIER1_TRIGGERS = [
    "plan", "analyze", "report", "compare", "evaluate",
    "reason", "generate report", "审核", "分析", "评估", "规划",
]
TIER2_TRIGGERS = [
    "classify", "extract", "summarize", "format", "translate",
    "tag", "分类", "提取", "摘要", "格式化", "翻译",
]
