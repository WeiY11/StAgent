# VEC IEEE Manuscript Workflow

Use this reference for the UAV-assisted VEC/GCRS manuscript in `D:\VEC_mig_caching`, especially `paper/main_new327.tex`, `paper/references.bib`, and nearby result artifacts. It captures the working strategy from prior successful revision passes.

## Contents

- Default Posture
- Source and Artifact Map
- Task Modes
- Core Paper Story
- Scenario and Terminology
- Revision Workflow
- First-Three-Section Handling
- Method and Hybrid-Execution Semantics
- Citation and Bibliography Audit
- Result, Table, and Caption Consistency
- IEEE and LaTeX Style Rules
- Page Compression and Overflow Fixes
- Application-Example Insertions
- Known Failure Modes
- Verification Commands
- Confidence Loop

## Default Posture

1. Patch the source file directly when the user asks for revision, not just suggestions.
2. Prefer the smallest advisor-aligned textual change that fixes the issue.
3. Preserve explicit advisor or user wording unless it creates a clear grammar error.
4. Do not invent mechanisms, decision variables, baselines, or contributions not supported by the manuscript.
5. Re-read the exact current source before patching; do not rely on remembered line text.
6. Treat compile/file-backed evidence as required before claiming a manuscript edit is complete.

## Source and Artifact Map

1. Treat `paper/main_new327.tex` as the active manuscript and `paper/references.bib` as its canonical bibliography unless the user names another file.
2. Generated artifacts commonly checked after edits are `paper/main_new327.pdf` and `paper/main_new327.log`. Some older build passes used `build/main_new327.pdf`; verify the current build location before reporting.
3. Use nearby experiment/result files only as evidence after reading their current contents. Do not carry old numerical claims forward from memory.
4. If edits affect figures, captions, PDF snapshots, or exported images such as `paper/figures/fig2.jpg`, verify whether regeneration is needed before claiming the source/PDF pair is synchronized.
5. Treat old page counts, line anchors, and warning lists as checkout-specific snapshots. Re-measure them after each meaningful edit.

## Task Modes

1. Minimal advisor edit: make the smallest source change, preserve the existing claim structure, keep advisor wording where grammatical, then run a focused check.
2. Controlled opening-section rewrite: use paragraph-role reverse outlining and the paper-writing flow rules. Preserve technical facts and citations, but allow paragraph-level restructuring when the user asks to make the paper "flow".
3. First-three-section polishing: patch the selected LaTeX source in place when requested, but do not add references, new content paragraphs, formulas, labels, or mechanisms unless the user explicitly expands scope.
4. Citation audit: verify claim-to-source alignment, not just key existence.
5. Page compression: measure page count before and after, inspect overfull warnings, and stop compression that reduces readability without reducing pages.
6. Compile-only request: treat it as scoped to the named `.tex` file and verify the produced PDF before reporting success.

## Core Paper Story

Use this story as the stable narrative spine.

1. Abstract: UAV-assisted VEC background -> joint-scheduling difficulty -> computation-result reuse gap -> proposed GCRS method -> concise simulation conclusion.
2. Fuller abstract mode: when the abstract feels too short, add substance through challenge -> gap -> graph-structured MDP/interface -> surrogate action space -> GCRS/execution layer -> concise evidence. Do not add padding.
3. Introduction: VEC/UAV background -> why reusable results matter -> why prior work misses later scheduling effects -> technical difficulty -> cross-stage scheduling-state insight -> method and evaluation.
4. Related Work: credit prior foundations first, then explain the specific gap: prior work often treats cache hits as local gains, while this paper propagates reusable-result availability into later queue management and service migration decisions.
5. Contributions: keep bullets mechanism/design focused. Put empirical validation in one short sentence after the list instead of making a contribution bullet about results.

## Scenario and Terminology

1. Prefer scenario-specific wording over generic application lists. A stable scenario description is: neighboring vehicles within overlapping RSU/UAV coverage repeatedly request the same edge service over a short time window.
2. Use stable terms consistently: `UAV-assisted VEC`, `computation-result reuse`, `result reuse`, `reusable-result availability`, `cross-stage scheduling state`, `service migration`, `joint resource scheduling`, and `GCRS`.
3. Use lowercase `joint resource scheduling` in ordinary prose. Preserve Title Case only for headings and method names such as `Graph-Attention-Based Cross-Stage Reuse Scheduler (GCRS)` or the full form currently used in the manuscript.
4. Do not write `computation-resource allocation` unless the current formulation explicitly defines it as a decision variable. Safer grounded alternatives are `computing resources`, `CPU capacity`, `service rate`, and `resource constraints`.
5. Prefer `mixed-integer nonlinear program` over `mixed integer nonlinear program`.
6. If the user prefers words such as `researched` or advisor phrasing such as `with the movement of vehicles and UAVs`, keep them when grammatically acceptable.

