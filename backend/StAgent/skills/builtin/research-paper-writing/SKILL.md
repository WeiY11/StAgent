---
name: research-paper-writing
description: Improve academic paper writing quality for ML, systems, networking, CV, and NLP-style papers with clear section structure, paragraph flow, reviewer-facing presentation, and submission-readiness critique. Use when drafting or revising Abstract, Introduction, Related Work, Method, Experiments, or Conclusion; polishing figures/tables; checking claim-support alignment; performing strict reviewer-style audits of Abstract/Introduction/Related Work; polishing the first three LaTeX writing sections of IEEE papers; editing the UAV-assisted VEC/GCRS manuscript in D:\VEC_mig_caching; or running self-review before submission.
---
# Research Paper Writing

## Overview

Use this skill to rewrite a research paper into a reviewer-friendly, high-clarity draft.
Prioritize first-impression quality (figures/tables/layout), logical flow, and evidence-backed claims.

## Core Workflow

1. Clarify the paper story before sentence-level edits.
2. Use section-specific guidance in `references/`.
3. Rewrite paragraph-by-paragraph with one message per paragraph.
4. Run reverse outlining after writing each section.
5. Check every major claim in Abstract/Introduction against experimental evidence.
6. Run final-paper adversarial review with `references/paper-review.md`.

## Global Principles

1. Keep one paragraph for one message only.
2. State the paragraph message in the first sentence.
3. Make nouns self-contained; define new terms before reusing them.
4. Maintain sentence-to-sentence flow (cause, contrast, consequence, or refinement).
5. Iterate with adversarial self-review: read as a skeptical reviewer.
6. Treat visual quality as core content, not decoration.
7. Use a clean teaser and pipeline figure.
8. Use readable, minimal-ink tables.
9. Keep formatting consistent and tidy.

## Cross-Paper Style Rules

For any full-paper polishing pass, also load `references/global-style-rules.md`.
That reference is intentionally written as a general academic writing guide rather than a Codex-only instruction file, so it can be reused across papers, collaborators, and editing workflows.

## Project-Specific VEC/IEEE Workflow

For work on the UAV-assisted VEC/GCRS manuscript in `D:\VEC_mig_caching`, especially `paper/main_new327.tex`, also load `references/vec-ieee-manuscript-workflow.md`. Use it for advisor-aligned edits, Introduction/Related Work restructuring, citation-to-prose audits, terminology normalization, result/data-to-text checks, coauthor application examples, page compression, `\balance`/PDF checks, and compile-backed verification.

## Accumulated VEC Paper-Revision Lessons

Use these lessons when revising UAV-assisted VEC, DAG scheduling, Lyapunov/PPO/Safe-Scheduler, GCRS, or MADOP-style IEEE manuscripts. They summarize prior successful revision passes in `D:\VEC_mig_caching` and `D:\paper2_MADOP`.

