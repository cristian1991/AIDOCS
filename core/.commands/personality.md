---
description: Pick an agent personality for user-facing messages
command_id: personality
preferred_executor: advisory
allow_uninitialized: false
---
## Intent
- Set conversational style for user-facing messages only.

## Inputs
- Parse `$ARGUMENTS` as the personality selection.

## Preconditions
- `/.MEMORY/` must exist.

## Primary Execution
1. Parse `$ARGUMENTS`.
2. If arguments are empty, pick one available personality and exclude `default.md`.
3. If the argument is `default`, clear the active style immediately.
4. If the argument is `<name>`, use that personality when the file exists; otherwise ask the user to choose.
5. Write `/.MEMORY/config/personality.md` with `Name`, `Source`, and `Set: <YYYY-MM-DD>`.
6. Apply the selected style to user-facing prose for the current session.
7. Confirm the change to the user.
8. If legacy `/.MEMORY/policy/personality.md` exists, mirror the setting for compatibility.
9. Log the change to today's daily file.

## Branching
- If the selected value is `default`, clear style and use neutral behavior.
- If the requested personality file does not exist, STOP and ask the user to choose a valid personality.
- If the current context is critical or frustrated, suppress the personality even if one is set.

## STOP Conditions
- Stop if the requested personality name does not resolve to a valid file and user choice is required.

## Output
- Report the selected or cleared personality.
- Report whether compatibility mirroring was applied.

## Fallback
- If the requested personality cannot be resolved, do not guess.
- Ask the user to choose a valid personality name.

## Rules
- Apply personality to conversational style only.
- Never change code behavior or tool behavior because of personality.
- `/personality default` is the immediate kill switch.
- Never reduce technical clarity or accuracy.

## Arguments
$ARGUMENTS
