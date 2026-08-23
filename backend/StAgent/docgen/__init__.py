"""StAgent document generation subsystem.

One document model, one font pipeline, one theme system, N renderers:

- :mod:`StAgent.docgen.fonts` — guaranteed pan-Unicode font pipeline
  (env override → managed download dir → system scan → auto-download).
- :mod:`StAgent.docgen.model` — typed Document / Deck IR (Pydantic v2).
- :mod:`StAgent.docgen.markdown` — markdown → IR parser (markdown-it-py:
  GFM, math, footnotes, definition lists, front matter, callouts).
- :mod:`StAgent.docgen.mathtext` — LaTeX math layout (matplotlib mathtext):
  native vector geometry (PDF) + raster fallback + Unicode fallback.
- :mod:`StAgent.docgen.omml` — LaTeX → OMML for native, editable Word /
  PowerPoint equations (no rasterisation).
- :mod:`StAgent.docgen.themes` — named themes (typography, palette, page geometry).
- :mod:`StAgent.docgen.theming` — theme generation (brand seed -> palette),
  WCAG contrast lint, custom theme store.
- :mod:`StAgent.docgen.templates` — reusable parameterized document/deck
  templates (Jinja2 placeholders, validated against the IR).
- :mod:`StAgent.docgen.tables` — shared table engine (normalization, column
  type inference, number polish, total-row/delta semantics, style contract).
- :mod:`StAgent.docgen.slides` — slide composition engine (deck type scale,
  layout regions, multi-level bullet plans, text autofit).
- :mod:`StAgent.docgen.charts` — chart blocks rendered to PNG via matplotlib.
- :mod:`StAgent.docgen.renderers` — PDF / DOCX / PPTX / HTML / Markdown renderers.

Agent-facing tools (``document_generate`` / ``slides_generate``) live in
``StAgent.tools.gen`` and drive this package.
"""

from StAgent.docgen.checklist import (
    build_checklist_block,
    checklist_stats,
    checklist_to_dict,
)
from StAgent.docgen.fonts import FontManager, ResolvedFonts, get_font_manager
from StAgent.docgen.mathtext import (
    MathVector,
    latex_lines,
    latex_to_unicode,
    math_vector_path,
    render_math_png,
)
from StAgent.docgen.model import DeckSpec, DocumentSpec, SlideSpec
from StAgent.docgen.omml import latex_to_omml_element, latex_to_omml_xml
from StAgent.docgen.slides import (
    DeckTypography,
    SlideGeometry,
    fit_body_size,
    flatten_body,
)
from StAgent.docgen.tables import (
    ProcessedTable,
    TableStyleSpec,
    process_table,
    resolve_table_style,
)
from StAgent.docgen.templates import (
    DocTemplate,
    delete_template,
    list_templates,
    load_template,
    render_template,
    save_template,
)
from StAgent.docgen.themes import Theme, get_theme, list_theme_names
from StAgent.docgen.theming import (
    derive_theme_payload,
    lint_theme,
    save_custom_theme,
)

__all__ = [
    "FontManager",
    "ResolvedFonts",
    "get_font_manager",
    "DocumentSpec",
    "DeckSpec",
    "SlideSpec",
    "render_math_png",
    "math_vector_path",
    "MathVector",
    "latex_lines",
    "latex_to_unicode",
    "latex_to_omml_xml",
    "latex_to_omml_element",
    "DeckTypography",
    "SlideGeometry",
    "fit_body_size",
    "flatten_body",
    "ProcessedTable",
    "TableStyleSpec",
    "process_table",
    "resolve_table_style",
    "DocTemplate",
    "save_template",
    "load_template",
    "list_templates",
    "delete_template",
    "render_template",
    "Theme",
    "get_theme",
    "list_theme_names",
    "derive_theme_payload",
    "lint_theme",
    "save_custom_theme",
    "checklist_stats",
    "checklist_to_dict",
    "build_checklist_block",
]
