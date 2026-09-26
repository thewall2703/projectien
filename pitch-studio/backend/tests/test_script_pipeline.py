from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.generation_cache import SCRIPT_PIPELINE_VERSION, compute_cache_key
from backend.pipeline.audience_sim import (
    listener_notes,
    listener_passed,
    listener_score,
    normalize_listener_result,
)
from backend.pipeline.flow_check import (
    flow_notes,
    flow_passed,
    flow_score,
    normalize_flow_result,
)
from backend.pipeline.prompts import script_messages
from backend.pipeline.script_flow import ScriptTopic
from backend.pipeline.script_plan import normalize_script_plan
from backend.pipeline.brand_deck import BrandSlide
from backend.pipeline.runner import generate_script_phase


class ScriptPlanNormaliseTests(unittest.TestCase):
    def test_keeps_topic_order_fills_missing_and_rescales_words(self):
        topics = [
            ScriptTopic(topic_id=10, title="A", pages=[1]),
            ScriptTopic(topic_id=20, title="B", pages=[2]),
            ScriptTopic(topic_id=30, title="C", pages=[3]),
        ]
        raw = {
            "throughline": "Build real",
            "listener_start": "skeptical",
            "listener_end": "ready",
            "arc": "setup → proof → ask",
            "ask": "Come Saturday",
            "beats": [
                {
                    "topic_id": 30,
                    "role": "BODY",
                    "point": "close point",
                    "proof": "p",
                    "proof_source": "LOCKED",
                    "story_device": "example",
                    "bridge_in": "so",
                    "approx_words": 10,
                },
                {
                    "topic_id": 10,
                    "point": "open point",
                    "proof": "q",
                    "proof_source": "FOUNDER",
                    "story_device": "question",
                    "bridge_in": "ignore",
                    "approx_words": 90,
                },
                {"topic_id": 999, "point": "unknown"},
            ],
        }
        plan = normalize_script_plan(raw, topic_flow=topics, word_budget=120)
        self.assertIsNotNone(plan)
        assert plan is not None
        ids = [beat["topic_id"] for beat in plan["beats"]]
        self.assertEqual(ids, [10, 20, 30])
        self.assertEqual(plan["beats"][0]["role"], "OPENING")
        self.assertEqual(plan["beats"][0]["bridge_in"], "")
        self.assertEqual(plan["beats"][1]["role"], "BODY")
        self.assertEqual(plan["beats"][1]["point"], "")  # placeholder
        self.assertEqual(plan["beats"][2]["role"], "CLOSE")
        self.assertEqual(sum(beat["approx_words"] for beat in plan["beats"]), 120)
        self.assertTrue(all(beat["approx_words"] >= 12 for beat in plan["beats"]))


