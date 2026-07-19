# Contributing to AIDOCS

AIDOCS is built to be extended. Here's where community contributions make the biggest impact.

## Language Descriptors

AIDOCS indexes code using TOML descriptor files (`mcp/server/aidocs_mcp/index_languages/*.toml`). Each descriptor teaches AIDOCS how to parse a language — outline patterns, component semantics, file roles, and module hints.

Currently shipped: Python, TypeScript, JavaScript, JSX, TSX, Rust, Go, Java, C#, Ruby, Kotlin, PHP, Swift, Dart, Elixir, Lua, SQL, CSS, SCSS, LESS, Sass, HTML, Vue, Svelte, TOML, YAML, JSON, Prisma, Shell, PowerShell.

**Wanted:**
- Zig, Nim, OCaml, Haskell, Scala, Clojure, F#, R, Julia, Perl
- Terraform/HCL, Dockerfile, Makefile, CMake
- GraphQL, Protobuf, Thrift
- Markdown (structural — headings, links, code blocks)
- Improvements to existing descriptors (better outline patterns, component semantics)

Contributing a descriptor is one TOML file — see `mcp/INDEX_LANGUAGE_DESCRIPTORS.md` for the schema.

## Host / Harness Integrations

AIDOCS currently supports Claude Code (hooks) and OpenCode (plugin). We want more:

- **Cursor** — deeper integration beyond startup-only packaging
- **GitHub Copilot CLI** — initial hook/plugin path
- **Windsurf / Codeium** — MCP-based integration
- **Continue.dev** — MCP or plugin integration
- **Aider** — hook or wrapper integration
- **Custom MCP clients** — any client that speaks MCP stdio

Each integration needs: startup routing, managed-mode awareness, and tool guardrails.

## Action Tokens (Intent Classification)

AIDOCS classifies user prompts into action kinds (edit, understand, trace, etc.) using language-specific token files (`action_tokens/*.toml`).

Currently shipped: English.

**Wanted:**
- Spanish, French, German, Portuguese, Italian, Romanian, Dutch
- Japanese, Chinese, Korean
- Hindi, Arabic, Turkish
- Any language where developers work

Contributing is one TOML file per language with translated intent phrases.

## Benchmarks

- Realistic prompt sets that test deep retrieval vs naive grep
- Multi-language project samples (public-safe)
- Workflow-heavy scenarios (session handoff, plan execution)
- Adversarial prompts that expose weak spots

## Dashboard

- UI/UX feedback and bug reports
- Feature requests for the operator dashboard
- Accessibility improvements
- Theme customization ideas

## How to Contribute

1. Fork the repo
2. Create a branch for your contribution
3. Submit a PR with a clear description

For language descriptors and action tokens, a single TOML file is enough — no Python code needed.

