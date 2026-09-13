from __future__ import annotations

import unittest

from stream_memory_graph_daily.prompts import (
    ANCHOR_PROMPT,
    COMMUNITY_PURIFICATION_PROMPT,
    COMMUNITY_TOPIC_PARTITION_PROMPT,
    CUTTING_PROMPT,
    MEMORY_EXTRACTION_PROMPT,
    MEMORY_FUSION_PROMPT,
    MEMORY_FUSION_FROM_L1_PROMPT,
    MEMORY_LEVEL_POLICY_PROMPT,
    SHARED_SEMANTICS,
    TOPIC_OWNER_ROUTING_PROMPT,
    render_memory_level_policy,
)


class PromptCompositionTests(unittest.TestCase):
    def test_shared_semantics_are_injected_once_into_every_stage(self) -> None:
        prompts = [
            CUTTING_PROMPT,
            ANCHOR_PROMPT,
            COMMUNITY_PURIFICATION_PROMPT,
            COMMUNITY_TOPIC_PARTITION_PROMPT,
            MEMORY_EXTRACTION_PROMPT,
            MEMORY_FUSION_PROMPT,
            MEMORY_FUSION_FROM_L1_PROMPT,
            TOPIC_OWNER_ROUTING_PROMPT,
        ]
        for prompt in prompts:
            self.assertNotIn("{{SHARED_SEMANTICS}}", prompt)
            self.assertEqual(prompt.count(SHARED_SEMANTICS), 1)

    def test_level_policy_is_a_reusable_prompt_without_unresolved_markers(self) -> None:
        rendered = render_memory_level_policy(2)

        self.assertIn("Target level: 2", rendered)
        self.assertIn("stable_topic_memory", rendered)
        self.assertIn("durable explicit user information", rendered)
        self.assertNotIn("{{", rendered)
        self.assertNotIn("}}", rendered)
        self.assertNotIn(SHARED_SEMANTICS, MEMORY_LEVEL_POLICY_PROMPT)

    def test_stage_identifiers_and_output_contracts_remain_available(self) -> None:
        self.assertTrue(CUTTING_PROMPT.startswith("You are segmenting a conversation"))
        self.assertIn('"segments"', CUTTING_PROMPT)
        self.assertTrue(
            ANCHOR_PROMPT.startswith(
                "You need to extract one semantic anchor for each"
            )
        )
        self.assertIn('"anchors"', ANCHOR_PROMPT)
        self.assertTrue(
            COMMUNITY_PURIFICATION_PROMPT.startswith(
                "Conservatively partition one graph community"
            )
        )
        self.assertIn('"groups"', COMMUNITY_PURIFICATION_PROMPT)
        self.assertTrue(
            COMMUNITY_TOPIC_PARTITION_PROMPT.startswith(
                "Conservatively partition one graph community"
            )
        )
        self.assertIn("same concrete subject or object", COMMUNITY_TOPIC_PARTITION_PROMPT)
        self.assertIn('"groups"', COMMUNITY_TOPIC_PARTITION_PROMPT)
        self.assertIn("extract one structured topic-memory", MEMORY_EXTRACTION_PROMPT)
        self.assertIn('"user_memories"', MEMORY_EXTRACTION_PROMPT)
        self.assertTrue(MEMORY_FUSION_PROMPT.startswith("You maintain structured memory"))
        self.assertIn('"operations"', MEMORY_FUSION_PROMPT)
        self.assertIn("provisional_l1", MEMORY_FUSION_FROM_L1_PROMPT)
        self.assertIn('"owner_memory_id"', TOPIC_OWNER_ROUTING_PROMPT)

    def test_memory_prompts_keep_one_time_facts_and_guard_learning_inference(self) -> None:
        for prompt in (MEMORY_EXTRACTION_PROMPT, MEMORY_FUSION_PROMPT):
            self.assertIn("one-time", prompt)
            self.assertIn("learning state", prompt)
            self.assertIn("question followed by an answer alone", prompt)


if __name__ == "__main__":
    unittest.main()
