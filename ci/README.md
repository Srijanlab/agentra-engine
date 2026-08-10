# CI workflow -- not yet active

`github-actions-ci.yml` in this directory is a real, ready-to-use GitHub
Actions workflow (py_compile across `agentra/`, `npm run build` in
`agentra/web/`) -- it isn't at `.github/workflows/ci.yml` because the
`agentra-orchestrator` GitHub App (the one this repo is itself registered
under, in agentra's own dashboard) was never granted the **Workflows**
permission. GitHub Apps categorically cannot push anything under
`.github/workflows/` without that scope -- confirmed live: agentra's own
autonomous cycle tried to add this file directly, got rejected with
`refusing to allow a GitHub App to create or update workflow ... without
'workflows' permission`, adapted by moving it here, and correctly stopped
rather than retrying blindly once a human-decision point was reached.

## To activate

1. github.com/settings/apps/agentra-orchestrator -> Permissions & events ->
   Repository permissions -> **Workflows: Read and write** -> Save (GitHub
   will require re-approving the install with the new scope).
2. `mv ci/github-actions-ci.yml .github/workflows/ci.yml && rmdir ci 2>/dev/null || true`
3. Commit and push -- now allowed, since the App has the permission.

Tracked as a real backlog item in agentra's own feature queue (app
`agentra`): "Add app-level auth to every /apps, /system/*, ... endpoint" is
a separate, related item about the same GitHub App's permission scope more
broadly.