See [Issues](https://github.com/cristian1991/AIDOCS/issues) for specific tasks or open a discussion.

## Review and enforcement — read this before you open a PR

AIDOCS treats **every submitted file as untrusted data until a human clears it** — this is the same quarantine posture the runtime applies to any external content. Review is not a formality; it is an adversarial read. Two rules govern the outcome, and both are grounds for an immediate, permanent ban with no appeal on the first offense:

1. **Every file with a legitimate purpose is inspected — anything hostile in it gets you banned.** A descriptor, a token file, a test, a doc: each is read line by line. If the file does something *beyond what it claims to do*, or hides intent behind obfuscation, encoding, or misdirection, you are banned. "It's just a config file" is not a shield — a TOML descriptor can carry a regex bomb, a data file can carry a prompt injection, a test can smuggle a network call.

2. **Every file that does not make sense gets you banned.** Every file in a PR must have a declared, obvious reason to exist. An unexplained file, a file unrelated to the PR's stated purpose, a binary you can't account for, a "helper" nobody asked for, a change to an area your PR has no business touching — if a reviewer cannot look at it and immediately understand *why it is here*, it does not get the benefit of the doubt. Unexplained is treated as hostile.

### What gets you banned (non-exhaustive)

- **Obfuscation of any kind** — base64/hex/rot payloads, minified or unreadable logic presented as source, string-assembled commands, unicode homoglyphs, zero-width characters, or code written to be hard to review. Clarity is mandatory; opacity is treated as intent to hide.
- **Credential or secret harvesting** — reading env vars, key files, `~/.aidocs`, `~/.ssh`, token stores, or the empire/kingdom databases and moving that data anywhere it doesn't belong.
- **Exfiltration or unexpected egress** — any network call, DNS lookup, telemetry, or outbound connection that the PR's purpose does not plainly require. Descriptors, tokens, tests, and docs have **zero** reason to touch the network.
- **Supply-chain tampering** — new dependencies added without justification, unpinned or digest-less versions, lockfile edits that don't match the stated change, install/build hooks, `setup.py`/`pyproject` execution hooks, or pulling from non-canonical registries.
- **Gate, audit, or sandbox tampering** — touching the security cascade, the deploy gate, the output guard, RBAC, the audit ledger, or the sandbox posture to weaken, bypass, or silence any of them. Weakening a test to make a red truth go green is in this category.
- **Prompt injection in data** — instructions embedded in descriptors, token files, sample projects, fixtures, memory/skill/doctrine content, or PR text that try to steer a reviewing agent or a future runtime. Data is data; it never issues commands.
- **Destructive or wildcard file operations** — recursive deletes, broad writes, path traversal, or edits to protected/indexed source outside your PR's scope.
- **Scope violation** — a "language descriptor" PR that edits Python runtime, a "docs" PR that ships executable code, or any diff whose reach exceeds its title. One PR, one clear purpose.
- **License laundering or provenance fraud** — code copied from an incompatible license, a false sign-off, or claiming authorship of work that isn't yours.
- **Low-effort or automated spam** — mass PRs, AI-slop with no working change, or churn submitted to farm contribution counts.

### How to stay on the right side of it

- **Declare every file.** Your PR description should account for each file it touches and why. If you can't explain a file in one sentence, don't include it.
- **Keep it readable.** Prefer the boring, legible implementation. If a reviewer has to decode it, you've already failed the review.
- **Stay in your lane.** A descriptor is a descriptor; a token file is a token file. Don't reach into runtime, gates, or CI unless that *is* the contribution (and then say so up front, ideally via an RFC first).
- **Ask before anything unusual.** A genuinely novel need (a new dependency, a build step, a network-touching benchmark) goes in the PR conversation *before* the code, not smuggled inside it.
- **Assume adversarial review.** We read like an attacker wrote the PR, because sometimes one does. Honest contributors have nothing to fear from that — it's the same read that keeps the ecosystem safe for everyone downstream.

Honest mistakes are met with correction, not the banhammer — ask a question, mislabel a commit, misunderstand the schema, and we'll help you fix it. The ban is for **deception and hostility**, not for being new. When intent is ambiguous, the reviewer decides, and unexplained-and-suspicious resolves against the submitter every time.

## Workflow expectations

- **Conventional commits** keep the auto-generated release status clean. Use one of `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `perf:`, `ci:`. The deploy gate's status pipeline categorizes by prefix.
- **Sign-off on every commit** (`git commit --signoff`) is appreciated as a lightweight DCO-style acknowledgement that you wrote the change and can submit it. We don't run a formal CLA bot; the sign-off is a courtesy.
- **Run the doctrine tests before opening the PR**:

  ```bash
  cd mcp && PYTHONPATH=server python -m pytest -p no:cacheprovider --no-cov
  ```

  The PR Doctrine workflow (`.github/workflows/pr-doctrine.yml`) runs the same suite plus ruff and the public-export verifier; landing them locally first makes the PR loop snappier.

- **Bigger architectural changes** belong in `docs/rfcs/` first — a short RFC, then the code.

## Legal

This guide deliberately introduces no licensing language. The repository's `LICENSE` file is the authoritative source. The project is in a launch-preparation window — managed / enterprise terms are not finalized yet and will be coordinated with counsel before any public announcement. See [`COMMERCIAL.md`](COMMERCIAL.md) for the contact placeholder.

If you are contributing code with the expectation that future managed-offering terms will or will not apply to it, **please ask in the PR before submitting**. We will not surprise contributors with surprise re-licensing.
