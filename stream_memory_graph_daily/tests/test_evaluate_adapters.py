from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from stream_memory_graph_daily.encoder import HashEncoder
from stream_memory_graph_daily.evaluate.conversation_to_memory import (
    MemoryBuildRunner,
    build_parser as build_memory_parser,
)
from stream_memory_graph_daily.evaluate.memory_to_answer import (
    MemoryEvaluationRunner,
    build_parser as build_evaluation_parser,
    generate_report,
    load_questions,
)
from stream_memory_graph_daily.pipeline import DailyMemoryGraph


class EvaluationFakeLLM:
    def complete(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        if system_prompt.startswith("You are segmenting a conversation"):
            units = json.loads(user_prompt.split("conversation:\n\n", 1)[1])
            return {
                "segments": [{
                    "segment_id": "seg001",
                    "start_unit_id": units[0]["unit_id"],
                    "end_unit_id": units[-1]["unit_id"],
                }]
            }
        if system_prompt.startswith("You need to extract one semantic anchor for each"):
            segments = json.loads(user_prompt.split("INPUT SEGMENTS\n", 1)[1])
            return {"anchors": [
                {
                    "segment_id": segment["segment_id"],
                    "coarse_candidate": "coffee",
                    "selected_anchor": "coffee spending",
                    "fine_candidate": "coffee purchase amount",
                    "reason": "The segment records coffee spending.",
                }
                for segment in segments
            ]}
        if system_prompt.startswith("Extract one structured topic-memory"):
            return {
                "topic": "coffee spending",
                "summary": "The user recorded a coffee purchase.",
                "topic_context": [],
                "user_memories": [
                    {"type": "purchase", "content": "The user spent $3.66 on coffee."}
                ],
            }
        if system_prompt.startswith("You maintain structured memory"):
            payload = json.loads(user_prompt.split("INPUT DATA\n", 1)[1])
            existing = payload["existing_memory"]
            new_rows = payload["new_group"]["segments"]
            return {
                "topic": existing["topic"],
                "summary": "The user recorded coffee purchases on two days.",
                "operations": [
                    {
                        "operation": "add",
                        "field": "user_memories",
                        "value": {
                            "type": "activity",
                            "content": "The user bought coffee again the next day.",
                        },
                        "source_segment_ids": [new_rows[0]["segment_id"]],
                    }
                ],
            }
        if system_prompt.startswith("You answer questions"):
            return {"answer": "The user spent $3.66 on coffee."}
        if system_prompt.startswith("You are an objective evaluator"):
            return {
                "answer": "yes",
                "confidence": 1.0,
                "explanation": "The response satisfies the evaluation question.",
            }
        raise AssertionError(f"unexpected prompt: {system_prompt[:70]}")


def conversation(session_id: int, event_date: str, text: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "date": event_date,
        "conversation": [{"turn": 1, "speaker": "user", "message": text}],
    }


class EvaluationAdapterTests(unittest.TestCase):
    def test_evaluation_commands_can_disable_environment_proxies(self) -> None:
        self.assertFalse(build_memory_parser().parse_args(["--no-proxy"]).use_proxy)
        self.assertFalse(build_evaluation_parser().parse_args(["--no-proxy"]).use_proxy)
        self.assertTrue(build_memory_parser().parse_args([]).use_proxy)

    def test_two_stage_artifacts_are_persisted_and_evaluated(self) -> None:
        fake = EvaluationFakeLLM()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conversation_dir = root / "conversations"
            build_dir = root / "build"
            evaluation_dir = root / "evaluation"
            conversation_dir.mkdir()
            rows = [
                conversation(1, "2025-06-01", "I spent $3.66 on coffee."),
                conversation(2, "2025-06-02", "I bought coffee again today."),
            ]
            files = []
            for row in rows:
                path = conversation_dir / f"session_{row['session_id']:04d}.json"
                path.write_text(json.dumps(row), encoding="utf-8")
                files.append(path)

            pipeline = DailyMemoryGraph(llm=fake, encoder=HashEncoder())
            build = MemoryBuildRunner(pipeline, build_dir)
            build_result = build.run(files)

            self.assertEqual(build_result["successful"], 2)
            state_path = build_dir / "memory_state.json"
            self.assertTrue(state_path.is_file())
            self.assertTrue((build_dir / "memories.jsonl").is_file())
            self.assertTrue((build_dir / "segments.jsonl").is_file())
            self.assertTrue((build_dir / "trace.jsonl").is_file())
            self.assertTrue((build_dir / "stage_audit.jsonl").is_file())

            restored = DailyMemoryGraph.load(state_path, encoder=HashEncoder())
            self.assertGreater(len(restored.stage_audit), 0)
            evaluator = MemoryEvaluationRunner(
                restored,
                evaluation_dir,
                answer_llm=fake,
                judge_llm=fake,
                top_k=5,
                metadata={"test": True},
            )
            questions = [{
                "question_id": "q1",
                "task_type": "Remembering",
                "question": "How much did I spend on coffee?",
                "question_date": "2025-06-02",
                "evaluation": {
                    "evaluation_questions": [{
                        "evaluation_question_id": "q1-presence",
                        "evaluation_question": "Does the response mention $3.66?",
                        "expected_answer": "yes",
                        "evaluation_type": "memory_presence",
                    }]
                },
            }]
            results = evaluator.run(questions)
            report = generate_report(results, {"test": True})

            self.assertEqual(results[0]["memories_retrieved"], 1)
            self.assertEqual(results[0]["fama"], 1.0)
            self.assertEqual(report["overall_metrics"]["fama"], 100.0)
            self.assertTrue(evaluator.results_file.is_file())
            self.assertTrue(evaluator.report_file.is_file())

    def test_supplied_question_file_is_supported(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "evaluate"
            / "data"
            / "evaluation_questions_academic_researcher.json"
        )
        metadata, questions = load_questions(path)
        self.assertEqual(metadata["persona"], "academic_researcher")
        self.assertEqual(len(questions), 15)
        self.assertEqual(
            {row["task_type"] for row in questions},
            {"Remembering", "Reasoning", "Recommending"},
        )


if __name__ == "__main__":
    unittest.main()
