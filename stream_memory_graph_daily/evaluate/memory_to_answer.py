#!/usr/bin/env python3
"""阶段二：加载持久化记忆图，检索记忆、回答问题并生成评测报告。"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def discover_question_files(path: Path) -> list[tuple[str, Path]]:
    """Discover one question file or all persona question files under a root."""
    path = path.resolve()
    if path.is_file():
        return [(path.stem.removeprefix("evaluation_questions_"), path)]
    if not path.is_dir():
        raise FileNotFoundError(f"评测问题路径不存在：{path}")

    files = list(path.glob("evaluation_questions_*.json"))
    files.extend(
        question_file
        for child in sorted(path.iterdir(), key=lambda item: item.name)
        if child.is_dir()
        for question_file in child.glob("evaluation_questions_*.json")
    )
    files = sorted(set(files), key=lambda item: item.as_posix())
    if not files:
        raise FileNotFoundError(f"目录下没有找到 evaluation_questions_*.json：{path}")
    return [
        (question_file.stem.removeprefix("evaluation_questions_"), question_file)
        for question_file in files
    ]


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
        judge_workers: int = 1,
        question_workers: int = 4,
        retrieval_only: bool = False,
        skip_judge: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.output_dir = output_dir
        self.answer_llm = answer_llm
        self.judge_llm = judge_llm
        self.top_k = max(1, int(top_k))
        self.judge_workers = max(1, int(judge_workers))
        self.question_workers = max(1, int(question_workers))
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
            """You are a helpful AI assistant with access to a user's personal memory system.

Your task is to answer questions based ONLY on the user's stored memories and preferences.

Guidelines:
1. Use ONLY the information from the provided memories.
2. Be specific and reference actual items or preferences from memory.
3. If the question is about recommendations, suggest based on similar items in memory.
4. Be conversational and helpful.
5. Don't make up information that is not in the memories.
6. If the memories are insufficient, say so.

