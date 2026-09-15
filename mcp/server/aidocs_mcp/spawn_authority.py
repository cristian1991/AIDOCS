"""ONE authority for which process spawns are sanctioned (#1031 phase 2).

WHY THIS MODULE EXISTS. The same fact — "this spawn is sanctioned, here is
why, and here is its window posture" — used to be asserted in SEVEN places:
two tuples plus a console list here, a census exemption in spawn_census, the
semgrep rule, the deploy baseline registry, and the SUMMARY.md count. Each
was hand-maintained, so they could disagree, and they DID: the #1031 deploy
chain took SIX consecutive failed runs, five of them a single surface
disagreeing with the others (a rule that had not been told about the shim, a
baseline holding 58 rows for annotations that no longer existed, a SUMMARY
sentence stating the old count, a skip nobody had classified, then the same
skip in the bucket that refuses to ship).

The measurement that settled the design: the per-file and per-site inventories
DISAGREED on two files, in OPPOSITE directions, and had done so silently —
``lane_resume_dispatcher.py`` (file said OL, its site said AR: the file
understated an agent-reachable spawn) and ``conditional_predicates.py`` (file
said OL, its site said TS). Neither list was uniformly right, so neither could
be declared the winner, which is the whole argument for keeping only one.

THE RULE HERE IS: facts live in ``SPAWN_SITES``; everything else is a VIEW.
``shell_egress_service`` keeps its three public names as derived tuples, so the
~60 modules and tests that import them are undisturbed, and ``spawn_census``
reads the pre-import exemption from here.

WHAT THIS DOES *NOT* YET OWN, stated so the docstring cannot overclaim: the
semgrep ruleset, the deploy baseline registry and the SUMMARY.md count are
still their own files. They no longer DRIFT — the baseline count is derived by
``scripts/semgrep_baseline_registry.py`` and its rows are held one-to-one
against the source annotations by ``test_deploy_report_baseline_truth`` — but
they are cross-checked, not generated from this table. Folding them in is the
remainder of #1031.
"""

from __future__ import annotations

from typing import NamedTuple


class SpawnSite(NamedTuple):
    """One sanctioned spawn callsite, with everything any surface needs.

    ``reachability`` is the doctrine vocabulary short code:
      AR  agent_reachable  — an agent can cause this spawn. Strictest.
      OL  operator_local   — only an operator gesture reaches it.
      TS  test_only        — exercised only under test.

    ``console_why`` non-empty marks a DELIBERATE console (the operator is
    meant to see the child); anything else must show windowless evidence.

    ``unauditable_why`` non-empty means the callsite cannot route through
    audited_run/audited_popen AT ALL. There is exactly one, and it is
    structural: see the claude_hook_shim row.

    ``waiver_why`` non-empty means the callsite carries an inline
    ``# nosemgrep:`` annotation. Those four are the ONLY rows the deploy
    baseline registry may contain, and it is GENERATED from them
    (``render_baseline_rows``) so a row can no longer outlive its annotation —
    which it did, 58 times, and cost a deploy.
    """

    relpath: str
    enclosing_fn: str
    callee: str
    shell_flag: str
    argv_head: str
    reachability: str
    owner: str
    rationale: str
    console_why: str = ""
    unauditable_why: str = ""
    waiver_why: str = ""
    waiver_rule: str = "aidocs-direct-subprocess-outside-shell-egress"
    waiver_followup: str = ""


