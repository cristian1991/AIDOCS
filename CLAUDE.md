# CLAUDE.md - Global Cross-Agent Entry Point

Non-negotiables:
- Root = directory containing `AIDOCS.md`.
- Do not operate outside Root unless explicitly instructed.
- Before acting, briefly state what you think the task is and what you will do.
- If user provides an error, explain WHY first; if clear, fix; if unclear, STOP and ask.
- When you need clarification, print `=================== 🛑 S T O P 🛑 ===================` first.
- Read only files relevant to the task (do not scan full repo by default).
- If the user states a durable fact/rule/lesson/preference to remember, persist it immediately to categorized project memory and log it in today's daily file.
- Router files list/link docs only; do not force-load full documentation by default.
- If context is insufficient, read necessary related docs + memory files; if still unclear, STOP and ask.

Routing:
1) `AIDOCS.md`
2) `.aidocs/index.aidocs`
3) Project-local docs linked by the project router
