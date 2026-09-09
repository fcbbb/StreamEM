from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import DailyGraphConfig
from .encoder import load_encoder
from .io import read_records
from .llm import OpenAIJsonLLM
from .pipeline import DailyMemoryGraph


REPO_ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a daily streaming topic-anchored memory graph."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--input-type", choices=("auto", "conversations", "segments"), default="auto")
    parser.add_argument("--state-out", type=Path, required=True)
    parser.add_argument("--state-in", type=Path)
    parser.add_argument("--no-finalize", action="store_true")
    parser.add_argument("--no-llm", action="store_true", help="keep LLM-requiring groups active")
    parser.add_argument("--llm-model", default="gpt-4o-mini")
    parser.add_argument("--api-key")
    parser.add_argument("--base-url")
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    proxy_group = parser.add_mutually_exclusive_group()
    proxy_group.add_argument("--use-proxy", dest="use_proxy", action="store_true")
    proxy_group.add_argument("--no-proxy", dest="use_proxy", action="store_false")
    parser.set_defaults(use_proxy=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--encoder-model", default="all-MiniLM-L6-v2")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--knn-k", type=int, default=10)
    parser.add_argument(
        "--postprocess-workers",
        type=int,
        default=1,
        help="并发执行社区纯化、记忆提取和记忆融合的 worker 数",
    )
    parser.add_argument("--new-new-threshold", type=float, default=0.5)
    parser.add_argument("--new-memory-threshold", type=float, default=0.5)
    parser.add_argument("--assignment-min-support", type=float, default=0.35)
    parser.add_argument("--assignment-margin", type=float, default=0.08)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = DailyGraphConfig(
        knn_k=args.knn_k,
        postprocess_workers=args.postprocess_workers,
        new_new_threshold=args.new_new_threshold,
        new_memory_threshold=args.new_memory_threshold,
        assignment_min_support=args.assignment_min_support,
        assignment_margin=args.assignment_margin,
    )
    encoder = load_encoder(args.encoder_model, args.device)
    llm = None if args.no_llm else OpenAIJsonLLM(
        model=args.llm_model,
        api_key=args.api_key,
        base_url=args.base_url,
        timeout=args.timeout,
        max_tokens=args.max_tokens,
        retries=config.llm_retries,
        use_proxy=args.use_proxy,
        env_file=args.env_file,
    )
    if args.state_in:
        pipeline = DailyMemoryGraph.load(args.state_in, llm=llm, encoder=encoder)
    else:
        pipeline = DailyMemoryGraph(llm=llm, encoder=encoder, config=config)

    records = read_records(args.input)
    for position, row in enumerate(records, start=1):
        input_type = args.input_type
        if input_type == "auto":
            input_type = "conversations" if "conversation" in row or "messages" in row else "segments"
        if input_type == "conversations":
            result = pipeline.ingest_conversation(row)
        else:
            result = pipeline.ingest_segment(row)
        print(json.dumps({"position": position, "result": result}, ensure_ascii=False))

    final = None if args.no_finalize else pipeline.finalize()
    pipeline.save(args.state_out)
    print(
        json.dumps(
            {"state": str(args.state_out), "finalize": final, "stats": pipeline.stats()},
            ensure_ascii=False,
            indent=2,
        )
    )


__all__ = ["build_parser", "main"]