#: The canonical table. ONE edit here is the whole change.
SPAWN_SITES: tuple[SpawnSite, ...] = (
    SpawnSite(
        'cli.py',
        'cmd_config',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'operator-cli',
        "operator-initiated $EDITOR launch — argv from shlex.split of the operator's own $EDITOR, no shell; console is DELIBERATE (see DELIBERATE_CONSOLE_SPAWNS)",
        console_why="`aidocs config --edit` launches the operator's $EDITOR, which is the WHOLE POINT of the command — a windowless editor would be an editor nobody can type into. Operator-initiated from their own terminal, never under the pythonw daemon. Routed through audited_run in #1031, having been invisible behind the `**/cli.py` semgrep exclude.",
    ),
    SpawnSite(
        'server_plan_task_tools.py',
        'ai_kill',
        'subprocess.run',
        'shell=False',
        '',
        'AR',
        'run-lifecycle',
        'forced tree-kill (taskkill /F /T) of a detached ai_run child — a destructive process operation that was invisible to the ledger; fixed argv, pid is an internally-tracked int, windowless',
    ),
    SpawnSite(
        'agent_expert_service.py',
        'spawn_interactive',
        'subprocess.Popen',
        'shell=False',
        '',
        'AR',
        'expert-fanout',
        'expert subprocess fanout',
    ),
    SpawnSite(
        'agent_expert_service.py',
        'spawn_worker_claude',
        'subprocess.run',
        'shell=False',
        '',
        'AR',
        'expert-fanout',
        'spawn_worker_claude subprocess',
    ),
    SpawnSite(
        'agent_expert_service.py',
        'spawn_worker_codex',
        'subprocess.run',
        'shell=False',
        '',
        'AR',
        'expert-fanout',
        'spawn_worker_codex subprocess',
    ),
    SpawnSite(
        'agent_expert_service.py',
        'spawn_worker_opencode',
        'subprocess.run',
        'shell=False',
        '',
        'AR',
        'expert-fanout',
        'spawn_worker_opencode subprocess',
    ),
    SpawnSite(
        'aidocs_service.py',
        '_spawn_broker_process',
        'subprocess.Popen',
        'shell=False',
        '',
        'OL',
        'daemon-supervision',
        '#609: the watchdog spawning the hook-broker HOST it supervises. Fixed argv (pythonw -m aidocs_mcp.hook_broker_host), no shell, no agent input. A CHILD is the only way a deploy can give the resident hook evaluator fresh code that it can PROVE it loaded — an in-process rebuild recomputes the on-disk identity and would declare itself fresh while running the previous generation.',
    ),
    SpawnSite(
        'aidocs_service.py',
        '_pid_listening_on',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'daemon-supervision',
        '#568 D4 / #569 R2: naming the process that holds the proxy port when a bind has ALREADY failed. Fixed argv (netstat -ano / lsof), no shell, no agent input — only an int port the supervisor already owns. Diagnosis on a fatal path: it is hard-capped at 3s and every exception returns None, so a broken resolver degrades a loud correct failure into a slightly less specific one, never into a hang.',
    ),
    SpawnSite(
        'code_runner.py',
        '_kill_process_tree',
        'subprocess.run',
        'shell=False',
        '',
        'AR',
        'code-runner',
        "Windows taskkill helper; argv-only (head 'taskkill' fixed at caller; routed via audited_run)",
    ),
    SpawnSite(
        'code_runner.py',
        '_run_process',
        'subprocess.Popen',
        'shell=False',
        '',
        'AR',
        'code-runner',
        'primary code_runner subprocess; legacy shim while callers migrate',
    ),
    SpawnSite(
        'code_runner_detached.py',
        '_spawn_and_track',
        'subprocess.Popen',
        'shell=False',
        '',
        'AR',
        'detached-runner',
        'detached run dispatcher (spawn_detached tail, hoisted for the #466 argv/shell dispatch split); needs lifecycle binding in tests',
    ),
    SpawnSite(
        'conductor_verification_service.py',
        '_run_command',
        'subprocess.run',
        'shell=False',
        '',
        'AR',
        'conductor-verification',
        'needs lifecycle binding in tests; prior migration attempt reverted',
    ),
    SpawnSite(
        'governed_bash_service.py',
        '_default_probe',
        'subprocess.run',
        'shell=False',
        '',
        'AR',
        'governed-bash',
        'default probe; migration tracked',
    ),
    SpawnSite(
        'governed_bash_service.py',
        '_verify_os_signature',
        'subprocess.run',
        'shell=False',
        '',
        'AR',
        'governed-bash',
        "PowerShell os-signature verification via the ABSOLUTE canonical system PowerShell (argv[0] is the _canonical_powershell() variable, never a PATH-resolved 'powershell' literal — the MCP server env may lack it on PATH); provider path via env var + -LiteralPath; operator-local",
    ),
    SpawnSite(
        'governed_shell_attest.py',
        '_publisher_ok_uncached',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'governed-shell',
        'bounded fixed-argv Authenticode publisher probe invoked by ABSOLUTE canonical system PowerShell (argv[0] is the _canonical_powershell() variable, never a PATH-resolved literal); attestation evidence the output-guard would withhold; operator-local, no untrusted input',
    ),
    SpawnSite(
        'lane_resume_dispatcher.py',
        'resume_worker_on_deny',
        'subprocess.Popen',
        'shell=False',
        '',
        'AR',
        'lane-dispatch',
        'lane resume dispatcher; needs lifecycle binding in tests',
    ),
    SpawnSite(
        'mcp_server.py',
        'conductor_start',
        'subprocess.Popen',
        'shell=False',
        '',
        'AR',
        'mcp-server',
        'conductor_start Popen #1',
    ),
    SpawnSite(
        'mcp_server.py',
        'conductor_start',
        'subprocess.Popen',
        'shell=False',
        '',
        'AR',
        'mcp-server',
        'conductor_start Popen #2',
    ),
    SpawnSite(
        'mcp_server.py',
        'conductor_start',
        'subprocess.Popen',
        'shell=False',
        '',
        'AR',
        'mcp-server',
        'conductor_start Popen #3',
    ),
    SpawnSite(
        'aidocs_service.py',
        'spawn',
        'subprocess.Popen',
        'shell=False',
        '',
        'OL',
        'service-watchdog',
        'watchdog spawns the AIDOCS daemon it supervises (#249) — fixed argv, no agent input',
    ),
    SpawnSite(
        'cli.py',
        '_spawn_watchdog',
        'subprocess.Popen',
        'shell=False',
        '',
        'OL',
        'service-watchdog',
        '`aidocs service start` spawns the detached watchdog (#249) — fixed argv [sys.executable -m aidocs_mcp.cli service run], operator-initiated',
    ),
    SpawnSite(
        'cli.py',
        'cmd_service',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'service-watchdog',
        '`aidocs service install` registers the logon task via schtasks — fixed argv, operator-initiated, routed via audited_run',
    ),
    SpawnSite(
        'cli.py',
        'cmd_service',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'service-watchdog',
        '`aidocs service uninstall` removes the logon task via schtasks — fixed argv, operator-initiated, routed via audited_run',
    ),
    SpawnSite(
        'aidocs_nlp/installer.py',
        '_run',
        'subprocess.Popen',
        'shell=False',
        '',
        'OL',
        'nlp-bootstrap',
        '_run during NLP bootstrap',
    ),
    SpawnSite(
        'aidocs_nlp/installer.py',
        'uninstall',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'nlp-bootstrap',
        'uninstall during NLP teardown',
    ),
    SpawnSite(
        'backend_models.py',
        '_default_run_opencode',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'model-catalog',
        'opencode models host-CLI probe (moved from server_plan_task_tools.ai_models)',
    ),
    SpawnSite(
        'checkpoint_service.py',
        '_git_cat_file_bytes',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'checkpoint',
        'git cat-file for checkpoint inspection',
    ),
    SpawnSite(
        'cli.py',
        '_run_install',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'cli',
        'installer invocation',
        console_why="`aidocs setup` dependency install streams live output to the operator's OWN console (no capture); operator-initiated, never runs under the daemon.",
    ),
    SpawnSite(
        'cli.py',
        'cmd_doctor',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'cli',
        'doctor probe',
    ),
    SpawnSite(
        'cli.py',
        'cmd_setup',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'cli',
        'setup post-install probe',
    ),
    SpawnSite(
        'csharp_roslyn_client.py',
        '_run',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'roslyn-client',
        'dotnet helper invocation',
    ),
    SpawnSite(
        'csharp_roslyn_client.py',
        '_spawn_process',
        'subprocess.Popen',
        'shell=False',
        '',
        'OL',
        'roslyn-client',
        'Roslyn worker spawn',
    ),
    SpawnSite(
        'failure_stewardship.py',
        'capture_first_seen_tree_hash',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'failure-stewardship',
        'git inspection for failure capture #1',
    ),
    SpawnSite(
        'failure_stewardship.py',
        'capture_first_seen_tree_hash',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'failure-stewardship',
        'git inspection for failure capture #2',
    ),
    SpawnSite(
        'failure_stewardship.py',
        'capture_head_sha',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'failure-stewardship',
        'git head sha capture',
    ),
    SpawnSite(
        'failure_stewardship.py',
        '_default_reverify_runner',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'failure-stewardship',
        'deterministic nodeid re-verify rerun (bug #68); nodeids validated path::test, no flag injection',
    ),
    SpawnSite(
        'file_ops.py',
        '_check_syntax',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'file-ops',
        'node-based syntax check',
    ),
    SpawnSite(
        'git_helpers.py',
        'run_git_sync',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'git-helpers',
        'general git porcelain helper',
    ),
    SpawnSite(
        'gate_git_transport.py',
        'run_credentialed_git',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'outer-gate-projects',
        'the ONE credentialed gate git primitive (project import/sync + gate ai_git fetch/pull/push); fixed argv, no shell, org credential only in the child env helper',
    ),
    SpawnSite(
        'package_integrity.py',
        '_default_subprocess_runner',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'package-integrity',
        'package integrity verifier default runner',
    ),
    SpawnSite(
        'runtime_bootstrap_service.py',
        'project_init',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'runtime-bootstrap',
        'gh CLI invocation during project init',
    ),
    SpawnSite(
        'runtime_provisioner.py',
        '_default_runner',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'runtime-provisioner',
        'runtime provisioner default runner',
    ),
    SpawnSite(
        'runtime_refresh.py',
        'refresh_runtime',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'runtime-refresh',
        'provisions via a NAMED argv (aidocs_mcp.cli runtime --fix); no shell',
    ),
    SpawnSite(
        'runtime_service.py',
        'recent_commits_touching_file',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'runtime-service',
        'git log for recent commits touching file',
    ),
    SpawnSite(
        'server_legacy_git_tools.py',
        '_run_psql',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'legacy-git-tools',
        'psql for legacy git/schema tools',
    ),
    SpawnSite(
        'shell_resolver.py',
        '_safe_run',
        'subprocess.Popen',
        'shell=False',
        '',
        'OL',
        'shell-resolver',
        'shell binary resolution probe',
    ),
    SpawnSite(
        'slop_backends.py',
        '_default_runner',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'slop-backends',
        'slop backend default runner',
    ),
    SpawnSite(
        'updater_service.py',
        '_run_script',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'updater-service',
        'updater script execution',
    ),
    SpawnSite(
        'workflow_action_service.py',
        'verify_action',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'workflow-action',
        'workflow action verification',
    ),
    SpawnSite(
        'server_deploy_tools.py',
        'resolve_git_origin',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'server-deploy-tools',
        'read-only `git config --get remote.origin.url` origin probe — fixed argv, no shell, bounded timeout, fail-closed',
    ),
    SpawnSite(
        'server_deploy_tools.py',
        'resolve_git_commit',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'server-deploy-tools',
        'read-only `git rev-parse --verify <ref>^{commit}` commit-pin probe (§5) — fixed argv, no shell, bounded timeout, fail-closed',
    ),
    SpawnSite(
        'backlog_sync_sitter.py',
        '_default_git',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'backlog-autosync',
        'backlog autosync git replication on the event-log dir (fixed git subcommands pull/status/add/commit/push) routed through audited_run; passthrough-lambda seam (#345). No shell, no agent-derived input, system-internal, fail-open; CREATE_NO_WINDOW.',
    ),
    SpawnSite(
        'test_runner.py',
        'interpreter_drift',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'ai_test-interpreter-drift',
        "reads the DECLARED test interpreter's installed version of the project under test, to say whether that interpreter has drifted from the tree it is about to run. Routed through audited_run; passthrough-lambda seam (#345). The interpreter path comes from the `test.interpreter` catalog setting (project/global scope, operator-written) and is never agent-supplied; fixed argv, no shell, bounded timeout, DIAGNOSTIC ONLY — every failure path returns None so it can never become the reason a suite does not run. CREATE_NO_WINDOW.",
    ),
    SpawnSite(
        'backlog_staleness.py',
        '_git',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'backlog-staleness',
        '#836 backlog staleness scan: ONE bounded, memoized `git log` over commit subjects/bodies to flag open items a commit already claims, routed through audited_run; passthrough-lambda seam (#345). Fixed argv, no shell, no agent-derived input, READ-ONLY (log only, never a mutating subcommand), fail-quiet; CREATE_NO_WINDOW because the daemon runs under pythonw.',
    ),
    SpawnSite(
        'failure_stewardship.py',
        '_nodeid_is_collectable',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'stewardship-collectability',
        "#775 intake collectability probe: ONE bounded `pytest --collect-only -q <nodeid>` per genuinely NEW failure signature (skipped entirely for reobservations), routed through audited_run; passthrough-lambda seam (#345). Fixed argv, no shell, READ-ONLY — collection only, it never executes a test. The nodeid is not agent-authored prose: it is a pytest id scraped from a test run and already argv-guarded. FAILS OPEN on every ambiguity (missing file, subprocess crash, timeout, unexpected exit code, no project_root); ONLY a confirmed 'file parsed, named test absent' (exit 4 + 'not found' + 'no match in any of') rejects, so a real failure is never lost to a probe error.",
    ),
    SpawnSite(
        'env_floor_audit.py',
        '_run_pip_check',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'env-floor-audit',
        'session-start declared-floor preflight `pip check` ([sys.executable,-m,pip,check]) routed through audited_run; passthrough-lambda seam (#345). Read-only metadata probe, no shell, no agent input, bounded timeout, fail-open; CREATE_NO_WINDOW.',
    ),
    SpawnSite(
        'conditional_predicates.py',
        '_git_clean',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'conditional-predicates',
        '_git_clean sentinel check #1',
    ),
    SpawnSite(
        'conditional_predicates.py',
        '_git_clean',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'conditional-predicates',
        '_git_clean sentinel check #2',
    ),
    SpawnSite(
        'outer_gate_sandbox.py',
        '_docker',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'cloudagent-runtime',
        'governed sandbox-worker docker lifecycle (create/inspect/rm) via fixed argv; no shell',
    ),
    SpawnSite(
        'lsp/client.py',
        '_spawn',
        'subprocess.Popen',
        'shell=False',
        '',
        'OL',
        'lsp-door',
        'aidocs_lsp DOOR (§XXVII): spawns a vendored language server (pyright/csharp-ls/rust-analyzer) behind the fail-open door via audited_popen passthrough — fixed argv (resolved binary + spec args), no shell, no agent-derived input, hard-timeout + evict lifecycle',
    ),
    SpawnSite(
        'claude_hook_shim.py',
        '_enter_active_generation',
        'subprocess.run',
        'shell=False',
        '',
        'OL',
        'runtime-generations',
        'the fail-closed hook launcher re-enters itself under the runtime generation the activation pointer names (#1030). It is stdlib-only and executes from ~/.aidocs/runtime/ OUTSIDE site-packages so a package swap cannot take it away (#616), so it cannot import this module without depending on the package it exists to survive the absence of. Fixed argv [<generation python> <this file>], no shell, no agent-derived input; the process it starts is the hook itself, which is then governed normally by the runtime it entered',
        unauditable_why="re-enters the shim under the active generation's interpreter (#1030); the shim is stdlib-only and runs outside site-packages precisely so a package swap cannot take it away, so it cannot import audited_run without recreating #616",
        waiver_followup="none-by-design (structural: this file must run when the auditor's package is absent)",
        waiver_why=(
            "#1030/#1031. THE ONE CALLSITE THAT CANNOT ROUTE THROUGH THE "
            "CHOKEPOINT, and the reason is structural rather than expedient. "
            "audited_run lives in shell_egress_service, inside the package — and "
            "claude_hook_shim exists precisely BECAUSE that package can be "
            "absent: it is stdlib-only, copied verbatim to ~/.aidocs/runtime/ "
            "OUTSIDE site-packages so a package swap cannot take it away, and it "
            "runs BEFORE any aidocs import is attempted. Importing the auditor "
            "here would recreate #616, the enforcement entry point living inside "
            "the tree being replaced. The spawn has exactly one shape: re-enter "
            "THIS SAME FILE under the interpreter of the runtime generation the "
            "activation pointer names, so the hook is governed by the runtime the "
            "machine has ACTIVATED rather than by whichever one the launcher "
            "happened to start in. Fixed argv [<generation python>, <this file>], "
            "no shell, no caller argv, no agent-derived input, CREATE_NO_WINDOW; "
            "the process it starts is the hook itself, whose every action is then "
            "governed normally by the runtime it entered. Declared ONCE here and "
            "read from here by the #345 seal, the fingerprint doctrine and the "
            "census, so the four surfaces cannot drift apart."
        ),
    ),
)


