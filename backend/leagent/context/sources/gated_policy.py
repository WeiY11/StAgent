"""Relevance-gated policy sources.

These sources reuse the existing ``policies/<name>.md`` templates as the single
source of truth, but only inject them into the system prompt when the turn is
actually about their domain (decided by a :class:`~leagent.context.relevance.RelevanceGate`).
This keeps heavy, domain-specific manuals (canvas/GenUI rules, document-font
guidance) out of the always-on prompt while still delivering them automatically
the moment a relevant turn arrives — or when the runtime harness opts in.

Contrast with :mod:`leagent.context.sources.policies`, which loads the small,
universally-relevant policies (``file_access``, ``database_tool``, ``human_gate``)
on every turn.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar

import structlog

from leagent.context.relevance import RelevanceGate
from leagent.context.sources import SOURCE_REGISTRY
from leagent.context.sources.policies import _get_enabled_tool_names, _substitute
from leagent.context.types import ContextBlock, ContextScope, RenderTarget

if TYPE_CHECKING:
    from leagent.context.sources.base import ResolveContext

logger = structlog.get_logger(__name__)


class GatedPolicySource:
    """Base class: load a fixed set of policy snippets only when relevant.

    Subclasses set :attr:`id`, :attr:`priority`, :attr:`gate`, and
    :attr:`policy_names`. The block is assembled from the existing
    ``policies/<name>`` markdown templates, with template-variable substitution
    and the same ``requires_tools`` filtering used by the always-on policies
    source.
    """

    kind: str = "identity"
    scope: ContextScope = ContextScope.TURN
    weight: float = 1.0
    render_target: RenderTarget = RenderTarget.SYSTEM

    # Subclass overrides
    id: ClassVar[str] = ""
    priority: ClassVar[int] = 1100
    gate: ClassVar[RelevanceGate] = RelevanceGate(name="")
    policy_names: ClassVar[tuple[str, ...]] = ()

    def _is_relevant(self, ctx: ResolveContext) -> bool:
        return self.gate.matches(
            ctx.query,
            workflow_hint=ctx.workflow_hint,
            template_vars=ctx.template_vars,
        )

    def invalidation_key(self, ctx: ResolveContext) -> str:
        return f"{self.id}:{self._is_relevant(ctx)}"

    async def resolve(self, ctx: ResolveContext) -> ContextBlock | None:
        try:
            if not self._is_relevant(ctx):
                return None
            if ctx.prompt_registry is None:
                logger.warning("gated_policy_resolve_no_registry", source=self.id)
                return None

            variables = {
                "agent_name": ctx.agent_id,
                "cwd": ctx.cwd,
                **ctx.template_vars,
            }
            enabled_tool_names = _get_enabled_tool_names(ctx)

            bodies: list[str] = []
            for policy_name in self.policy_names:
                try:
                    policy = ctx.prompt_registry.get(
                        f"policies/{policy_name}",
                        variant="default",
                    )
                except Exception:
                    logger.warning(
                        "gated_policy_snippet_missing",
                        source=self.id,
                        policy=policy_name,
                    )
                    continue

                if (
                    policy.requires_tools
                    and enabled_tool_names is not None
                    and not any(t in enabled_tool_names for t in policy.requires_tools)
                ):
                    logger.debug(
                        "gated_policy_skipped_missing_tools",
                        source=self.id,
                        policy=policy_name,
                        requires=policy.requires_tools,
                    )
                    continue

                body = _substitute(policy.body, variables).strip()
                if body:
                    bodies.append(body)

            if not bodies:
                return None

            body = "\n\n".join(bodies)
            return ContextBlock(
                source_id=self.id,
                kind=self.kind,
                render_target=self.render_target,
                body=body,
                tokens=ContextBlock.approx_tokens(body),
                cost=ContextBlock.approx_tokens(body),
                signature=ContextBlock.content_signature(self.id, body),
                priority=self.priority,
                weight=self.weight,
                metadata={"count": len(bodies), "gated": True},
            )
        except Exception:
            logger.exception("gated_policy_resolve_failed", source=self.id)
            return None


class CanvasGuideSource(GatedPolicySource):
    """Canvas / GenUI authoring rules, loaded only for visual-deliverable turns."""

    id = "canvas_guide"
    priority = 1150
    policy_names = (
        "canvas_routing",
        "genui_components",
        "blob_staging",
    )
    gate = RelevanceGate(
        name="canvas_guide",
        hints=(
            "canvas",
            "genui",
            "gen ui",
            "dashboard",
            "poster",
            "slide",
            "deck",
            "chart",
            "kpi",
            "emit_ui_tree",
            "emit_ui_patch",
            "canvas_publish",
            "htmlframe",
            "html frame",
            "html mode",
            "html 模式",
            "webpage",
            "web page",
            "landing page",
            "网页",
            "落地页",
            "画布",
            "卡片",
            "看板",
            "海报",
            "仪表盘",
            "幻灯片",
            "图表",
            # Follow-up redesign / skill polish (same session as first canvas publish)
            "frontend-design",
            "frontend_design",
            "优化设计",
            "改版",
            "美化",
            "重新设计",
            "优化一下设计",
            "改一下设计",
            "redesign",
            "restyle",
        ),
        opt_in_keys=("canvas_guide", "enable_canvas"),
    )


CANVAS_INTENT_MAX_OUTPUT_TOKENS = 32_768

_CANVAS_STICKY_TOOLS = frozenset({"canvas_publish", "get_html_canvas_guide"})


def _messages_imply_canvas_followup(messages: list[Any] | None) -> bool:
    """True when recent transcript already did canvas HTML / guide work."""
    if not messages:
        return False
    for message in messages[-16:]:
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                name = tc.get("name")
                if not name and isinstance(tc.get("function"), dict):
                    name = tc["function"].get("name")
                if name in _CANVAS_STICKY_TOOLS:
                    return True
        content = message.get("content")
        if not isinstance(content, str):
            continue
        low = content.lower()
        if "/api/v1/canvas/preview" in low or "canvas_publish" in low:
            return True
        if '"preview_path"' in low and "canvas" in low:
            return True
    return False


def resolve_canvas_intent_max_output_tokens(
    query: str | None,
    *,
    base: int | None,
    messages: list[Any] | None = None,
) -> int | None:
    """Bump ``max_output_tokens`` for canvas/GenUI turns (and sticky follow-ups).

    Uses :class:`CanvasGuideSource`'s keyword gate, plus a light transcript scan
    so ``优化一下设计`` after a prior ``canvas_publish`` still gets the 32k cap.
    """
    hit = CanvasGuideSource.gate.matches(query or "") or _messages_imply_canvas_followup(messages)
    if not hit:
        return base
    if base is None:
        return CANVAS_INTENT_MAX_OUTPUT_TOKENS
    return max(base, CANVAS_INTENT_MAX_OUTPUT_TOKENS)


class ChartGuideSource(GatedPolicySource):
    """Professional chart mode guide, loaded only for chart/visualization turns."""

    id = "chart_guide"
    priority = 1140
    policy_names = ("chart_guide",)
    gate = RelevanceGate(
        name="chart_guide",
        hints=(
            "chart",
            "plot",
            "graph",
            "visualiz",
            "visualis",
            "histogram",
            "boxplot",
            "box plot",
            "violin",
            "heatmap",
            "scatter",
            "candlestick",
            "waterfall",
            "sankey",
            "treemap",
            "sunburst",
            "funnel",
            "gauge",
            "pareto",
            "radar",
            "contour",
            "regression",
            "ecdf",
            "prochart",
            "chart_generator",
            "图表",
            "画图",
            "绘图",
            "可视化",
            "柱状图",
            "折线图",
            "饼图",
            "散点图",
            "直方图",
            "箱线图",
            "小提琴图",
            "热力图",
            "K线",
            "k线",
            "蜡烛图",
            "瀑布图",
            "桑基图",
            "漏斗图",
            "雷达图",
            "仪表盘图",
        ),
        opt_in_keys=("chart_guide", "enable_charts"),
    )


# Per-attachment summary line cap and how many attachments we summarise at most.
_TABULAR_SUMMARY_MAX_CHARS = 400
_TABULAR_SUMMARY_MAX_ATTACHMENTS = 10


async def _load_tabular_snapshots(ctx: ResolveContext) -> list[tuple[str, dict[str, Any]]]:
    """Collect ``(filename, tabular_snapshot)`` pairs for the current session.

    Snapshots are built at upload time by the session manager
    (``SessionAttachment.extra["tabular_snapshot"]``); this only reads them.
    """
    if ctx.session_manager is None or ctx.session_id is None:
        return []
    try:
        attachments = await ctx.session_manager.list_attachments(ctx.session_id, user_id=ctx.user_id)
    except Exception:
        logger.debug("tabular_snapshot_lookup_failed", exc_info=True)
        return []
    pairs: list[tuple[str, dict[str, Any]]] = []
    for att in attachments:
        extra = getattr(att, "extra", None)
        snapshot = extra.get("tabular_snapshot") if isinstance(extra, dict) else None
        if isinstance(snapshot, dict):
            pairs.append((getattr(att, "filename", "") or "attachment", snapshot))
    return pairs[:_TABULAR_SUMMARY_MAX_ATTACHMENTS]


def _summarise_tabular_snapshot(filename: str, snapshot: dict[str, Any]) -> str:
    """Render a one-line dataset summary, capped at 400 chars per attachment."""
    if snapshot.get("hint") == "large_file":
        size = snapshot.get("file_size")
        size_txt = f"~{size / (1024 * 1024):.0f}MB" if isinstance(size, (int, float)) else "oversized"
        line = f"- {filename}: large file ({size_txt}), snapshot skipped — process in chunks via code_execution."
        return line[:_TABULAR_SUMMARY_MAX_CHARS]
    columns = snapshot.get("columns") or []
    col_parts = [f"{c.get('name')}({c.get('dtype')})" for c in columns if isinstance(c, dict)]
    approx = "~" if snapshot.get("truncated") else ""
    line = f"- {filename}: {approx}{snapshot.get('row_count_estimate')} rows; {len(col_parts)} columns: " + ", ".join(
        col_parts
    )
    if len(line) > _TABULAR_SUMMARY_MAX_CHARS:
        line = line[: _TABULAR_SUMMARY_MAX_CHARS - 1] + "…"
    return line


class DataAnalysisGuideSource(GatedPolicySource):
    """Data-analysis conversation mode guide, loaded for tabular-data turns.

    Besides the keyword/opt-in gate, a session that carries tabular attachments
    (CSV/Excel uploads with a ``tabular_snapshot``) deterministically opens the
    gate, and the upload-time schema summaries are appended to the block so the
    agent sees each dataset's shape without re-reading the files.
    """

    id = "data_analysis_guide"
    priority = 1145
    policy_names = ("data_analysis_guide",)
    gate = RelevanceGate(
        name="data_analysis_guide",
        hints=(
            "analyze this data",
            "analyse this data",
            "data analysis",
            "data profiling",
            "data quality",
            "exploratory",
            "eda",
            "dataset",
            "dataframe",
            "pivot",
            "group by",
            "groupby",
            "correlation",
            "data_profile",
            "data_clean",
            "data_aggregate",
            "data_transform",
            "sql_query",
            "csv",
            "xlsx",
            "spreadsheet",
            "数据分析",
            "分析数据",
            "分析这个",
            "分析一下",
            "数据探索",
            "数据画像",
            "数据质量",
            "数据清洗",
            "数据集",
            "透视",
            "分组统计",
            "相关性",
            "表格数据",
            "电子表格",
        ),
        opt_in_keys=("data_analysis_guide", "enable_data_analysis"),
    )

    async def resolve(self, ctx: ResolveContext) -> ContextBlock | None:
        snapshots = await _load_tabular_snapshots(ctx)
        if snapshots:
            # Tabular attachments force the gate open regardless of query
            # wording. Copy template_vars so concurrent sources are unaffected.
            ctx = replace(
                ctx,
                template_vars={**ctx.template_vars, "enable_data_analysis": True},
            )
        block = await super().resolve(ctx)
        if block is None or not snapshots:
            return block

        summary = "\n".join(_summarise_tabular_snapshot(name, snap) for name, snap in snapshots)
        body = (
            f"{block.body}\n\n### Attached tabular datasets\n"
            "Upload-time snapshots (schema + row estimate) of this session's "
            "tabular attachments:\n"
            f"{summary}"
        )
        return ContextBlock(
            source_id=block.source_id,
            kind=block.kind,
            render_target=block.render_target,
            body=body,
            tokens=ContextBlock.approx_tokens(body),
            cost=ContextBlock.approx_tokens(body),
            signature=ContextBlock.content_signature(self.id, body),
            priority=block.priority,
            weight=block.weight,
            metadata={**block.metadata, "tabular_snapshots": len(snapshots)},
        )


class DocumentFontsSource(GatedPolicySource):
    """Mixed CJK/Latin font guidance, loaded only for document-generation turns."""

    id = "document_fonts"
    priority = 1100
    policy_names = ("document_fonts",)
    gate = RelevanceGate(
        name="document_fonts",
        hints=(
            "pdf",
            "docx",
            "pptx",
            "xlsx",
            "excel",
            "word document",
            "powerpoint",
            "reportlab",
            "font",
            "字体",
            "报告",
            "文档",
            "幻灯片",
            "document_generate",
            "slides_generate",
            "excel_generator",
        ),
        opt_in_keys=("document_fonts", "enable_fonts"),
    )


class DocumentGenerationSource(GatedPolicySource):
    """Document/slide authoring quality guide, loaded for doc-generation turns."""

    id = "document_generation"
    priority = 1105
    policy_names = ("document_generation",)
    gate = RelevanceGate(
        name="document_generation",
        hints=(
            "pdf",
            "docx",
            "pptx",
            "word document",
            "powerpoint",
            "presentation",
            "slide",
            "deck",
            "report",
            "whitepaper",
            "proposal",
            "document_generate",
            "slides_generate",
            "theme_designer",
            "document_template",
            "报告",
            "文档",
            "白皮书",
            "方案书",
            "幻灯片",
            "演示文稿",
            "简报",
            "模板",
            "主题",
        ),
        opt_in_keys=("document_generation", "enable_docgen"),
    )


class EmailToolSource(GatedPolicySource):
    """Outbound SMTP settings and sending — loaded for mail-related turns."""

    id = "email_tool"
    priority = 1100
    policy_names = ("email_tool",)
    gate = RelevanceGate(
        name="email_tool",
        hints=(
            "email",
            "e-mail",
            "mail",
            "smtp",
            "outbound mail",
            "send email",
            "mail settings",
            "mail config",
            "email config",
            "发件",
            "发信",
            "邮件",
            "邮箱",
            "smtp",
            "邮件配置",
            "邮件设置",
            "发件邮箱",
            "测试邮件",
            "邮件连接",
            "LEAGENT_SMTP",
        ),
        opt_in_keys=("email_tool", "enable_email"),
    )


class SettingsSetupSource(GatedPolicySource):
    """Env secrets / MCP / channels setup via configure_settings."""

    id = "settings_setup"
    priority = 1100
    policy_names = ("settings_setup",)
    gate = RelevanceGate(
        name="settings_setup",
        hints=(
            "configure",
            "configuration",
            "settings",
            "api key",
            "api_key",
            "secret",
            "token",
            "mcp",
            "mcp server",
            "dingtalk",
            "feishu",
            "lark",
            "wechat work",
            "wecom",
            "webhook",
            "smtp",
            "web search",
            "bing",
            "searxng",
            "deepseek",
            "openai key",
            "anthropic",
            "dashscope",
            "configure_settings",
            "环境密钥",
            "环境变量",
            "配置",
            "设置",
            "密钥",
            "api密钥",
            "飞书",
            "钉钉",
            "企微",
            "企业微信",
            "联网搜索",
            "网页搜索",
            "mcp服务器",
        ),
        opt_in_keys=("settings_setup", "enable_settings_setup"),
    )


class StructuredOutputElicitationSource(GatedPolicySource):
    """Ask blocking layout/business params before underspecified spreadsheet/report gen."""

    id = "structured_output_elicitation"
    priority = 1110
    policy_names = ("structured_output_elicitation",)
    gate = RelevanceGate(
        name="structured_output_elicitation",
        hints=(
            "签到表",
            "考勤",
            "attendance",
            "spreadsheet",
            "excel",
            "xlsx",
            "excel_generator",
            "报表",
            "填表",
            "差旅",
            "报销",
            "采购审核",
            "audit",
            "审核",
            "generate table",
            "fill template",
            "打印",
            "行高",
            "sign-in sheet",
            "attendance sheet",
        ),
        opt_in_keys=(
            "structured_output_elicitation",
            "enable_structured_elicitation",
        ),
    )


SOURCE_REGISTRY[CanvasGuideSource.id] = CanvasGuideSource
SOURCE_REGISTRY[ChartGuideSource.id] = ChartGuideSource
SOURCE_REGISTRY[DataAnalysisGuideSource.id] = DataAnalysisGuideSource
SOURCE_REGISTRY[DocumentFontsSource.id] = DocumentFontsSource
SOURCE_REGISTRY[DocumentGenerationSource.id] = DocumentGenerationSource
SOURCE_REGISTRY[EmailToolSource.id] = EmailToolSource
SOURCE_REGISTRY[SettingsSetupSource.id] = SettingsSetupSource
SOURCE_REGISTRY[StructuredOutputElicitationSource.id] = StructuredOutputElicitationSource
