---
category: dvx
name: template
description: Template showing the skill file format
arguments:
  - name: required_arg
    description: A required argument
    required: true
  - name: optional_arg
    description: An optional argument
    required: false
---

# DVX Template Skill

This is a template showing the structure of a dvx skill file.

## Arguments

Access arguments using `$ARGUMENTS.name` syntax:
- Required: $ARGUMENTS.required_arg
- Optional: $ARGUMENTS.optional_arg

## Instructions

1. First step...
2. Second step...

## Output Markers

Skills should output structured markers for parsing:

- `[APPROVED]` - Success/approved
- `[ISSUES]` - Problems found
- `[BLOCKED: reason]` - Cannot proceed
- `[DECISION: topic]` - Logged a decision

## Example Decision Log

```
[DECISION: topic name]
Decision: What you decided
Reasoning: Why you chose this
Alternatives: Other options considered
```