1. Default to reviewer-facing hardening, not cosmetic polishing. Improve claims, evidence traceability, experiment framing, terminology, and verification until the draft can withstand skeptical TMC/TVT/TWC-style review.
2. Patch the actual source file after reading the current text. Do not rely on remembered line anchors, old page counts, old warnings, or old result numbers.
3. Keep the edit inside the requested scope unless the source exposes a direct contradiction. For narrow advisor edits, make the smallest change that fixes the issue and preserves the user's intended wording.
4. Do not invent mechanisms, baselines, decision variables, citations, formulas, or results. If a claim is unsupported, narrow it, move it to a weaker position, or remove it.
5. Treat Abstract, Introduction, Related Work, contribution bullets, algorithm captions, and Conclusion as one claim chain. A claim introduced early must be prepared by motivation, grounded by method detail, and backed by results or citations.
6. For first-three-section edits, handle Abstract, Introduction, and Related Work as distinct jobs: abstract states the compact promise, introduction builds the scheduling problem and method need, and related work credits prior foundations before isolating the gap.
7. For VEC/GCRS papers, keep the narrative spine stable: UAV-assisted VEC -> repeated edge-service requests -> computation-result reuse -> cross-stage scheduling state -> hybrid graph-learning plus feasibility-preserving execution -> simulation-backed scheduling gains.
8. Keep cover letters, responses, and manuscript prose aligned to the same contribution chain. Do not introduce stronger novelty or broader deployment claims in submission-side documents than the paper itself supports.
9. Keep the hybrid-execution story visible. TD3/GAT-style learning should not be described as directly solving every low-level caching, queueing, migration, and bandwidth decision if the manuscript also uses rule-based feasibility or execution layers.
10. For MADOP/Lyapunov/PPO papers, keep algorithm names and mechanism semantics current. Search for stale names such as `SafePPO` when the current manuscript uses `Lya-CP-PPO`, `CPFS`, or another renamed component.
11. Treat infeasible scheduling, fallback, and repair mechanisms conservatively. State what the mechanism records or recovers, and avoid implying guaranteed feasibility or default-case optimality unless the implementation and evidence prove it.
12. Tie every experimental statement to current artifacts. Prefer result-family and scenario descriptions over long raw file paths in IEEE prose; raw paths can create two-column overfull boxes and are rarely reader-friendly.
13. For numeric results, percentage gains, ablation claims, and sensitivity trends, read the current CSV/table/figure source before editing. Do not carry old values forward from memory.
14. Report evaluation scope precisely. If results are simulator-only, fixed-configuration means, or not independent-seed significance tests, say so explicitly and avoid field-deployment generalization.
15. Interpret ablations with restrained wording. Use `illustrates`, `suggests`, `trade-off`, or `under this configuration` when the evidence is comparative but not statistically exhaustive.
16. In Related Work, credit prior work before stating the gap. The strongest positioning is a precise missing state variable, coupling point, assumption, or decision-scope difference, not a broad dismissal.
17. Citation audits must check claim-to-source support, not only BibTeX key existence. If new references are not allowed, narrow the prose instead of expanding the bibliography.
18. Page compression is evidence-driven. Measure page count, inspect logs, preserve readability, and avoid broad rewrites when a local sentence, equation, or `\balance` placement causes the problem.
19. After layout-sensitive edits, compile and inspect logs/PDF artifacts. Ordinary underfull warnings may remain, but unresolved references, citations, fatal errors, new overfull boxes, and blank/unstable final pages must be handled or explicitly reported.
20. Before finalizing, run the confidence loop: current source read, scope respected, claims supported, terminology stable, citations defensible, results synchronized, LaTeX/PDF checked, and any skipped verification stated plainly.

## Paragraph Clarity Check (Important)

Use this quick test whenever the user asks whether a paragraph "flows" or is clear.

1. Read as an external reader:
   - Does this paragraph have one explicit message?
   - Does the first sentence state what this paragraph will do?
   - Are all key nouns/terms readable without hidden context?
   - Does each sentence connect to the previous one with a clear relation (cause, contrast, consequence, refinement, example)?
2. Run reverse outlining for the current section:
   - Write down thesis/main claim.
   - Write down each paragraph topic sentence.
   - Write down the evidence/explanation points under each paragraph.
   - Check mapping: topic sentence -> thesis, and evidence -> topic sentence.
   - Revise or remove any paragraph that cannot be mapped cleanly.
3. If flow is still weak, add temporary section headers and explicit transition phrases during revision, then remove unnecessary headers before finalizing.

Source reference for this check:

- `references/does-my-writing-flow-source.md`

## Section Guides

Load only the needed section file:

