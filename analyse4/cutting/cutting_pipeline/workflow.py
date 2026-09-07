from __future__ import annotations

import json
import sys
import hashlib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import RUNS_DIR
from .data import load_annotations, load_conversations, make_units, read_jsonl, validate_annotation_units, write_jsonl
from .llm import build_openai_client, call_cutting_model
from .metrics import aggregate_boundary_metrics, boundary_error_breakdown, gold_positions, positions_from_segments
from .prompt import load_prompt
from .purity import make_purity_template, purity_metrics


def select_sessions(args: Any) -> list[dict[str, Any]]:
    conversations = load_conversations(Path(args.data_dir))
    annotations = {}
    if args.session_source == "annotations":
        annotations = load_annotations(Path(args.annotations))
        session_ids = sorted(annotations)
    else:
        session_ids = sorted(conversations)
    if args.session_ids:
        requested = {int(value) for value in args.session_ids.split(",") if value.strip()}
        session_ids = [session_id for session_id in session_ids if session_id in requested]
    if args.limit is not None:
        session_ids = session_ids[: args.limit]
    missing = [session_id for session_id in session_ids if session_id not in conversations]
    if missing:
        raise ValueError(f"annotation sessions missing from data: {missing[:10]}")
    records = []
    for session_id in session_ids:
        conversation = conversations[session_id]
        annotation = annotations.get(session_id)
        units = validate_annotation_units(annotation, conversation) if annotation else make_units(conversation["conversation"])
        records.append({
            "session_id": session_id,
            "session_type": conversation.get("session_type"),
            "operation": conversation.get("operation"),
            "source_file": f"data/conversations/session_{session_id:04d}.json",
            "units": units,
        })
    return records


