from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from backend.pipeline.brand_deck import BrandSlide
from backend.pipeline.llm import _extract_json, _multimodal_payload, _request_payload
from backend.pipeline.prompts import (
    UNIVERSITY_STATUS_LINE,
    duration_framework,
    review_pratham_voice,
    script_messages,
)
from backend.pipeline.resolver import _compose_fallback, parse_sequence
from backend.pipeline.runner import _validate_and_repair_budget, pick_assets, run
from backend.pipeline.script_flow import ScriptFlowError, ScriptTopic, build_script_topics, notes_by_page
from backend.pipeline.validator import (
    align_script_to_recipe,
    align_script_to_topics,
    count_script_words,
    trim_script_to_budget,
    validate_script,
)
from backend.schemas import ScriptPayload


def fact(status: str, value: str, name: str = "x") -> SimpleNamespace:
    return SimpleNamespace(status=status, value=value, fact=name)


class ValidatorTests(unittest.TestCase):
    def test_forbidden_fact_is_caught(self):
        script = {
            "sections": [{"module_id": "M01", "text": "Blue Brew raised a huge valuation."}],
            "cta": "Apply",
        }
        facts = [fact("do_not_use", "Blue Brew raised a huge valuation.")]
        violations = validate_script(script, facts, ["M01"], 10)
        self.assertTrue(any("Forbidden" in item for item in violations))

    def test_average_requires_median(self):
        script = {
            "sections": [{"module_id": "M07", "text": "Average CTC is ₹33.39 LPA this year."}],
            "cta": "Talk to admissions",
        }
        violations = validate_script(script, [], ["M07"], 8)
        self.assertTrue(any("median" in item.lower() for item in violations))

    def test_average_with_median_passes_pair_rule(self):
        text = (
            "Average CTC is ₹33.39 LPA and the median is ₹27.78 LPA, "
            "side by side, audited carefully for this conversation."
        )
        script = {"sections": [{"module_id": "M07", "text": text}], "cta": "Apply now"}
        violations = validate_script(script, [], ["M07"], len(text.split()))
        self.assertFalse(any("median" in item.lower() for item in violations))

    def test_sequence_mismatch(self):
        script = {"sections": [{"module_id": "M02", "text": "Institution first."}], "cta": "x"}
        violations = validate_script(script, [], ["M01", "M02"], 3)
        self.assertTrue(any("order" in item for item in violations))

    def test_align_restores_missing_close(self):
        script = {
            "sections": [
                {"module_id": "M01", "heading": "Hook", "text": "We train operators."},
                {"module_id": "M02", "heading": "Institution", "text": "The school is the proof."},
            ],
            "cta": "Come to campus this week.",
        }
        m14 = SimpleNamespace(
            id="M14",
            name="The close",
            job="Ask for the next step",
            core_content="Name one next step. Sit in a class. Then decide.",
        )
        aligned = align_script_to_recipe(script, ["M01", "M02", "M14"], [m14])
        self.assertEqual([section["module_id"] for section in aligned["sections"]], ["M01", "M02", "M14"])
        self.assertIn("campus", aligned["sections"][-1]["text"].lower())
        violations = validate_script(aligned, [], ["M01", "M02", "M14"], 20)
        self.assertFalse(any("order" in item for item in violations))

    def test_cta_is_not_counted_twice(self):
        script = {
            "sections": [
                {"module_id": "M14", "heading": "Ask", "text": "Come to campus this Saturday."}
            ],
            "cta": "Come to campus this Saturday.",
        }
        self.assertEqual(count_script_words(script), 5)

    def test_accreditation_wording(self):
        script = {
            "sections": [
                {
                    "module_id": "M02",
                    "text": "We have AACSB accreditation and that settles it.",
                }
            ],
            "cta": "Visit campus",
        }
        violations = validate_script(script, [], ["M02"], 8)
        self.assertTrue(any("accreditation" in item.lower() for item in violations))

    def test_missing_final_ask(self):
        script = {"sections": [{"module_id": "M01", "text": "We train operators on live work."}], "cta": ""}
        violations = validate_script(script, [], ["M01"], 6)
        self.assertTrue(any("final ask" in item for item in violations))

    def test_old_script_payload_still_parses(self):
        payload = ScriptPayload.model_validate(
            {"sections": [{"module_id": "M01", "heading": "Hook", "text": "Hi there."}], "cta": "Visit"}
        )
        self.assertEqual(payload.sections[0].topic_id, 0)
        self.assertEqual(payload.sections[0].pages, [])

    def test_over_budget_script_is_trimmed_to_the_hard_limit(self):
        script = {
            "sections": [
                {
                    "topic_id": 7,
                    "topic_title": "Student builders",
                    "pages": [13, 14],
                    "heading": "Build for real",
                    "text": " ".join(f"word{index}" for index in range(238)) + ".",
                }
            ],
            "cta": "Come sit in class Saturday.",
        }
        trimmed = trim_script_to_budget(script, 240)
        self.assertEqual(count_script_words(script), 243)
        self.assertEqual(count_script_words(trimmed), 240)
        self.assertEqual(trimmed["sections"][0]["topic_id"], 7)
        self.assertEqual(trimmed["sections"][0]["pages"], [13, 14])
        self.assertEqual(trimmed["cta"], script["cta"])

    def test_runner_repairs_a_word_only_validation_failure(self):
        topic = ScriptTopic(topic_id=7, title="Student builders", pages=[13, 14])
        script = {
            "sections": [
                {
                    "topic_id": 7,
                    "topic_title": topic.title,
                    "pages": [13, 14],
                    "heading": "Build for real",
                    "text": " ".join(f"word{index}" for index in range(238)) + ".",
                }
            ],
            "cta": "Come sit in class Saturday.",
        }
        repaired, violations = _validate_and_repair_budget(script, [], ["M01"], 240, [topic])
        self.assertEqual(violations, [])
        self.assertEqual(count_script_words(repaired), 240)


