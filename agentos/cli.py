import argparse
import asyncio
from pathlib import Path

from agentos.orchestrator import run_cycle


def main() -> None:
    parser = argparse.ArgumentParser(prog="agentos")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run one understand->implement->test->deploy cycle")
    run_p.add_argument("--repo", required=True, type=Path, help="Path to the target repository")
    run_p.add_argument("--objective", required=True, help="Business objective, e.g. 'improve retention'")
    run_p.add_argument("--feature", required=True, help="Specific feature to implement this cycle")
    run_p.add_argument("--skip-deploy", action="store_true", help="Skip the deployment step")

    args = parser.parse_args()

    if args.command == "run":
        report = asyncio.run(
            run_cycle(args.repo, args.objective, args.feature, skip_deploy=args.skip_deploy)
        )
        print(f"\nRun {report.run_id}")
        print(f"  codebase understood: {report.codebase_ok}")
        print(f"  implementation ok:   {report.implementation_ok}")
        print(f"  tests passed:        {report.testing_ok}")
        print(f"  deployed:            {report.deployment_ok}")
        print(f"\nDetails written to <repo>/.agentos/memory/ and <repo>/.agentos/logs/{report.run_id}.log")


if __name__ == "__main__":
    main()
