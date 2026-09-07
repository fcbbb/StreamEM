#!/usr/bin/env python3
"""Run the v1 semantic-anchor extraction prompt over a pilot JSONL file."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent.parent
PROJECT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_INPUT = ROOT / "artifacts" / "anchor_dataset_v1" / "pilot_v1" / "pilot_sample_60.jsonl"
DEFAULT_PROMPT = ROOT / "artifacts" / "anchor_dataset_v1" / "pilot_v1" / "anchor_extraction_prompt_v8_target_aspect.md"
DEFAULT_OUTPUT = ROOT / "artifacts" / "anchor_dataset_v1" / "pilot_v1" / "anchor_outputs_target_aspect_v8.jsonl"
DEFAULT_CHAT_COMPLETIONS_URL = "http://localhost:8080/v1/chat/completions"
DEFAULT_BASE_URL = DEFAULT_CHAT_COMPLETIONS_URL.removesuffix("/chat/completions")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--method", default="target_aspect_v8")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key")
    parser.add_argument("--env-file", default=str(PROJECT_ENV_FILE))
    proxy_group = parser.add_mutually_exclusive_group()
    proxy_group.add_argument("--use-proxy", dest="use_proxy", action="store_true")
    proxy_group.add_argument("--no-proxy", dest="use_proxy", action="store_false")
    parser.set_defaults(use_proxy=True)
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N input rows; omit to process all rows")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--request-timeout", type=float, default=120.0)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-json-mode", action="store_true", help="Do not send response_format=json_object")
    parser.add_argument("--segment-id", help="Process only the input row with this segment_id")
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load_prompt(path: Path) -> str:
    """Read the first fenced text prompt from a Markdown prompt document."""
    document = path.read_text(encoding="utf-8")
    match = re.search(r"```text\s*\n(.*?)\n```", document, flags=re.DOTALL)
    return match.group(1).strip() if match else document.strip()


def load_project_env(path: Path) -> None:
    """Match the cutting runner's simple .env loading behavior."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("'\"")