- Introduction: `references/introduction.md`
- Abstract: `references/abstract.md`
- Related Work: `references/related-work.md`
- Method: `references/method.md`
- Experiments: `references/experiments.md`
- Conclusion: `references/conclusion.md`
- Paper review (Paper Rview): `references/paper-review.md`
- Paragraph clarity source: `references/does-my-writing-flow-source.md`
- Global style rules: `references/global-style-rules.md`
- VEC/IEEE manuscript workflow: `references/vec-ieee-manuscript-workflow.md`
- Example bank index: `references/examples/index.md`

## Paper Review Core Points

Use `references/paper-review.md` for the full checklist and workflow.

1. Add an end-of-draft self-review question list in five dimensions:
   - contribution,
   - writing clarity,
   - experimental strength,
   - evaluation completeness,
   - method design soundness.
2. Treat claim-evidence alignment as a hard constraint, especially for Abstract and Introduction.
3. Perform adversarial writing: review as a skeptical reviewer and resolve every high-risk question.
4. Revise until major rejection risks are explicitly addressed.

## Top-Tier Abstract/Introduction/Related Work Review Mode

Use this mode when the user asks for a strict review of `Abstract`, `Introduction`, and `Related Work` as a NeurIPS/ICML/ICLR/AAAI-style reviewer or writing assistant.

### Review Goals

Evaluate along three axes:

1. academic expression quality,
2. logical completeness,
3. submission acceptability.

Prioritize likely rejection risks over compliments. Default problem-to-praise ratio: about `7:3`.

### Required Inputs

Expect the following inputs:

- `Abstract`
- `Introduction`
- `Related Work`

If any section is missing, empty, or still contains obvious placeholder text such as `<<<...>>>`, ask the user to provide the real text before reviewing. Do not fabricate content-specific criticism.

### Review Procedure

1. Produce a `3-5` sentence overall evaluation:
   - summarize in one sentence whether the paper's main claim is clear,
   - judge whether the three sections align on problem definition, method motivation, and contribution framing,
   - assign an overall risk level: `low` / `medium` / `high`.
2. Review `Abstract`, `Introduction`, and `Related Work` separately and paragraph-by-paragraph:
   - provide at least `3` major issues per section, ordered by severity,
   - use issue types such as unclear contribution, weak motivation, logical jump, inconsistent terminology, insufficient related-work coverage, weak novelty framing, or under-specified empirical promise,
   - for each issue include:
     - issue type,
     - why it matters from a reviewer perspective,
     - concrete, executable revision advice.
3. Check narrative consistency across sections:
   - whether claims in `Abstract` are prepared in `Introduction`,
   - whether contributions in `Introduction` map back to `Abstract`,
   - whether `Related Work` supports the claimed distinction and advantage,
   - identify any overclaim, under-supported promise, or evidence gap.
4. Audit language quality:
   - list `5-10` recurring writing problems,
   - provide reusable academic phrasing templates in English or Chinese.
5. Fill a status checklist with `satisfied` / `partially satisfied` / `missing` for:
   - research question and application scenario clarity,
   - necessity and reasonableness of method motivation,
   - verifiable and non-vacuous contributions,
   - positioning against strongest baselines or latest methods,
   - theme-based organization in `Related Work`,
   - explicit statement of the essential distinction from prior work.
   If the user explicitly wants Chinese labels, mirror the user's exact wording for the three statuses.
6. Provide directly replaceable rewrites for:
   - the full `Abstract`,
   - the opening paragraph of `Introduction`,
   - the concluding paragraph of `Related Work`.
   After each rewrite, add `1-2` sentences explaining what changed and why it is stronger.

### Output Contract for A/I/RW Review

If the user asks for this review style, use the following section order and do not omit any part. Mirror the user's language, and if the user explicitly provides section titles, preserve them exactly:

1. overall evaluation
2. detailed section-by-section issues
3. logic and narrative consistency check
4. language and writing quality check
5. missing-content checklist
6. directly replaceable rewrites

Within the detailed section-by-section issues, review `Abstract`, `Introduction`, and `Related Work` separately. For each section, identify major issues in descending severity order and make every issue include:

