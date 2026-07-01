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
