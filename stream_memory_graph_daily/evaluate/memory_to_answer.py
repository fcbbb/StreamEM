#!/usr/bin/env python3
"""阶段二：加载持久化记忆图，检索记忆、回答问题并生成评测报告。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
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
from stream_memory_graph_daily.llm import JsonLLM, OpenAIJsonLLM, load_env
from stream_memory_graph_daily.pipeline import DailyMemoryGraph


DEFAULT_QUESTIONS = EVALUATE_ROOT / "data" / "evaluation_questions_academic_researcher.json"
DEFAULT_STATE = EVALUATE_ROOT / "artifacts" / "memory_build" / "memory_state.json"
DEFAULT_OUTPUT_DIR = EVALUATE_ROOT / "artifacts" / "evaluation"
TASK_NAMES = {
    "remembering": "Remembering",
    "reasoning": "Reasoning",
    "recommending": "Recommending",
}


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


def fama_score(
    memory_presence_correct: int,
    memory_presence_total: int,
    forgetting_absence_correct: int,
    forgetting_absence_total: int,
) -> float:
    """FAMA = max(0, MPA - lambda * (1 - FAA))。"""

    if memory_presence_total == 0 and forgetting_absence_total == 0:
        return 0.0
    mpa = (
        memory_presence_correct / memory_presence_total
        if memory_presence_total
        else 0.0
    )
    faa = (
        forgetting_absence_correct / forgetting_absence_total
        if forgetting_absence_total
        else 1.0
    )
    weight = forgetting_absence_total / (
        memory_presence_total + forgetting_absence_total
    )
    return max(0.0, mpa - weight * (1.0 - faa))


def load_questions(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.is_file():
        raise FileNotFoundError(f"评测问题文件不存在：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    groups = data.get("questions")
    if not isinstance(groups, dict):
        raise ValueError("评测文件必须包含 questions 对象")
    questions: list[dict[str, Any]] = []
    for group_name, rows in groups.items():
        if not isinstance(rows, list):
            continue
        task_type = TASK_NAMES.get(str(group_name).casefold(), str(group_name))
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            item["task_type"] = task_type
            questions.append(item)
    metadata = {key: value for key, value in data.items() if key != "questions"}
    metadata["total_questions"] = len(questions)
    return metadata, questions


def filter_questions(
    questions: list[dict[str, Any]],
    *,
    task_types: set[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    selected = questions
    if task_types:
        normalized = {value.casefold() for value in task_types}
        selected = [
            question
            for question in selected
            if str(question.get("task_type", "")).casefold() in normalized
        ]
    if limit is not None:
        if limit < 1:
            raise ValueError("--limit 必须为正整数")
        selected = selected[:limit]
    return selected


def compact_memory(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("kind") == "segment":
        segment = row["segment"]
        return {
            "anchor": segment["anchor"],
            "text": segment["text"],
        }
    memory = row["memory"]
    return {
        "topic": memory["topic"],
        "summary": memory["summary"],
        "topic_context": memory["topic_context"],
        "user_memories": memory["user_memories"],
    }


def _ratio(correct: int, total: int) -> float | None:
    return correct / total if total else None


def generate_report(
    results: list[dict[str, Any]], metadata: dict[str, Any]
) -> dict[str, Any]:
    def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
        evaluations = [
            evaluation
            for row in rows
            for evaluation in row.get("evaluation_questions", [])
        ]
        memory_rows = [
            evaluation
            for evaluation in evaluations
            if evaluation.get("evaluation_type") == "memory_presence"
        ]
        forgetting_rows = [
            evaluation
            for evaluation in evaluations
            if evaluation.get("evaluation_type") == "forgetting_absence"
        ]
        memory_correct = sum(
            bool(row.get("evaluation_result", {}).get("is_correct"))
            for row in memory_rows
        )
        forgetting_correct = sum(
            bool(row.get("evaluation_result", {}).get("is_correct"))
            for row in forgetting_rows
        )
        total_correct = sum(
            bool(row.get("evaluation_result", {}).get("is_correct"))
            for row in evaluations
        )
        fama_values = [row["fama"] for row in rows if row.get("fama") is not None]
        return {
            "total_questions": len(rows),
            "answered_questions": sum(bool(row.get("model_response")) for row in rows),
            "failed_questions": sum(bool(row.get("error")) for row in rows),
            "retrieval_hit_questions": sum(row.get("memories_retrieved", 0) > 0 for row in rows),
            "total_evaluation_questions": len(evaluations),
            "total_correct_evaluations": total_correct,
            "overall_accuracy": _ratio(total_correct, len(evaluations)),
            "memory_presence_total": len(memory_rows),
            "memory_presence_correct": memory_correct,
            "memory_presence_accuracy": _ratio(memory_correct, len(memory_rows)),
            "forgetting_absence_total": len(forgetting_rows),
            "forgetting_absence_correct": forgetting_correct,
            "forgetting_absence_accuracy": _ratio(forgetting_correct, len(forgetting_rows)),
            "fama": (
                100.0 * sum(fama_values) / len(fama_values) if fama_values else None
            ),
        }

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[str(result.get("task_type", "Unknown"))].append(result)
    return {
        "schema_version": "daily_memory_evaluation_report_v1",
        "generated_at": utc_now(),
        "metadata": metadata,
        "overall_metrics": summarize(results),
        "by_task_type": {
            task_type: summarize(rows) for task_type, rows in sorted(grouped.items())
        },
    }


class MemoryEvaluationRunner:
    def __init__(
        self,
        pipeline: DailyMemoryGraph,
        output_dir: Path,
        *,
        answer_llm: JsonLLM | None,
        judge_llm: JsonLLM | None,
        top_k: int = 10,
        retrieval_only: bool = False,
        skip_judge: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.output_dir = output_dir
        self.answer_llm = answer_llm
        self.judge_llm = judge_llm
        self.top_k = max(1, int(top_k))
        self.retrieval_only = retrieval_only
        self.skip_judge = skip_judge
        self.metadata = dict(metadata or {})
        self.results_file = output_dir / "evaluation_results.json"
        self.report_file = output_dir / "evaluation_report.json"

    def retrieve(self, question: str) -> list[dict[str, Any]]:
        return [compact_memory(row) for row in self.pipeline.retrieve(question, self.top_k)]

    def retrieve_diagnostics(self, question: str) -> list[dict[str, Any]]:
        return [
            {
                "memory_id": row.get("memory_id", row.get("segment_id")),
                "score": round(float(row["score"]), 6),
                "similarity": round(float(row.get("similarity", 0.0)), 6),
                "lexical_score": round(float(row.get("lexical_score", 0.0)), 6),
                "entity_score": round(float(row.get("entity_score", 0.0)), 6),
                "topic": (
                    row["memory"]["topic"]
                    if row.get("kind") != "segment"
                    else row["segment"]["anchor"]
                ),
                "kind": row.get("kind", "memory"),
            }
            for row in self.pipeline.retrieve(question, self.top_k)
        ]

    def generate_answer(
        self,
        question: str,
        question_date: str | None,
        memories: list[dict[str, Any]],
    ) -> str | None:
        if self.retrieval_only:
            return None
        if not memories:
            return "I don't have enough information in my memory to answer this question accurately."
        if self.answer_llm is None:
            raise RuntimeError("回答生成需要 LLM；或者使用 --retrieval-only")
        value = self.answer_llm.complete(
            """You answer questions using only the supplied structured user memories.