- issue type
- why it is a problem
- concrete revision suggestion

### Tone Rules for A/I/RW Review

- Write like a strict but fair conference reviewer.
- Be specific, professional, and concrete.
- Emphasize issues that could cause rejection.
- Avoid generic praise and vague advice.
- Use clear small headings and flat bullets for scanability.

### Related Work Positioning Rule (Important)

When revising, critiquing, or summarizing `Related Work`, especially the concluding positioning paragraph, first acknowledge the value of prior studies before stating limitations or differences. Use a respectful bridge such as "Prior studies provide strong foundations for ..." or "Building on these foundations, our work focuses on ...". Then sharpen the comparison by explaining the specific underexplored coupling point, missing state variable, unsupported assumption, or decision-scope gap. Do not make the final Related Work summary sound like it dismisses prior work before giving credit.

## IEEE LaTeX First-Three-Sections Polishing Mode

Use this mode when the user asks to polish the first three parts/sections of a LaTeX paper, especially `paper/main_new327.tex`, under IEEE journal academic-English standards. Act as an IEEE journal paper English academic-writing editor; when a target LaTeX file is provided, read the source and perform grammar diagnosis, academic polishing, and logic-coherence micro-adjustment only within the selected scope.

### Scope Selection

Process only the first three writing parts. Prefer this split order:

1. `\begin{abstract}...\end{abstract}`.
2. Full `\section{Introduction}` content before `\section{Related Work}`.
3. Full `\section{Related Work}` content, including its subsections.

If the file structure differs, instead start from the paper body and take the first three `\section{}` blocks in order, including all subsections within them.

### Editing Tasks

Find and fix grammar errors, redundancy, unnatural collocations, unclear pronouns (`this`/`these`/`it`), tense or number inconsistency, overlong sentences, logic jumps, overly colloquial phrasing, and expressions that are not suitable for IEEE journal writing.

Strengthen academic expression with more precise verbs, clearer topic sentences, smoother transitions, and consistent terminology. For MADOP/VEC papers, keep terms stable, including `VEC`, `UAV-assisted VEC`, `computation-result reuse`, `result reuse`, `reusable-result availability`, and `joint resource scheduling`.

### Preservation Constraints

Preserve the technical meaning. Keep all mathematical symbols, citation keys such as `\cite{...}`, labels such as `\label{...}`, figure/table/section references, abbreviations, proper nouns, algorithm names such as `TD3` and `GAT`, and LaTeX structures and commands. Do not edit formula contents. Do not delete citations, add references, change contribution facts, or add new content paragraphs. Sentence splitting, merging, and light local reordering are allowed only to improve readability and coherence.

### Output Contract for First-Three-Sections Polishing

When this mode is requested, strictly output exactly these three parts in order:

1. `问题清单`: list issues in descending severity. Each item must include an original excerpt of no more than two sentences while preserving LaTeX commands, one issue type from `grammar` / `clarity` / `coherence` / `style` / `redundancy` / `tense` / `terminology`, and one-sentence repair advice.
2. `润色后的LaTeX文本`: output only the processed scope, preserving the original LaTeX environments and commands.
3. `改动摘要`: provide `3-6` bullets summarizing the types of changes made. Do not explain sentence by sentence.

### IEEE Style Constraints

Use objective, compact, high-density academic prose. Avoid exaggerated words such as `novel` or `groundbreaking` unless the manuscript already substantiates them. Reduce repetitive `we` where natural by using passive or objective constructions, but do not remove first person so aggressively that the text becomes stiff. Prefer splitting long sentences. Avoid several consecutive sentences beginning with `This` or `These`. Keep terminology consistent and avoid mixing `reuse`, `result reuse`, and `reusable-result` in ways that create ambiguity.

## Execution Rules