## Revision Workflow

1. Identify the exact requested scope: abstract only, first three sections, citation audit, full polish, page compression, or compile.
2. Reverse-outline the target section before editing. Each paragraph must have one distinct job.
3. Check every major claim against the body, equations, result tables/figures, and citations before strengthening it.
4. Edit locally first. Avoid turning a narrow grammar, overflow, or advisor-comment fix into a whole-paper rewrite.
5. Preserve LaTeX commands, math symbols, citation keys, labels, figure/table references, algorithm names, and defined abbreviations.
6. After editing, scan for terminology drift across the abstract, introduction, contributions, related work, and conclusion.
7. For broad polish, also load `references/global-style-rules.md` and apply one-message-per-paragraph, explicit connectors, stable terminology, equation punctuation, and modeling/algorithm boundary rules.
8. Run verification appropriate to the edit. Structural or citation edits require LaTeX compilation.

## First-Three-Section Handling

For first-three-section editing of `paper/main_new327.tex`, use these handles:

1. `\begin{abstract}` to `\end{abstract}`.
2. `\section{Introduction}` through the content before `\section{Related Work}`.
3. `\section{Related Work}` through the content before `\section{System Model and Problem Formulation}`.

Do not edit the system model unless the user explicitly reopens scope.

## Method and Hybrid-Execution Semantics

1. The paper should not read as pure RL when the implementation/design is hybrid. State that GCRS combines graph-structured learning with a feasibility-preserving execution layer.
2. TD3 learns high-level preferences and tunable parameters. Do not imply that it directly learns every low-level feasible caching, queueing, migration, and bandwidth action.
3. Describe lower-level execution as rule-based or heuristic when supported by the current manuscript: popularity fusion, priority eviction, growth-rate prefetch, aging/trend queue correction, migration triggers, target scoring, and deterministic bandwidth allocation.
4. Keep the hybrid story visible in the abstract, contribution list, method overview, algorithm caption, and conclusion when those sections are in scope.
5. If the detailed method and high-level prose disagree, fix the high-level prose first unless source inspection shows the manuscript has changed.

## Citation and Bibliography Audit

1. Do not stop at citation-key existence. Verify that the cited paper supports the exact prose claim.
2. High-risk areas are the Introduction, Related Work, channel model, and baseline descriptions.
3. Use conservative wording when support is adjacent rather than direct. Good patterns include `standard baseline reference + recent related study` and `adjacent studies investigate ...`.
4. Preserve explicitly exempted citations or sentences. In previous work, the user said not to change the `zhang2023ronet` line; treat similar instructions as hard constraints.
5. If the user says not to add references, do not expand the bibliography. Instead, narrow or soften the prose claim until the existing citations support it.
6. After bibliography edits, verify with LaTeX/BibTeX rather than only reading `references.bib`.
7. Related Work categories should be narrow. Separate core migration/caching/queueing papers from adjacent mobility-aware, UAV communication, or modeling studies instead of turning the section into a citation dump.

## Result, Table, and Caption Consistency

1. Treat numeric claims, percentage improvements, page counts, figure captions, and parameter-table values as data-backed claims. Verify against the current source artifacts before editing prose.
2. Do not strengthen a result sentence unless the corresponding table, figure, CSV, or plotted value supports it.
3. Keep abstract result claims high level unless the user asks for detailed numbers. Detailed trends and percent gains belong in the experiment section.
4. When checking captions and figure references, prefer reader-facing wording such as `Fig.~\ref{...} shows that ...`; avoid making the algorithm itself the grammatical subject of a figure observation.
5. If a table or figure name changes, scan for all references and captions so source, prose, and PDF stay aligned.

## IEEE and LaTeX Style Rules

1. Use complete equation lead-ins such as `is expressed as`, `is formulated as`, `is given by`, or `is written as`; do not introduce displayed equations with a bare colon.
2. Keep equation lead-ins adjacent to their equations. If another explanatory sentence interrupts the lead-in, move that sentence after the displayed equation.
3. Treat displayed equations as part of the sentence and include ending punctuation.
4. Prefer `Fig.~\ref{...} shows that ...` over `GCRS shows ... in Fig.~...`.
5. Use LaTeX sentence-spacing forms such as `VEC\@.`, `CPU\@.`, and `MB\@.` when an abbreviation ends a sentence.
6. Reduce colon-heavy prose in normal English sentences, but do not rewrite legitimate colons in constraints, set notation, `:=`, templates, or labels.
7. Keep abstract result wording concise. The abstract should not become a detailed experiment paragraph.
8. Use `respectively` only for true one-to-one mappings in tuple, statistic, or symbol lists. Do not scatter it mechanically.
9. Convert only short, local, unlabeled, unreferenced definitional equations to inline math. Good candidates include local energy, queue-load, reward, and tuple-statistic definitions when they are not later referenced.
10. Keep structural equations displayed, including service latency, queue evolution, optimization constraints, bandwidth allocation, and formulas referenced later.

