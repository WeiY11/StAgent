"""Heuristic verification gate for mission segments.

The toolchain-aware verification-gap heuristics were migrated here from
``leagent.agent.subagent.assess_verification_gap`` so both the sub-agent
path and the mission loop share one implementation (subagent re-imports
them for backward compatibility).

:class:`HeuristicVerifier` implements the mission loop's
:class:`~leagent.mission.runner.MissionVerifier` seam:

- ``verify_level=none`` → always pass.
- CODE steps that changed files must have run a matching toolchain
  verification command (pytest / vitest / cargo …) via ``project_shell``.
- FILE steps must have produced at least one artifact.
- A segment with no text output never passes.

``verify_level=llm_critic`` currently falls back to the same heuristics;
the LLM critic is a follow-up layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from leagent.db.models.mission import MissionVerifyLevel
from leagent.utils.logging import get_logger

if TYPE_CHECKING:
    from leagent.db.models.mission import Mission, MissionStep
    from leagent.mission.types import SegmentResult, VerificationResult

logger = get_logger(__name__)

#: Heuristic map: changed-file extension → verification command tokens.
_VERIFY_BY_EXT: dict[str, tuple[str, ...]] = {
    ".py": ("pytest", "python -m pytest", "python -m unittest", "mypy", "ruff", "pyright"),
    ".pyi": ("mypy", "pyright"),
    ".ts": ("vitest", "jest", "npm test", "pnpm test", "yarn test", "tsc", "eslint"),
    ".tsx": ("vitest", "jest", "npm test", "pnpm test", "yarn test", "tsc", "eslint"),
    ".js": ("vitest", "jest", "npm test", "pnpm test", "yarn test", "eslint"),
    ".jsx": ("vitest", "jest", "npm test", "pnpm test", "yarn test", "eslint"),
    ".rs": ("cargo test", "cargo check", "cargo clippy"),
    ".go": ("go test", "golangci-lint"),
    ".java": ("mvn test", "gradle test", "./gradlew test"),
}


def _expected_verify_tokens(changed_paths: set[str]) -> list[str]:
    """Union of verification tokens implied by the changed file set."""
    tokens: list[str] = []
    seen: set[str] = set()
    for path in changed_paths:
        lower = path.lower()
        for ext, cands in _VERIFY_BY_EXT.items():
            if lower.endswith(ext):
                for token in cands:
                    if token not in seen:
                        seen.add(token)
                        tokens.append(token)
                break
    return tokens


def _matched_verification(
    shell_blobs: list[str],
    expected_tokens: list[str],
) -> bool:
    """True when at least one shell invocation matches an expected verify token."""
    if not expected_tokens:
        # Unknown toolchain: any project_shell counts (legacy heuristic).
        return bool(shell_blobs)
    joined = "\n".join(shell_blobs)
    return any(tok in joined for tok in expected_tokens)


def assess_verification_gap(
    *,
    changed_paths: set[str],
    shell_blobs: list[str],
    enforce: bool,
    partial: bool,
    error: str | None,
) -> str | None:
    """Return a verification_gap message when changed files lack matching tests.

    Upgrades the old "did any shell run?" check to "did a shell command
    matching the changed-files toolchain run?" (pytest / vitest / cargo …).
    """
    if not enforce or not changed_paths or partial or error is not None:
        return None
    expected = _expected_verify_tokens(changed_paths)
    if _matched_verification(shell_blobs, expected):
        return None
    if not shell_blobs:
        hint = ", ".join(expected[:4]) if expected else "tests/lint/typecheck"
        return (
            "Files were modified but project_shell was not run for "
            f"verification ({hint})."
        )
    hint = ", ".join(expected[:4]) if expected else "a matching test/lint command"
    return (
        "Files were modified and project_shell ran, but no command matched "
        f"the expected verification for the changed files ({hint})."
    )


class HeuristicVerifier:
    """Heuristic verification gate for mission segments.

    Attributes:
        enforce_code_verify: When true, CODE steps whose segments changed
            files must have run a matching toolchain verification command.
            Defaults to true; tests may relax it.
    """

    def __init__(self, *, enforce_code_verify: bool = True) -> None:
        self._enforce_code_verify = enforce_code_verify

    async def verify(
        self,
        *,
        mission: Mission,
        step: MissionStep,
        result: SegmentResult,
    ) -> VerificationResult:
        from leagent.mission.types import VerificationResult

        if mission.verify_level == MissionVerifyLevel.NONE:
            return VerificationResult(passed=True, evidence={"verify_level": "none"})

        feedback: list[str] = []
        evidence: dict[str, Any] = {
            "changed_paths": list(result.changed_paths or []),
            "produced_files": list(result.produced_files or []),
        }

        if self._enforce_code_verify and _kind_value(step.kind) == "code":
            changed = set(result.changed_paths or [])
            if changed:
                gap = assess_verification_gap(
                    changed_paths=changed,
                    shell_blobs=list(result.shell_blobs or []),
                    enforce=True,
                    partial=False,
                    error=None,
                )
                if gap:
                    feedback.append(gap)
                    evidence["verification_gap"] = gap

        if _kind_value(step.kind) == "file" and not result.produced_files:
            feedback.append("file 类步骤未产出任何文件")

        if not result.text.strip():
            feedback.append("步骤未产生任何文本输出")

        if feedback:
            return VerificationResult(
                passed=False,
                feedback="; ".join(feedback),
                evidence=evidence,
            )
        return VerificationResult(passed=True, evidence=evidence)


def _kind_value(value: Any) -> str:
    """Read a ``MissionStepKind`` enum (or plain string) as its string value."""
    if value is None:
        return ""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


__all__ = [
    "HeuristicVerifier",
    "_VERIFY_BY_EXT",
    "_expected_verify_tokens",
    "_matched_verification",
    "assess_verification_gap",
]
