#!/usr/bin/env python3
"""评估不经过记忆检索的两个回答基线。

实验：
1. full_context：把 158 个 session 的对话文本全部交给回答模型；
2. evidence_oracle：只把当前问题对应的全部 gold evidence 交给回答模型。

``--evidence-scope all`` 可选地把所有评测问题的 evidence 一起交给模型，
用于测量带无关 evidence 的干扰效果；它不是主实验，因为会混入其他题目的金标。
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EVALUATE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = EVALUATE_ROOT.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from stream_memory_graph_daily.llm import JsonLLM, OpenAIJsonLLM, load_env
from stream_memory_graph_daily.evaluate.memory_to_answer import (
    load_questions,
    TASK_NAMES,
)


DEFAULT_QUESTIONS = EVALUATE_ROOT / "data" / "evaluation_questions_academic_researcher.json"
DEFAULT_CONVERSATIONS = EVALUATE_ROOT / "data" / "conversations"
DEFAULT_OUTPUT_ROOT = EVALUATE_ROOT / "artifacts" / "direct_answer_baselines"


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


def token_encoder() -> Any:
    try:
        import tiktoken

        return tiktoken.encoding_for_model("gpt-5")
    except Exception:
        return None


def token_count(text: str, encoder: Any) -> int:
    if encoder is not None:
        return len(encoder.encode(text))
    # Fallback is deliberately labelled as an estimate. English-heavy data is
    # close to this ratio, while CJK text can differ substantially.
    return max(1, len(text.encode("utf-8")) // 4)


def session_id_from_path(path: Path) -> int:
    match = re.search(r"session_(\d+)", path.name)
    if not match:
        raise ValueError(f"无法从文件名提取 session_id：{path.name}")
    return int(match.group(1))


def session_transcript(session: dict[str, Any]) -> str:
    # Do not expose session type or session ID to the answer model. The direct
    # context baseline should contain only the conversation and its date.
    lines = [f"Conversation date: {session.get('date', '')}"]
    for turn in session.get("conversation", []):
        speaker = "User" if turn.get("speaker") == "user_agent" else "Assistant"
        lines.append(f"{speaker}: {turn.get('message', '')}")
    return "\n".join(lines)


def load_sessions(directory: Path) -> list[dict[str, Any]]:
    paths = sorted(directory.glob("session_*.json"), key=session_id_from_path)
    if not paths:
        raise FileNotFoundError(f"没有找到 session_*.json：{directory}")
    rows: list[dict[str, Any]] = []
    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        value["_transcript"] = session_transcript(value)
        rows.append(value)
    return rows


def flatten_questions(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return data


def raw_evidence_payload(question: dict[str, Any]) -> dict[str, Any]:
    return {
        "memory_evidence": question.get("memory_evidence", {}),
        "forgetting_evidence": question.get("forgetting_evidence", {}),
    }


def strip_session_ids(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_session_ids(child)
            for key, child in value.items()
            if key != "session_id"
        }
    if isinstance(value, list):
        return [strip_session_ids(child) for child in value]
    return value


def evidence_payload(question: dict[str, Any]) -> dict[str, Any]:
    # Deliberately exclude evaluation_questions and expected_answer to avoid
    # leaking the judge labels into the answer prompt. Source session IDs are
    # also metadata rather than answer evidence, so they are removed here.
    return strip_session_ids(raw_evidence_payload(question))


def evidence_session_ids(question: dict[str, Any]) -> list[int]:
    ids: set[int] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("session_id"), int):
                ids.add(value["session_id"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(raw_evidence_payload(question))
    return sorted(ids)


def build_token_stats(
    sessions: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    encoder: Any,
) -> dict[str, Any]:
    all_context = "\n\n".join(row["_transcript"] for row in sessions)
    per_session = [token_count(row["_transcript"], encoder) for row in sessions]
    evidence_rows = []
    for question in questions:
        payload = json.dumps(evidence_payload(question), ensure_ascii=False, indent=2)
        evidence_rows.append(
            {
                "question_id": question["question_id"],
                "evidence_session_ids": evidence_session_ids(question),
                "evidence_tokens": token_count(payload, encoder),
                "question_tokens": token_count(str(question["question"]), encoder),
            }
        )
    return {
        "tokenizer": "tiktoken encoding_for_model(gpt-5) proxy"
        if encoder is not None
        else "byte_length_div_4 estimate",
        "session_count": len(sessions),
        "question_count": len(questions),
        "all_context_tokens": token_count(all_context, encoder),
        "all_context_characters": len(all_context),
        "per_session_tokens": {
            "min": min(per_session),
            "median": sorted(per_session)[len(per_session) // 2],
            "mean": round(sum(per_session) / len(per_session), 1),
            "max": max(per_session),
        },
        "question_evidence": evidence_rows,
        "question_evidence_total_tokens": sum(
            row["evidence_tokens"] for row in evidence_rows
        ),
    }


def answer_system_prompt() -> str:
    # Keep this aligned with MemoryEvaluationRunner.generate_answer().
    return """You are a helpful AI assistant with access to a user's personal memory system.