class ScriptFlowTests(unittest.TestCase):
    def test_collapses_consecutive_pages_of_the_same_topic(self):
        plan = [
            BrandSlide(1, "", "Cover"),
            BrandSlide(6, "M01", "Origin"),
            BrandSlide(7, "M01", "Story"),
            BrandSlide(51, "M04", "Proof"),
            BrandSlide(92, "M14", "Close"),
        ]
        topics = [
            SimpleNamespace(id=1, title="Open", pages_json="[1]", summary="Start here", vision="", module_ids=""),
            SimpleNamespace(
                id=2,
                title="Origin",
                pages_json="[6,7,8]",
                summary="Why we exist",
                vision="Build operators",
                module_ids="M01",
            ),
            SimpleNamespace(id=3, title="Proof", pages_json="[51]", summary="Outcomes", vision="", module_ids="M04,M07"),
            SimpleNamespace(id=4, title="Close", pages_json="[92]", summary="Ask", vision="", module_ids="M14"),
        ]
        flow = build_script_topics(plan, topics, ["M01", "M04", "M14"])
        self.assertEqual([topic.topic_id for topic in flow], [1, 2, 3, 4])
        self.assertEqual(flow[1].pages, [6, 7])
        self.assertEqual(flow[1].labels, ["Origin", "Story"])
        self.assertEqual(flow[1].recipe_modules, ["M01"])
        self.assertEqual(flow[2].recipe_modules, ["M04"])

    def test_collapsed_topic_collects_slide_modules(self):
        plan = [
            BrandSlide(6, "M01", "Origin"),
            BrandSlide(51, "M04", "Proof"),
        ]
        topics = [
            SimpleNamespace(id=2, title="Story", pages_json="[6,51]", summary="", vision="", module_ids="M01"),
        ]
        flow = build_script_topics(plan, topics, ["M01", "M04", "M14"])
        self.assertEqual(len(flow), 1)
        self.assertEqual(flow[0].pages, [6, 51])
        self.assertEqual(flow[0].recipe_modules, ["M01", "M04"])

    def test_missing_topic_index_fails(self):
        with self.assertRaises(ScriptFlowError):
            build_script_topics([BrandSlide(1, "", "Cover")], [], ["M01"])

    def test_unmapped_page_fails(self):
        with self.assertRaises(ScriptFlowError):
            build_script_topics(
                [BrandSlide(99, "M01", "Unknown")],
                [SimpleNamespace(id=1, title="Open", pages_json="[1]", summary="", vision="", module_ids="")],
                ["M01"],
            )

    def test_align_fills_from_topic_and_recipe(self):
        topics = [
            ScriptTopic(
                topic_id=1,
                title="Origin",
                pages=[6, 7],
                recipe_modules=["M01"],
                module_ids=["M01"],
                summary="Why this exists",
            ),
            ScriptTopic(topic_id=2, title="Close", pages=[92], recipe_modules=["M14"], module_ids=["M14"], summary="Ask"),
        ]
        script = {
            "sections": [{"topic_id": 2, "heading": "Ask", "text": "Come this Saturday.", "pages": [92]}],
            "cta": "Come this Saturday.",
        }
        module = SimpleNamespace(id="M01", name="Origin", job="", core_content="We train operators on live projects.")
        aligned = align_script_to_topics(script, topics, [module])
        self.assertEqual([section["topic_id"] for section in aligned["sections"]], [1, 2])
        self.assertEqual(aligned["sections"][0]["pages"], [6, 7])
        self.assertEqual(aligned["sections"][0]["topic_title"], "Origin")
        self.assertIn("operators", aligned["sections"][0]["text"])

    def test_topic_order_and_page_coverage(self):
        topics = [
            ScriptTopic(topic_id=1, title="A", pages=[1, 2]),
            ScriptTopic(topic_id=2, title="B", pages=[3]),
        ]
        script = {
            "sections": [
                {"topic_id": 2, "pages": [3], "text": "Second first."},
                {"topic_id": 1, "pages": [1, 2], "text": "First second."},
            ],
            "cta": "Visit campus",
        }
        violations = validate_script(script, [], ["M01"], 6, topics=topics)
        self.assertTrue(any("topic order" in item for item in violations))
        self.assertTrue(any("coverage" in item or "pages" in item for item in violations))

    def test_notes_attach_to_each_selected_page(self):
        script = {
            "sections": [
                {"pages": [6, 7], "text": "Origin spoken note."},
                {"pages": [92], "text": "Close spoken note."},
            ]
        }
        self.assertEqual(
            notes_by_page(script),
            {6: "Origin spoken note.", 7: "Origin spoken note.", 92: "Close spoken note."},
        )


