from __future__ import annotations

import argparse
from pathlib import Path

from .config import ANNOTATION_FILE, DATA_DIR, DEFAULT_MODEL, DEFAULT_OUTPUT_DIR, PROJECT_ENV_FILE
from .data import read_jsonl
from .purity import make_purity_template
from .workflow import evaluate_experiment, run_experiment


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--annotations", default=str(ANNOTATION_FILE))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run and evaluate the single-call conversation cutting experiment.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run one LLM cutting call per session")
    _common_arguments(run)
    run.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    run.add_argument("--session-source", choices=["annotations", "all"], default="annotations")
    run.add_argument("--session-ids", help="comma-separated session IDs")
    run.add_argument("--limit", type=int)
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument("--temperature", type=float, default=0.0)
    run.add_argument("--max-tokens", type=int, default=4096)
    run.add_argument("--retries", type=int, default=3)
    run.add_argument("--timeout", type=float, default=120.0)
    run.add_argument("--api-key")
    run.add_argument("--base-url")
    run.add_argument("--env-file", default=str(PROJECT_ENV_FILE))
    proxy_group = run.add_mutually_exclusive_group()
    proxy_group.add_argument("--use-proxy", dest="use_proxy", action="store_true",
                             help="use HTTP(S) proxy environment variables (default)")
    proxy_group.add_argument("--no-proxy", dest="use_proxy", action="store_false",
                             help="disable HTTP(S) proxy environment variables")
    run.set_defaults(use_proxy=True)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--force", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(function=run_experiment)

    evaluate = subparsers.add_parser("evaluate", help="evaluate predictions against annotations")
    _common_arguments(evaluate)
    evaluate.add_argument("--predictions", required=True)
    evaluate.add_argument("--output")
    evaluate.add_argument("--purity")
    evaluate.add_argument("--tolerance", type=int, default=1)
    evaluate.add_argument("--no-tolerance", action="store_true")
    evaluate.set_defaults(function=evaluate_experiment)

    purity = subparsers.add_parser("purity-template", help="create the manual segment-purity review file")
    purity.add_argument("--predictions", required=True)
    purity.add_argument("--output", required=True)
    purity.add_argument("--sample-size", type=int, default=100)
    purity.set_defaults(function=lambda args: make_purity_template(
        read_jsonl(Path(args.predictions)), Path(args.output), args.sample_size
    ))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.function(args)
