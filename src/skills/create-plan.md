---
category: dvx
name: create-plan
description: Create a new implementation plan from requirements
arguments:
  - name: requirements
    description: What to plan for (feature description, problem to solve)
    required: true
  - name: output_file
    description: Optional output filename (if omitted, Claude suggests one)
    required: false
  - name: snapshot_content
    description: Optional context snapshot markdown to ground the plan
    required: false
  - name: interview_spec
    description: Optional interview spec markdown to treat as authoritative requirements
    required: false
---

# Create Implementation Plan

Write a detailed implementation plan based on the requirements below.

## Requirements

$ARGUMENTS.requirements

## Grounding context (optional)

If the block below is non-empty, treat it as the shared grounding for the plan.
Prefer its Known facts, Constraints, and Decision boundaries over your own
assumptions. If it is empty, proceed without it.

$ARGUMENTS.snapshot_content

## Interview spec (optional, authoritative when present)

If the block below is non-empty, it is the authoritative requirements source.
The plan's Acceptance Criteria must trace back to the spec's Acceptance
criteria, and the plan must not contradict the spec's Non-goals or Decision
boundaries without explicit justification.

$ARGUMENTS.interview_spec

## Standalone Mode

If the requirements above are empty or missing (e.g., this skill was invoked by Claude without the dvx CLI), infer requirements from the user's message or recent conversation context.

## Plan Structure

Create a markdown plan with this structure:

1. **Start with `# Plan: <title>` header**

2. **Include `## Overview` section** explaining the goal

3. **Break into `## Phase N: Title` sections** for logical groupings

4. **Be EXTREMELY detailed in each phase:**
   - Include complete code examples with types and functions
   - Show data structures with all fields
   - Include SQL queries where relevant
   - Specify file paths and directory structures
   - Write algorithm implementations, not just descriptions

5. **Each phase must be comprehensive enough to implement without clarification**

## Output Requirements

Output ONLY the raw markdown plan content.

- Start immediately with `# Plan:` - no preamble, no summary, no conversational text
- Do not wrap in code blocks
- Do not add "I've created" or similar phrases

## Filename

$ARGUMENTS.output_file

If the output_file argument is empty, at the very end of your response, on a new line, output ONLY:

```
FILENAME: PLAN-<descriptive-name>.md
```

Choose a descriptive name based on the plan content (e.g., `PLAN-user-authentication.md`, `PLAN-api-rate-limiting.md`).
