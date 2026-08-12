Committed on the correct branch (`dev/cb9ed749-set-up-frontend-unit-component-test-tooling-for-the-agentra-`), no push performed per instructions.

Summary of what was done:
- Added `vitest`, `@testing-library/react`, `@testing-library/jest-dom`, `@testing-library/user-event`, `jsdom` as devDependencies (pinned to Node-20-compatible majors to match the existing CI runner, since the latest majors require Node 22+).
- Wired a `test` block (jsdom environment, setup file) into `vite.config.ts` via `vitest/config`'s `defineConfig`, so build and test share one config file.
- Added `src/test/setup.ts`: jest-dom matchers, RTL `cleanup()` (needed because `Modal` portals to `document.body`), and a minimal `EventSource` stub since jsdom doesn't implement it and `RunDetailDrawer` opens one unconditionally.
- Added `npm test` script (`vitest run`).
- Wrote 12 tests across 3 files:
  - `RunDetailDrawer.test.tsx` — full data, missing/undefined `result`, partial `result` shapes, malformed/unknown-agent steps, sparse `AppDetail`, and the failed-run error banner.
  - `AgentsPanel.test.tsx` — full roster renders with zero activity, and aggregation math (success %, cost totals) survives steps missing numeric fields.
  - `StandupsPanel.test.tsx` — empty states, per-app filtering, and the async generate-button flow.
- Wired `npm test` into `ci/github-actions-ci.yml` (runs before the existing `npm run build`, which is unchanged) and added it to the target-repo memory's `test_commands` list (`.agentra/memory/architecture/codebase.md`), plus a small accuracy fix to `ci/README.md`'s description of what the workflow runs.
- Verified `npm test` and `npm run build` both pass cleanly, including `tsc -b` type-checking the new test files.

```json
{
  "feature": "Set up frontend unit/component test tooling (Vitest + RTL) for agentra/web/, with tests for RunDetailDrawer, AgentsPanel, and StandupsPaner",
  "status": "implemented",
  "files_changed": [
    "agentra/web/package.json",
    "agentra/web/package-lock.json",
    "agentra/web/vite.config.ts",
    "agentra/web/src/test/setup.ts",
    "agentra/web/src/components/RunDetailDrawer.test.tsx",
    "agentra/web/src/components/AgentsPanel.test.tsx",
    "agentra/web/src/components/StandupsPanel.test.tsx",
    "ci/github-actions-ci.yml",
    "ci/README.md",
    ".agentra/memory/architecture/codebase.md"
  ],
  "self_test_result": "pass",
  "notes": "npm test (12 tests, 3 files) and npm run build (tsc -b + vite build) both pass locally. Chose vitest@3.2.7/jsdom@26/@testing-library/jest-dom@6.9 (not the newest majors) specifically because those require Node >=22, while ci/github-actions-ci.yml pins node-version 20 -- newer majors installed with EBADENGINE warnings and would risk CI breakage. No dashboard component source files were modified, only test/config/CI/doc files. Committed on dev/cb9ed749-set-up-frontend-unit-component-test-tooling-for-the-agentra-; not pushed, per instructions to only push a feature branch off beta after review (push left to the deployment step)."
}
```