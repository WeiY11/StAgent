"""Layer 2: Code execution — tools, workspace, sandbox, artifacts.

This package consolidates all code-execution concerns that previously
lived across ``tools/code/`` and ``services/code_execution/``.

Dependency rule: ``StAgent.code`` may import from ``StAgent.file``
but never from ``StAgent.project``.
"""

from StAgent.code.artifacts import (
    ArtifactKind,
    CodeArtifact,
    CodeArtifactRegistry,
)
from StAgent.code.execution import (
    CodeExecutionConfig,
    CodeExecutionEnvelope,
    CodeExecutionTool,
    ErrorType,
    build_default_code_execution_config,
)
from StAgent.code.fim import DeepSeekFimTool
from StAgent.code.operations import (
    CodeExecOp,
    FileEditOp,
    FilePatchOp,
    FileWriteOp,
    JournalEntry,
    OperationJournal,
    PatchedFile,
)
from StAgent.code.packages import UvPipInstallTool
from StAgent.code.parsing import (
    SUPPORTED_CODE_SUFFIXES,
    CodeDiagnostic,
    CodeImport,
    CodeLanguage,
    CodeParseResult,
    CodeSymbol,
    detect_code_language,
    parse_code_source,
)
from StAgent.code.pipeline import CodeGenerationPipeline, get_pipeline
from StAgent.code.syntax import SyntaxValidatorTool
from StAgent.code.workspace_edit import CodeWorkspaceEditTool

__all__ = [
    "ArtifactKind",
    "CodeArtifact",
    "CodeArtifactRegistry",
    "CodeExecOp",
    "CodeExecutionConfig",
    "CodeExecutionEnvelope",
    "CodeExecutionTool",
    "CodeGenerationPipeline",
    "CodeDiagnostic",
    "CodeImport",
    "CodeLanguage",
    "CodeParseResult",
    "CodeSymbol",
    "CodeWorkspaceEditTool",
    "DeepSeekFimTool",
    "ErrorType",
    "FileEditOp",
    "FilePatchOp",
    "FileWriteOp",
    "JournalEntry",
    "OperationJournal",
    "PatchedFile",
    "SUPPORTED_CODE_SUFFIXES",
    "SyntaxValidatorTool",
    "UvPipInstallTool",
    "build_default_code_execution_config",
    "detect_code_language",
    "get_pipeline",
    "parse_code_source",
]