class PromptTests(unittest.TestCase):
    def test_duration_framework_progresses_with_time(self):
        self.assertIn("precise, specific, and factual", duration_framework("T0"))
        self.assertIn("position Masters' Union", duration_framework("T1"))
        self.assertIn("emotional state", duration_framework("T2"))
        self.assertIn("student case studies", duration_framework("T3"))

    def test_selected_duration_strategy_is_in_script_prompt(self):
        messages = script_messages(
            audience_cluster="A",
            duration="T2",
            channel="CH1",
            intent="I2",
            temperature="X3",
            context_note="",
            modules=[],
            sequence=["M01"],
            facts=[],
            word_budget=700,
        )
        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertIn("IMPACT MUST BE PROPORTIONAL TO TIME", system)
        self.assertIn("Duration strategy — follow this as the governing narrative brief", user)
        self.assertIn("emotional state", user)

    def test_university_status_uses_natural_approved_wording(self):
        old_wording = "Masters' Union University — bill passed by the Government of Haryana"
        status_fact = SimpleNamespace(
            fact="University status",
            value=old_wording,
            source="Haryana state legislation",
            status="verified",
            module_ids="M02",
        )
        messages = script_messages(
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I1",
            temperature="X2",
            context_note="",
            modules=[],
            sequence=["M02"],
            facts=[status_fact],
            word_budget=280,
        )
        user = messages[1]["content"]
        self.assertIn(UNIVERSITY_STATUS_LINE, user)
        self.assertNotIn(old_wording, user)

    def test_revision_includes_draft_and_range(self):
        draft = {
            "sections": [{"module_id": "M01", "heading": "Origin", "text": "Why this exists."}],
            "cta": "Visit campus.",
        }
        messages = script_messages(
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I2",
            temperature="X3",
            context_note="",
            modules=[],
            sequence=["M01"],
            facts=[],
            word_budget=280,
            corrections=["Word count 340 is outside the 238-280 range (280-word hard limit)"],
            draft=draft,
        )
        user = messages[1]["content"]
        self.assertIn("238-280", user)
        self.assertIn("Why this exists.", user)
        self.assertIn("Rewrite THAT draft", user)

    def test_topic_flow_governs_order_and_rewrite(self):
        topic = ScriptTopic(
            topic_id=4,
            title="The close",
            pages=[92],
            labels=["Come see"],
            summary="Ask on the last slide",
            recipe_modules=["M14"],
            module_ids=["M14"],
        )
        draft = {
            "sections": [
                {
                    "topic_id": 4,
                    "topic_title": "The close",
                    "pages": [92],
                    "heading": "Ask",
                    "text": "Come see the campus.",
                }
            ],
            "cta": "Come this Saturday.",
        }
        messages = script_messages(
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I2",
            temperature="X3",
            context_note="",
            modules=[],
            sequence=["M07", "M14"],
            facts=[],
            word_budget=280,
            topic_flow=[topic],
            draft=draft,
        )
        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertIn("DECK TOPIC FLOW", user)
        self.assertIn("The close", user)
        self.assertIn("Weave it into the most relevant existing", user)
        self.assertIn("topic_id", system)
        self.assertIn("Keep the same topic_id", user)