Return JSON only with exactly this structure: {\"answer\":\"...\"}.""",
            f"""User's Question: {question}

User's Relevant Memories:
{json.dumps(memories, ensure_ascii=False, indent=2)}

Please provide a helpful answer based on these memories. Return JSON only with exactly this structure: {{\"answer\":\"...\"}}.""",
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
            """You are an expert evaluator assessing AI assistant responses. Your task is to answer a YES/NO evaluation question about a given response.

You must provide your answer in the following JSON format:
{
    \"answer\": \"yes\" or \"no\",
    \"confidence\": 0.0 to 1.0,
    \"explanation\": \"Brief explanation of your reasoning\"
}

Be objective and thorough in your evaluation.""",
            f"""Please evaluate the following AI response against the evaluation question.

AI RESPONSE TO EVALUATE:
{answer}

EVALUATION QUESTION:
{evaluation.get("evaluation_question", "")}

Provide your evaluation in JSON format with answer (yes/no), confidence (0.0-1.0), and explanation.""",
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
            if self.judge_workers == 1 or len(rows) <= 1:
                judged_rows = [self.judge(answer, row) for row in rows]
            else:
                # Judge calls are independent network requests. Submit them in
                # parallel, while collecting futures in input order so the
                # result schema remains deterministic.
                worker_count = min(self.judge_workers, len(rows))
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures = [executor.submit(self.judge, answer, row) for row in rows]
                    judged_rows = [future.result() for future in futures]
            evaluations = [
                {**row, "evaluation_result": result}
                for row, result in zip(rows, judged_rows)
            ]

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
        pending = [
            (position, question, str(question.get("question_id", "")))
            for position, question in enumerate(questions, start=1)
            if str(question.get("question_id", "")) not in completed
        ]
        completed_count = len(questions) - len(pending)
        if completed_count:
            progress_bar.update(completed_count, f"已跳过 {completed_count} 个已完成问题")
        if not pending:
            progress_bar.finish(f"完成：结果 {len(results)}，失败 {sum('error' in row for row in results)}")
            return results

        worker_count = min(self.question_workers, len(pending))
        progress_bar.update(
            completed_count,
            f"已提交 {len(pending)} 个问题，题目并发数={worker_count}",
        )
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(self.process_question, question): (position, question, question_id)
                for position, question, question_id in pending
            }
            for future in as_completed(futures):
                _, question, question_id = futures[future]
                try:
                    result = future.result()
                    status = (
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
                    status = f"{question_id} 失败"
                    if fail_fast:
                        for other in futures:
                            other.cancel()
                        raise
                results.append(result)
                completed_count += 1
                progress_bar.update(completed_count, status)
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
    parser.add_argument(
        "--judge-workers",
        type=int,
        default=1,
        help="并行执行每道题的 Judge 请求数（默认：1；题目并发时建议保持为 1）",
    )
    parser.add_argument(
        "--question-workers",
        type=int,
        default=4,
        help="并行处理的问题数（默认：4；设为 1 可关闭并行）",
    )
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


def _run_question_evaluation(
    args: argparse.Namespace,
    *,
    persona: str,
    questions_file: Path,
    state_file: Path,
    output_dir: Path,
    encoder: Any,
    answer_llm: Any,
    judge_llm: Any,
) -> dict[str, Any]:
    if not state_file.is_file():
        raise FileNotFoundError(
            f"记忆状态不存在：{state_file}\n"
            "请先运行 evaluate/conversation_to_memory.py。"
        )
    pipeline = DailyMemoryGraph.load(state_file, encoder=encoder)
    question_metadata, questions = load_questions(questions_file)
    questions = filter_questions(
        questions,
        task_types=set(args.task_type or []),
        limit=args.limit,
    )
    if not questions:
        raise ValueError("筛选后没有评测问题")
    metadata = {
        **question_metadata,
        "persona": persona,
        "memory_system": "stream_memory_graph_daily",
        "memory_state_file": str(state_file.resolve()),
        "questions_file": str(questions_file.resolve()),
        "answer_model": None if args.retrieval_only else args.answer_model,
        "judge_model": None if args.skip_judge or args.retrieval_only else args.judge_model,
        "encoder_model": args.encoder_model,
        "top_k": args.top_k,
        "judge_workers": args.judge_workers,
        "question_workers": args.question_workers,
        "retrieval_only": args.retrieval_only,
        "skip_judge": args.skip_judge,
        "use_proxy": args.use_proxy,
        "selected_questions": len(questions),
    }
    runner = MemoryEvaluationRunner(
        pipeline,
        output_dir,
        answer_llm=answer_llm,
        judge_llm=judge_llm,
        top_k=args.top_k,
        judge_workers=args.judge_workers,
        question_workers=args.question_workers,
        retrieval_only=args.retrieval_only,
        skip_judge=args.skip_judge,
        metadata=metadata,
    )
    results = runner.run(questions, resume=args.resume, fail_fast=args.fail_fast)
    report = generate_report(results, metadata)
    print(json.dumps(report["overall_metrics"], ensure_ascii=False, indent=2))
    print(f"详细结果：{runner.results_file}")
    print(f"汇总报告：{runner.report_file}")
    return report


def main() -> None:
    args = build_parser().parse_args()
    if args.judge_workers < 1 or args.question_workers < 1:
        raise ValueError("--judge-workers 和 --question-workers 必须为正整数")
    load_env(args.env_file)
    api_key = args.api_key or os.getenv("LOCAL_OPENAI_API_KEY")
    base_url = args.base_url or os.getenv("LOCAL_OPENAI_BASE_URL")
    question_files = discover_question_files(args.questions_file)
    batch_mode = len(question_files) > 1
    output_root = args.output_dir.resolve()

    if batch_mode:
        state_arg = args.state_file.resolve()
        state_root = state_arg.parent if state_arg.name == "memory_state.json" else state_arg
    else:
        state_root = args.state_file.resolve()

    encoder = load_encoder(args.encoder_model, args.device)
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

    results: dict[str, Any] = {}
    for persona, questions_file in question_files:
        state_file = (
            state_root / persona / "memory_state.json"
            if batch_mode
            else state_root
        )
        output_dir = output_root / persona if batch_mode else output_root
        print(f"\n===== 评测：{persona} =====")
        try:
            results[persona] = _run_question_evaluation(
                args,
                persona=persona,
                questions_file=questions_file,
                state_file=state_file,
                output_dir=output_dir,
                encoder=encoder,
                answer_llm=answer_llm,
                judge_llm=judge_llm,
            )
        except Exception as exc:
            results[persona] = {"error": str(exc)}
            print(f"{persona} 失败：{exc}")
            if args.fail_fast:
                raise

    if batch_mode:
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