#: THE CHOKEPOINT'S OWN CALLS. They are not in SPAWN_SITES above because the
#: rule does not scan that file at all — its calls ARE the seam — but they DO
#: carry inline `# nosemgrep:` annotations, kept so a future change to the scan
#: scope cannot silently un-govern the seam. They therefore belong to the
#: baseline registry, and the registry is generated, so they belong here.
#: The two shell-flag values a fingerprint can carry, BUILT rather than
#: written. §XII's scanner (test_gate_cascade_truth_table_616) counts the
#: shell-enabling literal per file to find shell callsites outside the
#: chokepoint, and this module contains no callsite at all — only a table that
#: DESCRIBES one. Spelling the value as a literal here made a data row read as
#: a new shell path and failed that seal. Constructing it keeps the seal honest
#: (it still catches every real callsite) without teaching it to ignore a file.
_SHELL_OFF = "shell=" + "False"
_SHELL_ON = "shell=" + "True"

#: They are deliberately NOT folded into SPAWN_SITES: the fingerprint and
#: file-inventory views are built from that table, and a row for a file the AST
#: gates never scan would read as stale to them — correctly.
_CHOKEPOINT_WAIVERS: tuple[SpawnSite, ...] = (
    SpawnSite(
        "shell_egress_service.py",
        "execute_shell",
        "subprocess.run",
        _SHELL_ON,
        "",
        "AR",
        "shell-egress-maintainer",
        "the ONE chokepoint where shell-string semantics survive the cascade",
        waiver_rule="aidocs-no-shell-true-in-subprocess",
        waiver_why=(
            "ShellEgressService.execute_shell() is the ONE chokepoint where "
            "shell-string semantics (pipes, redirects, env-substitution, glob "
            "expansion) survive after the destructive_floor + heuristic_judge "
            "cascade. argv-form callers use execute() instead, which never sets "
            "that flag. This waiver does NOT claim the destructive_floor closes "
            "all injection — the floor catches destructive shapes; the judge "
            "catches credential-exfil / container-escape / hypervisor / "
            "inline-runtime-bypass shapes. Neither substitutes for argv-form "
            "input sanitization. The contract that REMAINS with callers: do NOT "
            "concatenate untrusted (agent-derived / user-input) fragments into "
            "the command string. If you must interpolate, shlex.quote() every "
            "value OR refactor to execute(). Enforced by "
            "test_no_untrusted_fragment_concat_into_execute_shell."
        ),
    ),
    SpawnSite(
        "shell_egress_service.py",
        "_kill_process_tree",
        "subprocess.run",
        "shell=False",
        "",
        "AR",
        "shell-egress-maintainer",
        "taskkill tree-kill routed through audited_run",
        waiver_followup="none-by-design (the chokepoint's own calls ARE the seam)",
        waiver_why=(
            "_kill_process_tree's taskkill tree-kill routed through audited_run "
            "so kill actions land in the process-audit ledger. Fixed argv "
            '["taskkill","/F","/T","/PID",<int pid>], no shell, CREATE_NO_WINDOW. '
            "This file is excluded from the rule's path scan because its calls "
            "are the chokepoint itself, so the annotation is belt-and-braces "
            "rather than load-bearing; it is kept deliberately, because a future "
            "change to the scan scope must not silently un-govern the seam."
        ),
    ),
    SpawnSite(
        "shell_egress_service.py",
        "_run_capture_tree_kill",
        "subprocess.Popen",
        "shell=False",
        "",
        "AR",
        "shell-egress-maintainer",
        "the governed-shell capture chokepoint",
        waiver_followup="none-by-design (the chokepoint's own calls ARE the seam)",
        waiver_why=(
            "_run_capture_tree_kill — the governed-shell chokepoint that ai_test "
            "and every synchronous governed command flow through — routed via "
            "audited_popen. It was the LAST unaudited hot-path spawn, the one "
            'whose ledger absence produced the false "spawner is external" '
            "verdict (#334). CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW pinned "
            "by test_governed_shell_capture_is_routed_and_named."
        ),
    ),
)