class FlowCheckUnitTests(unittest.TestCase):
    def test_normalise_passed_score_notes(self):
        raw = {
            "joins": [
                {
                    "from_topic": 1,
                    "to_topic": 2,
                    "score": 2,
                    "quote": "end … start",
                    "issue": "cold",
                    "suggested_bridge": "Which is why…",
                }
            ],
            "sections": [{"topic_id": 2, "story_shape": 2, "quote": "", "issue": "list"}],
            "arc": {"score": 4, "issue": ""},
            "naturalness": {"score": 4, "issue": ""},
            "repetition": ["same line"],
            "grammar": [{"topic_id": 2, "quote": "We is", "fix": "We are"}],
        }
        result = normalize_flow_result(raw)
        self.assertFalse(flow_passed(result))
        self.assertGreater(flow_score(result), 0.0)
        notes = flow_notes(result, limit=8)
        self.assertTrue(any("Transition from section 1 to 2" in note for note in notes))
        self.assertTrue(any("list of claims" in note for note in notes))
        self.assertTrue(any("grammar" in note for note in notes))

    def test_repetition_is_one_note_that_survives_the_cap(self):
        result = normalize_flow_result(
            {
                "grammar": [{"topic_id": n, "quote": f"We is {n}", "fix": "We are"} for n in range(12)],
                "repetition": ["Faculty mix in 7 and 8", "152 immersions in 7 and 18"],
            }
        )
        notes = flow_notes(result, limit=8)
        self.assertTrue(notes[0].startswith("Repetition"))
        self.assertIn("Faculty mix in 7 and 8", notes[0])
        self.assertIn("152 immersions in 7 and 18", notes[0])
        self.assertEqual(sum(note.startswith("Repetition") for note in notes), 1)

    def test_written_english_fails_flow_and_reaches_the_writer(self):
        result = normalize_flow_result(
            {
                "joins": [{"from_topic": 1, "to_topic": 2, "score": 4}],
                "arc": {"score": 4},
                "naturalness": {"score": 4},
                "spoken": {"score": 2, "issue": "essay read aloud"},
                "written_lines": [
                    {"topic_id": 5, "quote": "External judgment matters: 6 startups pitched.", "spoken_fix": "Six of our startups pitched on Shark Tank."}
                ],
                "cold_starts": [
                    {
                        "topic_id": 5,
                        "quote": "A. B. C.",
                        "chained": "A. And B. That's C.",
                    }
                ],
                "signposting": ["the next question is what learning produces"],
                "hedging": ["A pitch isn't a successful company"],
                "name_drops": ["Rhea and Ayush building Lexi's"],
                "grammar": [{"topic_id": n, "quote": f"We is {n}", "fix": "We are"} for n in range(12)],
            }
        )
        self.assertFalse(flow_passed(result))
        notes = flow_notes(result, limit=10)
        joined = "\n".join(notes)
        for expected in ("written English", "Announced transitions", "Reflexive caveats", "Names with no story", "say it like", "cold sentence starts"):
            self.assertIn(expected, joined)

    def test_flow_passed_thresholds(self):
        good = normalize_flow_result(
            {
                "joins": [{"from_topic": 1, "to_topic": 2, "score": 4}],
                "sections": [{"topic_id": 1, "story_shape": 4}],
                "arc": {"score": 4},
                "naturalness": {"score": 4},
                "spoken": {"score": 4},
                "repetition": [],
                "grammar": [],
            }
        )
        self.assertTrue(flow_passed(good))
        self.assertGreaterEqual(flow_score(good), 0.7)


class ListenerUnitTests(unittest.TestCase):
    def test_normalise_passed_score_notes(self):
        raw = {
            "sections": [
                {
                    "topic_id": 9,
                    "clarity": 2,
                    "lost_at": "then suddenly fees",
                    "grammar_issue": "We is ready",
                    "robotic_line": "In today's landscape",
                }
            ],
            "followability": 3,
            "sounds_human": 3,
            "grammar": 3,
            "takeaway": "something about fees",
        }
        result = normalize_listener_result(raw)
        self.assertFalse(listener_passed(result))
        self.assertLess(listener_score(result), 0.8)
        notes = listener_notes(result, limit=8)
        self.assertTrue(any("lost the thread in section 9" in note for note in notes))
        self.assertTrue(any("machine-written" in note for note in notes))
        self.assertTrue(any("grammar" in note for note in notes))

    def test_listener_passed(self):
        result = normalize_listener_result(
            {
                "sections": [{"topic_id": 1, "clarity": 4}],
                "followability": 4,
                "sounds_human": 5,
                "grammar": 4,
                "takeaway": "come visit",
            }
        )
        self.assertTrue(listener_passed(result))


class WriterPlanPromptTests(unittest.TestCase):
    def test_story_plan_block_present_when_plan_passed(self):
        plan = {
            "throughline": "Learn by doing",
            "listener_start": "doubt",
            "listener_end": "ready",
            "arc": "setup → proof → ask",
            "ask": "Visit campus",
            "beats": [
                {
                    "topic_id": 1,
                    "role": "OPENING",
                    "point": "open",
                    "proof": "p",
                    "proof_source": "LOCKED",
                    "story_device": "question",
                    "bridge_in": "",
                    "approx_words": 40,
                }
            ],
        }
        messages = script_messages(
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I2",
            temperature="X2",
            context_note="",
            modules=[],
            sequence=["M01"],
            facts=[],
            word_budget=80,
            topic_flow=[ScriptTopic(topic_id=1, title="Open", pages=[1])],
            plan=plan,
        )
        user = messages[1]["content"]
        self.assertIn("STORY PLAN — follow this spine", user)
        self.assertIn("Learn by doing", user)
        self.assertIn("approx_words=40", user)


