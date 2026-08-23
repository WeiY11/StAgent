# Global Academic Style Rules

This note is written as a general paper-editing guide rather than a tool-specific prompt.
It is intended to be reusable across papers, collaborators, and editing workflows.

## Scope

Apply these rules during broad polishing, structural cleanup, equation rewriting, or consistency review.
They are especially useful for ML, systems, networking, and related CS papers, but most rules are general academic-writing conventions.

## Discourse and Flow

1. When a paragraph presents several challenges, reasons, or contributions, prefer explicit ordered discourse such as `First`, `Second`, and `Third`.
2. Use explicit causal or logical connectors when the relation matters. Prefer wording such as `because`, `therefore`, `however`, `specifically`, and `in contrast` over punctuation-heavy chaining.
3. Keep one paragraph focused on one message. The first sentence should state what that paragraph is doing.
4. Split overlong interpretation sentences when one sentence tries to contain observation, mechanism, and takeaway at the same time.

## Terminology Consistency

1. Keep terminology globally consistent once a term choice is made.
2. Standardize repeated preposition choices such as `among` versus `across` unless the difference is semantically important.
3. Define important nouns before reusing them, and avoid switching between near-synonyms for the same concept without reason.

## Equation Writing

1. Do not introduce displayed equations with a bare colon.
2. Lead into equations with complete academic phrasing such as `is expressed as`, `is formulated as`, `is given by`, or `is written as`.
3. Treat each displayed equation as part of the surrounding sentence.
4. Put equation-ending punctuation inside the displayed equation line: use a comma if the sentence continues and a period if the sentence ends.

## Modeling Boundaries

1. Keep the modeling section focused on system definition, assumptions, variables, constraints, and objectives.
2. Do not place concrete parameter assignments in the modeling section unless a value is part of the formal problem definition itself.
3. Move operational parameter settings, simulator constants, and implementation values to the experiment or setup section.
4. Do not mention specific algorithms in the modeling or problem-formulation sections.
5. Move algorithmic choices, training procedures, and implementation mechanisms to the method, framework, or algorithm section.

## Punctuation and Formal Style

1. Reduce colon-heavy prose when the same meaning can be expressed more smoothly with integrated sentence structure.
2. When introducing formal entities, prefer phrasing such as `namely` when it improves readability.
3. Simplify table cells and captions by removing unnecessary punctuation that does not carry meaning.
4. Keep formal writing compact, but do not sacrifice sentence clarity for compression.

## Practical Review Checklist

Use this quick check before finalizing a revision pass:

1. Are enumerated challenges or contributions written with explicit ordering where needed?
2. Are repeated terms and prepositions consistent throughout the section?
3. Does every displayed equation have a proper lead-in phrase and end punctuation?
4. Has the modeling section been kept free of algorithm discussion and setup-specific parameter values?
5. Are tables, captions, and explanatory paragraphs visually and syntactically clean?