Your task is to answer questions based ONLY on the user's stored memories and preferences.

Guidelines:
1. Use ONLY the information from the provided memories.
2. Be specific and reference actual items or preferences from memory.
3. If the question is about recommendations, suggest based on similar items in memory.
4. Be conversational and helpful.
5. Don't make up information that is not in the memories.
6. If the memories are insufficient, say so.

Return JSON only with exactly this structure: {\"answer\":\"...\"}."""


def answer_user_prompt(
    question: dict[str, Any],
    source: str,
) -> str:
    return f"""User's Question: {question['question']}

User's Relevant Memories:
{source}

Please provide a helpful answer based on these memories. Return JSON only with exactly this structure: {{\"answer\":\"...\"}}."""


def judge(answer_llm: JsonLLM, answer: str, evaluation: dict[str, Any]) -> dict[str, Any]:
    value = answer_llm.complete(
        """You are an expert evaluator assessing an AI assistant response.
Return JSON only with exactly:
{"answer":"yes" or "no", "confidence":0.0, "explanation":"brief explanation"}
Be objective and evaluate only the stated evaluation question.""",
        f"""AI RESPONSE TO EVALUATE:
{answer}

EVALUATION QUESTION:
{evaluation.get('evaluation_question', '')}

Return the JSON evaluation now.""",
    )
    if set(value) != {"answer", "confidence", "explanation"}:
        raise ValueError("judge 输出字段不符合约定")
    judged = str(value["answer"]).strip().casefold()
    if judged not in {"yes", "no"}:
        raise ValueError("judge answer 必须为 yes 或 no")
    expected = str(evaluation.get("expected_answer", "yes")).strip().casefold()
    return {
        "llm_answer": judged,
        "is_correct": judged == expected,
        "confidence": float(value["confidence"]),
        "explanation": str(value["explanation"]),
    }


def fama_score(
    memory_presence_correct: int,
    memory_presence_total: int,
    forgetting_absence_correct: int,
    forgetting_absence_total: int,
) -> float:
    if memory_presence_total == 0 and forgetting_absence_total == 0:
        return 0.0
    mpa = memory_presence_correct / memory_presence_total if memory_presence_total else 0.0
    faa = (
        forgetting_absence_correct / forgetting_absence_total
        if forgetting_absence_total
        else 1.0
    )
    weight = forgetting_absence_total / (
        memory_presence_total + forgetting_absence_total
    )
    return max(0.0, mpa - weight * (1.0 - faa))


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    evaluations = [
        evaluation
        for row in results
        for evaluation in row.get("evaluation_questions", [])
    ]
    presence = [row for row in evaluations if row.get("evaluation_type") == "memory_presence"]
    forgetting = [
        row for row in evaluations if row.get("evaluation_type") == "forgetting_absence"
    ]
    correct = lambda rows: sum(bool(row.get("evaluation_result", {}).get("is_correct")) for row in rows)
    total_correct = correct(evaluations)
    fama_values = [row["fama"] for row in results if row.get("fama") is not None]
    return {
        "total_questions": len(results),
        "failed_questions": sum("error" in row for row in results),
        "answered_questions": sum(bool(row.get("model_response")) for row in results),
        "total_evaluation_questions": len(evaluations),
        "total_correct_evaluations": total_correct,
        "overall_accuracy": total_correct / len(evaluations) if evaluations else None,
        "memory_presence_total": len(presence),
        "memory_presence_correct": correct(presence),
        "memory_presence_accuracy": correct(presence) / len(presence) if presence else None,
        "forgetting_absence_total": len(forgetting),
        "forgetting_absence_correct": correct(forgetting),
        "forgetting_absence_accuracy": correct(forgetting) / len(forgetting) if forgetting else None,
        "fama": 100.0 * sum(fama_values) / len(fama_values)
        if fama_values
        else None,
    }


def build_report(results: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in results:
        grouped[str(row.get("task_type", "Unknown"))].append(row)
    return {
        "schema_version": "direct_answer_baseline_report_v1",
        "generated_at": utc_now(),
        "metadata": metadata,
        "overall_metrics": summarize(results),
        "by_task_type": {
            task: summarize(rows) for task, rows in sorted(grouped.items())
        },
    }


class DirectAnswerRunner:
    def __init__(
        self,
        *,
        mode: str,
        questions: list[dict[str, Any]],
        sessions: list[dict[str, Any]],
        answer_llm: JsonLLM,
        judge_llm: JsonLLM | None,
        output_dir: Path,
        question_workers: int,
        skip_judge: bool,
        evidence_scope: str,
        encoder: Any,
    ) -> None:
        self.mode = mode
        self.questions = questions
        self.sessions = sessions
        self.answer_llm = answer_llm
        self.judge_llm = judge_llm
        self.output_dir = output_dir
        self.question_workers = max(1, question_workers)
        self.skip_judge = skip_judge
        self.evidence_scope = evidence_scope
        self.encoder = encoder
        self.full_context = "\n\n".join(row["_transcript"] for row in sessions)
        self.all_evidence = "\n\n".join(
            f"QUESTION {question['question_id']} EVIDENCE:\n"
            + json.dumps(evidence_payload(question), ensure_ascii=False, indent=2)
            for question in questions
        )

    def process(self, question: dict[str, Any]) -> dict[str, Any]:
        evidence_text = json.dumps(
            evidence_payload(question), ensure_ascii=False, indent=2
        )
        source = self.full_context if self.mode == "full_context" else (
            self.all_evidence if self.evidence_scope == "all" else evidence_text
        )
        user_prompt = answer_user_prompt(question, source)
        value = self.answer_llm.complete(answer_system_prompt(), user_prompt)
        if set(value) != {"answer"} or not isinstance(value["answer"], str):
            raise ValueError("回答模型必须只返回字符串字段 answer")
        answer = value["answer"].strip()
        evaluations = []
        if answer and not self.skip_judge:
            if self.judge_llm is None:
                raise RuntimeError("judge LLM 未配置")
            evaluations = [
                {**row, "evaluation_result": judge(self.judge_llm, answer, row)}
                for row in question.get("evaluation", {}).get("evaluation_questions", [])
            ]
        presence = [row for row in evaluations if row.get("evaluation_type") == "memory_presence"]
        forgetting = [row for row in evaluations if row.get("evaluation_type") == "forgetting_absence"]
        pc = sum(bool(row["evaluation_result"]["is_correct"]) for row in presence)
        fc = sum(bool(row["evaluation_result"]["is_correct"]) for row in forgetting)
        return {
            "question_id": question["question_id"],
            "task_type": question.get("task_type"),
            "question": question["question"],
            "question_date": question.get("question_date"),
            "model_response": answer,
            "answer_input_tokens": token_count(user_prompt, self.encoder),
            "evidence_session_ids": evidence_session_ids(question),
            "evaluation_questions": evaluations,
            "memory_presence_total": len(presence),
            "memory_presence_correct": pc,
            "forgetting_absence_total": len(forgetting),
            "forgetting_absence_correct": fc,
            "fama": fama_score(pc, len(presence), fc, len(forgetting))
            if evaluations
            else None,
            "memory_evidence": question.get("memory_evidence", {}),
            "forgetting_evidence": question.get("forgetting_evidence", {}),
            "processed_at": utc_now(),
        }

    def run(self, *, resume: bool) -> list[dict[str, Any]]:
        results_file = self.output_dir / "evaluation_results.json"
        report_file = self.output_dir / "evaluation_report.json"
        results: list[dict[str, Any]] = []
        if resume and results_file.is_file():
            results = list(json.loads(results_file.read_text(encoding="utf-8")).get("results", []))
        completed = {str(row.get("question_id")) for row in results}
        pending = [
            question for question in self.questions
            if str(question.get("question_id")) not in completed
        ]
        with ThreadPoolExecutor(max_workers=min(self.question_workers, max(1, len(pending)))) as executor:
            futures = {executor.submit(self.process, question): question for question in pending}
            for future in as_completed(futures):
                question = futures[future]
                try:
                    row = future.result()
                except Exception as exc:
                    row = {
                        "question_id": question.get("question_id"),
                        "task_type": question.get("task_type"),
                        "question": question.get("question"),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "processed_at": utc_now(),
                    }
                    print(f"{question.get('question_id')} 失败：{exc}", file=sys.stderr)
                results.append(row)
                payload = {
                    "schema_version": "direct_answer_baseline_results_v1",
                    "updated_at": utc_now(),
                    "results": sorted(results, key=lambda item: str(item.get("question_id"))),
                }
                atomic_write_json(results_file, payload)
                atomic_write_json(
                    report_file,
                    build_report(payload["results"], {
                        "experiment": self.mode,
                        "evidence_scope": self.evidence_scope,
                    }),
                )
        return sorted(results, key=lambda item: str(item.get("question_id")))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", choices=("full_context", "evidence_oracle"), required=True)
    parser.add_argument("--questions-file", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--conversation-directory", type=Path, default=DEFAULT_CONVERSATIONS)
    parser.add_argument(
        "--batch-root",
        type=Path,
        help="weekly 根目录；每个 persona 子目录需要 conversations/ 和 evaluation_questions_<persona>.json",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--evidence-scope", choices=("question", "all"), default="question")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--question-workers", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--skip-judge", action="store_true")
    parser.add_argument("--answer-model", default="gpt-5.6-luna")
    parser.add_argument("--judge-model", default="gpt-5.6-luna")
    parser.add_argument("--api-key")
    parser.add_argument("--base-url")
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--answer-max-tokens", type=int, default=2500)
    parser.add_argument("--judge-max-tokens", type=int, default=1000)
    parser.add_argument("--no-proxy", dest="use_proxy", action="store_false")
    parser.set_defaults(use_proxy=True)
    return parser


def experiment_directory(args: argparse.Namespace) -> str:
    if args.experiment == "evidence_oracle" and args.evidence_scope == "all":
        return "evidence_oracle_all"
    return args.experiment


def run_one_persona(
    args: argparse.Namespace,
    *,
    persona: str,
    questions_file: Path,
    conversation_directory: Path,
    output_dir: Path,
    answer_llm: JsonLLM,
    judge_llm: JsonLLM | None,
    encoder: Any,
) -> dict[str, Any]:
    sessions = load_sessions(conversation_directory)
    _, questions = load_questions(questions_file)
    if args.limit is not None:
        questions = questions[: args.limit]
    stats = build_token_stats(sessions, questions, encoder)
    atomic_write_json(output_dir / "token_stats.json", stats)
    runner = DirectAnswerRunner(
        mode=args.experiment,
        questions=questions,
        sessions=sessions,
        answer_llm=answer_llm,
        judge_llm=judge_llm,
        output_dir=output_dir,
        question_workers=args.question_workers,
        skip_judge=args.skip_judge,
        evidence_scope=args.evidence_scope,
        encoder=encoder,
    )
    results = runner.run(resume=args.resume)
    metadata = {
        "experiment": args.experiment,
        "evidence_scope": args.evidence_scope,
        "persona": persona,
        "questions_file": str(questions_file.resolve()),
        "conversation_directory": str(conversation_directory.resolve()),
        "answer_model": args.answer_model,
        "judge_model": None if args.skip_judge else args.judge_model,
        "session_count": len(sessions),
        "question_count": len(questions),
        "token_stats_file": str((output_dir / "token_stats.json").resolve()),
    }
    report = build_report(results, metadata)
    atomic_write_json(output_dir / "evaluation_report.json", report)
    return report


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def aggregate_reports(reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    overall_rows = [report["overall_metrics"] for report in reports.values()]

    def aggregate_metric(rows: list[dict[str, Any]]) -> dict[str, Any]:
        total_evaluations = sum(row["total_evaluation_questions"] for row in rows)
        total_correct = sum(row["total_correct_evaluations"] for row in rows)
        presence_total = sum(row["memory_presence_total"] for row in rows)
        presence_correct = sum(row["memory_presence_correct"] for row in rows)
        forgetting_total = sum(row["forgetting_absence_total"] for row in rows)
        forgetting_correct = sum(row["forgetting_absence_correct"] for row in rows)
        fama_values = [row["fama"] for row in rows if row.get("fama") is not None]
        return {
            "total_questions": sum(row["total_questions"] for row in rows),
            "failed_questions": sum(row["failed_questions"] for row in rows),
            "answered_questions": sum(row["answered_questions"] for row in rows),
            "total_evaluation_questions": total_evaluations,
            "total_correct_evaluations": total_correct,
            "overall_accuracy": total_correct / total_evaluations if total_evaluations else None,
            "memory_presence_total": presence_total,
            "memory_presence_correct": presence_correct,
            "memory_presence_accuracy": presence_correct / presence_total if presence_total else None,
            "forgetting_absence_total": forgetting_total,
            "forgetting_absence_correct": forgetting_correct,
            "forgetting_absence_accuracy": forgetting_correct / forgetting_total if forgetting_total else None,
            "fama": sum(fama_values) / len(fama_values) if fama_values else None,
        }

    task_names = sorted({task for report in reports.values() for task in report["by_task_type"]})
    macro_by_task: dict[str, dict[str, Any]] = {}
    micro_by_task: dict[str, dict[str, Any]] = {}
    for task in task_names:
        rows = [report["by_task_type"][task] for report in reports.values() if task in report["by_task_type"]]
        micro_by_task[task] = aggregate_metric(rows)
        macro_by_task[task] = {
            "persona_count": len(rows),
            "overall_accuracy": average([row["overall_accuracy"] for row in rows if row.get("overall_accuracy") is not None]),
            "memory_presence_accuracy": average([row["memory_presence_accuracy"] for row in rows if row.get("memory_presence_accuracy") is not None]),
            "forgetting_absence_accuracy": average([row["forgetting_absence_accuracy"] for row in rows if row.get("forgetting_absence_accuracy") is not None]),
            "fama": average([row["fama"] for row in rows if row.get("fama") is not None]),
        }

    return {
        "schema_version": "direct_answer_baseline_batch_report_v1",
        "generated_at": utc_now(),
        "persona_count": len(reports),
        "personas": {
            persona: {
                "overall_metrics": report["overall_metrics"],
                "by_task_type": report["by_task_type"],
            }
            for persona, report in sorted(reports.items())
        },
        "micro_weighted_average": aggregate_metric(overall_rows),
        "macro_persona_average": {
            "overall_accuracy": average([row["overall_accuracy"] for row in overall_rows]),
            "memory_presence_accuracy": average([row["memory_presence_accuracy"] for row in overall_rows]),
            "forgetting_absence_accuracy": average([row["forgetting_absence_accuracy"] for row in overall_rows]),
            "fama": average([row["fama"] for row in overall_rows if row.get("fama") is not None]),
        },
        "micro_weighted_by_task_type": micro_by_task,
        "macro_persona_by_task_type": macro_by_task,
    }


def main() -> None:
    args = build_parser().parse_args()
    load_env(args.env_file)
    encoder = token_encoder()

    if args.batch_root is not None:
        batch_root = args.batch_root.resolve()
        items = []
        for persona_dir in sorted(path for path in batch_root.iterdir() if path.is_dir()):
            questions_file = persona_dir / f"evaluation_questions_{persona_dir.name}.json"
            conversation_directory = persona_dir / "conversations"
            if questions_file.is_file() and conversation_directory.is_dir():
                items.append((persona_dir.name, questions_file, conversation_directory))
        if not items:
            raise FileNotFoundError(f"weekly 目录下没有找到可运行的 persona：{batch_root}")
    else:
        items = [("default", args.questions_file.resolve(), args.conversation_directory.resolve())]

    api_key = args.api_key or os.getenv("LOCAL_OPENAI_API_KEY")
    base_url = args.base_url or os.getenv("LOCAL_OPENAI_BASE_URL")
    answer_llm = OpenAIJsonLLM(
        model=args.answer_model,
        api_key=api_key,
        base_url=base_url,
        timeout=args.timeout,
        max_tokens=args.answer_max_tokens,
        use_proxy=args.use_proxy,
        env_file=args.env_file,
    )
    judge_llm = None if args.skip_judge else OpenAIJsonLLM(
        model=args.judge_model,
        api_key=api_key,
        base_url=base_url,
        timeout=args.timeout,
        max_tokens=args.judge_max_tokens,
        use_proxy=args.use_proxy,
        env_file=args.env_file,
    )

    reports: dict[str, dict[str, Any]] = {}
    experiment_name = experiment_directory(args)
    for persona, questions_file, conversation_directory in items:
        output_dir = (
            args.output_dir / persona / experiment_name
            if args.batch_root is not None
            else args.output_dir / experiment_name
        )
        print(f"\n===== {persona} =====", file=sys.stderr)
        report = run_one_persona(
            args,
            persona=persona,
            questions_file=questions_file,
            conversation_directory=conversation_directory,
            output_dir=output_dir,
            answer_llm=answer_llm,
            judge_llm=judge_llm,
            encoder=encoder,
        )
        reports[persona] = report
        print(json.dumps(report["overall_metrics"], ensure_ascii=False), file=sys.stderr)

    if args.batch_root is not None:
        aggregate = aggregate_reports(reports)
        aggregate_dir = args.output_dir / "_aggregate" / experiment_name
        atomic_write_json(aggregate_dir / "evaluation_report.json", aggregate)
        print(json.dumps(aggregate, ensure_ascii=False, indent=2))
        print(f"批量汇总报告：{aggregate_dir / 'evaluation_report.json'}")
    else:
        report = reports["default"]
        output_dir = args.output_dir / experiment_name
        print(json.dumps(report["overall_metrics"], ensure_ascii=False, indent=2))
        print(f"详细结果：{output_dir / 'evaluation_results.json'}")
        print(f"汇总报告：{output_dir / 'evaluation_report.json'}")


if __name__ == "__main__":
    main()