class QualityLoopTests(unittest.TestCase):
    def _target(self):
        return SimpleNamespace(
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I2",
            temperature="X2",
            context_note="",
            recipe_ref="A1-1",
            deck_use_case="",
            module_sequence="",
            script_json="",
            validation_report="",
            error="",
            founder_quote_ids="",
            report_asset_ids="",
            report_passages_json="",
            script_plan_json="",
            quality_trace_json="",
        )

    def _db(self):
        db = mock.MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        db.query.return_value.filter.return_value.first.return_value = None
        db.query.return_value.all.return_value = []
        return db

    def _run_phase(
        self,
        target,
        *,
        script,
        reviews,
        extra_patches=None,
        chat_side_effect=None,
        plan_enabled=True,
        flow_enabled=True,
        listener_enabled=True,
    ):
        topic = ScriptTopic(topic_id=1, title="Open", pages=[1], recipe_modules=["M01"])
        quote = SimpleNamespace(
            id=7,
            text="Stay in India",
            speaker="Pratham Mittal",
            topic="vision",
            module_ids="M12",
            source_name="C0005.MP4",
            source_file_id="abc",
            start_sec=10,
        )
        settings_mod = __import__("backend.pipeline.runner", fromlist=["settings"]).settings
        stack = [
            mock.patch.object(settings_mod, "script_plan_enabled", plan_enabled),
            mock.patch.object(settings_mod, "flow_check_enabled", flow_enabled),
            mock.patch.object(settings_mod, "listener_enabled", listener_enabled),
            mock.patch(
                "backend.pipeline.runner.resolve_recipe",
                return_value=SimpleNamespace(
                    ref="A1-1", module_sequence=["M01"], word_budget=200
                ),
            ),
            mock.patch(
                "backend.pipeline.runner.plan_pages",
                return_value=[BrandSlide(1, "", "Cover")],
            ),
            mock.patch(
                "backend.pipeline.runner.plan_with_generated_slides",
                side_effect=lambda db, plan, *a, **k: list(plan),
            ),
            mock.patch("backend.pipeline.runner.pick_report_passages", return_value=[]),
            mock.patch("backend.pipeline.runner.load_script_topics", return_value=[topic]),
            mock.patch("backend.pipeline.runner.pick_founder_quotes", return_value=[quote]),
            mock.patch("backend.pipeline.runner.latest_style_guide", return_value=""),
            mock.patch(
                "backend.pipeline.runner.chat_json",
                side_effect=chat_side_effect or (lambda *_a, **_k: script),
            ),
            mock.patch("backend.pipeline.runner.ScriptPayload.model_validate", return_value=None),
            mock.patch(
                "backend.pipeline.runner.align_script_to_topics",
                side_effect=lambda payload, topics, modules: payload,
            ),
            mock.patch("backend.pipeline.runner.review_pratham_voice", side_effect=reviews),
            mock.patch(
                "backend.pipeline.runner.plan_script",
                return_value={
                    "throughline": "x",
                    "listener_start": "a",
                    "listener_end": "b",
                    "arc": "c",
                    "ask": "d",
                    "beats": [
                        {
                            "topic_id": 1,
                            "role": "OPENING + CLOSE",
                            "point": "p",
                            "proof": "",
                            "proof_source": "NONE",
                            "story_device": "none",
                            "bridge_in": "",
                            "approx_words": 80,
                        }
                    ],
                },
            ),
        ]
        for patcher in extra_patches or []:
            stack.append(patcher)
        for patcher in stack:
            patcher.start()
            self.addCleanup(patcher.stop)
        return generate_script_phase(self._db(), target, lambda _s: None)

    def _pass_flow(self):
        return {
            "joins": [],
            "sections": [{"topic_id": 1, "story_shape": 4, "quote": "", "issue": ""}],
            "arc": {"score": 4, "issue": ""},
            "naturalness": {"score": 4, "issue": ""},
            "spoken": {"score": 4, "issue": ""},
            "repetition": [],
            "grammar": [],
        }

    def _fail_flow(self):
        return {
            "joins": [
                {
                    "from_topic": 1,
                    "to_topic": 2,
                    "score": 2,
                    "quote": "abrupt",
                    "issue": "cold",
                    "suggested_bridge": "So…",
                }
            ],
            "sections": [{"topic_id": 1, "story_shape": 2, "quote": "", "issue": "list"}],
            "arc": {"score": 2, "issue": "flat"},
            "naturalness": {"score": 2, "issue": "stiff"},
            "repetition": [],
            "grammar": [],
        }

    def _pass_listener(self):
        return {
            "sections": [{"topic_id": 1, "clarity": 4, "lost_at": "", "grammar_issue": "", "robotic_line": ""}],
            "followability": 4,
            "sounds_human": 4,
            "grammar": 4,
            "takeaway": "come visit",
            "grounded": True,
        }

    def _fail_listener(self):
        return {
            "sections": [
                {
                    "topic_id": 1,
                    "clarity": 2,
                    "lost_at": "fees",
                    "grammar_issue": "",
                    "robotic_line": "leverage synergy",
                }
            ],
            "followability": 2,
            "sounds_human": 2,
            "grammar": 4,
            "takeaway": "confused",
            "grounded": False,
        }

    def test_stops_when_both_pass(self):
        target = self._target()
        script = {
            "sections": [
                {"topic_id": 1, "topic_title": "Open", "pages": [1], "heading": "Open", "text": "Come sit in."}
            ],
            "cta": "Come Saturday.",
        }
        with mock.patch("backend.audience.latest_listener_profile", return_value=""), mock.patch(
            "backend.audience.listener_examples_for_persona", return_value=[]
        ), mock.patch(
            "backend.pipeline.runner.check_flow", return_value=self._pass_flow()
        ) as flow, mock.patch(
            "backend.pipeline.runner.simulate_audience", return_value=self._pass_listener()
        ) as listener, mock.patch(
            "backend.pipeline.runner.validate_script", return_value=[]
        ):
            result = self._run_phase(
                target,
                script=script,
                reviews=[{"passed": True, "score": 0.9, "violations": []}],
            )
        self.assertIsNotNone(result)
        self.assertEqual(flow.call_count, 1)
        self.assertEqual(listener.call_count, 1)
        trace = json.loads(target.quality_trace_json)
        self.assertTrue(trace["rounds"][0]["passed"])
        self.assertEqual(trace["rounds"][0]["rewrite"], "none")
        self.assertTrue(target.script_plan_json)

    def test_worse_rewrite_keeps_best(self):
        target = self._target()
        original = {
            "sections": [
                {
                    "topic_id": 1,
                    "topic_title": "Open",
                    "pages": [1],
                    "heading": "Open",
                    "text": "Original script text here.",
                }
            ],
            "cta": "Come Saturday.",
        }
        rewritten = {
            "sections": [
                {
                    "topic_id": 1,
                    "topic_title": "Open",
                    "pages": [1],
                    "heading": "Open",
                    "text": "Rewritten script text here.",
                }
            ],
            "cta": "Come Saturday.",
        }
        calls = {"n": 0}

        def chat_side_effect(*_a, **_k):
            calls["n"] += 1
            return original if calls["n"] == 1 else rewritten

        with mock.patch("backend.audience.latest_listener_profile", return_value=""), mock.patch(
            "backend.audience.listener_examples_for_persona", return_value=[]
        ), mock.patch(
            "backend.pipeline.runner.check_flow",
            side_effect=[self._fail_flow(), self._fail_flow()],
        ), mock.patch(
            "backend.pipeline.runner.simulate_audience",
            side_effect=[self._fail_listener(), self._fail_listener()],
        ), mock.patch(
            "backend.pipeline.runner.validate_script", return_value=[]
        ):
            result = self._run_phase(
                target,
                script=original,
                reviews=[{"passed": True, "score": 0.9, "violations": []}] * 6,
                chat_side_effect=chat_side_effect,
            )
        self.assertEqual(result.script["sections"][0]["text"], "Original script text here.")
        trace = json.loads(target.quality_trace_json)
        self.assertEqual(trace["rounds"][0]["rewrite"], "accepted")
        self.assertEqual(trace["rounds"][1]["stopped"], "no_score_improvement")
        self.assertEqual(trace["kept_round"], 1)

    def test_rewrite_failing_validation_keeps_previous(self):
        target = self._target()
        original = {
            "sections": [
                {
                    "topic_id": 1,
                    "topic_title": "Open",
                    "pages": [1],
                    "heading": "Open",
                    "text": "Original script text here.",
                }
            ],
            "cta": "Come Saturday.",
        }
        rewritten = {
            "sections": [
                {
                    "topic_id": 1,
                    "topic_title": "Open",
                    "pages": [1],
                    "heading": "Open",
                    "text": "Broken rewrite.",
                }
            ],
            "cta": "Come Saturday.",
        }
        chat_calls = {"n": 0}

        def chat_side_effect(*_a, **_k):
            chat_calls["n"] += 1
            return original if chat_calls["n"] == 1 else rewritten

        validate_calls = {"n": 0}

        def validate_side_effect(*_a, **_k):
            validate_calls["n"] += 1
            if validate_calls["n"] == 1:
                return []
            return ["Invented claim not in locked facts"]

        with mock.patch("backend.audience.latest_listener_profile", return_value=""), mock.patch(
            "backend.audience.listener_examples_for_persona", return_value=[]
        ), mock.patch(
            "backend.pipeline.runner.check_flow", return_value=self._fail_flow()
        ), mock.patch(
            "backend.pipeline.runner.simulate_audience", return_value=self._fail_listener()
        ), mock.patch(
            "backend.pipeline.runner.validate_script", side_effect=validate_side_effect
        ):
            result = self._run_phase(
                target,
                script=original,
                reviews=[{"passed": True, "score": 0.9, "violations": []}] * 4,
                chat_side_effect=chat_side_effect,
            )
        self.assertEqual(result.script["sections"][0]["text"], "Original script text here.")
        trace = json.loads(target.quality_trace_json)
        self.assertEqual(trace["rounds"][0]["rewrite"], "rejected")

    def test_rewrite_with_rule_violation_gets_one_repair_pass(self):
        target = self._target()

        def script_with(text):
            return {
                "sections": [
                    {"topic_id": 1, "topic_title": "Open", "pages": [1], "heading": "Open", "text": text}
                ],
                "cta": "Come Saturday.",
            }

        replies = [
            script_with("Original script text here."),
            script_with("Rewrite with a slip."),
            script_with("Repaired rewrite text."),
        ]
        chat_messages: list = []

        def chat_side_effect(messages, *_a, **_k):
            chat_messages.append(messages)
            return replies[len(chat_messages) - 1]

        validations = iter([[], ["Section 1 has a sentence cut off mid-thought"], []])
        with mock.patch("backend.audience.latest_listener_profile", return_value=""), mock.patch(
            "backend.audience.listener_examples_for_persona", return_value=[]
        ), mock.patch(
            "backend.pipeline.runner.check_flow",
            side_effect=[self._fail_flow(), self._pass_flow()],
        ), mock.patch(
            "backend.pipeline.runner.simulate_audience",
            side_effect=[self._fail_listener(), self._pass_listener()],
        ), mock.patch(
            "backend.pipeline.runner.validate_script",
            side_effect=lambda *_a, **_k: next(validations),
        ):
            result = self._run_phase(
                target,
                script=replies[0],
                reviews=[{"passed": True, "score": 0.9, "violations": []}] * 4,
                chat_side_effect=chat_side_effect,
            )
        self.assertEqual(result.script["sections"][0]["text"], "Repaired rewrite text.")
        self.assertIn("cut off mid-thought", chat_messages[2][-1]["content"])
        trace = json.loads(target.quality_trace_json)
        self.assertEqual(trace["rounds"][0]["rewrite"], "accepted")
        self.assertEqual(trace["kept_round"], 2)

    def test_judge_exception_does_not_fail_run(self):
        target = self._target()
        script = {
            "sections": [
                {"topic_id": 1, "topic_title": "Open", "pages": [1], "heading": "Open", "text": "Come sit in."}
            ],
            "cta": "Come Saturday.",
        }
        with mock.patch("backend.audience.latest_listener_profile", return_value=""), mock.patch(
            "backend.audience.listener_examples_for_persona", return_value=[]
        ), mock.patch(
            "backend.pipeline.runner.check_flow", side_effect=RuntimeError("boom")
        ), mock.patch(
            "backend.pipeline.runner.simulate_audience", side_effect=RuntimeError("boom2")
        ), mock.patch(
            "backend.pipeline.runner.validate_script", return_value=[]
        ):
            result = self._run_phase(
                target,
                script=script,
                reviews=[{"passed": True, "score": 0.9, "violations": []}],
            )
        self.assertIsNotNone(result)
        self.assertEqual(target.error, "")
        trace = json.loads(target.quality_trace_json)
        self.assertEqual(trace["rounds"][0]["passed"], False)

    def test_flags_off_skip_planner_flow_listener(self):
        target = self._target()
        script = {
            "sections": [
                {"topic_id": 1, "topic_title": "Open", "pages": [1], "heading": "Open", "text": "Come sit in."}
            ],
            "cta": "Come Saturday.",
        }
        plan_mock = mock.Mock(side_effect=AssertionError("planner should not run"))
        flow_mock = mock.Mock(side_effect=AssertionError("flow should not run"))
        listener_mock = mock.Mock(side_effect=AssertionError("listener should not run"))
        with mock.patch("backend.pipeline.runner.validate_script", return_value=[]):
            result = self._run_phase(
                target,
                script=script,
                reviews=[{"passed": True, "score": 0.9, "violations": []}],
                plan_enabled=False,
                flow_enabled=False,
                listener_enabled=False,
                extra_patches=[
                    mock.patch("backend.pipeline.runner.plan_script", plan_mock),
                    mock.patch("backend.pipeline.runner.check_flow", flow_mock),
                    mock.patch("backend.pipeline.runner.simulate_audience", listener_mock),
                ],
            )
        self.assertIsNotNone(result)
        plan_mock.assert_not_called()
        flow_mock.assert_not_called()
        listener_mock.assert_not_called()

    def test_planner_failure_continues_without_plan(self):
        target = self._target()
        script = {
            "sections": [
                {"topic_id": 1, "topic_title": "Open", "pages": [1], "heading": "Open", "text": "Come sit in."}
            ],
            "cta": "Come Saturday.",
        }
        with mock.patch(
            "backend.pipeline.runner.check_flow", return_value=self._pass_flow()
        ), mock.patch(
            "backend.pipeline.runner.simulate_audience", return_value=self._pass_listener()
        ), mock.patch(
            "backend.audience.latest_listener_profile", return_value=""
        ), mock.patch(
            "backend.audience.listener_examples_for_persona", return_value=[]
        ), mock.patch(
            "backend.pipeline.runner.validate_script", return_value=[]
        ):
            result = self._run_phase(
                target,
                script=script,
                reviews=[{"passed": True, "score": 0.9, "violations": []}],
                extra_patches=[
                    mock.patch(
                        "backend.pipeline.runner.plan_script",
                        side_effect=RuntimeError("planner down"),
                    )
                ],
            )
        self.assertIsNotNone(result)
        self.assertEqual(target.script_plan_json, "")
        trace = json.loads(target.quality_trace_json)
        self.assertIn("planner down", trace["plan_error"])


class CacheKeyPipelineTests(unittest.TestCase):
    def test_writer_model_change_changes_key(self):
        from backend.config import settings

        db = mock.MagicMock()
        db.get.return_value = None
        axes = {
            "audience_cluster": "A",
            "duration": "T1",
            "channel": "CH1",
            "intent": "I2",
        }
        key1 = compute_cache_key(axes, "X2", "note", "", db)
        with mock.patch.object(settings, "script_writer_model", "openai/other-model"):
            key2 = compute_cache_key(axes, "X2", "note", "", db)
        self.assertNotEqual(key1, key2)
        self.assertEqual(SCRIPT_PIPELINE_VERSION, "11")


if __name__ == "__main__":
    unittest.main()
