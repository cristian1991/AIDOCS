---
description: Pick an agent personality for user-facing messages
agent: build
---
Set conversational style only (never code/tool behavior).

Flow
1) Parse `$ARGUMENTS`:
   - empty: pick one available personality (exclude `default.md`)
   - `default`: clear style
   - `<name>`: use that personality if file exists, else ask user to choose
2) Write `/.MEMORY/config/personality.md`:
   - `Name`, `Source`, `Set: <YYYY-MM-DD>`
3) Apply style to user prose for this session and confirm to user.
4) If legacy `/.MEMORY/policy/personality.md` exists, mirror for compatibility.
5) Log change to today's daily file.

Guardrails
- `/personality default` is immediate kill switch.
- Suppress personality for critical/frustrated contexts.
- Never reduce technical clarity/accuracy.

Extra constraints:
$ARGUMENTS
