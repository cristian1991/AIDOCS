"""Quality gate: validate ai_preflight against 10 real reinvention cases
witnessed in this codebase's recent history. Each case has the prompt
the agent would have typed and the WHEEL they should have seen first.

PASS = wheel visible in top 3 inspect_first OR in existing_wheels (high conf)
WEAK = visible but buried beyond top 3 / behind noise
FAIL = wheel absent from card entirely
"""
from aidocs_mcp.preflight_service import preflight
from pathlib import Path

PROJECT_ROOT = Path.cwd()

CASES = [
    {
        "task": "Rewrite intent_guard._load_intent_token_lists to read tokens from sqlite instead of TOML files",
        "wheel": "intent_tokens_store",
        "wheel_aliases": ["_load_intent_token_lists", "intent_tokens_store", "get_rows_by_kind"],
        "story": "Lane A worker silently broke the directory= param contract",
    },
    {
        "task": "Write a migration script that reads intent_tokens TOML files and seeds the empire sqlite database",
        "wheel": "seed_lang_from_toml",
        "wheel_aliases": ["seed_lang_from_toml", "_migrate_tomls", "intent_tokens_store"],
        "story": "I almost wrote my own migrator while seed_lang_from_toml already existed",
    },
    {
        "task": "Add a helper function that resolves the intent_tokens directory location with env-var override",
        "wheel": "_resolve_intent_tokens_dir",
        "wheel_aliases": ["_resolve_intent_tokens_dir", "_scoped_intent_tokens_dir"],
        "story": "Three duplicate copies of this helper exist across intent_guard/runtime_service/workflow_action",
    },
    {
        "task": "Add multi-word phrase matching to detect_grant tool surfacing layer",
        "wheel": "detect_grant",
        "wheel_aliases": ["detect_grant", "tool_keywords"],
        "story": "My anchor replace duplicated the Layer 2 block before I noticed",
    },
    {
        "task": "Rewrite the skill trigger consumer in aidocs_nlp to read skill_trigger rows from sqlite",
        "wheel": "load_skill_trigger_tokens",
        "wheel_aliases": ["load_skill_trigger_tokens", "detect_skill_triggers", "skill_trigger"],
        "story": "Lane B did this; opencode plugin's classify.js separately reinvented the read",
    },
    {
        "task": "Add a sqlite-backed gate message loader to config.py for the render_interaction_text function",
        "wheel": "render_interaction_text",
        "wheel_aliases": ["render_interaction_text", "_INTERACTION_TEXT", "gate_message_strings"],
        "story": "Lane D missed that render_interaction_text was the canonical entry",
    },
    {
        "task": "Build a unified entity linker that maps prompt tokens to code symbols, memory entries, and capabilities",
        "wheel": "ai_investigate",
        "wheel_aliases": ["ai_investigate", "ai_find", "ai_trace", "search_symbols"],
        "story": "ai_investigate + ai_find + ai_trace already do entity linking piecemeal",
    },
    {
        "task": "Add context-aware POS filtering to intent_guard.classify_action so it suppresses noun usages of action verbs",
        "wheel": "_alias_in_noun_context",
        "wheel_aliases": ["_alias_in_noun_context", "_alias_first_person_agent", "classify_action"],
        "story": "These helpers already exist in intent_grant_detector; classify_action should reuse",
    },
    {
        "task": "Add YAML files for the opencode plugin under intent_tokens/opencode/ for action token classification",
        "wheel": "intent_tokens_store",
        "wheel_aliases": ["intent_tokens_store", "_load_intent_token_lists", "intent_lemma_sets"],
        "story": "opencode plugin already has access to the sqlite empire DB via aidocs_sqlite.js",
    },
    {
        "task": "Build a hook that fires on every UserPromptSubmit to inject memory entries as additionalContext",
        "wheel": "claude_hook",
        "wheel_aliases": ["claude_hook", "_check_reconnect_required", "UserPromptSubmit", "memory_inject"],
        "story": "claude_hook.py is the UPS hook handler; memory injection already plumbed there",
    },
]


def _classify(card: dict, wheel_aliases: list[str]) -> tuple[str, str]:
    """Return (verdict, reason). verdict in {PASS, WEAK, FAIL}."""
    structured = card.get("structured", {})
    inspect = structured.get("inspect_first", [])
    wheels = structured.get("existing_wheels", [])
    confidence = structured.get("confidence", "low")
    aliases_lower = [a.lower() for a in wheel_aliases]

    def _hit_in_inspect(items, top_n):
        for i, item in enumerate(items[:top_n]):
            symbol = (item.get("symbol") or "").lower()
            path = (item.get("path") or "").lower()
            for a in aliases_lower:
                if a in symbol or a in path:
                    return i + 1
        return 0

    def _hit_in_wheels(items):
        for i, w in enumerate(items):
            name = (w.get("name") or "").lower()
            title = (w.get("title") or "").lower()
            for a in aliases_lower:
                if a in name or a in title:
                    return i + 1
        return 0

    top3 = _hit_in_inspect(inspect, 3)
    if top3:
        return "PASS", f"inspect_first #{top3}"
    wheel_pos = _hit_in_wheels(wheels)
    if wheel_pos and confidence == "high":
        return "PASS", f"existing_wheels #{wheel_pos} (high conf)"
    top10 = _hit_in_inspect(inspect, 10)
    if top10:
        return "WEAK", f"inspect_first #{top10} (buried)"
    if wheel_pos:
        return "WEAK", f"existing_wheels #{wheel_pos} (conf={confidence})"
    return "FAIL", "wheel not in card"


results: list[dict] = []
for case in CASES:
    card = preflight(PROJECT_ROOT, case["task"])
    verdict, reason = _classify(card, case["wheel_aliases"])
    results.append({**case, "verdict": verdict, "reason": reason, "card": card})

print("=" * 90)
print(f"{'#':3s} {'verdict':6s} {'wheel':35s} reason")
print("=" * 90)
for i, r in enumerate(results, 1):
    print(f"{i:2d}. {r['verdict']:6s} {r['wheel'][:35]:35s} {r['reason']}")
print("=" * 90)
pass_n = sum(1 for r in results if r["verdict"] == "PASS")
weak_n = sum(1 for r in results if r["verdict"] == "WEAK")
fail_n = sum(1 for r in results if r["verdict"] == "FAIL")
print(f"PASS: {pass_n}/{len(results)}  WEAK: {weak_n}  FAIL: {fail_n}")
print()
# Detail dump for non-PASS cases
for i, r in enumerate(results, 1):
    if r["verdict"] == "PASS":
        continue
    print(f"--- case #{i} ({r['verdict']}): {r['wheel']} ---")
    s = r["card"]["structured"]
    print(f"  task: {r['task']}")
    print(f"  story: {r['story']}")
    print(f"  seeds: {s.get('seeds')}")
    print(f"  existing_wheels: {[w['name'] for w in s.get('existing_wheels', [])[:5]]}")
    print(f"  inspect_first: {[(f.get('path'), f.get('symbol')) for f in s.get('inspect_first', [])[:8]]}")
    print()