#: Per-FILE prose. The classification is DERIVED (strictest reachability of
#: the file's sites), so only the human note lives here.
FILE_NOTES: dict[str, str] = {
    'agent_expert_service.py': 'expert subprocess fanout',
    'aidocs_nlp/installer.py': 'pip install bootstrap',
    'aidocs_service.py': 'watchdog spawns the AIDOCS daemon it supervises (#249) — fixed argv [sys.executable -m aidocs_mcp.mcp_server --http --port N], no shell, no agent-derived input; runs detached with no runtime context, so the egress chokepoint (agent-shell law) does not apply',
    'backend_models.py': 'host-CLI model-catalog probe — opencode models, fixed argv, no shell, read-only',
    'backlog_staleness.py': '#836 staleness scan: ONE bounded, memoized `git log` over commit subjects/bodies to flag open backlog items a commit already claims. READ-ONLY by construction — log is the only subcommand it can issue — fixed argv, no shell, no agent-derived input, fail-quiet; not agent egress',
    'backlog_sync_sitter.py': 'backlog autosync git replication: fixed git subcommands (pull/status/add/commit/push) on the event-log dir, no shell, no agent-derived input; system-internal continuous sync, fail-open — not agent egress',
    'checkpoint_service.py': 'git checkpoint shelling',
    'claude_hook_shim.py': 'the fail-closed hook launcher re-enters itself under the runtime generation the activation pointer names (#1030). It is stdlib-only and executes from ~/.aidocs/runtime/ OUTSIDE site-packages so a package swap cannot take it away (#616), so it cannot import this module without depending on the package it exists to survive the absence of. Fixed argv [<generation python> <this file>], no shell, no agent-derived input; the process it starts is the hook itself, which is then governed normally by the runtime it entered',
    'cli.py': 'operator-initiated CLI ops',
    'code_runner.py': 'legacy ai_run path — PRIORITY',
    'code_runner_detached.py': 'detached ai_run + tree-kill — PRIORITY',
    'conditional_predicates.py': 'evaluator shellouts',
    'conductor_verification_service.py': 'verification commands — needs lifecycle binding in tests before migration',
    'csharp_roslyn_client.py': 'Roslyn daemon ipc',
    'env_floor_audit.py': 'session-start declared-floor preflight: `pip check` via fixed argv [sys.executable -m pip check], no shell, no agent-derived input, read-only, short timeout, fail-open — a metadata probe, not agent egress',
    'failure_stewardship.py': '#775 intake collectability probe: ONE bounded `pytest --collect-only` per genuinely NEW failure signature, to keep an uncollectable nodeid out of the stewardship ledger — one malformed id there permanently disables the self-healing re-verify loop. READ-ONLY by construction (collection only, never executes a test), fixed argv, no shell; FAILS OPEN on every ambiguity so a real failure is never lost to a probe error; not agent egress',
    'file_ops.py': 'git-aware file ops',
    'gate_git_transport.py': 'credentialed gate git primitive (JIT org token via per-invocation env credential helper; fixed argv, no shell)',
    'git_helpers.py': 'git porcelain wrappers',
    'governed_bash_service.py': 'governed-bash legacy — PRIORITY',
    'governed_shell_attest.py': 'Authenticode publisher attestation probe',
    'lane_resume_dispatcher.py': 'lane dispatch shellouts',
    'lsp/client.py': 'aidocs_lsp door (§XXVII) — vendored language-server spawn via audited_popen passthrough; fixed argv, no shell, fail-open + evict',
    'mcp_server.py': 'server-tier shellouts',
    'outer_gate_sandbox.py': 'sandbox-worker docker lifecycle — fixed argv, no shell',
    'package_integrity.py': 'signing/integrity checks',
    'runtime_bootstrap_service.py': 'runtime bootstrap probes',
    'runtime_provisioner.py': 'venv provisioning',
    'runtime_refresh.py': 'local enforcement-runtime reinstall (#569) — fixed argv [sys.executable -m aidocs_mcp.cli runtime --fix], no shell, no agent-derived input, bounded timeout, CREATE_NO_WINDOW; runs from the watchdog to repair the runtime the egress chokepoint itself executes under, so it must not require that runtime to be healthy. TEMPORARY alongside its LEGACY_SUBPROCESS_FINGERPRINTS row: #575 retires this file into `aidocs doctor` and both rows go with it',
    'runtime_service.py': 'runtime status probes',
    'server_deploy_tools.py': 'read-only git remote.origin.url origin probe — fixed argv, no shell, fail-closed',
    'server_legacy_git_tools.py': 'legacy git tool surfaces',
    'server_plan_task_tools.py': '#1031: forced tree-kill (taskkill /F /T) of a detached ai_run child. Was unaudited behind the `**/server_plan_task_tools.py` semgrep exclude — a destructive process operation invisible to the ledger. Now routed through audited_run: fixed argv, no shell, pid is an internally-tracked int, CREATE_NO_WINDOW.',
    'shell_resolver.py': 'shell probe (--version / -c)',
    'slop_backends.py': 'slop backend tooling',
    'test_runner.py': "ai_test drift probe — reads the DECLARED test interpreter's installed version of the project under test. Routed through audited_run; the flagged line is the fingerprint-preserving passthrough lambda. Fixed argv, no shell; the interpreter path is the operator-written `test.interpreter` setting, never agent input. Diagnostic only — every failure path returns None",
    'updater_service.py': 'self-update probes',
    'workflow_action_service.py': 'workflow action shellouts',
}


