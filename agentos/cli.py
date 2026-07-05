import argparse
import asyncio
from pathlib import Path

from agentos import environments
from agentos.orchestrator import run_cycle, run_prod_debug_cycle, run_promote


def _read_analytics(path: Path | None) -> str:
    if path is None:
        return "not available"
    text = path.read_text()
    return text[:20000]


def _add_common_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True, type=Path, help="Path to the target repository")
    parser.add_argument("--objective", required=True, help="Business objective, e.g. 'improve retention'")
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
    parser.add_argument("--skip-deploy", action="store_true", help="Skip the beta deployment step")


def _print_report(report) -> None:
    print(f"\nRun {report.run_id}")
    print(f"  feature:             {report.feature!r}")
    if report.opportunities_considered:
        print("  opportunities considered:")
        for opp in report.opportunities_considered:
            print(f"    - {opp.get('feature')} (impact={opp.get('impact')}, effort={opp.get('effort')})")
    print(f"  codebase understood: {report.codebase_ok}")
    print(f"  implementation ok:   {report.implementation_ok}")
    print(f"  tests passed:        {report.testing_ok}")
    print(f"  deployed to beta:    {report.deployment_ok}")


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
    print("Configuring the dev -> beta -> prod pipeline. Press enter to accept each detected default.\n")
    dev_branch = _prompt("Dev branch", detected.dev_branch)
    beta_branch = _prompt("Beta branch", detected.beta_branch)
    prod_branch = _prompt("Prod branch", detected.prod_branch)
    vercel = _prompt_bool("Vercel configured for this app?", detected.vercel)
    firebase = _prompt_bool("Firebase configured for this app?", detected.firebase)
    firebase_beta_alias = detected.firebase_beta_alias
    firebase_prod_alias = detected.firebase_prod_alias
    if firebase:
        firebase_beta_alias = _prompt("Firebase BETA project alias (in .firebaserc)", detected.firebase_beta_alias)
        firebase_prod_alias = _prompt("Firebase PROD project alias (in .firebaserc)", detected.firebase_prod_alias)
    print(
        "\nBy default, production is only ever touched via `agentos promote` "
        "(a human runs it deliberately). The option below lets the Production "
        "Debugging Agent skip that and deploy a verified hotfix straight to "
        "prod on its own, once it has passed beta testing."
    )
    auto_remediate_prod = _prompt_bool(
        "Allow autonomous hotfix deploys to production (no human approval)?", False
    )
    return environments.EnvironmentConfig(
        dev_branch=dev_branch,
        beta_branch=beta_branch,
        prod_branch=prod_branch,
        vercel=vercel,
        firebase=firebase,
        firebase_beta_alias=firebase_beta_alias,
        firebase_prod_alias=firebase_prod_alias,
        auto_remediate_prod=auto_remediate_prod,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="agentos")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run one understand->discover->implement->test->deploy-to-beta cycle")
    _add_common_run_args(run_p)

    loop_p = sub.add_parser("loop", help="Run repeated autonomous cycles (vision.md section 6)")
    _add_common_run_args(loop_p)
    loop_p.add_argument("--cycles", type=int, default=5, help="Number of cycles to run (default 5)")
    loop_p.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Stop the loop as soon as a cycle fails implementation or testing",
    )

    env_p = sub.add_parser("env", help="Manage per-app dev/beta/prod environment config")
    env_sub = env_p.add_subparsers(dest="env_command", required=True)
    env_init_p = env_sub.add_parser("init", help="Detect and configure this app's environment pipeline")
    env_init_p.add_argument("--repo", required=True, type=Path)
    env_init_p.add_argument("--yes", action="store_true", help="Skip prompts; use detected values and flags as-is")
    env_init_p.add_argument("--auto-remediate-prod", action="store_true", default=False)

    promote_p = sub.add_parser("promote", help="Human-approved: promote the current beta branch to production")
    promote_p.add_argument("--repo", required=True, type=Path)
    promote_p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    debug_p = sub.add_parser("debug-prod", help="Diagnose (and, if opted in, auto-remediate) a production issue")
    debug_p.add_argument("--repo", required=True, type=Path)
    debug_p.add_argument("--objective", required=True, help="Business objective, used if a fix is built")
    debug_p.add_argument("--symptom", default=None, help="What's wrong, if known (else the agent scans logs itself)")

    args = parser.parse_args()

    if args.command == "run":
        analytics_summary = _read_analytics(args.analytics)
        report = asyncio.run(
            run_cycle(
                args.repo,
                args.objective,
                feature=args.feature,
                analytics_summary=analytics_summary,
                skip_deploy=args.skip_deploy,
            )
        )
        _print_report(report)
        print(f"\nDetails written to <repo>/.agentos/memory/ and <repo>/.agentos/logs/{report.run_id}.log")
        print("Ready for production? Run: agentos promote --repo <repo>")

    elif args.command == "loop":
        analytics_summary = _read_analytics(args.analytics)
        for i in range(1, args.cycles + 1):
            print(f"\n{'=' * 60}\nCycle {i}/{args.cycles}\n{'=' * 60}")
            feature = args.feature if i == 1 else None
            report = asyncio.run(
                run_cycle(
                    args.repo,
                    args.objective,
                    feature=feature,
                    analytics_summary=analytics_summary,
                    skip_deploy=args.skip_deploy,
                )
            )
            _print_report(report)
            if args.stop_on_failure and not (report.implementation_ok and report.testing_ok):
                print("\nStopping loop: cycle failed and --stop-on-failure was set.")
                break

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
                "asking. Edit .agentos/environments.yaml to turn it back off."
            )

    elif args.command == "promote":
        repo = args.repo.resolve()
        env = environments.load(repo) or environments.EnvironmentConfig()
        if not args.yes:
            confirm = input(
                f"This will merge '{env.beta_branch}' into '{env.prod_branch}' and deploy to PRODUCTION. "
                f"Type 'promote' to confirm: "
            ).strip()
            if confirm != "promote":
                print("Aborted.")
                return
        result = asyncio.run(run_promote(repo))
        print(f"\nRun {result['run_id']}")
        print(f"  promoted to prod: {result['ok']}")
        if not result["ok"]:
            print(f"  details written to <repo>/.agentos/memory/failures/{result['run_id']}-prod-promote.md")

    elif args.command == "debug-prod":
        report = asyncio.run(run_prod_debug_cycle(args.repo, args.objective, symptom=args.symptom))
        print(f"\nRun {report.run_id}")
        print(f"  root cause found:  {report.root_cause_found}")
        print(f"  severity:          {report.severity}")
        print(f"  fix attempted:     {report.fix_attempted}")
        print(f"  promoted to prod:  {report.promoted_to_prod}")
        if report.root_cause_found and not report.fix_attempted:
            print(
                "\n  Filed as a known bug for the next `agentos run`/`agentos loop` "
                "cycle (auto_remediate_prod is off for this app)."
            )


if __name__ == "__main__":
    main()