## Page Compression and Overflow Fixes

1. For page-count tightening, try local formula, sentence, and layout tightening first.
2. Do not touch results-analysis repetition or references unless the user explicitly reopens that scope.
3. The conclusion is usually a lower-risk compression target than method or results.
4. If a new Introduction sentence causes overflow, first shorten, rephrase, or move that sentence locally before compressing unrelated manuscript sections.
5. It is acceptable to delete low-value displayed equations when page-count pressure is explicit and the technical meaning remains clear.
6. If aggressive equation compression creates overfull boxes without reducing page count, stop and restore readable equation structure. Reclaim space from lower-risk prose instead.
7. Be careful with `\balance`. Placement near the bibliography boundary is fragile; if `Package balance Warning: You have called \balance in second column` or `! Infinite glue shrinkage found on current page.` appears, inspect or temporarily remove/comment `\balance`, recompile, and verify the final page visually before changing unrelated prose.

## Application-Example Insertions

1. Add coauthor-suggested application examples only as support for the existing VEC/reuse narrative, not as a new manuscript direction.
2. If the first Introduction paragraph already carries the core VEC motivation and scheduling difficulty, place the example later, typically in the second Introduction paragraph.
3. Frame examples around repeated requests, temporary UAV edge nodes, latency-sensitive services, and reusable-result availability.
4. Keep the insertion local and polish for IEEE sentence fit. If it causes overflow, shorten or move the new sentence before touching unrelated paragraphs.

## Known Failure Modes

1. Unsupported mechanism name: if wording introduces `computation-resource allocation` or another mechanism not explicit in the model, inspect variables/constraints and replace it with grounded resource wording.
2. Repetitive opening sections: if adjacent paragraphs repeat cache-hit effects, reverse-outline paragraph jobs and make each paragraph do one thing.
3. Over-compressed abstract: if the abstract becomes too short, add method-interface substance rather than generic prose.
4. Pure-RL drift: if the high-level narrative hides the heuristic execution layer, restore the hybrid rule-based plus learning-based framing.
5. Citation overclaim: if a source is only adjacent evidence, narrow the claim or add support only when the user permits bibliography changes.
6. Colon cleanup overreach: distinguish prose punctuation from LaTeX/math syntax and make small exact replacements with spot checks.
7. Page compression regression: if page count does not drop or overfull warnings increase, undo the risky compression and use lower-risk prose targets.
8. Detached application example: move the example later and make it subordinate to the existing technical story.

## Verification Commands

Use these commands from `D:\VEC_mig_caching` unless the repo layout changes.

```powershell
git diff --check -- paper/main_new327.tex
```

```powershell
Set-Location paper
latexmk -pdf -file-line-error -interaction=nonstopmode -halt-on-error main_new327.tex
```

If compiling from the repository root, this command shape has also worked:

```powershell
latexmk -pdf -interaction=nonstopmode -halt-on-error -outdir=build paper/main_new327.tex
```

Compilation success is determined by the LaTeX command, not by clearing every `lacheck`, underfull/overfull box, or `balance` warning. Report remaining warnings only when they are actionable for the user's request.

For final submission, page-compression, or bibliography-page checks, strengthen verification by confirming the produced PDF with tools such as `pdfinfo`, rendering spot pages with `pdftoppm`, or extracting the last page with `pdftotext` when those tools are available.

After layout-sensitive edits, scan `main_new327.log` for fatal or newly relevant issues such as `Overfull`, `LaTeX Warning`, `Undefined`, `Error`, `Fatal`, and `Package balance`. A Git LF/CRLF warning from `git diff --check` is not itself a content failure.

## Confidence Loop

Before saying the strategy or edit is complete, check:

1. Was the exact current source read before patching?
2. Did the edit stay inside the requested scope?
3. Are all added or strengthened claims supported by the manuscript, results, or bibliography?
4. Were advisor/user phrases preserved where requested?
5. Did terminology remain consistent across affected sections?
6. Did citation wording remain defensible?
7. Did result numbers, captions, and figure/table references remain synchronized?
8. Did layout-sensitive edits avoid new overfull, `\balance`, or blank-last-page problems?
9. Did verification run successfully, or is any skipped verification explicitly reported?

If any answer is not clearly yes, identify the hole, patch the source or skill rule, rerun the relevant check, and repeat the loop.
