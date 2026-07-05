import argparse
import asyncio
from pathlib import Path

from agentos.orchestrator import run_cycle


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
    parser.add_argument("--skip-deploy", action="store_true", help="Skip the deployment step")


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
    print(f"  deployed:            {report.deployment_ok}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="agentos")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run one understand->discover->implement->test->deploy cycle")
    _add_common_run_args(run_p)

    loop_p = sub.add_parser("loop", help="Run repeated autonomous cycles (vision.md section 6)")
    _add_common_run_args(loop_p)
    loop_p.add_argument("--cycles", type=int, default=5, help="Number of cycles to run (default 5)")
    loop_p.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Stop the loop as soon as a cycle fails implementation or testing",
    )

    args = parser.parse_args()
    analytics_summary = _read_analytics(args.analytics)

    if args.command == "run":
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

    elif args.command == "loop":
        for i in range(1, args.cycles + 1):
            print(f"\n{'=' * 60}\nCycle {i}/{args.cycles}\n{'=' * 60}")
            # Feature is only honored on the first cycle; autonomous discovery
            # takes over from there so each cycle picks a fresh opportunity.
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


if __name__ == "__main__":
    main()
