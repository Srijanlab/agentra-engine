"""agentra CLI — the engine's admin surface.

The engine does not run cycles / promotions / prod-debug (that is agentra-loop);
this CLI manages the registry, per-app environment config, the inbox, and the
`serve` entry point.
"""

import argparse
from pathlib import Path

from agentra import environments, observability, registry
from agentra.memory import Memory


def _prompt(label: str, default: str) -> str:
    val = input(f"{label} [{default}]: ").strip()
    return val or default


def _prompt_bool(label: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    val = input(f"{label} [{suffix}]: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")


def _interactive_env_init(detected: environments.EnvironmentConfig) -> environments.EnvironmentConfig:
    print("Configuring the local -> pre-prod -> prod pipeline. Press enter to accept each detected default.\n")
    local_branch = _prompt("Feature branch prefix (each feature gets its own {prefix}/run-id-slug branch)", detected.local_branch)
    pre_prod_branch = _prompt("Pre-prod branch", detected.pre_prod_branch)
    prod_branch = _prompt("Prod branch", detected.prod_branch)
    vercel = _prompt_bool("Vercel configured for this app?", detected.vercel)
    firebase = _prompt_bool("Firebase configured for this app?", detected.firebase)
    firebase_pre_prod_alias = detected.firebase_pre_prod_alias
    firebase_prod_alias = detected.firebase_prod_alias
    if firebase:
        firebase_pre_prod_alias = _prompt("Firebase PRE-PROD project alias (in .firebaserc)", detected.firebase_pre_prod_alias)
        firebase_prod_alias = _prompt("Firebase PROD project alias (in .firebaserc)", detected.firebase_prod_alias)
    ci_cd_on_push = _prompt_bool(
        "Does this app already have CI/CD that deploys on push to the pre-prod/prod branches "
        "(GitHub Actions, Vercel git integration, etc.)? If yes, the Deployment Agent will only "
        "merge+push and verify the resulting CI run, not also deploy directly.",
        detected.ci_cd_on_push,
    )
    print(
        "\nBy default, production is only ever touched via `agentra promote` (a human runs it "
        "deliberately). The option below lets the Production Debugging Agent skip that and deploy "
        "a verified hotfix straight to prod on its own, once it has passed pre-prod testing."
    )
    auto_remediate_prod = _prompt_bool("Allow autonomous hotfix deploys to production (no human approval)?", False)
    return environments.EnvironmentConfig(
        local_branch=local_branch,
        pre_prod_branch=pre_prod_branch,
        prod_branch=prod_branch,
        vercel=vercel,
        firebase=firebase,
        firebase_pre_prod_alias=firebase_pre_prod_alias,
        firebase_prod_alias=firebase_prod_alias,
        ci_cd_on_push=ci_cd_on_push,
        auto_remediate_prod=auto_remediate_prod,
    )


def main() -> None:
    import atexit

    observability.init_observability()
    atexit.register(observability.flush)
    parser = argparse.ArgumentParser(prog="agentra")
    sub = parser.add_subparsers(dest="command", required=True)

    env_p = sub.add_parser("env", help="Manage per-app local/pre-prod/prod environment config")
    env_sub = env_p.add_subparsers(dest="env_command", required=True)
    env_init_p = env_sub.add_parser("init", help="Detect and configure this app's environment pipeline")
    env_init_p.add_argument("--repo", required=True, type=Path)
    env_init_p.add_argument("--yes", action="store_true", help="Skip prompts; use detected values and flags as-is")
    env_init_p.add_argument("--auto-remediate-prod", action="store_true", default=False)

    obj_p = sub.add_parser("objective", help="Manage the persistent business objective for a repo")
    obj_sub = obj_p.add_subparsers(dest="objective_command", required=True)
    obj_set_p = obj_sub.add_parser("set", help="Set the current objective")
    obj_set_p.add_argument("objective_text")
    obj_set_p.add_argument("--repo", required=True, type=Path)
    obj_show_p = obj_sub.add_parser("show", help="Show the current objective")
    obj_show_p.add_argument("--repo", required=True, type=Path)

    apps_p = sub.add_parser("apps", help="Manage the multi-app registry")
    apps_sub = apps_p.add_subparsers(dest="apps_command", required=True)
    apps_add_p = apps_sub.add_parser("add", help="Register an app")
    apps_add_p.add_argument("name")
    apps_add_p.add_argument("--repo", required=True, type=Path)
    apps_sub.add_parser("list", help="List registered apps")
    apps_remove_p = apps_sub.add_parser("remove", help="Unregister an app")
    apps_remove_p.add_argument("name")

    submit_p = sub.add_parser("submit", help="Durably submit a request into an app's inbox")
    submit_p.add_argument("--app", required=True, help="Registered app name (see `agentra apps list`)")
    submit_p.add_argument("--type", required=True, choices=registry.REQUEST_TYPES)
    submit_p.add_argument("--description", required=True)
    submit_p.add_argument("--severity", default=None, help="For --type bug: low|medium|high|critical")
    submit_p.add_argument("--screenshot-url", default=None)

    sub.add_parser("dispatch", help="Absorb everything currently in the inbox into each app's ledgers")

    serve_p = sub.add_parser("serve", help="Run the always-on HTTP server for scheduled/alarm/queue triggers")
    serve_p.add_argument("--host", default="0.0.0.0")
    serve_p.add_argument("--port", type=int, default=None, help="Defaults to $PORT if set, else 8080")

    args = parser.parse_args()

    if args.command == "env" and args.env_command == "init":
        repo = args.repo.resolve()
        detected = environments.detect(repo)
        if args.yes:
            detected.auto_remediate_prod = args.auto_remediate_prod
            config = detected
        else:
            config = _interactive_env_init(detected)
        environments.save(repo, config)
        print("\nSaved to GitHub Actions Variables (AGENTRA_*).")
        print(config)

    elif args.command == "objective" and args.objective_command == "set":
        Memory(args.repo.resolve()).set_objective(args.objective_text)
        print(f"objective: {args.objective_text}")

    elif args.command == "objective" and args.objective_command == "show":
        objective = Memory(args.repo.resolve()).get_objective()
        print(objective if objective else "(no objective set for this repo)")

    elif args.command == "apps" and args.apps_command == "add":
        registry.register_app(args.name, str(args.repo.resolve()))
        print(f"Registered {args.name!r} -> {args.repo.resolve()}")

    elif args.command == "apps" and args.apps_command == "list":
        apps = registry.list_apps()
        if not apps:
            print("(no apps registered)")
        for name, info in apps.items():
            print(f"  {name}: {info.get('repo_path', info.get('repos'))}")

    elif args.command == "apps" and args.apps_command == "remove":
        removed = registry.remove_app(args.name)
        print(f"Removed {args.name!r}" if removed else f"{args.name!r} was not registered")

    elif args.command == "submit":
        request_id = registry.submit_request(
            app=args.app,
            request_type=args.type,
            description=args.description,
            severity=args.severity,
            screenshot_url=args.screenshot_url,
        )
        print(f"Submitted request {request_id} for app {args.app!r} (pending in the inbox until `agentra dispatch` runs)")

    elif args.command == "dispatch":
        summary = registry.dispatch_once()
        print(f"Resumed stale (crashed prior run): {summary.resumed_stale}")
        print(f"Processed: {summary.processed}")
        if summary.errors:
            print(f"Errors ({len(summary.errors)}):")
            for err in summary.errors:
                print(f"  - {err}")

    elif args.command == "serve":
        import os

        import uvicorn

        port = args.port if args.port is not None else int(os.environ.get("PORT", "8080"))
        print(f"[agentra] serving on {args.host}:{port} -- /health, /trigger/*, /internal/*")
        uvicorn.run("agentra.server:app", host=args.host, port=port)


if __name__ == "__main__":
    main()
