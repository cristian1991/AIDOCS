# Special Thanks

AIDOCS is original work. Its memory model, security gate cascade, taxonomy judge, conductor and lanes, indexed-retrieval engine, audit ledger, deploy-gate authority, and doctrine are AIDOCS's own design and code — the overwhelming majority of this project.

For a handful of standard, well-solved jobs — parsing, charting, packaging — AIDOCS *uses* mature third-party tools rather than reinventing them. They are utilities and ornaments around the AIDOCS core, not its foundation. This page credits each of them and thanks their authors.

Each tool is the property of its authors and is used under its own license; this list is attribution, not a claim of ownership. Where a version is pinned, the binding pin lives in `mcp/pyproject.toml` (Python) and `apps/aidocs-dashboard/package.json` (dashboard).

> If your project is listed here and you'd like the attribution corrected, the license noted differently, or a link added, please open an issue — we'll fix it gladly.

---

## Vendored

Shipped inside this repository so a fresh install needs no extra fetch.

| Project | Where | License | What AIDOCS uses it for |
|---|---|---|---|
| **MemPalace** | `third_party/mempalace/` | MIT | Memory-palace backend for the durable-memory layer |

## Python runtime — libraries AIDOCS uses

| Project | License | Used for |
|---|---|---|
| **FastMCP** | Apache-2.0 | MCP server transport AIDOCS runs its tools over |
| **Model Context Protocol SDK** (`mcp`) | MIT | The MCP protocol implementation |
| **Pydantic** | MIT | Typed models + validation at the tool boundary |
| **tree-sitter** + grammars (C#, HTML, Python, JavaScript, TypeScript) | MIT | Structural parsing for the syntax-validating edit path and the indexer |
| **PyYAML** | MIT | YAML edit-time validation |
| **click** | BSD-3-Clause | CLI command surface (`aidocs …`) |
| **tomli** | MIT | TOML parsing on Python < 3.11 |
| **setuptools** | MIT | Build backend |

## Language & retrieval

| Project | License | Used for |
|---|---|---|
| **spaCy** + `en_core_web_sm` | MIT | POS / dependency parse / NER / lemmatization feeding the NLP intent layer |
| **rapidfuzz** | MIT | Fuzzy symbol matching in indexed search |
| **lingua-language-detector** | Apache-2.0 | Language detection for the multi-language NLP stack |
| **ChromaDB** | Apache-2.0 | Vector store behind the memory-palace / semantic layer |

## Security & cryptography

| Project | License | Used for |
|---|---|---|
| **cryptography** | Apache-2.0 / BSD | Cryptographic primitives (signing & verification) |
| **bcrypt** | Apache-2.0 | Password hashing |

## Dashboard

| Project | License | Used for |
|---|---|---|
| **Tauri** | MIT / Apache-2.0 | Native desktop shell for the dashboard |
| **React** + **React-DOM** | MIT | Dashboard UI |
| **Vite** + **@vitejs/plugin-react** | MIT | Dev server + build |
| **Vitest** | MIT | Dashboard tests |
| **TypeScript** | Apache-2.0 | Typed dashboard source |
| **Recharts** | MIT | Charts and metrics visualizations |
| **CodeMirror 6** (`@codemirror/*`) | MIT | In-app code/diff editing |
| **lucide-react** | ISC | Icon set |
| **Tailwind CSS** + **PostCSS** + **Autoprefixer** | MIT | Styling pipeline |

---

## Standards & protocols

- **Model Context Protocol** (Anthropic) — the open protocol AIDOCS speaks to every host.
- **Business Source License 1.1** (MariaDB) — the license model AIDOCS ships under; see [`LICENSE`](LICENSE).
- **Landlock**, **gVisor**, **Syft**, **Grype**, **Sigstore** — projects on the security roadmap as evaluated/adopted backends; see [`PUBLIC_ROADMAP.md`](PUBLIC_ROADMAP.md).

Thank you to the authors of every tool above for the time they save the rest of us.
