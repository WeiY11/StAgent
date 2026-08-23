"""End-to-end harness verification."""
from StAgent.harness import (
    TaskEpisode, EpisodeStatus, EpisodeOutcome, FindingSeverity,
    FindingsTracker, OutcomeEvaluator, SessionScorer, RepairPlanner,
    ValidationClosure, AcceptanceCriteria, HarnessHook, DurableReport,
)
import asyncio
import time
import tempfile
from pathlib import Path

# 1. Create episode
ep = TaskEpisode(session_id="test-1", goal="Fix the login bug", status=EpisodeStatus.RUNNING, started_at=time.time())
print(f"[1] Episode created: {ep.id}, status={ep.status}")

# 2. Track findings
tracker = FindingsTracker(episode_id=ep.id)
f1 = tracker.record("SQL injection in login", severity=FindingSeverity.CRITICAL, category="security")
f2 = tracker.record("Deprecated API usage", severity=FindingSeverity.LOW, category="style")
print(f"[2] Findings: {tracker.count}, critical={tracker.critical_count}")

# 3. Add tool results
ep.tool_results = [
    {"name": "web_search", "success": True},
    {"name": "code_edit", "success": True},
    {"name": "run_tests", "success": False, "error": "assertion failed"},
]
ep.findings = tracker.findings
ep.completed_at = time.time() + 5

# 4. Evaluate
evaluator = OutcomeEvaluator()
outcome = evaluator.evaluate(ep)
ep.outcome = outcome
print(f"[3] Outcome: {outcome.value}")

# 5. Score
scorer = SessionScorer()
score = scorer.score(ep)
ep.score = score
print(f"[4] Score: overall={score.overall}, dims={score.dimensions}")

# 6. Repair plan
planner = RepairPlanner()
plan = planner.plan(ep)
print(f"[5] Repair plan: {len(plan.actions)} actions" if plan else "[5] No repair needed")

# 7. Durable report
tmp = Path(tempfile.mkdtemp())
report_gen = DurableReport(reports_dir=tmp)
path = report_gen.persist(ep)
print(f"[6] Report saved: {path.name}")

# 8. Validation closure
from StAgent.harness.validation import check_no_destructive_ops

async def test_val():
    closure = ValidationClosure()
    closure.register_pre("no_destruct", check_no_destructive_ops)
    r = await closure.run_pre({"tool_name": "shell", "tool_params": {"cmd": "echo hello"}})
    return r

results = asyncio.run(test_val())
print(f"[7] Validation (safe cmd): passed={results[0].passed}")

# 9. HarnessHook instantiation
hook = HarnessHook(reports_dir=tmp, enable_validation=True, enable_reports=True)
print(f"[8] HarnessHook priority={hook.priority}, name={hook.name}")

# 10. AcceptanceCriteria
crit = AcceptanceCriteria(description="All tests pass", check_type="tool_success", check_params={"tool_name": "run_tests"})
ep.criteria = [crit]
evaluator.evaluate_criterion(crit, {"tool_results": [{"name": "run_tests", "success": True}]})
print(f"[9] Criterion met={crit.met}, evidence={crit.evidence}")

print("\n=== ALL HARNESS FEATURES WORKING ===")
