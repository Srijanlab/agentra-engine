import argparse
import asyncio
from pathlib import Path

from agentra import environments, registry
from agentra.agents.brain import run_autonomous_cycle
from agentra.memory import Memory
from agentra.orchestrator import run_cycle, run_prod_debug_cycle, run_promote


def _read_analytics(path: Path | None) -> str:
    if path is None:
        return "not available"
    text = path.read_text()
    return text[:20000]


def _resolve_objective(repo: Path, cli_value: str | None) -> str:
    """--objective always wins if given; otherwise fall back to the persistent
    setting (`agentra objective set`, or an objective_change request absorbed by
    `agentra dispatch`) so a scheduled/unattended run doesn't need one passed
    in every time."""
    if cli_value:
        return cli_value
    stored = Memory(repo).get_objective()
    if stored:
        return stored
    raise SystemExit(
        "No --objective given and none set for this repo. Either pass --objective, "
        "or run `agentra objective set \"...\" --repo <repo>` first."
    )


def _add_common_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True, type=Path, help="Path to the target repository")
    parser.add_argument(
        "--objective",
        default=None,
        help="Business objective, e.g. 'improve retention'. Omit to use the persistent "
        "setting from `agentra objective set`.",
    )
    parser.add_argument(
        "--feature",
        default=None,
        help="Specific feature to implement. Omit to let the Product Discovery Agent choose autonomously.",
    )
    parser.add_argument(
        "--analytics",
        type=Path,
        default=None,
        help="Path to an analytics export (JSON/CSV/text) to inform feature discovery",
    )
    parser.add_argument("--skip-deploy", action="store_true", help="Skip the pre-prod deployment step")
    parser.add_argument(
        "--fixed-pipeline",
        action="store_true",
        help=(
            "Use the old hardcoded understand->discover->implement->test->deploy sequence "
            "(orchestrator.py::run_cycle) instead of the default: the Orchestrator Agent "
            "deciding which specialized agent to call, in what order (agents/brain.py). "
            "Production is never reachable from either mode."
        ),
    )


def _print_autonomous_report(report) -> None:
    print(f"\nRun {report.run_id}")
    print("  actions taken:")
    for action in report.actions:
        print(f"    - {action}")
    print(f"\n  final message: {report.final_message}")
    print(f"  cost: ${report.cost_usd:.4f}")


