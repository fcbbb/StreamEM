#!/usr/bin/env python3
"""阶段一：把评测对话按日期流式转换并持久化为每日记忆图。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
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
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.output_dir = output_dir
        self.state_file = state_file or output_dir / "memory_state.json"
        self.progress_file = progress_file or output_dir / "progress.json"
        self.save_every = max(1, int(save_every))
        self.metadata = dict(metadata or {})
        self.progress = self._load_progress() if resume_progress else self._new_progress()

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
        self.progress["updated_at"] = utc_now()
        self.progress["state_file"] = str(self.state_file.resolve())
        self.progress["stats"] = self.pipeline.stats()
        atomic_write_json(self.progress_file, self.progress)
        atomic_write_json(
            self.output_dir / "run_manifest.json",
            {
                "schema_version": "daily_memory_build_manifest_v1",
                "updated_at": utc_now(),
                "state_file": str(self.state_file.resolve()),
                "progress_file": str(self.progress_file.resolve()),
                "stage_audit_file": str((self.output_dir / "stage_audit.jsonl").resolve()),
                "pipeline_schema": self.pipeline.schema_version,
                "config": self.pipeline.config.to_dict(),
                "stats": self.pipeline.stats(),
                **self.metadata,
            },
        )

    def run(self, files: list[Path], *, fail_fast: bool = False) -> dict[str, Any]:
        completed = {int(value) for value in self.progress.get("successful_sessions", [])}
        processed_since_save = 0
        progress_bar = ConsoleProgress(len(files), "构建记忆")
        for position, path in enumerate(files, start=1):
            session_id = session_id_from_path(path)
            if session_id in completed:
                progress_bar.update(position, f"session_{session_id:04d} 已存在")
                continue
            progress_bar.update(position - 1, f"session_{session_id:04d} 处理中")
            try:
                conversation = json.loads(path.read_text(encoding="utf-8"))
                result = self.pipeline.ingest_conversation(conversation)
                self.progress["successful_sessions"].append(session_id)
                self.progress["session_results"].append(
                    {
                        "session_id": session_id,
                        "file": str(path.resolve()),
                        "status": "success",
                        "event_date": result["event_date"],
                        "cut_segments": result["cut_segments"],
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


def main() -> None:
    args = build_parser().parse_args()
    load_env(args.env_file)
    api_key = args.api_key or os.getenv("LOCAL_OPENAI_API_KEY")
    base_url = args.base_url or os.getenv("LOCAL_OPENAI_BASE_URL")
    output_dir = args.output_dir.resolve()
    state_file = (args.state_file or output_dir / "memory_state.json").resolve()
    progress_file = (args.progress_file or output_dir / "progress.json").resolve()
    config = DailyGraphConfig(
        knn_k=args.knn_k,
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
    if args.resume:
        if not state_file.is_file():
            raise FileNotFoundError(f"--resume 指定的状态文件不存在：{state_file}")
        pipeline = DailyMemoryGraph.load(state_file, llm=llm, encoder=encoder)
    else:
        pipeline = DailyMemoryGraph(llm=llm, encoder=encoder, config=config)

    files = select_session_files(
        args.conversation_directory,
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
        metadata={
            "conversation_directory": str(args.conversation_directory.resolve()),
            "selected_session_count": len(files),
            "llm_model": None if args.no_llm else args.llm_model,
            "encoder_model": args.encoder_model,
            "use_proxy": args.use_proxy,
        },
    )
    result = runner.run(files, fail_fast=args.fail_fast)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