class VoiceReviewTests(unittest.TestCase):
    def test_missing_excerpts_fail(self):
        result = review_pratham_voice({"sections": [], "cta": "Visit"}, [])
        self.assertFalse(result["passed"])
        self.assertTrue(any("Pratham" in item for item in result["violations"]))

    def test_reviewer_pass_is_the_gate(self):
        with mock.patch(
            "backend.pipeline.llm.chat_json",
            return_value={"passed": True, "score": 0.95, "violations": ["A leftover brochure word"]},
        ):
            result = review_pratham_voice(
                {"sections": [{"text": "Come sit in a class."}], "cta": "Come Saturday"},
                [SimpleNamespace(text="Stay in India", topic="vision", module_ids="M12", speaker="Pratham Mittal", source_name="C0005.MP4", source_file_id="abc", start_sec=10)],
            )
        self.assertTrue(result["passed"])
        self.assertIn("A leftover brochure word", result["violations"])

    def test_low_score_fails(self):
        with mock.patch(
            "backend.pipeline.llm.chat_json",
            return_value={"passed": False, "score": 0.4, "violations": ["Sounds like a brochure"]},
        ):
            result = review_pratham_voice(
                {"sections": [{"text": "A world-class ecosystem."}], "cta": "Apply"},
                [SimpleNamespace(text="Stay in India", topic="vision", module_ids="M12", speaker="Pratham Mittal", source_name="C0005.MP4", source_file_id="abc", start_sec=10)],
            )
        self.assertFalse(result["passed"])
        self.assertIn("Sounds like a brochure", result["violations"])

    def test_clean_review_passes(self):
        with mock.patch(
            "backend.pipeline.llm.chat_json",
            return_value={"passed": True, "score": 0.9, "violations": []},
        ):
            result = review_pratham_voice(
                {"sections": [{"text": "Come sit in a class."}], "cta": "Come Saturday"},
                [SimpleNamespace(text="Stay in India", topic="vision", module_ids="M12", speaker="Pratham Mittal", source_name="C0005.MP4", source_file_id="abc", start_sec=10)],
            )
        self.assertTrue(result["passed"])