def run_experiment(args: Any) -> None:
    from .data import load_dotenv

    if args.env_file:
        load_dotenv(Path(args.env_file), override=True)
    prompt = load_prompt(Path(args.prompts_dir) / args.prompt_version, args.prompt_version)
    records = select_sessions(args)
    output_dir = Path(args.run_dir) if args.run_dir else RUNS_DIR / prompt.version / args.run_id
    predictions_path = output_dir / "predictions.jsonl"
    prompt_sha256 = hashlib.sha256(
        (prompt.system_prompt + "\n" + prompt.user_template).encode("utf-8")
    ).hexdigest()
    manifest_path = output_dir / "run_manifest.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (previous.get("prompt_version") != prompt.version or
                previous.get("prompt_sha256") != prompt_sha256):
            raise ValueError(
                f"run directory {output_dir} belongs to a different Prompt; "
                "use a new --run-id or --run-dir"
            )
    if predictions_path.exists() and not (args.resume or args.force or args.dry_run):
        raise FileExistsError(
            f"predictions already exist in {output_dir}; use --resume, --force, or a new run id"
        )
    existing = {}
    if args.resume and predictions_path.exists():
        existing = {int(row["session_id"]): row for row in read_jsonl(predictions_path)}
    if args.dry_run:
        for record in records:
            json.dumps(record["units"], ensure_ascii=False)
        print(f"dry-run ok: sessions={len(records)} prompt_version={prompt.version} "
              f"run_dir={output_dir} python={sys.executable}")
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    client = build_openai_client(
        args.api_key, args.base_url, args.timeout, args.env_file, args.use_proxy
    )
    results = []
    for number, record in enumerate(records, start=1):
        session_id = record["session_id"]
        if session_id in existing and not args.force:
            results.append(existing[session_id])
            print(f"[{number}/{len(records)}] session_{session_id:04d}: resumed")
            continue
        result = {
            "session_id": session_id, "session_type": record["session_type"],
            "operation": record["operation"], "units": record["units"],
            "model": args.model, "temperature": args.temperature,
            "prompt_version": prompt.version, "status": "error",
        }
        try:
            normalized, raw_text = call_cutting_model(
                client, record["units"], args.model, args.temperature, args.max_tokens, args.retries,
                system_prompt=prompt.system_prompt, user_template=prompt.user_template,
            )
            result.update({
                "status": "ok", "segments": normalized.segments,
                "predicted_boundary_positions": normalized.boundary_positions,
                "raw_response": raw_text,
            })
            print(f"[{number}/{len(records)}] session_{session_id:04d}: ok segments={len(normalized.segments)}")
        except Exception as exc:
            result["error"] = str(exc)
            print(f"[{number}/{len(records)}] session_{session_id:04d}: ERROR {exc}", file=sys.stderr)
        results.append(result)
        write_jsonl(predictions_path, sorted(results, key=lambda row: int(row["session_id"])))

    write_jsonl(predictions_path, sorted(results, key=lambda row: int(row["session_id"])))
    manifest = {
        "schema_version": "cutting-run-v2",
        "created_at": datetime.now(timezone.utc).isoformat(), "prompt_version": prompt.version,
        "prompt_dir": str(prompt.directory),
        "prompt_sha256": prompt_sha256,
        "system_prompt": prompt.system_prompt, "user_template": prompt.user_template,
        "model": args.model, "temperature": args.temperature,
        "max_tokens": args.max_tokens, "session_source": args.session_source,
        "use_proxy": args.use_proxy,
        "annotations": str(Path(args.annotations)), "prediction_file": str(predictions_path),
        "env_file": str(Path(args.env_file).resolve()) if args.env_file else None,
        "python_executable": sys.executable,
        "sessions": len(results),
        "successful_sessions": sum(row.get("status") == "ok" for row in results),
        "failed_sessions": sum(row.get("status") != "ok" for row in results),
    }
    (output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {predictions_path}")


def _metric(value: Any) -> str:
    return "—" if value is None else f"{value:.4f}" if isinstance(value, float) else str(value)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 对话事件切割实验结果", "",
        f"- Prompt：`{report['prompt_version']}`",
        f"- 标注状态：`{report['annotation_statuses']}`",
        f"- {report['tolerance_note']}", "",
        "| 样本类型 | Precision | Recall | Boundary F1 | F1@±1 | Pk | WindowDiff | 段内纯度 | 漏切数 | 过切数 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, metrics in report["strata"].items():
        boundary = metrics["boundary"]
        tolerance = metrics.get("boundary_f1_tolerance", {})
        purity = report["segment_purity"]["pure"] if name == "all" else None
        values = [name, _metric(boundary["precision"]), _metric(boundary["recall"]),
                  _metric(boundary["f1"]), _metric(tolerance.get("f1")), _metric(metrics["pk"]),
                  _metric(metrics["window_diff"]), _metric(purity), str(boundary["missed"]),
                  str(boundary["oversegmented"])]
        lines.append("| " + " | ".join(values) + " |")
    lines += [
        "", "## 说明", "",
        "- 漏切数是 gold 边界未命中的数量；过切数是预测边界未命中的数量，均按精确边界统计。",
        "- Pk 与 WindowDiff 使用每个 session 的 gold 平均事件长度确定窗口，并在 session 间取平均。",
        "- 段内纯度需要人工填写 purity review 文件；未提供时显示为 `—`。",
        "- 若需要显式/隐式转场、话题回归或同 turn 转场的独立行，请在标注记录增加 `scenario_types`，脚本会自动生成对应 strata。",
        "",
    ]
    if report.get("boundary_error_breakdown"):
        lines += ["## 边界错误分布", "",
                  "| 边界类型 | Gold 边界数 | 命中 | 漏切 | Recall | Recall@±1 |",
                  "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for name, values in report["boundary_error_breakdown"].items():
            lines.append(f"| {name} | {values['gold']} | {values['matched']} | {values['missed']} | "
                         f"{_metric(values['recall'])} | {_metric(values.get('recall_tolerance'))} |")
        lines.append("")
    return "\n".join(lines)


def evaluate_experiment(args: Any) -> None:
    prediction_rows = read_jsonl(Path(args.predictions))
    annotations = load_annotations(Path(args.annotations))
    conversations = load_conversations(Path(args.data_dir))
    joined = []
    for prediction in prediction_rows:
        session_id = int(prediction["session_id"])
        if session_id not in annotations:
            raise ValueError(f"prediction session {session_id} has no matching annotation")
        annotation = annotations[session_id]
        if session_id in conversations:
            annotation = {**annotation, "units": validate_annotation_units(annotation, conversations[session_id])}
        joined.append({"session_id": session_id, "gold": annotation, "prediction": prediction})
    if not joined:
        raise ValueError("prediction file is empty")
    tolerance = args.tolerance if not args.no_tolerance else None
    strata = {"all": joined}
    for row in joined:
        gold = row["gold"]
        if gold.get("session_type"):
            strata.setdefault(f"session_type={gold['session_type']}", []).append(row)
        if gold.get("operation"):
            strata.setdefault(f"operation={gold['operation']}", []).append(row)
        labels = gold.get("scenario_types") or []
        if isinstance(labels, str):
            labels = [labels]
        for label in labels:
            strata.setdefault(f"scenario={label}", []).append(row)
    report = {
        "schema_version": "cutting-evaluation-v2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "prompt_version": ",".join(sorted({row["prediction"].get("prompt_version", "unknown") for row in joined})),
        "prompt_versions": sorted({row["prediction"].get("prompt_version", "unknown") for row in joined}),
        "annotations": str(Path(args.annotations)), "predictions": str(Path(args.predictions)),
        "annotation_statuses": dict(Counter(row["gold"].get("annotation_status") for row in joined)),
        "tolerance_note": ("±1 is auxiliary only; the supplied annotation file has one annotation layer, "
                           "so inter-annotator disagreement is not available to justify selecting it."
                           if tolerance is not None else "±1 tolerance disabled"),
        "overall": aggregate_boundary_metrics(joined, tolerance),
        "strata": {name: aggregate_boundary_metrics(group, tolerance) for name, group in sorted(strata.items())},
        "boundary_error_breakdown": boundary_error_breakdown(joined, tolerance),
        "segment_purity": purity_metrics(Path(args.purity) if args.purity else None),
    }
    output_path = Path(args.output) if args.output else Path(args.predictions).with_name("evaluation.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_path.with_suffix(".md").write_text(render_markdown(report), encoding="utf-8")
    print(f"wrote {output_path}")
    print(f"wrote {output_path.with_suffix('.md')}")


def compare_runs(args: Any) -> None:
    """Create a compact comparison table for prompt iteration."""
    rows = []
    for run_name in args.runs:
        run_dir = Path(run_name)
        evaluation_path = run_dir / "evaluation.json"
        if not evaluation_path.exists():
            raise FileNotFoundError(f"evaluation.json not found in {run_dir}")
        report = json.loads(evaluation_path.read_text(encoding="utf-8"))
        overall = report.get("overall", {})
        boundary = overall.get("boundary", {})
        rows.append({
            "run": run_dir.name,
            "run_dir": str(run_dir),
            "prompt_versions": ",".join(report.get("prompt_versions", [])),
            "sessions": overall.get("sessions"),
            "failed_sessions": overall.get("failed_sessions"),
            "precision": boundary.get("precision"),
            "recall": boundary.get("recall"),
            "f1": boundary.get("f1"),
            "f1_tolerance": overall.get("boundary_f1_tolerance", {}).get("f1"),
            "pk": overall.get("pk"),
            "window_diff": overall.get("window_diff"),
            "missed": boundary.get("missed"),
            "oversegmented": boundary.get("oversegmented"),
            "purity": report.get("segment_purity", {}).get("pure"),
        })
    rows.sort(key=lambda row: row["run"])
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps({"schema_version": "cutting-comparison-v1", "runs": rows},
                                      ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    headers = ["run", "prompt_versions", "f1", "f1_tolerance", "pk", "window_diff",
               "purity", "missed", "oversegmented", "failed_sessions"]
    def fmt(value: Any) -> str:
        return "—" if value is None else f"{value:.4f}" if isinstance(value, float) else str(value)
    lines = ["# Cutting run comparison", "", "| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(fmt(row.get(header)) for header in headers) + " |")
    output_path.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {output_path}")
    print(f"wrote {output_path.with_suffix('.md')}")


__all__ = ["compare_runs", "evaluate_experiment", "make_purity_template", "run_experiment"]
