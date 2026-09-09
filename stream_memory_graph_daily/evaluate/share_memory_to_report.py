#!/usr/bin/env python3
"""离线评估 share_memory 内容保留；监督信息绝不回流到记忆构建管线。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EVALUATE_ROOT = Path(__file__).resolve().parent
PACKAGE_ROOT = EVALUATE_ROOT.parent
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from stream_memory_graph_daily.encoder import load_encoder
from stream_memory_graph_daily.evaluate.progress import ConsoleProgress
from stream_memory_graph_daily.evaluate.share_memory import (
    build_share_memory_attributions,
)
from stream_memory_graph_daily.llm import JsonLLM, OpenAIJsonLLM, load_env
from stream_memory_graph_daily.pipeline import DailyMemoryGraph


DEFAULT_BUILD_DIR = EVALUATE_ROOT / "artifacts" / "memory_build"

RETENTION_JUDGE_PROMPT = """You are evaluating a memory system after it has already run.
The evaluation-only target and benchmark metadata were hidden from every memory-building
stage. Decide whether the supplied memory output correctly preserves the material user
information expressed by the target message.

For add, the new facts and concrete details should be represented. For update, the new
state should be represented without presenting superseded values as current. For delete,
success means the requested removal or resulting state is correctly reflected; deleted
content does not need to remain active. Judge meaning rather than exact wording, but require
important entities, values, dates, counts, distinctions, and state changes.

Return JSON only with exactly:
{"retained":true,"confidence":0.0,"failure_type":"none|missing|detail_loss|wrong_state|wrong_topic|uncertain","reason":"concise explanation"}
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: expected an object")
        rows.append(value)
    return rows


def _memory_outputs(
    attribution: dict[str, Any], pipeline: DailyMemoryGraph
) -> list[dict[str, Any]]:
    stage_ids = set(str(value) for value in attribution.get("memory_apply_stage_ids", []))
    outputs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in pipeline.stage_audit:
        if str(event.get("stage_id")) not in stage_ids:
            continue
        memory = event.get("output", {}).get("memory")
        if not isinstance(memory, dict):
            continue
        identity = f"{memory.get('memory_id')}:{memory.get('version')}"
        if identity not in seen:
            outputs.append(memory)
            seen.add(identity)
    # Prefer the snapshot written by the checkpoint that processed this
    # evidence. A later final state may legitimately supersede an earlier add,
    # update, or delete and would make event-level judging temporally wrong.
    if not outputs:
        for memory_id in attribution.get("memory_ids", []):
            memory = pipeline.memories.get(str(memory_id))
            if memory is None:
                continue
            row = memory.to_dict()
            identity = f"{row.get('memory_id')}:{row.get('version')}"
            if identity not in seen:
                outputs.append(row)
                seen.add(identity)
    return outputs


class ShareMemorySemanticEvaluator:
    def __init__(self, pipeline: DailyMemoryGraph, judge_llm: JsonLLM) -> None:
        self.pipeline = pipeline
        self.judge_llm = judge_llm

    def evaluate(self, attribution: dict[str, Any]) -> dict[str, Any]:
        memories = _memory_outputs(attribution, self.pipeline)
        base = {
            "label_id": attribution.get("label_id"),
            "conversation_id": attribution.get("conversation_id"),
            "message_id": attribution.get("message_id"),
            "segment_ids": attribution.get("segment_ids", []),
            "segment_statuses": attribution.get("segment_statuses", []),
            "structural_stage": attribution.get("structural_stage"),
            "fully_compressed": attribution.get("structural_stage")
            == "compressed_needs_content_evaluation",
        }
        if not memories:
            return {
                **base,
                "retained": False,
                "confidence": 1.0,
                "failure_type": str(attribution.get("structural_stage", "missing")),
                "reason": "No committed memory output is linked to the target message.",
                "evaluated_at": utc_now(),
            }
        payload = {
            "target_message": attribution.get("text"),
            "operation": attribution.get("operation"),
            "operation_details": attribution.get("operation_details"),
            "memory_outputs_after_processing": memories,
        }
        value = self.judge_llm.complete(
            RETENTION_JUDGE_PROMPT,
            "EVALUATION DATA\n" + json.dumps(payload, ensure_ascii=False, indent=2),
        )
        required = {"retained", "confidence", "failure_type", "reason"}
        if set(value) != required:
            raise ValueError(f"retention judge fields must be exactly {sorted(required)}")
        if not isinstance(value["retained"], bool):
            raise ValueError("retention judge retained must be boolean")
        confidence = float(value["confidence"])
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("retention judge confidence must be between 0 and 1")
        failure_type = str(value["failure_type"]).strip()
        allowed = {"none", "missing", "detail_loss", "wrong_state", "wrong_topic", "uncertain"}
        if failure_type not in allowed:
            raise ValueError(f"invalid retention judge failure_type: {failure_type}")
        if value["retained"] != (failure_type == "none"):
            raise ValueError(
                "retention judge retained and failure_type must describe the same verdict"
            )
        return {
            **base,
            "retained": value["retained"],
            "confidence": confidence,
            "failure_type": failure_type,
            "reason": str(value["reason"]).strip(),
            "evaluated_at": utc_now(),
        }


