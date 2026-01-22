---
category: dvx
name: update-plan
description: Update an existing plan file with changes
arguments:
  - name: plan_file
    description: Path to the plan file to update
    required: true
  - name: changes
    description: What to add or modify in the plan
    required: true
  - name: existing_content
    description: Current content of the plan file
    required: true
---

# Update Implementation Plan

You are updating an existing plan file.

## Plan File

$ARGUMENTS.plan_file

## Existing Plan

$ARGUMENTS.existing_content

## Requested Changes

$ARGUMENTS.changes

## Instructions

1. Read and understand the existing plan structure
2. Apply the requested changes
3. Maintain the existing structure and formatting where appropriate
4. If adding new tasks, follow the existing task numbering scheme
5. If modifying tasks, preserve task IDs unless renumbering is necessary

## Output Requirements

Output ONLY the complete updated plan file content.

- No explanations before or after
- No "I've updated" or similar phrases
- Start directly with the plan content (usually `# Plan:`)
