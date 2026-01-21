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
---

# Create Implementation Plan

Write a detailed implementation plan based on the requirements below.

## Requirements

$ARGUMENTS.requirements

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