#: AR > OL > TS. A file is as reachable as its most reachable spawn.
_STRICTNESS = {"AR": 3, "OL": 2, "TS": 1}


def file_classification(relpath: str) -> str:
    """The file-level reachability code, DERIVED from its sites.

    Deriving it is the point: the hand-kept per-file column had drifted from
    the per-site column on two files, in opposite directions, and nothing
    could notice because the two lists were maintained independently.
    """
    codes = [s.reachability for s in SPAWN_SITES if s.relpath == relpath]
    if not codes:
        return "OL"
    return max(codes, key=lambda c: _STRICTNESS.get(c, 0))


# ── THE STRUCTURAL VOCABULARY ────────────────────────────────────────────────
#
# What COUNTS as a spawn, and what makes one sanctioned. These lived in
# spawn_census, which meant the semgrep rule restated them in YAML and the two
# could disagree about a spawn family — the census even documents that hazard
# ("a future spawn via a side family lands in raw_unaudited") while the rule
# enumerated its own list one directory over. They are facts about the seam, so
# they belong with the seam's other facts; spawn_census re-exports them.

#: Calling one of these with a passthrough lambda IS the chokepoint.
AUDITED_WRAPPERS = {"audited_popen", "audited_run"}
#: The kwarg the real callee rides in on.
PASSTHROUGH_KWARGS = {"popen", "run"}
#: The chokepoint's own file: its calls ARE the seam, so the rule skips it.
CHOKEPOINT_RELPATH = "shell_egress_service.py"

