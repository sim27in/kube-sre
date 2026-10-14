"""Eval CLI.

python -m kube_sre.evals record [SCENARIO ...]            # needs the kind cluster
python -m kube_sre.evals run [--agents v0 v1] [--repeats 3] [--scenarios ...]
python -m kube_sre.evals rescore evals/results/<run>  # re-judge after changing a scenario or the judge
python -m kube_sre.evals report evals/results/<run>
"""

import argparse
import asyncio
from pathlib import Path

from dotenv import load_dotenv

from kube_sre.evals.scenarios import load_scenarios


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m kube_sre.evals")
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="Record scenario snapshots from the live cluster.")
    rec.add_argument("scenarios", nargs="*", help="Scenario ids (default: all).")
    rec.add_argument("--keep", action="store_true", help="Keep the namespaces after recording.")
    rec.add_argument("--timeout", type=int, default=300)

    run = sub.add_parser("run", help="Run agents against the snapshots (offline; needs an OpenAI key).")
    run.add_argument("--agents", nargs="+", default=["v0", "v1"], choices=["v0", "v1"])
    run.add_argument("--scenarios", nargs="+", help="Scenario ids (default: all).")
    run.add_argument("--repeats", type=int, default=3)
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--no-judge", action="store_true", help="Skip the LLM judge (root cause scores stay 0).")
    run.add_argument("--label", default="", help="Suffix for the results directory name.")

    res = sub.add_parser("rescore", help="Judge existing runs again after a scenario or judge change.")
    res.add_argument("results", type=Path)
    res.add_argument("--concurrency", type=int, default=6)

    rep = sub.add_parser("report", help="Re-print the summary of a results directory.")
    rep.add_argument("results", type=Path)

    args = parser.parse_args()
    if args.command == "record":
        from kube_sre.evals.record import record

        failed = record(load_scenarios(args.scenarios or None), keep=args.keep, timeout=args.timeout)
        raise SystemExit(1 if failed else 0)

    from kube_sre.evals.judge import get_judge_model
    from kube_sre.evals.report import summarize
    from kube_sre.evals.runner import load_runs, rescore, run_all

    if args.command == "run":
        judge_model = None if args.no_judge else get_judge_model()
        scenarios = load_scenarios(args.scenarios)
        out_dir = asyncio.run(run_all(args.agents, scenarios, args.repeats, args.concurrency, judge_model, args.label))
    elif args.command == "rescore":
        scenarios = {s.id: s for s in load_scenarios()}
        out_dir = asyncio.run(rescore(args.results, scenarios, get_judge_model(), args.concurrency))
    else:
        out_dir = args.results
    summary = summarize(load_runs(out_dir))
    (out_dir / "summary.md").write_text(summary)
    print("\n" + summary)


if __name__ == "__main__":
    main()
