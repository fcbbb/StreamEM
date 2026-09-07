"""Run and evaluate conservative LLM community purification."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent.parent
DEFAULT_INPUT_DIR = REPO_ROOT / "analyse3" / "graph" / "experiment" / "results" / "two_days_v1"
DEFAULT_RUN = "knn_k10_tau0p50__leiden__r1p0"
DEFAULT_SEGMENTS = REPO_ROOT / "analyse3" / "anchor" / "artifacts" / "anchor_dataset_v1" / "segments.jsonl"
DEFAULT_PAIR_CANDIDATES = REPO_ROOT / "analyse3" / "anchor" / "artifacts" / "anchor_dataset_v1" / "pair_candidates.jsonl"
DEFAULT_PAIR_GOLD = REPO_ROOT / "analyse3" / "anchor" / "artifacts" / "anchor_dataset_v1" / "pair_gold_labels.jsonl"
DEFAULT_PROMPT = ROOT / "community_purify_prompt_v1.md"
DEFAULT_ENV = REPO_ROOT / ".env"
DEFAULT_MODEL = "gpt-5.6-luna"
RESPONSES_MODELS = {"gpt-5.6-luna"}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


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
        raise ValueError(f"Prompt has no {language!r} fenced block: {path}")
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


def validate_groups(value: dict[str, Any], expected_ids: list[str]) -> list[dict[str, Any]]:
    groups = value.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("model output must contain a non-empty groups list")
    expected = set(expected_ids)
    seen: list[str] = []
    normalized: list[dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        if not isinstance(group, dict):
            raise ValueError(f"group {index} is not an object")
        ids = group.get("segment_ids")
        if not isinstance(ids, list) or not ids or any(not isinstance(x, str) for x in ids):
            raise ValueError(f"group {index} must have a non-empty segment_ids list")
        seen.extend(ids)
        normalized.append({"group_id": f"g{index}", "segment_ids": ids})
    if len(seen) != len(set(seen)):
        raise ValueError("segment_id appears more than once")
    if set(seen) != expected:
        missing = sorted(expected - set(seen))
        extra = sorted(set(seen) - expected)
        raise ValueError(f"segment coverage mismatch: missing={missing[:5]}, extra={extra[:5]}")
    return normalized


def make_client(args: argparse.Namespace) -> Any:
    load_env(Path(args.env_file))
    api_key = (
        args.api_key
        or os.getenv("COMMUNITY_PURIFY_OPENAI_API_KEY")
        or os.getenv("CUTTING_OPENAI_API_KEY")
        or os.getenv("GRAPH_WEEKLY_OPENAI_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
    )
    base_url = (
        args.base_url
        or os.getenv("COMMUNITY_PURIFY_OPENAI_BASE_URL")
        or os.getenv("CUTTING_OPENAI_BASE_URL")
        or os.getenv("GRAPH_WEEKLY_OPENAI_BASE_URL")
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


def load_communities(input_dir: Path, run: str) -> list[dict[str, Any]]:
    path = input_dir / "runs" / run / "communities.jsonl"
    rows = read_jsonl(path)
    if not rows:
        raise ValueError(f"empty communities file: {path}")
    return rows


def build_inputs(
    communities: list[dict[str, Any]],
    segment_rows: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    by_community: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in communities:
        segment_id = str(row["segment_id"])
        extra = segment_rows.get(segment_id, {})
        by_community[str(row["community_id"])].append(
            {
                "segment_id": segment_id,
                "anchor": row.get("anchor"),
                "text": extra.get("text"),
            }
        )
    inputs = []
    original = {}
    for community_id, segments in sorted(by_community.items(), key=lambda item: int(item[0])):
        ids = [segment["segment_id"] for segment in segments]
        original[community_id] = ids
        if len(ids) > 1:
            inputs.append({"community_id": community_id, "segments": segments})
    return inputs, original


def evaluate_partition(
    original: dict[str, list[str]],
    purified_rows: list[dict[str, Any]],
    pair_candidates: list[dict[str, Any]],
    pair_gold: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    after: dict[str, dict[str, str]] = {}
    result_by_community = {str(row["community_id"]): row for row in purified_rows if row.get("status") == "ok"}
    for community_id, ids in original.items():
        row = result_by_community.get(community_id)
        if row is None:
            continue
        mapping = {}
        for group in row["groups"]:
            for segment_id in group["segment_ids"]:
                mapping[segment_id] = group["group_id"]
        after[community_id] = mapping

    node_to_original = {segment_id: community_id for community_id, ids in original.items() for segment_id in ids}
    pair_rows = []
    for candidate in pair_candidates:
        left = candidate.get("left_segment_id")
        right = candidate.get("right_segment_id")
        community_id = node_to_original.get(left)
        if community_id is None or node_to_original.get(right) != community_id:
            continue
        gold = pair_gold.get(candidate.get("pair_id"))
        if not gold:
            continue
        relation = gold.get("relation")
        before_same = True
        after_same = (
            community_id in after
            and left in after[community_id]
            and right in after[community_id]
            and after[community_id][left] == after[community_id][right]
        )
        pair_rows.append({"relation": relation, "before_same": before_same, "after_same": after_same})

    counts = Counter(row["relation"] for row in pair_rows)

    def rate(numerator: int, denominator: int) -> float | None:
        return numerator / denominator if denominator else None

    def summarize(use_after: bool) -> dict[str, Any]:
        same_key = "after_same" if use_after else "before_same"
        unrelated = [row for row in pair_rows if row["relation"] in {"unrelated", "shared_entity_only"}]
        memory = [row for row in pair_rows if row["relation"] == "same_memory_item"]
        specific = [row for row in pair_rows if row["relation"] == "same_specific_topic"]
        broad = [row for row in pair_rows if row["relation"] == "same_broad_family"]
        compatible = memory + specific + broad
        return {
            "pair_count": len(pair_rows),
            "cross_topic_merge_rate": rate(sum(row[same_key] for row in unrelated), len(unrelated)),
            "hard_oversplit_rate": rate(sum(not row[same_key] for row in memory), len(memory)),
            "soft_oversplit_rate": rate(sum(not row[same_key] for row in specific), len(specific)),
            "broad_topic_retention": rate(sum(row[same_key] for row in broad), len(broad)),
            "compatible_pair_retention": rate(sum(row[same_key] for row in compatible), len(compatible)),
            "relation_counts": dict(counts),
        }

    split_rows = [row for row in purified_rows if row.get("status") == "ok" and len(row.get("groups", [])) > 1]
    return {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "annotated_pairs_inside_original_communities": len(pair_rows),
        "original_community_count": len(original),
        "non_singleton_community_count": sum(len(ids) > 1 for ids in original.values()),
        "successful_community_count": len(result_by_community),
        "split_community_count": len(split_rows),
        "baseline_no_purification": summarize(False),
        "purified": summarize(True),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--run", default=DEFAULT_RUN)
    parser.add_argument("--segments", type=Path, default=DEFAULT_SEGMENTS)
    parser.add_argument("--pair-candidates", type=Path, default=DEFAULT_PAIR_CANDIDATES)
    parser.add_argument("--pair-gold", type=Path, default=DEFAULT_PAIR_GOLD)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--language", choices=("zh", "en"), default="en")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts" / "two_days_v1")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-key")
    parser.add_argument("--base-url")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    proxy = parser.add_mutually_exclusive_group()
    proxy.add_argument("--use-proxy", dest="use_proxy", action="store_true")
    proxy.add_argument("--no-proxy", dest="use_proxy", action="store_false")
    parser.set_defaults(use_proxy=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    communities = load_communities(args.input_dir, args.run)
    segment_rows = {str(row["segment_id"]): row for row in read_jsonl(args.segments)}
    inputs, original = build_inputs(communities, segment_rows)
    prompt = load_prompt(args.prompt, args.language)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.output_dir / "community_inputs.jsonl", inputs)

    pair_candidates = read_jsonl(args.pair_candidates)
    pair_gold = {str(row["pair_id"]): row for row in read_jsonl(args.pair_gold)}
    baseline_rows = [
        {"community_id": community_id, "status": "ok", "groups": [{"group_id": "g1", "segment_ids": ids}]}
        for community_id, ids in original.items()
    ]
    baseline_metrics = evaluate_partition(original, baseline_rows, pair_candidates, pair_gold)
    (args.output_dir / "baseline_metrics.json").write_text(
        json.dumps(baseline_metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    output_path = args.output_dir / "purified_communities.jsonl"
    existing = {str(row["community_id"]): row for row in read_jsonl(output_path)} if args.resume and output_path.exists() else {}
    results: list[dict[str, Any]] = []
    client = None if args.dry_run else make_client(args)
    for index, item in enumerate(inputs, start=1):
        community_id = str(item["community_id"])
        if community_id in existing and existing[community_id].get("status") in {"ok", "dry_run"}:
            results.append(existing[community_id])
            print(f"[{index}/{len(inputs)}] resume community={community_id}")
            continue
        started = time.time()
        result: dict[str, Any] = {
            "community_id": community_id,
            "status": "error",
            "model": args.model,
            "prompt": str(args.prompt),
            "language": args.language,
        }
        try:
            if args.dry_run:
                result.update({"status": "dry_run", "groups": [{"group_id": "g1", "segment_ids": [x["segment_id"] for x in item["segments"]]}]})
            else:
                raw = call_model(client, prompt, item, args)
                value = parse_json_object(raw)
                groups = validate_groups(value, [x["segment_id"] for x in item["segments"]])
                result.update({"status": "ok", "groups": groups, "raw_response": raw})
            result["elapsed_seconds"] = round(time.time() - started, 3)
            print(f"[{index}/{len(inputs)}] community={community_id} groups={len(result['groups'])}")
        except Exception as exc:
            result.update({"error": repr(exc), "elapsed_seconds": round(time.time() - started, 3)})
            print(f"[{index}/{len(inputs)}] ERROR community={community_id}: {exc}")
        existing[community_id] = result
        write_jsonl(output_path, sorted(existing.values(), key=lambda row: int(row["community_id"])))
        results = list(existing.values())

    if args.dry_run:
        metrics = baseline_metrics
    else:
        all_rows = baseline_rows[:]
        by_id = {str(row["community_id"]): row for row in results}
        for community_id, ids in original.items():
            if len(ids) == 1:
                all_rows.append({"community_id": community_id, "status": "ok", "groups": [{"group_id": "g1", "segment_ids": ids}]})
            elif community_id in by_id:
                all_rows.append(by_id[community_id])
        metrics = evaluate_partition(original, all_rows, pair_candidates, pair_gold)
    (args.output_dir / "evaluation.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_dir": str(args.input_dir),
        "run": args.run,
        "prompt": str(args.prompt),
        "language": args.language,
        "model": args.model,
        "community_input_count": len(inputs),
        "node_count": len(communities),
        "dry_run": args.dry_run,
    }
    (args.output_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {output_path}")
    print(f"wrote {args.output_dir / 'evaluation.json'}")


if __name__ == "__main__":
    main()