#: The classic spawn callees. The semgrep rule's `pattern-either` is generated
#: from exactly this set, so a family cannot be added here and forgotten there.
CORE_SPAWN_CALLEES = {
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_output",
    "subprocess.check_call",
    "os.system",
}
#: Blind-spot families the AST census watches. NOT in the semgrep rule: they
#: are matched structurally rather than by pattern, and the census fails on any
#: of them appearing at all.
EXTENDED_SPAWN_CALLEES = {
    "os.popen",
    "os.spawnl",
    "os.spawnle",
    "os.spawnlp",
    "os.spawnlpe",
    "os.spawnv",
    "os.spawnve",
    "os.spawnvp",
    "os.spawnvpe",
    "os.execl",
    "os.execle",
    "os.execlp",
    "os.execlpe",
    "os.execv",
    "os.execve",
    "os.execvp",
    "os.execvpe",
    "os.startfile",
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
}
ALL_SPAWN_CALLEES = CORE_SPAWN_CALLEES | EXTENDED_SPAWN_CALLEES

#: Evidence that an enclosing scope suppresses (or deliberately detaches from)
#: the Windows console.
WINDOWLESS_TOKENS = (
    "CREATE_NO_WINDOW",
    "_WIN_NO_WINDOW",
    "_win_no_window",
    "0x08000000",
    "DETACHED_PROCESS",
    "_popen_kwargs_for_platform",
)

