Lint: not_configured
Typecheck: pass
Notes: Verified the actual feature: agentra/web/src/components/StandupsPanel.tsx now renders a 'Start standup' gate (started state) and withholds appNames from useStandupChannels until clicked, so no WebSocket -- and therefore no backend generate_standup_updates() LLM call -- fires merely from mounting/viewing the panel (relevant given App.tsx pre-renders all tabs via CSS `hidden` rather than conditional mounting). Web test suite (vitest) and production build (tsc -b + vite build) both pass. Python suite is 308/309 passing; the 1 failure is a pre-existing test-isolation gap in an unrelated alarm-webhook test, not a regression from this change -- reproduced it in isolation and root-caused it to a real env var leaking into the test rather than product code. No source files were modified.

## Pre-prod verification (human-owned)
- Pre-prod (beta) must use its own `AGENTRA_DYNAMODB_TABLE_PREFIX` and distinct `AGENTRA_INTERNAL_TOKEN` / `AGENTRA_TICK_TOKEN` / `AGENTRA_VERIFY_TOKEN`; never share the live loop's registry.
- Send `X-Agentra-Verify-Token: <token>` (not `Authorization`) to read `GET /apps`, `GET /apps/{name}/schedule`, `GET /runs/{run_key}` with no Firebase token. Every other route/method returns 401; Production returns 403 for the header.
- `GET /trigger/cron` needs the tick token (or `CRON_SECRET`); the verify token never works there. See `docs/deployment.md`.
- The OpenAPI schema/docs (`/openapi.json`, `/docs`, `/redoc`) are intentionally auth-gated (401); on non-production only, black-box checks read them via `GET` + `X-Agentra-Verify-Token` (never non-GET; production refuses with 403).
