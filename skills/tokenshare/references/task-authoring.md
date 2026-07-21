# Tokenshare Task Authoring

Use this reference only for `-ct/--create-task` and `-gt/--grill-task`.

## Planning standard

Inspect the repository before questioning the owner. Ask only about decisions that cannot be
derived locally. A task is ready when an unattended coding agent can implement and verify it
without pausing for clarification.

Resolve and record, when relevant:

- the concrete outcome and user-visible success criteria;
- affected users, workflows, components, and compatibility constraints;
- explicit in-scope and out-of-scope behavior;
- interfaces, commands, arguments, schemas, files, and state transitions;
- data flow, ordering, persistence, migration, and concurrency expectations;
- validation, errors, recovery, security, and destructive-action boundaries;
- tests, fixtures, observable acceptance criteria, and documentation changes.

Prefer repository-specific facts over generic implementation advice. Do not prescribe files or
symbols that repository inspection does not support. Preserve intentional flexibility when the
owner does not need to choose an implementation detail.

## Task block

Write the final task as valid Markdown under `## Pending Tasks`:

```markdown
### <task> [Pending] Concise Unique Title

#### Objective

Describe the intended outcome and why it matters.

#### Requirements

- State decision-complete functional and technical requirements.

#### Validation

- State exact tests and observable acceptance criteria.

#### Out of scope

- Record important exclusions when needed.

### </task>
```

Omit empty headings. Keep the title unique across Pending, WIP, and Completed tasks. Do not add
controller approval markers, task numbers, fingerprints, author metadata, or branch metadata to
the repository tasklist.

## Missing tasklist

When creation is approved and no tasklist exists, create `tokenshare_tasklist.md` at the repository
root with this skeleton and insert the new block below `## Pending Tasks`:

```markdown
# Tokenshare Tasklist

## Configuration

allow-multiple-branches: false

## Pending Tasks

## WIP Tasks

## Completed Tasks
```

## Safe application

- Edit only the selected task block, or the new root tasklist when none exists.
- Preserve surrounding formatting, configuration, other tasks, and unrelated working-tree changes.
- Re-read the finished file and validate exact section names, state/section alignment, closing
  markers, and title uniqueness.
- Never commit, push, approve, or run the task as part of authoring or grilling.
