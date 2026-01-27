---
category: dvx
name: create-list
description: Create a YAML queue file for dvx run with a list of plan files
arguments:
  - name: args
    description: Optional output filename and/or list of plan files (can be natural language)
    required: false
---

# Create DVX Queue List

Create a YAML queue file that `dvx run` can process sequentially.

## Input

$ARGUMENTS.args

## Instructions

### 1. Parse the Input

The input can be:
- **Empty**: Ask user what files to include
- **Just a filename**: e.g., `tasks.yaml` - ask user what files to include
- **Explicit files**: e.g., `tasks.yaml plans/FIX-bug1.md plans/FIX-bug2.md`
- **Natural language**: e.g., `BUG-ontology bugs 6-10` or `all FIX files in plans/`

### 2. Resolve Files

If given natural language patterns:
- `bugs 6-10` → expand to bug6, bug7, bug8, bug9, bug10
- `FIX-*` → glob for matching files
- Search in `plans/` directory by default

Use Glob to find matching files. Verify each file exists.

### 3. Determine Output Filename

- If a `.yaml` or `.yml` filename is provided, use it
- Otherwise default to `plans/tasks.yaml`

### 4. Write the YAML File

Format:
```yaml
- plans/FIX-bug1.md
- plans/FIX-bug2.md
- plans/FIX-bug3.md
```

### 5. Confirm to User

Output:
```
Created {filename} with {count} plans:
  1. plans/FIX-bug1.md
  2. plans/FIX-bug2.md
  ...

Run with: dvx run {filename}
```

## Examples

### Example 1: Explicit files
Input: `tasks.yaml plans/FIX-auth.md plans/FIX-api.md`

Action: Write tasks.yaml with those two files.

### Example 2: Natural language with range
Input: `BUG-ontology bugs 6-10`

Action:
1. Search `plans/` for files matching `*BUG-ontology*bug6*`, `*BUG-ontology*bug7*`, etc.
2. Write plans/tasks.yaml with found files

### Example 3: Glob pattern
Input: `queue.yaml plans/FIX-*.md`

Action:
1. Glob for `plans/FIX-*.md`
2. Write queue.yaml with matches (sorted)

### Example 4: Empty input
Input: (empty)

Action: Ask user "What plan files should I add to the queue?"

## Error Handling

- If no files found for a pattern, report which patterns failed
- If a specified file doesn't exist, warn but continue with others
- If output file already exists, ask before overwriting

## Output

After creating the file, show:
- The filename created
- The number of plans
- The list of plans (numbered)
- The command to run it
