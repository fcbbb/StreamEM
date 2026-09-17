from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stream_memory_graph_daily.encoder import HashEncoder
from stream_memory_graph_daily.evaluate.conversation_to_memory import (
    MemoryBuildRunner,
    recover_incremental_state,
)
from stream_memory_graph_daily.models import SegmentRecord
from stream_memory_graph_daily.pipeline import DailyMemoryGraph


class IncrementalPersistenceTests(unittest.TestCase):
    def test_delta_is_recovered_into_the_pipeline_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            state_file = output_dir / "memory_state.json"
            pipeline = DailyMemoryGraph(encoder=HashEncoder())
            runner = MemoryBuildRunner(
                pipeline,
                output_dir,
                state_file=state_file,
                snapshot_every=100,
            )

            pipeline.ingest_segment(
                SegmentRecord("s1", "first", "first anchor", "2025-06-01")
            )
            runner.persist()
            snapshot = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(set(snapshot["segments"]), {"s1"})

            pipeline.ingest_segment(
                SegmentRecord("s2", "second", "second anchor", "2025-06-01")
            )
            runner.persist()
            self.assertTrue((output_dir / "memory_state.json.delta.jsonl").is_file())
            snapshot = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(set(snapshot["segments"]), {"s1"})

            recover_incremental_state(state_file)
            restored = DailyMemoryGraph.load(state_file, encoder=HashEncoder())
            self.assertEqual(set(restored.segments), {"s1", "s2"})
            self.assertEqual(set(restored.active_graph.segment_ids()), {"s1", "s2"})

    def test_final_persist_compacts_the_journal_and_exports_all_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            pipeline = DailyMemoryGraph(encoder=HashEncoder())
            runner = MemoryBuildRunner(pipeline, output_dir, snapshot_every=100)
            for index in (1, 2):
                pipeline.ingest_segment(
                    SegmentRecord(
                        f"s{index}",
                        f"text {index}",
                        f"anchor {index}",
                        "2025-06-01",
                    )
                )
                runner.persist()

            runner.persist(force_snapshot=True)
            self.assertFalse((output_dir / "memory_state.json.delta.jsonl").exists())
            rows = (output_dir / "segments.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual({json.loads(row)["segment_id"] for row in rows}, {"s1", "s2"})


if __name__ == "__main__":
    unittest.main()
