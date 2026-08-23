"""Workflow, rule type, and business module constants."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Rule types
# ---------------------------------------------------------------------------

RULE_TYPES = [
    "compare",
    "date_range",
    "threshold",
    "contains_all",
    "date_diff",
    "regex",
    "in_set",
    "custom_expr",
    "llm_judge",
]

# ---------------------------------------------------------------------------
# Workflow node types
# ---------------------------------------------------------------------------

WORKFLOW_NODE_TYPES = [
    "start",
    "end",
    "tool_call",
    "llm_call",
    "condition",
    "parallel",
    "loop",
    "human_review",
    "sub_workflow",
    "wait_timer",
    "error_handler",
]

# ---------------------------------------------------------------------------
# Business modules
# ---------------------------------------------------------------------------

BUSINESS_MODULES = {
    "finance": {
        "code": "F",
        "name": "财务模块",
        "name_en": "Finance",
        "workflows": ["F-001", "F-002", "F-003", "F-004"],
    },
    "hr": {
        "code": "H",
        "name": "人力模块",
        "name_en": "Human Resources",
        "workflows": ["H-001", "H-002", "H-003", "H-004", "H-005", "H-006", "H-007", "H-008"],
    },
    "logistics": {
        "code": "L",
        "name": "后勤模块",
        "name_en": "Logistics",
        "workflows": ["L-001"],
    },
    "research": {
        "code": "R",
        "name": "科研模块",
        "name_en": "Research",
        "workflows": ["R-001", "R-002"],
    },
    "education": {
        "code": "E",
        "name": "教育模块",
        "name_en": "Education",
        "workflows": ["E-001", "E-002"],
    },
    "qianhai": {
        "code": "Q",
        "name": "前海模块",
        "name_en": "Qianhai Policy",
        "workflows": ["Q-001"],
    },
}
