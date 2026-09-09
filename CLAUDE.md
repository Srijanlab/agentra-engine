# Claude Guidelines & Codebase Rules

When interacting with this codebase, please adhere to the following architectural, formatting, and behavioral guidelines.

## 1. Comment & Documentation Style
- **Concise Docstrings:** Do not write excessively long, multi-paragraph docstrings. Module and function docstrings should be limited to a single, clear summary sentence.
- **No Dead Code:** Do not leave large blocks of commented-out code. If code is no longer needed, delete it completely.
- **Avoid Over-Commentation:** Rely on readable, self-documenting code. Do not write large paragraphs of inline `#` comments unless absolutely critical for explaining an obscure bug or workaround.

## 2. Design Patterns & Architecture
- **Single Responsibility Principle (SRP):** Every class, function, and module should have one, and only one, reason to change. Do not create "god objects" or massive utility files.
- **Sub-folder Organization:** Organize code logically into domain-specific subfolders. Avoid dumping everything into the root of a package. Use subfolders to encapsulate specific features and their internal logic.

## 3. File Size Constraints
- **500 Lines Limit:** No file in this codebase should exceed 500 lines. If a file is approaching or exceeding 500 lines, it is a clear sign that SRP is being violated. You must refactor and split the file into smaller, focused modules inside an appropriate subfolder.

## 4. `.agentra/` Specs — Do Not Hand-Edit
Ownership and freshness rules are in [`docs/agentra-spec.md`](docs/agentra-spec.md).
- **Never hand-edit this repo's generated specs:** `.agentra/architecture.md`, `.agentra/design.md`, `.agentra/testing.md`, `.agentra/state.json`, and `.agentra/memory/architecture/{codebase,design,deployment,documentation,local-test-summary}.md`. They are `owner: agent:codebase` / `generated` — the Codebase / Testing Agents rewrite them (delta-mode) on the next cycle after a ship, keyed off `state.json.indexed_sha`. A manual edit is churn and gets overwritten.
- **Exception — human-owned:** `.agentra/memory/architecture/testing-notes.md` (test gotchas a human wants preserved) and `.agentra/product.md`. Edit these by hand freely.
- **Architecture-level changes go in the coordination repo**, not here: `Srijanlab/agentra` → `.agentra/architecture.md` (the system map) and an append-only ADR in `.agentra/decisions/NNNN-*.md`. Both are `owner: human`; no agent code writes them. Update them in the same change set when you alter the RPC boundary, deploy topology, credential ownership, or the cycle/pipeline contract.