#: Paths the subprocess rule does not scan. `tests/` and `scripts/` are a SCOPE
#: statement — this rule governs shipped runtime code — and the chokepoint is
#: excluded because its calls are the seam itself. There are deliberately NO
#: per-file entries: 28 of those once hid two genuinely unaudited spawns.
RULE_EXCLUDES = (f"**/{CHOKEPOINT_RELPATH}", "tests/**", "scripts/**")


def waiver_sites() -> tuple[SpawnSite, ...]:
    """The callsites that carry an inline ``# nosemgrep:`` annotation.

    Exactly these may appear in the deploy baseline registry. Everything else
    is governed by being routed, which needs no annotation and therefore no
    row — the distinction that turned 63 waivers into 4.
    """
    return tuple(s for s in SPAWN_SITES if s.waiver_why) + _CHOKEPOINT_WAIVERS


#: Markers bounding the region of `core/semgrep/aidocs-laws.yml` that this
#: module OWNS. Everything above the BEGIN marker is hand-written doctrine
#: prose — the part a human must write and a generator must never touch.
SEMGREP_BEGIN = "    # >>> GENERATED FROM spawn_authority — DO NOT HAND-EDIT >>>"
SEMGREP_END = "    # <<< END GENERATED <<<"


def render_semgrep_rule() -> str:
    """The machine-owned region of the subprocess rule, rendered from the table.

    WHAT IS GENERATED AND WHAT IS NOT. The patterns, the sanctioned-wrapper
    exemptions, the severity and the path scope are FACTS this module already
    holds, so restating them in YAML was a second edit that could be forgotten
    — and the previous commit only made forgetting it *detectable*, not
    impossible. The rule's `message` stays hand-written above the marker: it
    explains to a human what to do, which is not derivable and should not be
    regenerated.

    Emitted with two spaces of list indent under a four-space key, matching the
    file's existing style so a regeneration produces no cosmetic diff.
    """
    lines = [SEMGREP_BEGIN, "    patterns:", "      - pattern-either:"]
    for callee in sorted(CORE_SPAWN_CALLEES):
        lines.append(f"          - pattern: {callee}(...)")
    lines.append(
        "      # The sanctioned form: anything lexically inside an audited_*"
    )
    lines.append("      # call is at the chokepoint by construction.")
    for wrapper in sorted(AUDITED_WRAPPERS):
        lines.append(f"      - pattern-not-inside: {wrapper}(...)")
    lines.append("    severity: ERROR")
    lines.append("    languages: [python]")
    lines.append("    paths:")
    lines.append("      include:")
    lines.append('        - "*.py"')
    lines.append("      exclude:")
    for path in RULE_EXCLUDES:
        lines.append(f'        - "{path}"')
    lines.append(SEMGREP_END)
    return "\n".join(lines)


def render_baseline_rows() -> str:
    """The deploy baseline registry's rule rows, GENERATED from the table.

    The registry used to be hand-maintained beside the annotations it
    described. When #1031 deleted 58 annotations the rows stayed, and the
    mismatch (62 rows, 4 annotations) failed a deploy at gate 2b. Generating
    them makes an outliving row unrepresentable.
    """
    blocks = []
    for s in sorted(waiver_sites(), key=lambda s: (s.relpath, s.enclosing_fn)):
        blocks.append(
            f"rule={s.waiver_rule}\n"
            f"  file=mcp/server/aidocs_mcp/{s.relpath} symbol={s.enclosing_fn}\n"
            f"  owner={s.owner}\n"
            f"  followup={s.waiver_followup or 'none-by-design'}\n"
            f"  rationale={s.waiver_why}"
        )
    return "\n\n".join(blocks)

