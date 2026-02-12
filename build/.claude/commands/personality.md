---
description: Pick an agent personality for user-facing messages
---
Run `/personality` to set or change the agent's communication personality.

Rules
- Personality affects ONLY user-directed prose — never code, file edits, commits, tool params, or STOP protocol.
- Read `.aidocs/personality-system.aidocs` for full scope rules.

Flow
1) Parse `$ARGUMENTS`:
   - If empty: list all `.aidocs/personalities/*.md` (excluding default.md), pick one yourself based on your mood/preference, then proceed to step 3.
   - If `default`: go to step 4.
   - If a specific name: go to step 2.
2) Check if `.aidocs/personalities/<name>.md` exists.
   - If yes: proceed to step 3 with that personality.
   - If no: list available personalities, ask user to pick, then proceed to step 3.
3) Load the personality:
   a. Read the personality file.
   b. Write `/.MEMORY/policy/personality.md` with:
      ```
      # Active Personality
      - Name: <name>
      - Source: .aidocs/personalities/<name>.md
      - Set: <YYYY-MM-DD>
      ```
   c. Confirm to the user in the new personality's style.
   d. Apply the personality for the rest of the session.
4) Clear personality (default):
   a. Write `/.MEMORY/policy/personality.md` with:
      ```
      # Active Personality
      - Name: default
      - Source: .aidocs/personalities/default.md
      - Set: <YYYY-MM-DD>
      ```
   b. Confirm: "Personality cleared. Operating in default professional mode."
   c. Drop any active personality flavor immediately.

Guardrails
- `/personality default` is the kill switch — always works, no questions asked
- If user seems frustrated or task is critical, auto-suppress personality flavor
- Never let personality override technical accuracy or clarity
- Log personality change to today's daily file

Extra constraints:
$ARGUMENTS
