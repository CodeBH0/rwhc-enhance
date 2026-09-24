# Agent Handover Protocol

This repository may be worked on by multiple AI coding agents (including
Codex and DeepSeek-based agents). Do not assume the next agent has access
to your conversation, context, memory, or reasoning history.

## Communication

Communicate with the user in Simplified Chinese by default.

Use Chinese for explanations, progress updates, questions, summaries, and
final responses unless the user explicitly requests another language.

Preserve English where it is technically appropriate, including code,
identifiers, commands, file paths, API names, error messages, and established
technical terminology.

Repository files should follow the language and style already used by the
project unless the user explicitly requests otherwise.

Write `HANDOVER.md` in Simplified Chinese unless there is a specific reason not to.

## Startup

Before modifying the repository:

1. Read this file.
2. Read `HANDOVER.md` if it exists.
3. Inspect the current repository state, including relevant `git status`
   and `git diff`.
4. Treat `HANDOVER.md` as context, not ground truth. Verify important claims
   against the current code and repository state.

If `HANDOVER.md` does not exist, create it when there is meaningful work
state to preserve.

## Handover

Maintain `HANDOVER.md` at the repository root as the shared recovery and
handover document for any subsequent coding agent.

Update it:
- after completing a substantial unit of work;
- before intentionally ending a work session;
- before switching agents when possible;
- whenever remaining context or usage may be insufficient to finish safely.

`HANDOVER.md` must contain, where applicable:

1. Current objective and intended final outcome.
2. Work already completed.
3. Files changed and why.
4. Important architectural or codebase discoveries.
5. Decisions made and their rationale.
6. Unfinished work and exact next steps.
7. Known bugs, failed approaches, and approaches that should not be retried.
8. Commands/tests already run and their results.
9. Commands/tests the next agent should run.
10. Assumptions, uncertainties, and risks.
11. Relevant git status, branch, and commit information.
12. A concise `Start here` section for the next agent.

## Rules

Keep `HANDOVER.md` factual, concise, and current.

Update or replace stale information instead of accumulating a chronological
session diary.

Do not claim that work or tests succeeded unless they actually did.

Do not overwrite unrelated changes made by the user or another agent.

The next agent must be able to resume the task using only the repository,
this file, and `HANDOVER.md`.