Do not use outside knowledge or invent facts. Preserve concrete dates, amounts,
counts, states, and distinctions between current and historical information.
For recommendations, explain them only from the supplied preferences or history.
If the memories are insufficient, say so. Return JSON only: {\"answer\":\"...\"}.""",
            json.dumps(
                {
                    "question": question,
                    "question_date": question_date,
                    "retrieved_memories": memories,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        if set(value) != {"answer"} or not isinstance(value["answer"], str):
            raise ValueError("回答模型必须只返回字符串字段 answer")
        return value["answer"].strip()

    def judge(
        self, answer: str, evaluation: dict[str, Any]
    ) -> dict[str, Any]:
        if self.judge_llm is None:
            raise RuntimeError("执行评测需要 judge LLM；或者使用 --skip-judge")
        value = self.judge_llm.complete(
            """You are an objective evaluator. Answer the supplied yes/no evaluation
question about the candidate response. Return JSON only with exactly:
{\"answer\":\"yes or no\",\"confidence\":0.0,\"explanation\":\"...\"}.""",
            json.dumps(
                {
                    "candidate_response": answer,
                    "evaluation_question": evaluation.get("evaluation_question", ""),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        if set(value) != {"answer", "confidence", "explanation"}:
            raise ValueError("judge 输出字段不符合约定")
        judged_answer = str(value["answer"]).strip().casefold()
        if judged_answer not in {"yes", "no"}:
            raise ValueError("judge answer 必须为 yes 或 no")
        expected = str(evaluation.get("expected_answer", "yes")).strip().casefold()
        return {
            "llm_answer": judged_answer,
            "is_correct": judged_answer == expected,
            "confidence": float(value["confidence"]),
            "explanation": str(value["explanation"]),
        }

    def process_question(self, question: dict[str, Any]) -> dict[str, Any]:
        question_id = str(question.get("question_id", ""))
        question_text = str(question.get("question", ""))
        if not question_id or not question_text:
            raise ValueError("评测问题缺少 question_id 或 question")
        retrieval_rows = self.pipeline.retrieve(question_text, self.top_k)
        memories = [compact_memory(row) for row in retrieval_rows]
        retrieval_diagnostics = [
            {
                "memory_id": row.get("memory_id", row.get("segment_id")),
                "score": round(float(row["score"]), 6),
                "similarity": round(float(row.get("similarity", 0.0)), 6),
                "lexical_score": round(float(row.get("lexical_score", 0.0)), 6),
                "entity_score": round(float(row.get("entity_score", 0.0)), 6),
                "topic": (
                    row["memory"]["topic"]
                    if row.get("kind") != "segment"
                    else row["segment"]["anchor"]
                ),
                "kind": row.get("kind", "memory"),
            }
            for row in retrieval_rows
        ]
        answer = self.generate_answer(
            question_text, question.get("question_date"), memories
        )
        evaluations: list[dict[str, Any]] = []
        if answer and not self.skip_judge:
            rows = question.get("evaluation", {}).get("evaluation_questions", [])
            for row in rows:
                result = self.judge(answer, row)
                evaluations.append({**row, "evaluation_result": result})

        memory_rows = [
            row for row in evaluations if row.get("evaluation_type") == "memory_presence"
        ]
        forgetting_rows = [
            row for row in evaluations if row.get("evaluation_type") == "forgetting_absence"
        ]
        memory_correct = sum(
            bool(row["evaluation_result"]["is_correct"]) for row in memory_rows
        )
        forgetting_correct = sum(
            bool(row["evaluation_result"]["is_correct"]) for row in forgetting_rows
        )
        has_evaluation = bool(evaluations)
        return {
            "question_id": question_id,
            "task_type": question.get("task_type"),
            "question": question_text,
            "question_date": question.get("question_date"),
            "model_response": answer,
            "memories_retrieved": len(memories),
            "memories": memories,
            "retrieval_diagnostics": retrieval_diagnostics,
            "evaluation_questions": evaluations,
            "memory_presence_total": len(memory_rows),
            "memory_presence_correct": memory_correct,
            "memory_presence_accuracy": _ratio(memory_correct, len(memory_rows)),
            "forgetting_absence_total": len(forgetting_rows),
            "forgetting_absence_correct": forgetting_correct,
            "forgetting_absence_accuracy": _ratio(
                forgetting_correct, len(forgetting_rows)
            ),
            "fama": (
                fama_score(
                    memory_correct,
                    len(memory_rows),
                    forgetting_correct,
                    len(forgetting_rows),
                )
                if has_evaluation
                else None
            ),
            "memory_evidence": question.get("memory_evidence", {}),
            "forgetting_evidence": question.get("forgetting_evidence", {}),
            "processed_at": utc_now(),
        }

    def persist(self, results: list[dict[str, Any]]) -> None:
        payload = {
            "schema_version": "daily_memory_evaluation_results_v1",
            "updated_at": utc_now(),
            "metadata": self.metadata,
            "results": results,
        }
        atomic_write_json(self.results_file, payload)
        atomic_write_json(self.report_file, generate_report(results, self.metadata))

    def run(
        self,
        questions: list[dict[str, Any]],
        *,
        resume: bool = False,
        fail_fast: bool = False,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if resume and self.results_file.is_file():
            saved = json.loads(self.results_file.read_text(encoding="utf-8"))
            results = list(saved.get("results", []))
        completed = {str(row.get("question_id")) for row in results}
        progress_bar = ConsoleProgress(len(questions), "回答评测")
        for position, question in enumerate(questions, start=1):
            question_id = str(question.get("question_id", ""))
            if question_id in completed:
                progress_bar.update(position, f"{question_id} 已存在")
                continue
            progress_bar.update(position - 1, f"{question_id} 处理中")
            try:
                result = self.process_question(question)
                progress_bar.update(
                    position,
                    f"{question_id} 完成，"
                    f"检索记忆={result['memories_retrieved']}，"
                    f"评测项={len(result['evaluation_questions'])}"
                )
            except Exception as exc:
                result = {
                    "question_id": question_id,
                    "task_type": question.get("task_type"),
                    "question": question.get("question"),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "processed_at": utc_now(),
                }
                progress_bar.write(f"{question_id} 失败：{exc}")
                progress_bar.update(position, f"{question_id} 失败")
                results.append(result)
                self.persist(results)
                if fail_fast:
                    raise
                continue
            results.append(result)
            self.persist(results)
        failed = sum("error" in row for row in results)
        progress_bar.finish(f"完成：结果 {len(results)}，失败 {failed}")
        return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("questions_file", type=Path, nargs="?", default=DEFAULT_QUESTIONS)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--task-type",
        action="append",
        choices=("Remembering", "Reasoning", "Recommending"),
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--retrieval-only", action="store_true")
    parser.add_argument("--skip-judge", action="store_true")
    parser.add_argument("--answer-model", default="gpt-5.6-luna")
    parser.add_argument("--judge-model", default="gpt-5.6-luna")
    parser.add_argument("--api-key")
    parser.add_argument("--base-url")
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    proxy_group = parser.add_mutually_exclusive_group()
    proxy_group.add_argument("--use-proxy", dest="use_proxy", action="store_true")
    proxy_group.add_argument("--no-proxy", dest="use_proxy", action="store_false")
    parser.set_defaults(use_proxy=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--answer-max-tokens", type=int, default=3000)
    parser.add_argument("--judge-max-tokens", type=int, default=1000)
    parser.add_argument("--encoder-model", default="all-MiniLM-L6-v2")
    parser.add_argument("--device", default="cpu")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    load_env(args.env_file)
    api_key = args.api_key or os.getenv("LOCAL_OPENAI_API_KEY")
    base_url = args.base_url or os.getenv("LOCAL_OPENAI_BASE_URL")
    if not args.state_file.is_file():
        raise FileNotFoundError(
            f"记忆状态不存在：{args.state_file}\n"
            "请先运行 evaluate/conversation_to_memory.py。"
        )
    encoder = load_encoder(args.encoder_model, args.device)
    pipeline = DailyMemoryGraph.load(args.state_file, encoder=encoder)

    need_answer_llm = not args.retrieval_only
    need_judge_llm = need_answer_llm and not args.skip_judge
    answer_llm = OpenAIJsonLLM(
        model=args.answer_model,
        api_key=api_key,
        base_url=base_url,
        timeout=args.timeout,
        max_tokens=args.answer_max_tokens,
        use_proxy=args.use_proxy,
        env_file=args.env_file,
    ) if need_answer_llm else None
    judge_llm = OpenAIJsonLLM(
        model=args.judge_model,
        api_key=api_key,
        base_url=base_url,
        timeout=args.timeout,
        max_tokens=args.judge_max_tokens,
        use_proxy=args.use_proxy,
        env_file=args.env_file,
    ) if need_judge_llm else None

    question_metadata, questions = load_questions(args.questions_file)
    questions = filter_questions(
        questions,
        task_types=set(args.task_type or []),
        limit=args.limit,
    )
    if not questions:
        raise ValueError("筛选后没有评测问题")
    metadata = {
        **question_metadata,
        "memory_system": "stream_memory_graph_daily",
        "memory_state_file": str(args.state_file.resolve()),
        "questions_file": str(args.questions_file.resolve()),
        "answer_model": None if args.retrieval_only else args.answer_model,
        "judge_model": None if args.skip_judge or args.retrieval_only else args.judge_model,
        "encoder_model": args.encoder_model,
        "top_k": args.top_k,
        "retrieval_only": args.retrieval_only,
        "skip_judge": args.skip_judge,
        "use_proxy": args.use_proxy,
        "selected_questions": len(questions),
    }
    runner = MemoryEvaluationRunner(
        pipeline,
        args.output_dir,
        answer_llm=answer_llm,
        judge_llm=judge_llm,
        top_k=args.top_k,
        retrieval_only=args.retrieval_only,
        skip_judge=args.skip_judge,
        metadata=metadata,
    )
    results = runner.run(questions, resume=args.resume, fail_fast=args.fail_fast)
    report = generate_report(results, metadata)
    print(json.dumps(report["overall_metrics"], ensure_ascii=False, indent=2))
    print(f"详细结果：{runner.results_file}")
    print(f"汇总报告：{runner.report_file}")


if __name__ == "__main__":
    main()