def _print_report(report) -> None:
    print(f"\nRun {report.run_id}")
    print(f"  feature:             {report.feature!r}")
    if report.opportunities_considered:
        print("  opportunities considered:")
        for opp in report.opportunities_considered:
            print(f"    - {opp.get('feature')} (impact={opp.get('impact')}, effort={opp.get('effort')})")
    print(f"  codebase understood:   {report.codebase_ok}")
    print(f"  implementation ok:     {report.implementation_ok}")
    print(f"  local tests passed:    {report.testing_ok}")
    print(f"  deployed to pre-prod:  {report.deployment_ok}")
    print(f"  verified live:         {report.pre_prod_verified}")


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
        firebase_pre_prod_alias = _prompt(
            "Firebase PRE-PROD project alias (in .firebaserc)", detected.firebase_pre_prod_alias
        )
        firebase_prod_alias = _prompt("Firebase PROD project alias (in .firebaserc)", detected.firebase_prod_alias)
    ci_cd_on_push = _prompt_bool(
        "Does this app already have CI/CD that deploys on push to the pre-prod/prod "
        "branches (GitHub Actions, Vercel git integration, etc.)? If yes, Deployment "
        "Agent will only merge+push and verify the resulting CI run, not also deploy directly.",
        detected.ci_cd_on_push,
    )
    print(
        "\nBy default, production is only ever touched via `agentra promote` "
        "(a human runs it deliberately). The option below lets the Production "
        "Debugging Agent skip that and deploy a verified hotfix straight to "
        "prod on its own, once it has passed pre-prod testing."
    )
    auto_remediate_prod = _prompt_bool(
        "Allow autonomous hotfix deploys to production (no human approval)?", False
    )
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
    parser = argparse.ArgumentParser(prog="agentra")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run one understand->discover->implement->test->deploy-to-pre-prod cycle")
    _add_common_run_args(run_p)

    loop_p = sub.add_parser("loop", help="Run repeated autonomous cycles (vision.md section 6)")
    _add_common_run_args(loop_p)
    loop_p.add_argument("--cycles", type=int, default=5, help="Number of cycles to run (default 5)")
    loop_p.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Stop the loop as soon as a cycle fails implementation or testing",
    )

    env_p = sub.add_parser("env", help="Manage per-app local/pre-prod/prod environment config")
    env_sub = env_p.add_subparsers(dest="env_command", required=True)
    env_init_p = env_sub.add_parser("init", help="Detect and configure this app's environment pipeline")
    env_init_p.add_argument("--repo", required=True, type=Path)
    env_init_p.add_argument("--yes", action="store_true", help="Skip prompts; use detected values and flags as-is")
    env_init_p.add_argument("--auto-remediate-prod", action="store_true", default=False)

    promote_p = sub.add_parser("promote", help="Human-approved: promote the current pre-prod branch to production")
    promote_p.add_argument("--repo", required=True, type=Path)
    promote_p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    debug_p = sub.add_parser("debug-prod", help="Diagnose (and, if opted in, auto-remediate) a production issue")
    debug_p.add_argument("--repo", required=True, type=Path)
    debug_p.add_argument(
        "--objective", default=None, help="Business objective, used if a fix is built. Omit to use the persistent setting."
    )
    debug_p.add_argument("--symptom", default=None, help="What's wrong, if known (else the agent scans logs itself)")

    obj_p = sub.add_parser("objective", help="Manage the persistent business objective for a repo")
    obj_sub = obj_p.add_subparsers(dest="objective_command", required=True)
    obj_set_p = obj_sub.add_parser("set", help="Set the current objective")
    obj_set_p.add_argument("objective_text")
    obj_set_p.add_argument("--repo", required=True, type=Path)
    obj_show_p = obj_sub.add_parser("show", help="Show the current objective")
    obj_show_p.add_argument("--repo", required=True, type=Path)

    apps_p = sub.add_parser("apps", help="Manage the multi-app registry (~/.agentra/apps.json)")
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

    args = parser.parse_args()

    if args.command == "run":
        analytics_summary = _read_analytics(args.analytics)
        repo = args.repo.resolve()
        objective = _resolve_objective(repo, args.objective)
        if args.fixed_pipeline:
            report = asyncio.run(
                run_cycle(
                    repo,
                    objective,
                    feature=args.feature,
                    analytics_summary=analytics_summary,
                    skip_deploy=args.skip_deploy,
                )
            )
            _print_report(report)
            print(f"\nDetails written to <repo>/.agentra/memory/ and <repo>/.agentra/logs/{report.run_id}.log")
        else:
            env = environments.load(repo) or environments.EnvironmentConfig()
            report = asyncio.run(
                run_autonomous_cycle(
                    repo,
                    objective,
                    env,
                    analytics_summary=analytics_summary,
                    feature=args.feature,
                    skip_deploy=args.skip_deploy,
                )
            )
            _print_autonomous_report(report)
        print("Ready for production? Run: agentra promote --repo <repo>")

    elif args.command == "loop":
        analytics_summary = _read_analytics(args.analytics)
        repo = args.repo.resolve()
        objective = _resolve_objective(repo, args.objective)
        env = None if args.fixed_pipeline else (environments.load(repo) or environments.EnvironmentConfig())
        for i in range(1, args.cycles + 1):
            print(f"\n{'=' * 60}\nCycle {i}/{args.cycles}\n{'=' * 60}")
            feature = args.feature if i == 1 else None
            if args.fixed_pipeline:
                report = asyncio.run(
                    run_cycle(
                        repo,
                        objective,
                        feature=feature,
                        analytics_summary=analytics_summary,
                        skip_deploy=args.skip_deploy,
                    )
                )
                _print_report(report)
                if args.stop_on_failure and not (report.implementation_ok and report.testing_ok):
                    print("\nStopping loop: cycle failed and --stop-on-failure was set.")
                    break
                continue
            report = asyncio.run(
                run_autonomous_cycle(
                    repo,
                    objective,
                    env,
                    analytics_summary=analytics_summary,
                    feature=feature,
                    skip_deploy=args.skip_deploy,
                )
            )
            _print_autonomous_report(report)

    elif args.command == "env" and args.env_command == "init":
        repo = args.repo.resolve()
        detected = environments.detect(repo)
        if args.yes:
            detected.auto_remediate_prod = args.auto_remediate_prod
            config = detected
        else:
            config = _interactive_env_init(detected)
        path = environments.save(repo, config)
        print(f"\nWrote {path}")
        print(config)
        if config.auto_remediate_prod:
            print(
                "\nNote: auto_remediate_prod is ON for this app. The Production "
                "Debugging Agent may deploy verified hotfixes to prod without "
                "asking. Edit .agentra/environments.yaml to turn it back off."
            )

    elif args.command == "promote":
        repo = args.repo.resolve()
        env = environments.load(repo) or environments.EnvironmentConfig()
        if not args.yes:
            confirm = input(
                f"This will merge '{env.pre_prod_branch}' into '{env.prod_branch}' and deploy to PRODUCTION. "
                f"Type 'promote' to confirm: "
            ).strip()
            if confirm != "promote":
                print("Aborted.")
                return
        result = asyncio.run(run_promote(repo))
        print(f"\nRun {result['run_id']}")
        print(f"  promoted to prod: {result['ok']}")
        if not result["ok"]:
            print(f"  details written to <repo>/.agentra/memory/failures/{result['run_id']}-prod-promote.md")

    elif args.command == "debug-prod":
        repo = args.repo.resolve()
        objective = _resolve_objective(repo, args.objective)
        report = asyncio.run(run_prod_debug_cycle(repo, objective, symptom=args.symptom))
        print(f"\nRun {report.run_id}")
        print(f"  root cause found:  {report.root_cause_found}")
        print(f"  severity:          {report.severity}")
        print(f"  fix attempted:     {report.fix_attempted}")
        print(f"  promoted to prod:  {report.promoted_to_prod}")
        if report.root_cause_found and not report.fix_attempted:
            print(
                "\n  Filed as a known bug for the next `agentra run`/`agentra loop` "
                "cycle (auto_remediate_prod is off for this app)."
            )

    elif args.command == "objective" and args.objective_command == "set":
        path = Memory(args.repo.resolve()).set_objective(args.objective_text)
        print(f"Wrote {path}")
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
            print(f"  {name}: {info['repo_path']}")

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


if __name__ == "__main__":
    main()