class RunnerGateTests(unittest.TestCase):
    def _generation(self):
        return SimpleNamespace(
            id=1,
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I2",
            temperature="X2",
            context_note="",
            recipe_ref="A2-1",
            module_sequence="",
            status="queued",
            script_json="",
            validation_report="",
            error="",
            deck_spec_json="",
            pptx_path="",
            asset_ids="",
            objection_ids="",
            founder_quote_ids="",
            report_asset_ids="",
            report_passages_json="",
        )

    def _db(self, generation):
        db = mock.MagicMock()
        db.get.return_value = generation
        db.query.return_value.filter.return_value.all.return_value = []
        db.query.return_value.all.return_value = []
        return db

    def _resolved(self):
        return SimpleNamespace(ref="A2-1", module_sequence=["M01", "M14"], word_budget=80)

    def test_fails_when_topics_cannot_be_mapped(self):
        generation = self._generation()
        with mock.patch("backend.pipeline.runner.SessionLocal", return_value=self._db(generation)), mock.patch(
            "backend.pipeline.runner.resolve_recipe", return_value=self._resolved()
        ), mock.patch(
            "backend.pipeline.runner.plan_pages", return_value=[BrandSlide(1, "", "Cover")]
        ), mock.patch(
            "backend.pipeline.runner.pick_report_passages", return_value=[]
        ), mock.patch(
            "backend.pipeline.runner.load_script_topics",
            side_effect=ScriptFlowError("Prepare Brand Deck topics before generating a pitch"),
        ):
            run(1)
        self.assertEqual(generation.status, "failed")
        self.assertIn("topics", generation.error.lower())

    def test_fails_without_pratham_excerpts(self):
        generation = self._generation()
        topic = ScriptTopic(topic_id=1, title="Open", pages=[1])
        with mock.patch("backend.pipeline.runner.SessionLocal", return_value=self._db(generation)), mock.patch(
            "backend.pipeline.runner.resolve_recipe", return_value=self._resolved()
        ), mock.patch(
            "backend.pipeline.runner.plan_pages", return_value=[BrandSlide(1, "", "Cover")]
        ), mock.patch(
            "backend.pipeline.runner.pick_report_passages", return_value=[]
        ), mock.patch(
            "backend.pipeline.runner.load_script_topics", return_value=[topic]
        ), mock.patch(
            "backend.pipeline.runner.pick_founder_quotes", return_value=[]
        ):
            run(1)
        self.assertEqual(generation.status, "failed")
        self.assertIn("Pratham", generation.error)

    def test_voice_gate_retries_then_fails(self):
        generation = self._generation()
        topic = ScriptTopic(topic_id=1, title="Open", pages=[1], recipe_modules=["M01"])
        script = {
            "sections": [{"topic_id": 1, "topic_title": "Open", "pages": [1], "heading": "Open", "text": "Come sit in."}],
            "cta": "Come Saturday.",
        }
        reviews = [
            {"passed": False, "score": 0.4, "violations": ["Sounds written"]},
            {"passed": False, "score": 0.5, "violations": ["Still corporate"]},
            {"passed": False, "score": 0.5, "violations": ["Still corporate"]},
        ]
        with mock.patch("backend.pipeline.runner.SessionLocal", return_value=self._db(generation)), mock.patch(
            "backend.pipeline.runner.resolve_recipe", return_value=self._resolved()
        ), mock.patch(
            "backend.pipeline.runner.plan_pages", return_value=[BrandSlide(1, "", "Cover")]
        ), mock.patch(
            "backend.pipeline.runner.pick_report_passages", return_value=[]
        ), mock.patch(
            "backend.pipeline.runner.load_script_topics", return_value=[topic]
        ), mock.patch(
            "backend.pipeline.runner.pick_founder_quotes",
            return_value=[
                SimpleNamespace(
                    id=7,
                    text="Stay in India",
                    speaker="Pratham Mittal",
                    topic="vision",
                    module_ids="M12",
                    source_name="C0005.MP4",
                    source_file_id="abc",
                    start_sec=10,
                )
            ],
        ), mock.patch(
            "backend.pipeline.runner.chat_json", return_value=script
        ), mock.patch(
            "backend.pipeline.runner.ScriptPayload.model_validate", return_value=None
        ), mock.patch(
            "backend.pipeline.runner.align_script_to_topics", side_effect=lambda payload, topics, modules: payload
        ), mock.patch(
            "backend.pipeline.runner.validate_script", return_value=[]
        ), mock.patch(
            "backend.pipeline.runner.review_pratham_voice", side_effect=reviews
        ):
            run(1)
        self.assertEqual(generation.status, "failed")
        self.assertIn("voice", generation.error.lower())

    def test_voice_rewrite_can_pass(self):
        generation = self._generation()
        topic = ScriptTopic(topic_id=1, title="Open", pages=[1], recipe_modules=["M01"])
        script = {
            "sections": [{"topic_id": 1, "topic_title": "Open", "pages": [1], "heading": "Open", "text": "Come sit in."}],
            "cta": "Come Saturday.",
        }
        reviews = [
            {"passed": False, "score": 0.4, "violations": ["Sounds written"]},
            {"passed": True, "score": 0.9, "violations": []},
        ]
        with mock.patch("backend.pipeline.runner.SessionLocal", return_value=self._db(generation)), mock.patch(
            "backend.pipeline.runner.resolve_recipe", return_value=self._resolved()
        ), mock.patch(
            "backend.pipeline.runner.plan_pages", return_value=[BrandSlide(1, "", "Cover")]
        ), mock.patch(
            "backend.pipeline.runner.pick_report_passages", return_value=[]
        ), mock.patch(
            "backend.pipeline.runner.load_script_topics", return_value=[topic]
        ), mock.patch(
            "backend.pipeline.runner.pick_founder_quotes",
            return_value=[
                SimpleNamespace(
                    id=7,
                    text="Stay in India",
                    speaker="Pratham Mittal",
                    topic="vision",
                    module_ids="M12",
                    source_name="C0005.MP4",
                    source_file_id="abc",
                    start_sec=10,
                )
            ],
        ), mock.patch(
            "backend.pipeline.runner.chat_json", return_value=script
        ), mock.patch(
            "backend.pipeline.runner.ScriptPayload.model_validate", return_value=None
        ), mock.patch(
            "backend.pipeline.runner.align_script_to_topics", side_effect=lambda payload, topics, modules: payload
        ), mock.patch(
            "backend.pipeline.runner.validate_script", return_value=[]
        ), mock.patch(
            "backend.pipeline.runner.review_pratham_voice", side_effect=reviews
        ), mock.patch(
            "backend.pipeline.runner.deck_spec_from_plan",
            return_value=SimpleNamespace(model_dump_json=lambda: "{}"),
        ), mock.patch(
            "backend.pipeline.runner.render_pptx", return_value="/tmp/deck.pptx"
        ), mock.patch(
            "backend.pipeline.runner.brand_deck_file_key", return_value=""
        ), mock.patch(
            "backend.pipeline.runner._select_assets", return_value=[]
        ), mock.patch(
            "backend.pipeline.runner._select_objections", return_value=[]
        ):
            run(1)
        self.assertEqual(generation.status, "done")
        self.assertEqual(generation.pptx_path, "/tmp/deck.pptx")


