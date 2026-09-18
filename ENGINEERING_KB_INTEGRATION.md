# Engineering Knowledge Base Integration

Status: **ACTIVE AI AND ENGINEERING GOVERNANCE**

Effective date: **2026-09-18**

This document defines how an external, generic Engineering Knowledge Base (KB)
may inform work on next.company / Kontragent without overriding project facts,
accepted decisions or runtime evidence.

## Truth hierarchy

When sources disagree, use this order:

1. **Level 1 — current code and runtime evidence.** The checked-out revision,
   reproducible commands, isolated database results, provider responses and
   current browser/API behavior.
2. **Level 2 — canonical project governance.** `PROJECT_STATUS.md`,
   `EVIDENCE_REPORTING_POLICY.md`, relevant acceptance documents and source
   passports.
3. **Level 3 — project-specific decisions and architecture.** Recorded design
   decisions, invariants, roadmap constraints and approved implementation plans.
4. **Level 4 — generic Engineering Knowledge Base.** Reusable standards,
   patterns, checklists and anti-patterns.
5. **Level 5 — external research.** Vendor material, public documentation,
   articles, examples and other outside sources.

A lower level may identify a question or propose an improvement, but it cannot
silently override a higher-level project fact or decision. Resolve a genuine
conflict explicitly, record the decision and update the appropriate canonical
project document.

## What the Engineering KB may provide

Relevant KB material may guide:

- architecture principles;
- Python standards;
- database and migration standards;
- testing standards;
- security standards;
- performance rules;
- observability;
- failure handling;
- code review rules;
- anti-pattern detection.

Knowledge is not implementation. The presence of a rule in the KB does not
prove that this project implements it. Only project artifacts and evidence can
support an implementation or runtime claim.

## Startup and retrieval protocol

Before every substantial task:

1. Read `AI_PROJECT_CONTEXT.md`.
2. Read `PROJECT_STATUS.md`.
3. Read `EVIDENCE_REPORTING_POLICY.md`.
4. Read `ENGINEERING_KB_INTEGRATION.md`.
5. Read the acceptance documents relevant to the task.
6. Read each relevant source passport.
7. Define the task contract, invariants and evidence required for completion.
8. Retrieve only the KB sections needed for that contract.

Do not load or summarize the entire KB blindly. Use bounded retrieval terms
derived from the task, such as the affected component, technology, invariant,
failure mode and required verification.

## How to apply retrieved knowledge

For each material KB recommendation:

- identify it as generic guidance rather than a project fact;
- compare it with the truth hierarchy and project invariants;
- confirm whether the current code already implements it;
- adapt it to the smallest safe project-specific change;
- test the actual change;
- collect runtime evidence for every runtime claim;
- record any accepted project decision in a project-owned document.

Do not convert a generic checklist item into `COMPLETE`, `PASS`, `SUPPORTED` or
`SECURE` without corresponding project evidence.

## Conflict handling

If generic KB guidance conflicts with project-specific architecture or an
accepted decision:

1. keep the current project behavior unchanged;
2. document the conflict and its concrete impact;
3. classify available evidence under `EVIDENCE_REPORTING_POLICY.md`;
4. request or record an explicit project decision;
5. only then update implementation and canonical documentation.

External research follows the same rule and has lower authority than the
generic KB. Source freshness or popularity does not increase its authority over
project-specific evidence.

## Required development loop

Substantial work follows:

`CURRENT STATE → TASK CONTRACT → INVARIANTS → DEPENDENCIES → MINIMAL IMPLEMENTATION → TESTS → REAL RUNTIME EVIDENCE → DIFF REVIEW → STATUS UPDATE → COMMIT`

The loop is incomplete if it stops at passing tests. Code presence, test
coverage, loaded data, a live provider response, PostgreSQL persistence,
browser acceptance and deployed operations are separate claims.

## Safe-change boundary

Do not perform a Big Bang Rewrite based on a generic KB recommendation. Split
large changes into bounded tasks with tests, runtime checks, evidence and a
rollback point. Respect the current blocked-stage rules in
`AI_PROJECT_CONTEXT.md` and the approved roadmap in `PROJECT_STATUS.md`.

## RAG and review output contract

RAG answers and AI code reviews should distinguish:

- **project fact** — cite the project path, revision or runtime evidence;
- **historical evidence** — cite the dated acceptance artifact;
- **generic recommendation** — cite the KB section and label it advisory;
- **external research** — cite the source and retrieval date;
- **inference** — name the premises and remaining uncertainty;
- **unverified claim** — label it `UNVERIFIED` instead of filling the gap.

The immutable reference state for comparisons initiated from this integration
is `PROJECT_BASELINE_2026_09_18.md`. Future state belongs in the dynamic context
and status documents; never edit the immutable baseline.
