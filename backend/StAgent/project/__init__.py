"""Layer 3: Coding project — tools, workspace, runtime, scaffold.

This package consolidates all coding-project concerns that previously
lived across ``tools/project/``, ``tools/coding_project/``, and
``services/coding_projects/``.

Dependency rule: ``StAgent.project`` may import from ``StAgent.file``
but never from ``StAgent.code``.  Project edits write directly to disk
via ``resolve_in_project()`` and never create ``File`` DB rows.
"""

from StAgent.project.tools.edit import ProjectEditTool
from StAgent.project.tools.glob import ProjectGlobTool
from StAgent.project.tools.grep import ProjectGrepTool
from StAgent.project.tools.multiedit import ProjectMultieditTool
from StAgent.project.tools.outline import ProjectOutlineTool
from StAgent.project.tools.patch import ProjectApplyPatchTool
from StAgent.project.tools.read import ProjectReadTool
from StAgent.project.tools.shell import ProjectShellTool
from StAgent.project.tools.tree import ProjectTreeTool
from StAgent.project.tools.write import ProjectWriteTool

__all__ = [
    "ProjectApplyPatchTool",
    "ProjectEditTool",
    "ProjectGlobTool",
    "ProjectGrepTool",
    "ProjectMultieditTool",
    "ProjectOutlineTool",
    "ProjectReadTool",
    "ProjectShellTool",
    "ProjectTreeTool",
    "ProjectWriteTool",
]