class JsonExtractTests(unittest.TestCase):
    def test_strips_markdown_fence(self):
        payload = _extract_json('```json\n{"sections": [], "cta": "Apply"}\n```')
        self.assertEqual(payload["cta"], "Apply")

    def test_empty_raises(self):
        with self.assertRaises(Exception):
            _extract_json("")


class LLMRequestTests(unittest.TestCase):
    def test_uses_opus_46_with_medium_adaptive_reasoning(self):
        payload = _request_payload([{"role": "user", "content": "Write the pitch."}])
        self.assertEqual(payload["model"], "anthropic/claude-opus-4.6")
        self.assertEqual(payload["reasoning"], {"enabled": True})
        self.assertEqual(payload["verbosity"], "medium")

    def test_multimodal_payload_sends_image_parts_without_json_format(self):
        payload = _multimodal_payload("Describe these images.", [b"abc", b"xyz"])
        content = payload["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "Describe these images."})
        self.assertEqual(len(content), 3)
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertNotIn("response_format", payload)


class ResolverHelperTests(unittest.TestCase):
    def test_parse_sequence(self):
        self.assertEqual(parse_sequence("M02 > M04 > M14"), ["M02", "M04", "M14"])

    def test_short_fallback_trims(self):
        sequence = _compose_fallback("A", "T1", "I2", "X2")
        self.assertEqual(sequence[-1], "M14")
        self.assertLessEqual(len(sequence), 3)

    def test_closing_adds_honest_module(self):
        sequence = _compose_fallback("A", "T2", "I2", "X4")
        self.assertIn("M13", sequence)


class AssetPickTests(unittest.TestCase):
    def test_round_robin_includes_later_modules(self):
        assets = [
            SimpleNamespace(
                title="Prospectus",
                module_ids="M02,M10",
                file_status="stored",
                file_key="a",
                status="exists",
            ),
            SimpleNamespace(
                title="Another Prospectus",
                module_ids="M02,M10",
                file_status="stored",
                file_key="b",
                status="exists",
            ),
            SimpleNamespace(
                title="PGP TBM Placement Report 2024",
                module_ids="M07,M13",
                file_status="stored",
                file_key="c",
                status="exists",
            ),
            SimpleNamespace(
                title="Brand Deck",
                module_ids="M01,M02,M07",
                file_status="external",
                file_key="",
                status="exists",
            ),
        ]
        selected = pick_assets(assets, ["M02", "M04", "M07", "M13", "M14"], limit=3)
        titles = [item.title for item in selected]
        self.assertIn("PGP TBM Placement Report 2024", titles)
        self.assertNotIn("Brand Deck", titles)


if __name__ == "__main__":
    unittest.main()
