"""Run and evaluate topic-memory extraction prompts."""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent.parent
DEFAULT_INPUT = REPO_ROOT / "analyse3" / "community_purify" / "artifacts" / "two_days_v1"
DEFAULT_PROMPT = ROOT / "memory_extract_prompt_v1.md"
DEFAULT_OUTPUT = ROOT / "artifacts" / "two_days_v1"
DEFAULT_ENV = REPO_ROOT / ".env"
DEFAULT_MODEL = "gpt-5.6-luna"
RESPONSES_MODELS = {"gpt-5.6-luna"}
RATING_FIELDS = ("topic", "faithfulness", "retention_precision", "retention_recall", "user_memory_grounding")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("'\"")


def load_prompt(path: Path, language: str) -> str:
    document = path.read_text(encoding="utf-8")
    blocks = re.findall(r"```text\s*\n(.*?)\n```", document, flags=re.DOTALL)
    if not blocks:
        return document.strip()
    index = 0 if language == "zh" else 1
    if index >= len(blocks):
        return blocks[0].strip()
    return blocks[index].strip()


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("model output must be a JSON object")
    return value


def make_client(args: argparse.Namespace) -> Any:
    load_env(Path(args.env_file))
    api_key = (
        args.api_key
        or os.getenv("MEMORY_EXTRACT_OPENAI_API_KEY")
        or os.getenv("COMMUNITY_PURIFY_OPENAI_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
    )
    base_url = (
        args.base_url
        or os.getenv("MEMORY_EXTRACT_OPENAI_BASE_URL")
        or os.getenv("COMMUNITY_PURIFY_OPENAI_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or "https://openrouter.ai/api/v1"
    ).rstrip("/")
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")]
    if not api_key:
        raise RuntimeError("missing API key")
    try:
        import httpx
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(f"missing API dependency: {exc}") from exc
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=args.timeout,
        max_retries=0,
        http_client=httpx.Client(trust_env=args.use_proxy, follow_redirects=True),
    )


def call_model(client: Any, system_prompt: str, payload: dict[str, Any], args: argparse.Namespace) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]
    if args.model in RESPONSES_MODELS:
        response = client.responses.create(
            model=args.model,
            input=messages,
            max_output_tokens=args.max_output_tokens,
        )
        return getattr(response, "output_text", "") or ""
    response = client.chat.completions.create(
        model=args.model,
        messages=messages,
        temperature=args.temperature,
        max_tokens=args.max_output_tokens,
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content or ""


def build_inputs(input_dir: Path) -> list[dict[str, Any]]:
    community_rows = {
        str(row["community_id"]): row for row in read_jsonl(input_dir / "community_inputs.jsonl")
    }
    purified_rows = read_jsonl(input_dir / "purified_communities.jsonl")
    inputs: list[dict[str, Any]] = []
    for purified in purified_rows:
        if purified.get("status") != "ok":
            continue
        community_id = str(purified["community_id"])
        source_segments = {
            str(segment["segment_id"]): segment
            for segment in community_rows.get(community_id, {}).get("segments", [])
        }
        for group in purified.get("groups", []):
            group_id = str(group["group_id"])
            segments = [source_segments[str(segment_id)] for segment_id in group["segment_ids"]]
            inputs.append(
                {
                    "case_id": f"{community_id}::{group_id}",
                    "community_id": community_id,
                    "group_id": group_id,
                    "segments": segments,
                }
            )
    return inputs


def valid_memory_items(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    return all(
        isinstance(item, dict)
        and isinstance(item.get("type"), str)
        and bool(item["type"].strip())
        and isinstance(item.get("content"), str)
        and bool(item["content"].strip())
        for item in value
    )


def validate_memory(value: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    expected_segments = [str(segment["segment_id"]) for segment in item["segments"]]
    expected_anchors = [str(segment["anchor"]) for segment in item["segments"] if segment.get("anchor")]
    if not value:
        return {
            "status": "empty",
            "topic": None,
            "summary": None,
            "topic_context": [],
            "user_memories": [],
            "source_anchors": expected_anchors,
            "source_segments": expected_segments,
            "validation": {
                "empty_memory_valid": True,
                "source_fields_generated_by_code": True,
            },
        }
    required = {
        "topic": isinstance(value.get("topic"), str) and bool(value["topic"].strip()),
        "summary": isinstance(value.get("summary"), str) and bool(value["summary"].strip()),
        "topic_context": valid_memory_items(value.get("topic_context")),
        "user_memories": valid_memory_items(value.get("user_memories")),
    }
    if not all(required.values()):
        missing = [key for key, valid in required.items() if not valid]
        raise ValueError(f"invalid memory fields: {missing}")
    return {
        "topic": value["topic"],
        "summary": value["summary"],
        "topic_context": value["topic_context"],
        "user_memories": value["user_memories"],
        "source_anchors": expected_anchors,
        "source_segments": expected_segments,
        "validation": {
            "required_fields_valid": True,
            "source_fields_generated_by_code": True,
        },
    }


def dry_run_memory(item: dict[str, Any]) -> dict[str, Any]:
    segments = item["segments"]
    anchors = [str(segment["anchor"]) for segment in segments if segment.get("anchor")]
    return {
        "topic": anchors[0] if anchors else "未命名主题",
        "summary": "DRY RUN: 未调用模型。",
        "topic_context": [],
        "user_memories": [],
        "source_anchors": anchors,
        "source_segments": [str(segment["segment_id"]) for segment in segments],
    }


def structural_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    attempted = len(rows)
    status_counts = Counter(str(row.get("status")) for row in rows)
    successful = [row for row in rows if row.get("status") in {"ok", "dry_run", "empty"}]
    validations = [row.get("validation", {}) for row in successful]

    def mean(key: str) -> float | None:
        values = [float(item[key]) for item in validations if isinstance(item.get(key), (int, float))]
        return round(statistics.mean(values), 4) if values else None

    return {
        "case_count": attempted,
        "status_counts": dict(status_counts),
        "json_and_schema_valid_rate": len(successful) / attempted if attempted else None,
        "source_linkage_generated_by_code_rate": sum(
            bool(item.get("source_fields_generated_by_code")) for item in validations
        ) / len(validations) if validations else None,
    }


def make_annotation_template(output_path: Path, candidates_path: Path, inputs: list[dict[str, Any]]) -> None:
    candidates = {str(row["case_id"]): row for row in read_jsonl(candidates_path)}
    rows = []
    for item in inputs:
        case_id = item["case_id"]
        candidate = candidates.get(case_id)
        rows.append(
            {
                "case_id": case_id,
                "community_id": item["community_id"],
                "group_id": item["group_id"],
                "input": item,
                "candidate": candidate,
                "ratings": {
                    "topic": None,
                    "faithfulness": None,
                    "retention_precision": None,
                    "retention_recall": None,
                    "user_memory_grounding": None,
                },
                "notes": "",
            }
        )
    write_jsonl(output_path, rows)


def evaluate_annotations(path: Path) -> dict[str, Any]:
    rows = read_jsonl(path)
    values: dict[str, list[float]] = {field: [] for field in RATING_FIELDS}
    complete = 0
    for row in rows:
        ratings = row.get("ratings", {})
        if all(isinstance(ratings.get(field), (int, float)) for field in RATING_FIELDS):
            complete += 1
            for field in RATING_FIELDS:
                values[field].append(float(ratings[field]))
    averages = {
        field: round(statistics.mean(scores), 4) if scores else None
        for field, scores in values.items()
    }
    all_scores = [score for scores in values.values() for score in scores]
    return {
        "annotation_file": str(path),
        "case_count": len(rows),
        "complete_rating_count": complete,
        "scale": "1=poor, 3=acceptable, 5=excellent",
        "mean_scores": averages,
        "overall_mean": round(statistics.mean(all_scores), 4) if all_scores else None,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--language", choices=("zh", "en"), default="en")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-key")
    parser.add_argument("--base-url")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--make-annotation-template", action="store_true")
    parser.add_argument("--annotations", type=Path)
    proxy = parser.add_mutually_exclusive_group()
    proxy.add_argument("--use-proxy", dest="use_proxy", action="store_true")
    proxy.add_argument("--no-proxy", dest="use_proxy", action="store_false")
    parser.set_defaults(use_proxy=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    inputs = build_inputs(args.input_dir)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    candidates_path = args.output_dir / "memory_candidates.jsonl"

    if args.make_annotation_template:
        if not candidates_path.exists():
            raise RuntimeError(f"candidate output not found: {candidates_path}")
        output_path = args.annotations or (args.output_dir / "annotation_template.jsonl")
        make_annotation_template(output_path, candidates_path, inputs)
        print(f"wrote {output_path}")
        return

    if not inputs:
        raise RuntimeError(f"no purified groups found in {args.input_dir}")
    prompt = load_prompt(args.prompt, args.language)
    existing = {}
    if args.resume and candidates_path.exists():
        existing = {str(row["case_id"]): row for row in read_jsonl(candidates_path)}
    client = None if args.dry_run else make_client(args)
    results: dict[str, dict[str, Any]] = dict(existing)

    for index, item in enumerate(inputs, start=1):
        case_id = item["case_id"]
        if case_id in results and results[case_id].get("status") in {"ok", "dry_run"}:
            print(f"[{index}/{len(inputs)}] resume case={case_id}")
            continue
        started = time.time()
        result: dict[str, Any] = {
            "case_id": case_id,
            "community_id": item["community_id"],
            "group_id": item["group_id"],
            "status": "error",
            "model": args.model,
            "prompt": str(args.prompt),
            "language": args.language,
        }
        try:
            raw = dry_run_memory(item) if args.dry_run else parse_json_object(call_model(client, prompt, item, args))
            memory = validate_memory(raw, item)
            result.update(memory)
            if args.dry_run:
                result["status"] = "dry_run"
            elif result.get("status") != "empty":
                result["status"] = "ok"
            if not args.dry_run:
                result["raw_response"] = json.dumps(raw, ensure_ascii=False)
            print(f"[{index}/{len(inputs)}] case={case_id} status={result['status']}")
        except Exception as exc:
            result["error"] = repr(exc)
            print(f"[{index}/{len(inputs)}] ERROR case={case_id}: {exc}")
        result["elapsed_seconds"] = round(time.time() - started, 3)
        results[case_id] = result
        write_jsonl(candidates_path, [results[key] for key in sorted(results)])

    ordered = [results[key] for key in sorted(results)]
    evaluation = {"evaluated_at": datetime.now(timezone.utc).isoformat(), "structural": structural_metrics(ordered)}
    if args.annotations and args.annotations.exists():
        evaluation["human"] = evaluate_annotations(args.annotations)
    write_json(args.output_dir / "evaluation.json", evaluation)
    write_json(
        args.output_dir / "run_manifest.json",
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "input_dir": str(args.input_dir),
            "prompt": str(args.prompt),
            "language": args.language,
            "model": args.model,
            "case_count": len(inputs),
            "dry_run": args.dry_run,
        },
    )
    print(f"wrote {candidates_path}")
    print(f"wrote {args.output_dir / 'evaluation.json'}")


if __name__ == "__main__":
    main()