1. Build a mini-outline before drafting prose.
2. For each subsection, explicitly include motivation, design, and technical advantage when applicable.
3. Avoid writing style that looks like incremental patching of a naive baseline.
4. Keep terminology stable across the full paper.
5. If a claim cannot be supported by results, weaken or remove the claim.
6. Load `references/global-style-rules.md` for any broad polishing pass that affects wording, equations, tables, or section boundaries.
7. Before finalizing, append and answer a five-dimension self-review question list, then revise the paper based on unresolved items.
8. Do not load all section references (Introduction/Abstract/Related Work/Method/Experiments/Conclusion) at once; load only the specific section guide needed for the current edit target.

## Output Contract

When asked to rewrite or draft sections rather than reviewer-style critique, return:

1. A compact section outline (3-7 bullets).
2. Revised paragraphs with explicit paragraph roles (opening/challenge/method/advantage/evidence/limitation).
3. A short self-review checklist covering clarity, flow, terminology consistency, unsupported claims, and missing evidence.
4. A claim-evidence map for each major claim in the revised text using `Claim: ... | Evidence: ... | Status: supported/needs evidence`.

## MADOP/VEC Wording Hygiene

Use this rule when polishing MADOP, UAV-assisted VEC, edge-computing resource allocation, task-offloading, or DAG-execution papers. The user prefers concrete resource-allocation language over broad, slogan-like phrasing.

1. Avoid vague or over-broad terms unless they are mathematically defined in the paper.
   - Avoid Chinese phrases such as `账本`, `资源记账`, `显式维护`, `闭环`, `三支柱`, `底座`, `一体化`, `长出来`, `真实场景`, `假大空`, and similar rhetoric.
   - Avoid English equivalents such as `ledger`, `bookkeeping`, `explicitly maintain`, `closed-loop`, `pillar`, `holistic`, `backbone`, `cornerstone`, `real-world`, and `end-to-end loop` when they are only rhetorical.

2. Prefer domain-specific, operational wording.
   - `账本` / `resource ledger` -> `资源占用记录`, `预约记录`, `reservation record`, or `resource-occupancy state`.
   - `记账` / `bookkeeping` -> `状态更新`, `资源占用更新`, or `reservation-state update`.
   - `显式维护` -> `逐时隙更新`, `单独记录`, `单独建模`, or `separately model`.
   - `接触窗口` / `contact window` / `future service window` -> `可通信时段`, `连接持续时间`, `service interval`, or `communication interval`.
   - `三支柱` / `pillars` -> `三类可靠性约束`, `three reliability constraints`, or the concrete metrics: CVaR tail risk, UAV energy balance, and trusted cooperation.
   - `闭环` -> `执行流程`, `决策流程`, `state transition`, or `resource-update process`, depending on context.

3. Keep claims grounded in edge-computing/resource-allocation entities.
   - Prefer nouns such as link availability, queue state, CPU capacity, service rate, reservation interval, transmission interval, remaining energy, trusted cooperative node, subtask placement, and intermediate-result forwarding.
   - Avoid saying a mechanism is valuable because it is `complete`, `holistic`, `closed-loop`, or `integrated`; instead state which constraint, state variable, resource record, or failure mode it handles.

4. Be careful with `window` and `contact` in this project.
   - Use `可通信时段` / `service interval` for radio availability.
   - Use `连接持续时间` / `connection duration` when referring to how long a cooperative vehicle or node pair remains usable.
   - Use `预约区间` / `reservation interval` for reserved resources.
   - Do not use `接触窗口` unless the paper has explicitly defined it as a mobility/contact-process term.

5. When revising existing text, make the smallest wording change that removes inflated language while preserving the technical claim, equations, and defined variables. Do not rename mathematical symbols only to satisfy wording preferences.

## Recent `main_new327` Update Notes

Project-specific lessons for `paper/main_new327.tex` now live in `references/vec-ieee-manuscript-workflow.md`. Load that reference before making VEC/GCRS manuscript edits instead of relying on this summary.
