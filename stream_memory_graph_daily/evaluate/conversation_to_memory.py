#!/usr/bin/env python3
"""阶段一：把评测对话按日期流式转换并持久化为每日记忆图。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


EVALUATE_ROOT = Path(__file__).resolve().parent
PACKAGE_ROOT = EVALUATE_ROOT.parent
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from stream_memory_graph_daily.config import DailyGraphConfig
from stream_memory_graph_daily.encoder import load_encoder
from stream_memory_graph_daily.evaluate.progress import ConsoleProgress
from stream_memory_graph_daily.evaluate.share_memory import (
    build_share_memory_attributions,
    extract_share_memory_labels,
    generate_share_memory_report,
    sanitize_conversation_for_inference,
)
from stream_memory_graph_daily.llm import OpenAIJsonLLM, load_env
from stream_memory_graph_daily.pipeline import DailyMemoryGraph


DEFAULT_CONVERSATIONS = EVALUATE_ROOT / "data" / "conversations"
DEFAULT_OUTPUT_DIR = EVALUATE_ROOT / "artifacts" / "memory_build"


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


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def session_id_from_path(path: Path) -> int:
    match = re.search(r"session_(\d+)", path.name)
    if not match:
        raise ValueError(f"无法从文件名提取 session_id：{path.name}")
    return int(match.group(1))


def parse_range(value: str) -> tuple[int, int]:
    try:
        start_text, end_text = value.split("-", 1)
        start, end = int(start_text), int(end_text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("范围格式必须为 START-END") from exc
    if start < 1 or end < start:
        raise argparse.ArgumentTypeError("session 范围必须满足 1 <= START <= END")
    return start, end


def select_session_files(
    directory: Path,
    *,
    session_range: tuple[int, int] | None = None,
    limit: int | None = None,
) -> list[Path]:
    if not directory.is_dir():
        raise FileNotFoundError(f"对话目录不存在：{directory}")
    files = sorted(set(directory.rglob("session_*.json")), key=session_id_from_path)
    if session_range:
        start, end = session_range
        files = [path for path in files if start <= session_id_from_path(path) <= end]
    if limit is not None:
        if limit < 1:
            raise ValueError("--limit 必须为正整数")
        files = files[:limit]
    if not files:
        raise ValueError(f"没有找到符合条件的 session_*.json：{directory}")
    return files


def discover_conversation_directories(directory: Path) -> list[tuple[str, Path]]:
    """Return one or more dataset names and conversation directories.

    A normal persona directory contains ``conversations/session_*.json`` and
    is returned as one dataset.  A directory containing persona subdirectories
    is treated as a batch root; each persona is kept as a separate dataset so
    its memory state cannot be mixed with another persona's state.
    """
    directory = directory.resolve()
    if not directory.is_dir():
        raise FileNotFoundError(f"对话目录不存在：{directory}")

    datasets: list[tuple[str, Path]] = []
    for child in sorted(directory.iterdir(), key=lambda path: path.name):
        if not child.is_dir():
            continue
        conversation_dir = child / "conversations"
        if conversation_dir.is_dir() and any(conversation_dir.rglob("session_*.json")):
            datasets.append((child.name, conversation_dir))

    if datasets:
        return datasets
    return [(directory.name, directory)]


class MemoryBuildRunner:
    """运行对话建图，并在每个 session 后保存可恢复状态。"""

    def __init__(
        self,
        pipeline: DailyMemoryGraph,
        output_dir: Path,
        *,
        state_file: Path | None = None,
        progress_file: Path | None = None,
        save_every: int = 1,
        resume_progress: bool = False,
        preprocess_workers: int = 1,
        use_cache: bool = False,
        cache_dir: Path | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.output_dir = output_dir
        self.state_file = state_file or output_dir / "memory_state.json"
        self.progress_file = progress_file or output_dir / "progress.json"
        self.share_memory_labels_file = output_dir / "share_memory_labels.jsonl"
        self.share_memory_attribution_file = output_dir / "share_memory_attribution.jsonl"
        self.share_memory_report_file = output_dir / "share_memory_report.json"
        self.save_every = max(1, int(save_every))
        self.preprocess_workers = max(1, int(preprocess_workers))
        self.use_cache = bool(use_cache)
        self.cache_dir = cache_dir or output_dir / "preprocess_cache"
        self.metadata = dict(metadata or {})
        self.progress = self._load_progress() if resume_progress else self._new_progress()
        self.share_memory_labels = self._load_share_memory_labels() if resume_progress else []

    @staticmethod
    def _new_progress() -> dict[str, Any]:
        return {
            "schema_version": "daily_memory_build_progress_v1",
            "started_at": utc_now(),
            "updated_at": utc_now(),
            "successful_sessions": [],
            "failed_sessions": [],
            "session_results": [],
            "finalize_result": None,
        }

    def _load_progress(self) -> dict[str, Any]:
        if self.progress_file.is_file():
            value = json.loads(self.progress_file.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        return self._new_progress()

    def _load_share_memory_labels(self) -> list[dict[str, Any]]:
        if not self.share_memory_labels_file.is_file():
            return []
        rows = []
        for line in self.share_memory_labels_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
        return rows

    def _upsert_share_memory_labels(self, rows: Iterable[dict[str, Any]]) -> None:
        by_id = {
            str(row.get("label_id")): dict(row)
            for row in self.share_memory_labels
            if row.get("label_id")
        }
        for row in rows:
            label_id = str(row.get("label_id", ""))
            if not label_id:
                raise ValueError("share_memory evaluation label has no label_id")
            by_id[label_id] = dict(row)
        self.share_memory_labels = [by_id[key] for key in sorted(by_id)]

    def persist(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.pipeline.save(self.state_file)
        write_jsonl(
            self.output_dir / "memories.jsonl",
            (memory.to_dict() for memory in self.pipeline.memories.values()),
        )
        write_jsonl(
            self.output_dir / "segments.jsonl",
            (segment.to_dict() for segment in self.pipeline.segments.values()),
        )
        write_jsonl(
            self.output_dir / "boundaries.jsonl",
            (boundary.to_dict() for boundary in self.pipeline.boundaries.values()),
        )
        write_jsonl(self.output_dir / "trace.jsonl", self.pipeline.trace)
        write_jsonl(
            self.output_dir / "stage_audit.jsonl",
            self.pipeline.stage_audit,
        )
        attributions = build_share_memory_attributions(
            self.share_memory_labels, self.pipeline
        )
        share_memory_report = generate_share_memory_report(
            attributions, self.pipeline
        )
        write_jsonl(self.share_memory_labels_file, self.share_memory_labels)
        write_jsonl(self.share_memory_attribution_file, attributions)
        atomic_write_json(self.share_memory_report_file, share_memory_report)
        self.progress["updated_at"] = utc_now()
        self.progress["state_file"] = str(self.state_file.resolve())
        self.progress["stats"] = self.pipeline.stats()
        self.progress["share_memory"] = {
            "labels": len(self.share_memory_labels),
            "mapped": share_memory_report["mapped_labels"],
            "fully_compressed": share_memory_report["fully_compressed_labels"],
            "supervision_leakage_events": share_memory_report[
                "supervision_leakage_event_count"
            ],
        }
        atomic_write_json(self.progress_file, self.progress)
        atomic_write_json(
            self.output_dir / "run_manifest.json",
            {
                "schema_version": "daily_memory_build_manifest_v1",
                "updated_at": utc_now(),
                "state_file": str(self.state_file.resolve()),
                "progress_file": str(self.progress_file.resolve()),
                "stage_audit_file": str((self.output_dir / "stage_audit.jsonl").resolve()),
                "share_memory_labels_file": str(self.share_memory_labels_file.resolve()),
                "share_memory_attribution_file": str(
                    self.share_memory_attribution_file.resolve()
                ),
                "share_memory_report_file": str(self.share_memory_report_file.resolve()),
                "pipeline_schema": self.pipeline.schema_version,
                "config": self.pipeline.config.to_dict(),
                "stats": self.pipeline.stats(),
                **self.metadata,
            },
        )

    def _cache_model(self) -> str | None:
        model = getattr(self.pipeline.llm, "model", None)
        return str(model) if model else self.metadata.get("llm_model")

    def _cache_path(self, session_id: int) -> Path:
        return self.cache_dir / f"session_{session_id:04d}.json"

    def _prepare_file(
        self,
        item: tuple[int, Path, dict[str, Any], str],
    ) -> dict[str, Any]:
        """Prepare one session without mutating graph state.

        This function is safe to run in a worker thread. Each worker only
        calls the stateless cutting/anchoring stages and writes its own cache
        file; graph ingestion remains on the main thread.
        """

        position, path, conversation, source_sha256 = item
        session_id = session_id_from_path(path)
        cache_path = self._cache_path(session_id)
        cache_model = self._cache_model()
        if self.use_cache and cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if (
                    isinstance(cached, dict)
                    and cached.get("schema_version") == "conversation_preprocess_cache_v1"
                    and cached.get("source_sha256") == source_sha256
                    and cached.get("llm_model") == cache_model
                    and isinstance(cached.get("prepared"), dict)
                ):
                    return {
                        "position": position,
                        "path": path,
                        "status": "success",
                        "prepared": cached["prepared"],
                        "cache_hit": True,
                    }
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                # A malformed or stale cache is simply rebuilt.
                pass

        inference_conversation = sanitize_conversation_for_inference(conversation)
        prepared = self.pipeline.prepare_conversation(inference_conversation)
        if self.use_cache:
            atomic_write_json(
                cache_path,
                {
                    "schema_version": "conversation_preprocess_cache_v1",
                    "source_sha256": source_sha256,
                    "llm_model": cache_model,
                    "session_id": session_id,
                    "prepared": prepared,
                },
            )
        return {
            "position": position,
            "path": path,
            "status": "success",
            "prepared": prepared,
            "cache_hit": False,
        }

    def _prepare_file_safe(
        self,
        item: tuple[int, Path, dict[str, Any], str],
    ) -> dict[str, Any]:
        try:
            return self._prepare_file(item)
        except Exception as exc:
            return {
                "position": item[0],
                "path": item[1],
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

    def run(self, files: list[Path], *, fail_fast: bool = False) -> dict[str, Any]:
        completed = {int(value) for value in self.progress.get("successful_sessions", [])}
        processed_since_save = 0
        progress_bar = ConsoleProgress(len(files), "构建记忆")

        pending: list[tuple[int, Path, dict[str, Any], str]] = []
        for position, path in enumerate(files, start=1):
            session_id = session_id_from_path(path)
            raw = path.read_bytes()
            conversation = json.loads(raw.decode("utf-8"))
            self._upsert_share_memory_labels(
                extract_share_memory_labels(conversation, source_file=path)
            )
            if session_id in completed:
                progress_bar.update(position, f"session_{session_id:04d} 已存在")
                continue

            pending.append(
                (position, path, conversation, hashlib.sha256(raw).hexdigest())
            )

        def consume(prepared_result: dict[str, Any]) -> None:
            nonlocal processed_since_save
            position = int(prepared_result["position"])
            path = Path(prepared_result["path"])
            session_id = session_id_from_path(path)
            progress_bar.update(position - 1, f"session_{session_id:04d} 处理中")
            try:
                if prepared_result.get("status") != "success":
                    raise RuntimeError(str(prepared_result.get("error", "preprocessing failed")))
                result = self.pipeline.ingest_prepared_conversation(
                    prepared_result["prepared"]
                )
                self.progress["successful_sessions"].append(session_id)
                self.progress["session_results"].append(
                    {
                        "session_id": session_id,
                        "file": str(path.resolve()),
                        "status": "success",
                        "event_date": result["event_date"],
                        "cut_segments": result["cut_segments"],
                        "cache_hit": bool(prepared_result.get("cache_hit", False)),
                        "saved_at": utc_now(),
                    }
                )
                progress_bar.update(
                    position,
                    f"session_{session_id:04d} 完成，"
                    f"日期={result['event_date']}，segments={result['cut_segments']}"
                )
            except Exception as exc:
                error = {
                    "session_id": session_id,
                    "file": str(path.resolve()),
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "failed_at": utc_now(),
                }
                self.progress["failed_sessions"].append(error)
                self.progress["session_results"].append(error)
                progress_bar.write(f"session_{session_id:04d} 失败：{exc}")
                progress_bar.update(position, f"session_{session_id:04d} 失败")
                self.persist()
                if fail_fast:
                    raise
            processed_since_save += 1
            if processed_since_save >= self.save_every:
                self.persist()
                processed_since_save = 0

        if self.preprocess_workers > 1 and pending:
            with ThreadPoolExecutor(max_workers=self.preprocess_workers) as executor:
                for prepared_result in executor.map(self._prepare_file_safe, pending):
                    consume(prepared_result)
        else:
            for item in pending:
                prepared_result = self._prepare_file_safe(item)
                consume(prepared_result)

        self.progress["finalize_result"] = self.pipeline.finalize()
        self.persist()
        progress_bar.finish(
            f"完成：成功 {len(self.progress['successful_sessions'])}，"
            f"失败 {len(self.progress['failed_sessions'])}"
        )
        return {
            "state_file": str(self.state_file),
            "successful": len(self.progress["successful_sessions"]),
            "failed": len(self.progress["failed_sessions"]),
            "stats": self.pipeline.stats(),
            "finalize": self.progress["finalize_result"],
            "share_memory_report": str(self.share_memory_report_file),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversation-directory", type=Path, default=DEFAULT_CONVERSATIONS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--progress-file", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--session-range", type=parse_range, metavar="START-END")
    parser.add_argument("--save-every", type=int, default=1)
    parser.add_argument(
        "--preprocess-workers",
        type=int,
        default=1,
        help="并发执行 cutting/anchor 预处理的 worker 数；图写入仍按 session 顺序进行",
    )
    parser.add_argument(
        "--postprocess-workers",
        type=int,
        default=1,
        help="并发执行社区纯化、记忆提取和记忆融合的 worker 数；状态提交仍按稳定顺序进行",
    )
    parser.add_argument(
        "--use-cache",
        action="store_true",
        help="启用 session 预处理缓存，缓存按源文件哈希和模型名校验",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help="预处理缓存目录，默认是 output-dir/preprocess_cache",
    )
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--llm-model", default="gpt-5.6-luna")
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
    parser.add_argument("--new-new-threshold", type=float, default=0.5)
    parser.add_argument("--new-memory-threshold", type=float, default=0.5)
    parser.add_argument("--assignment-min-support", type=float, default=0.35)
    parser.add_argument("--assignment-margin", type=float, default=0.08)
    return parser


def _run_memory_build(
    args: argparse.Namespace,
    *,
    dataset_name: str,
    conversation_directory: Path,
    output_dir: Path,
    state_file: Path,
    progress_file: Path,
    cache_dir: Path,
    encoder: Any,
    llm: Any,
    config: DailyGraphConfig,
) -> dict[str, Any]:
    if args.resume:
        if not state_file.is_file():
            raise FileNotFoundError(f"--resume 指定的状态文件不存在：{state_file}")
        pipeline = DailyMemoryGraph.load(state_file, llm=llm, encoder=encoder)
    else:
        pipeline = DailyMemoryGraph(llm=llm, encoder=encoder, config=config)

    files = select_session_files(
        conversation_directory,
        session_range=args.session_range,
        limit=args.limit,
    )
    runner = MemoryBuildRunner(
        pipeline,
        output_dir,
        state_file=state_file,
        progress_file=progress_file,
        save_every=args.save_every,
        resume_progress=args.resume,
        preprocess_workers=args.preprocess_workers,
        use_cache=args.use_cache,
        cache_dir=cache_dir,
        metadata={
            "dataset_name": dataset_name,
            "conversation_directory": str(conversation_directory.resolve()),
            "selected_session_count": len(files),
            "llm_model": None if args.no_llm else args.llm_model,
            "encoder_model": args.encoder_model,
            "use_proxy": args.use_proxy,
            "preprocess_workers": args.preprocess_workers,
            "postprocess_workers": args.postprocess_workers,
            "use_cache": args.use_cache,
            "cache_dir": str(cache_dir),
        },
    )
    return runner.run(files, fail_fast=args.fail_fast)


def main() -> None:
    args = build_parser().parse_args()
    load_env(args.env_file)
    api_key = args.api_key or os.getenv("LOCAL_OPENAI_API_KEY")
    base_url = args.base_url or os.getenv("LOCAL_OPENAI_BASE_URL")
    output_root = args.output_dir.resolve()
    datasets = discover_conversation_directories(args.conversation_directory)
    batch_mode = len(datasets) > 1

    if args.preprocess_workers < 1:
        raise ValueError("--preprocess-workers 必须为正整数")
    if args.postprocess_workers < 1:
        raise ValueError("--postprocess-workers 必须为正整数")
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
        api_key=api_key,
        base_url=base_url,
        timeout=args.timeout,
        max_tokens=args.max_tokens,
        retries=config.llm_retries,
        use_proxy=args.use_proxy,
        env_file=args.env_file,
    )

    results: dict[str, Any] = {}
    state_root = (
        args.state_file.resolve().parent
        if args.state_file and args.state_file.name == "memory_state.json"
        else args.state_file.resolve() if args.state_file else None
    )
    progress_root = (
        args.progress_file.resolve().parent
        if args.progress_file and args.progress_file.name == "progress.json"
        else args.progress_file.resolve() if args.progress_file else None
    )
    for dataset_name, conversation_directory in datasets:
        output_dir = output_root / dataset_name if batch_mode else output_root
        state_file = (
            (state_root / dataset_name / "memory_state.json")
            if batch_mode and state_root
            else (output_dir / "memory_state.json")
        )
        progress_file = (
            (progress_root / dataset_name / "progress.json")
            if batch_mode and progress_root
            else (output_dir / "progress.json")
        )
        cache_dir = (
            (args.cache_dir.resolve() / dataset_name)
            if batch_mode and args.cache_dir
            else output_dir / "preprocess_cache"
        )
        print(f"\n===== 构建记忆：{dataset_name} =====")
        try:
            results[dataset_name] = _run_memory_build(
                args,
                dataset_name=dataset_name,
                conversation_directory=conversation_directory,
                output_dir=output_dir,
                state_file=state_file,
                progress_file=progress_file,
                cache_dir=cache_dir,
                encoder=encoder,
                llm=llm,
                config=config,
            )
        except Exception as exc:
            results[dataset_name] = {"error": str(exc)}
            print(f"{dataset_name} 失败：{exc}")
            if args.fail_fast:
                raise

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