def extract_json(content: str) -> dict:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(content[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("Model output is not a JSON object")
    return value


def _validate_nullable_string(value: dict, key: str) -> None:
    if value[key] is not None and not isinstance(value[key], str):
        raise ValueError(f"{key} must be a string or null")


def validate_result(value: dict) -> None:
    # Minimal target/aspect schema: the extraction contract intentionally has
    # only the two semantic fields needed by the caller.
    if set(value) == {"core_target", "aspect"}:
        _validate_nullable_string(value, "core_target")
        _validate_nullable_string(value, "aspect")
        if value["core_target"] is None and value["aspect"] is not None:
            raise ValueError("aspect requires core_target in target/aspect schema")
        return
    # Minimal prompt schema: the extraction contract is intentionally just one
    # anchor field.  Keep it separate from the richer legacy schema below.
    if set(value) == {"selected_anchor"}:
        _validate_nullable_string(value, "selected_anchor")
        return
    # Keep the original candidate-selection schema usable while allowing the
    # targeted-v2 schema to add a stable clustering target.  This is detected
    # from schema_version/cluster_anchor so old result files remain readable.
    targeted = value.get("schema_version") == "targeted_v2" or "cluster_anchor" in value
    if targeted:
        required = {
            "schema_version",
            "target_type",
            "target_name",
            "target_scope",
            "facet",
            "operation",
            "cluster_anchor",
            "detail_anchor",
            "selected_anchor",
            "reason",
        }
    else:
        required = {"coarse_candidate", "selected_anchor", "fine_candidate", "reason"}
    missing = required - set(value)
    if missing:
        raise ValueError(f"Missing output fields: {sorted(missing)}")
    if targeted:
        if value["schema_version"] != "targeted_v2":
            raise ValueError("schema_version must be targeted_v2")
        for key in (
            "target_type",
            "target_name",
            "target_scope",
            "facet",
            "operation",
            "cluster_anchor",
            "detail_anchor",
            "selected_anchor",
        ):
            _validate_nullable_string(value, key)
        if value["selected_anchor"] != value["detail_anchor"]:
            raise ValueError("selected_anchor must equal detail_anchor in targeted_v2")
        if value["cluster_anchor"] is not None and (
            value["target_type"] is None or value["target_name"] is None
        ):
            raise ValueError("cluster_anchor requires target_type and target_name")
    else:
        for key in ("coarse_candidate", "selected_anchor", "fine_candidate"):
            _validate_nullable_string(value, key)
    if not isinstance(value["reason"], str):
        raise ValueError("reason must be a string")


def main() -> None:
    args = parse_args()
    load_project_env(Path(args.env_file))
    args.api_key = (
        args.api_key
        or os.getenv("LOCAL_OPENAI_API_KEY")
        or os.getenv("ANCHOR_OPENAI_API_KEY")
        or os.getenv("CUTTING_OPENAI_API_KEY")
        or os.getenv("GRAPH_WEEKLY_OPENAI_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
    )
    args.base_url = (
        args.base_url
        or os.getenv("LOCAL_OPENAI_BASE_URL")
        or os.getenv("ANCHOR_OPENAI_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or DEFAULT_BASE_URL
    )
    # The OpenAI SDK appends /chat/completions itself. Accept the full endpoint
    # in CLI/env input as a convenience, while passing only the API root to SDK.
    args.base_url = args.base_url.rstrip("/")
    if args.base_url.endswith("/chat/completions"):
        args.base_url = args.base_url[: -len("/chat/completions")]
    if not args.api_key:
        raise SystemExit(
            "Missing API key. Set LOCAL_OPENAI_API_KEY, ANCHOR_OPENAI_API_KEY, "
            "OPENAI_API_KEY, "
            "or pass --api-key."
        )

    rows = load_jsonl(args.input)
    prompt = load_prompt(args.prompt)
    if "{{conversation_segment}}" not in prompt:
        raise SystemExit("Prompt does not contain {{conversation_segment}} placeholder")
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be positive")
    if args.segment_id is not None:
        selected_rows = [row for row in rows if row.get("segment_id") == args.segment_id]
        if not selected_rows:
            raise SystemExit(f"segment_id not found in input: {args.segment_id}")
    else:
        selected_rows = rows if args.limit is None else rows[:args.limit]

    existing: dict[str, dict] = {}
    if args.resume and args.output.exists():
        for row in load_jsonl(args.output):
            existing[row["segment_id"]] = row

    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        import httpx
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit(f"Missing API dependency: {exc}") from exc
    http_client = httpx.Client(trust_env=args.use_proxy, follow_redirects=True)
    client_kwargs = {
        "api_key": args.api_key,
        "base_url": args.base_url,
        "timeout": args.request_timeout,
        "max_retries": 0,
        "http_client": http_client,
    }
    client = OpenAI(**client_kwargs)

    for position, row in enumerate(selected_rows, start=1):
        segment_id = row["segment_id"]
        if existing.get(segment_id, {}).get("status") == "success":
            print(f"[{position}/{len(selected_rows)}] skip {segment_id} (already completed)")
            continue
        user_prompt = prompt.replace("{{conversation_segment}}", row["text"])
        request = {"model": args.model}
        content = ""

        started = time.time()
        base_result = {
            "segment_id": segment_id,
            "method": args.method,
            "input_variant": "single_segment",
            "model": args.model,
            "temperature": args.temperature,
            "seed": args.seed,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            request.update({
                "messages": [{"role": "user", "content": user_prompt}],
                "temperature": args.temperature,
                "max_tokens": args.max_output_tokens,
            })
            if args.seed is not None:
                request["seed"] = args.seed
            if not args.no_json_mode:
                request["response_format"] = {"type": "json_object"}
            response = client.chat.completions.create(**request)
            content = response.choices[0].message.content or ""
            value = extract_json(content)
            validate_result(value)
            result = {**base_result, **value, "status": "success", "elapsed_seconds": round(time.time() - started, 3)}
            display_anchor = value.get("selected_anchor") or value.get("core_target")
            print(f"[{position}/{len(selected_rows)}] success {segment_id}: {display_anchor}")
        except Exception as exc:  # preserve failures for resumable runs
            result = {
                **base_result,
                "status": "error",
                "error": repr(exc),
                "raw_response": content[:4000],
                "elapsed_seconds": round(time.time() - started, 3),
            }
            print(f"[{position}/{len(selected_rows)}] ERROR {segment_id}: {exc}")
        existing[segment_id] = result
        with args.output.open("w", encoding="utf-8") as out:
            for saved in existing.values():
                out.write(json.dumps(saved, ensure_ascii=False, separators=(",", ":")) + "\n")

    print(f"Finished {len(selected_rows)} input rows; output: {args.output}")


if __name__ == "__main__":
    main()