def generate_semantic_report(results: list[dict[str, Any]]) -> dict[str, Any]:
    failures = Counter(
        str(row.get("failure_type", "unknown"))
        for row in results
        if not row.get("retained")
    )
    retained = sum(bool(row.get("retained")) for row in results)
    fully_compressed = sum(bool(row.get("fully_compressed")) for row in results)
    return {
        "schema_version": "share_memory_semantic_report_v1",
        "generated_at": utc_now(),
        "total_labels": len(results),
        "retained_labels": retained,
        "retention_rate": retained / len(results) if results else None,
        "fully_compressed_labels": fully_compressed,
        "fully_compressed_rate": fully_compressed / len(results) if results else None,
        "failures_by_type": dict(sorted(failures.items())),
        "evaluation_is_offline": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_BUILD_DIR / "memory_state.json")
    parser.add_argument(
        "--labels-file", type=Path, default=DEFAULT_BUILD_DIR / "share_memory_labels.jsonl"
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_BUILD_DIR)
    parser.add_argument("--judge-model", default="gpt-5.6-luna")
    parser.add_argument("--api-key")
    parser.add_argument("--base-url")
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--encoder-model", default="all-MiniLM-L6-v2")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--resume", action="store_true")
    proxy_group = parser.add_mutually_exclusive_group()
    proxy_group.add_argument("--use-proxy", dest="use_proxy", action="store_true")
    proxy_group.add_argument("--no-proxy", dest="use_proxy", action="store_false")
    parser.set_defaults(use_proxy=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    load_env(args.env_file)
    pipeline = DailyMemoryGraph.load(
        args.state_file,
        encoder=load_encoder(args.encoder_model, args.device),
    )
    labels = load_jsonl(args.labels_file)
    attributions = build_share_memory_attributions(labels, pipeline)
    judge = OpenAIJsonLLM(
        model=args.judge_model,
        api_key=args.api_key or os.getenv("LOCAL_OPENAI_API_KEY"),
        base_url=args.base_url or os.getenv("LOCAL_OPENAI_BASE_URL"),
        timeout=args.timeout,
        max_tokens=args.max_tokens,
        use_proxy=args.use_proxy,
        env_file=args.env_file,
    )
    evaluator = ShareMemorySemanticEvaluator(pipeline, judge)
    output_dir = args.output_dir.resolve()
    results_file = output_dir / "share_memory_semantic_results.json"
    report_file = output_dir / "share_memory_semantic_report.json"
    results: list[dict[str, Any]] = []
    if args.resume and results_file.is_file():
        saved = json.loads(results_file.read_text(encoding="utf-8"))
        results = list(saved.get("results", []))
    completed = {str(row.get("label_id")) for row in results}
    progress = ConsoleProgress(len(attributions), "评估 share_memory")
    for position, row in enumerate(attributions, start=1):
        label_id = str(row.get("label_id", ""))
        if label_id in completed:
            progress.update(position, f"{label_id} 已存在")
            continue
        progress.update(position - 1, f"{label_id} 处理中")
        result = evaluator.evaluate(row)
        results.append(result)
        atomic_write_json(
            results_file,
            {
                "schema_version": "share_memory_semantic_results_v1",
                "updated_at": utc_now(),
                "results": results,
            },
        )
        atomic_write_json(report_file, generate_semantic_report(results))
        progress.update(position, f"{label_id} 完成")
    progress.finish(f"完成：{len(results)} 条")
    print(json.dumps(generate_semantic_report(results), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
