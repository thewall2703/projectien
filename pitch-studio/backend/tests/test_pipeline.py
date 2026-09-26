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
from backend.pipeline.script_flow import (
    ScriptFlowError,
    ScriptTopic,
    build_script_topics,
    notes_by_page,
    notes_by_slide_key,
    reconcile_realized_slide_mapping,
)
from backend.pipeline.validator import (
    align_script_to_recipe,
    align_script_to_topics,
    budget_range,
    count_script_words,
    paragraphize_spoken_text,
    sentence_integrity_violations,
    split_spoken_sentences,
    spoken_english_violations,
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

    @staticmethod
    def _sentences(count: int, words: int = 10) -> list[str]:
        return [
            " ".join(f"s{index}w{position}" for position in range(words)) + "."
            for index in range(count)
        ]

    def _over_budget_script(self) -> dict:
        return {
            "sections": [
                {
                    "topic_id": 7,
                    "topic_title": "Student builders",
                    "pages": [13, 14],
                    "heading": "Build for real",
                    "text": " ".join(self._sentences(24)),
                }
            ],
            "cta": "Come sit in class Saturday.",
        }

    def test_over_budget_script_is_trimmed_by_whole_sentences(self):
        script = self._over_budget_script()
        sentences = self._sentences(24)
        trimmed = trim_script_to_budget(script, 240)
        self.assertEqual(count_script_words(script), 245)
        low, high = budget_range(240)
        self.assertTrue(low <= count_script_words(trimmed) <= high)
        kept = split_spoken_sentences(" ".join(trimmed["sections"][0]["text"].split()))
        self.assertEqual(len(kept), 23)
        self.assertTrue(set(kept) <= set(sentences))
        self.assertEqual(kept[0], sentences[0])
        self.assertEqual(kept[-1], sentences[-1])
        self.assertEqual(trimmed["sections"][0]["topic_id"], 7)
        self.assertEqual(trimmed["sections"][0]["pages"], [13, 14])
        self.assertEqual(trimmed["cta"], script["cta"])

    def test_trim_never_cuts_inside_a_sentence(self):
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
        self.assertEqual(trimmed["sections"][0]["text"], script["sections"][0]["text"])
        violations = validate_script(trimmed, [], ["M01"], 240)
        self.assertTrue(any(item.startswith("Word count ") for item in violations))

    def test_runner_repairs_a_word_only_validation_failure(self):
        topic = ScriptTopic(topic_id=7, title="Student builders", pages=[13, 14])
        script = self._over_budget_script()
        repaired, violations = _validate_and_repair_budget(script, [], ["M01"], 240, [topic])
        self.assertEqual(violations, [])
        self.assertEqual(count_script_words(repaired), 235)

    def _integrity(self, text: str, budget: int = 400, cta: str = "Book one campus visit this week.") -> list[str]:
        script = {"sections": [{"topic_title": "Recognition", "text": text}], "cta": cta}
        return sentence_integrity_violations(script, budget)

    def test_dangling_sentence_endings_are_flagged(self):
        for text in (
            "Recognition matters to every parent we meet. Now, an obvious parent question: is this an.",
            "There are several programmes, each built for a different stage. So which route is yours? If you’re.",
            "Placement data is published every year in full. We read it closely, and.",
        ):
            with self.subTest(text=text):
                self.assertTrue(any("cut off mid-thought" in item for item in self._integrity(text)))

    def test_complete_sentences_are_not_flagged(self):
        text = (
            "People ask what this place is really about. That's what it's about. "
            "Now look at the wider bodies we're part of. "
            "You can see the work, meet the people and decide for yourself what's true."
        )
        self.assertEqual(self._integrity(text), [])

    def _spoken(self, *texts: str) -> list[str]:
        sections = [{"topic_title": f"T{n}", "text": text} for n, text in enumerate(texts, 1)]
        return spoken_english_violations({"sections": sections, "cta": "Come and sit in on a class."})

    def test_announced_transitions_are_flagged_past_the_limit(self):
        gen84 = (
            "If Learn by Doing is the answer, the next question is what problem needed a new institution.",
            "The origin explains the idea. Now let's make it physical.",
            "Once that institutional foundation is clear, the next question is what it produces.",
            "With the three-part model named, let's zoom into the first part.",
        )
        violations = self._spoken(*gen84)
        self.assertTrue(any("announces its own transitions 4 times" in item for item in violations))
        self.assertEqual(self._spoken(*gen84[:2]), [])

    def test_slashes_and_label_colons_are_flagged(self):
        slash = self._spoken("The faculty mix is 40% practitioners / 30% full-time PhD / 30% visiting international.")
        self.assertTrue(any("reads out a slash" in item for item in slash))
        colons = self._spoken(
            "External judgment matters: six startups pitched on Shark Tank India.",
            "Here is the model: learn, act, observe.",
        )
        self.assertTrue(any("Colons read like slide labels" in item for item in colons))

    def test_spoken_lines_are_not_flagged(self):
        self.assertEqual(
            self._spoken(
                "Forty percent of the people teaching you are practitioners. We're open 24/7 during exams.",
                "So what does that look like on a Tuesday? Class starts at 9:30 and you're pitching by noon.",
                "Here's the thing: nobody cares where you studied.",
            ),
            [],
        )

    def test_section_without_a_final_sentence_is_flagged(self):
        violations = self._integrity("We spent a year building the lab, testing it with students, and then")
        self.assertTrue(any("ends without a complete sentence" in item for item in violations))

    def test_stub_section_is_flagged(self):
        violations = self._integrity("Pratham Mittal's role on Shark Tank India.")
        self.assertTrue(any("is only 8 words" in item for item in violations))

    def test_short_sections_allowed_in_tiny_scripts(self):
        script = {
            "sections": [{"topic_title": f"Beat {i}", "text": "We train operators on live work."} for i in range(4)],
            "cta": "Visit campus.",
        }
        self.assertEqual(sentence_integrity_violations(script, 48), [])

    def test_topic_opening_cannot_start_like_a_continuation(self):
        topic = ScriptTopic(topic_id=1, title="Origin", pages=[1, 2])
        script = {
            "sections": [
                {
                    "topic_id": 1,
                    "topic_title": "Origin",
                    "pages": [1, 2],
                    "heading": "Why here",
                    "text": "Then there's Gurugram. The city becomes part of the classroom.",
                }
            ],
            "cta": "Come sit in a class.",
        }
        violations = validate_script(script, [], ["M01"], 15, topics=[topic])
        self.assertTrue(any("opening starts like a continuation" in item for item in violations))

    def test_independent_topic_opening_passes_opening_rule(self):
        topic = ScriptTopic(topic_id=1, title="Campus", pages=[8], recipe_modules=["M09"])
        script = {
            "sections": [
                {
                    "topic_id": 1,
                    "topic_title": "Campus",
                    "pages": [8],
                    "heading": "Why here",
                    "text": "Why Gurugram? It was a teaching decision, not an address decision.",
                }
            ],
            "cta": "Come sit in a class.",
        }
        violations = validate_script(script, [], ["M01"], 16, topics=[topic])
        self.assertFalse(any("opening starts like a continuation" in item for item in violations))
        self.assertFalse(any("jumps ahead to Gurugram" in item for item in violations))

    def test_opening_cannot_jump_to_campus_before_campus_beat(self):
        topics = [
            ScriptTopic(topic_id=1, title="Learn by Doing", pages=[1]),
            ScriptTopic(topic_id=2, title="Gurugram", pages=[8], recipe_modules=["M09"]),
        ]
        script = {
            "sections": [
                {
                    "topic_id": 1,
                    "topic_title": "Learn by Doing",
                    "pages": [1],
                    "heading": "Why here",
                    "text": "The campus sits in Gurugram, surrounded by companies.",
                },
                {
                    "topic_id": 2,
                    "topic_title": "Gurugram",
                    "pages": [8],
                    "heading": "The location",
                    "text": "That location turns the city into part of the classroom.",
                },
            ],
            "cta": "Come sit in a class.",
        }
        violations = validate_script(script, [], ["M01", "M09"], 25, topics=topics)
        self.assertTrue(any("jumps ahead to Gurugram" in item for item in violations))


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
        self.assertEqual([topic.topic_id for topic in flow], [1, 2, 3])
        self.assertEqual(flow[0].pages, [1, 6, 7])
        self.assertEqual(flow[0].slide_keys, ["brand:p1", "brand:p6", "brand:p7"])
        self.assertEqual(flow[0].labels, ["Cover", "Origin", "Story"])
        self.assertEqual(flow[0].title, "The Founding Story")
        self.assertEqual(flow[0].section, "The Founding Story")
        self.assertEqual(flow[0].recipe_modules, ["M01"])
        self.assertIn("Why we exist", flow[0].summary)
        self.assertEqual(flow[1].recipe_modules, ["M04"])
        self.assertEqual(flow[2].title, "Vision Ahead")
        # Every beat carries one stable slide key per selected page.
        for topic in flow:
            self.assertEqual(len(topic.slide_keys), len(topic.pages))
            self.assertEqual(topic.slide_keys, [f"brand:p{page}" for page in topic.pages])

    def test_broad_topic_splits_when_slide_module_changes(self):
        plan = [
            BrandSlide(6, "M01", "Origin"),
            BrandSlide(51, "M04", "Proof"),
        ]
        topics = [
            SimpleNamespace(id=2, title="Story", pages_json="[6,51]", summary="", vision="", module_ids="M01"),
        ]
        flow = build_script_topics(plan, topics, ["M01", "M04", "M14"])
        self.assertEqual(len(flow), 2)
        self.assertEqual([topic.topic_id for topic in flow], [1, 2])
        self.assertEqual([topic.pages for topic in flow], [[6], [51]])
        self.assertEqual([topic.recipe_modules for topic in flow], [["M01"], ["M04"]])
        self.assertEqual([topic.title for topic in flow], ["The Founding Story", "Immersions"])

    def test_founding_story_heading_is_one_beat_covering_its_modules(self):
        plan = [
            BrandSlide(1, "", "Learn by Doing"),
            BrandSlide(4, "M01", "Medical students learn from doctors"),
            BrandSlide(8, "M09", "In the heart of Gurugram's business hub"),
        ]
        topics = [
            SimpleNamespace(
                id=7,
                title="The Origin of Masters' Union",
                pages_json="[1,2,3,4,5,6,7,8]",
                summary="The origin and location.",
                vision="Explain why the institution exists.",
                module_ids="M01,M09",
            )
        ]
        flow = build_script_topics(plan, topics, ["M01", "M09"])
        self.assertEqual([topic.pages for topic in flow], [[1, 4, 8]])
        self.assertEqual(flow[0].title, "The Founding Story")
        self.assertEqual(flow[0].recipe_modules, ["M01", "M09"])

    def test_generated_slide_keeps_own_beat_under_current_heading(self):
        placeholder = SimpleNamespace(
            source="generated",
            page=None,
            slide_key="generated:x",
            module_id="M07",
            recipe_modules=("M07",),
            title="Placement method",
            summary="How the numbers are counted",
            claim="",
            vision="",
            evidence_page=None,
        )
        plan = [
            BrandSlide(57, "M07", "Outcomes of this approach"),
            placeholder,
            BrandSlide(58, "M07", "Placements"),
        ]
        topics = [
            SimpleNamespace(id=13, title="Outcomes", pages_json="[57,58]", summary="", vision="", module_ids="M07"),
        ]
        flow = build_script_topics(plan, topics, ["M07"])
        self.assertEqual([topic.title for topic in flow], ["Outcomes", "Placement method", "Outcomes"])
        self.assertEqual([topic.section for topic in flow], ["Outcomes", "Outcomes", "Outcomes"])


class OrderByHeadingsTests(unittest.TestCase):
    def test_body_follows_sheet_heading_order_and_tags_sections(self):
        from backend.pipeline.vision_deck import order_by_headings

        dsai = SimpleNamespace(source="dsai", page=4, slide_key="dsai:p4")
        plan = [
            BrandSlide(1, "", "Cover"),
            BrandSlide(58, "M07", "Placements"),
            BrandSlide(22, "M08", "Faculty mix"),
            dsai,
            BrandSlide(6, "M01", "Born"),
            BrandSlide(49, "M06", "Funds"),
            BrandSlide(28, "M08", "Who taught"),
            BrandSlide(91, "M14", "Close"),
        ]
        ordered = order_by_headings(plan)
        self.assertEqual(
            [getattr(slide, "page", None) for slide in ordered],
            [1, 6, 22, 4, 28, 49, 58, 91],
        )
        self.assertIs(ordered[3], dsai)
        self.assertEqual(ordered[2].section, "Inclass")
        self.assertEqual(ordered[5].section, "Funded Ventures")
        self.assertEqual(ordered[-1].section, "Vision Ahead")

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
            "sections": [
                {
                    "module_id": None,
                    "topic_id": 2,
                    "heading": "Ask",
                    "text": "Come this Saturday.",
                    "pages": [92],
                }
            ],
            "cta": "Come this Saturday.",
        }
        module = SimpleNamespace(id="M01", name="Origin", job="", core_content="We train operators on live projects.")
        aligned = align_script_to_topics(script, topics, [module])
        ScriptPayload.model_validate(aligned)
        self.assertEqual([section["topic_id"] for section in aligned["sections"]], [1, 2])
        self.assertEqual([section["module_id"] for section in aligned["sections"]], ["M01", "M14"])
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

    def test_notes_by_slide_key_matches_page_notes_without_repeats(self):
        plan = [
            BrandSlide(1, "", "Cover"),
            BrandSlide(6, "M01", "Origin"),
            BrandSlide(7, "M01", "Story"),
            BrandSlide(92, "M14", "Close"),
        ]
        script = {
            "sections": [
                {"pages": [6, 7], "text": "Origin spoken note."},
                {"pages": [92], "text": "Close spoken note."},
            ]
        }
        self.assertEqual(
            notes_by_slide_key(script, plan),
            {
                "brand:p6": "Origin spoken note.",
                "brand:p7": "Origin spoken note.",
                "brand:p92": "Close spoken note.",
            },
        )

    def test_notes_by_slide_key_fixes_repeated_page_collision(self):
        from backend.pipeline.brand_deck import _assign_occurrences

        plan = _assign_occurrences(
            [
                BrandSlide(1, "", "Cover"),
                BrandSlide(8, "M09", "Gurugram"),
                BrandSlide(8, "M09", "Gurugram, revisited"),
                BrandSlide(92, "M14", "Close"),
            ]
        )
        script = {
            "sections": [
                {"pages": [8], "text": "First pass on Gurugram."},
                {"pages": [8], "text": "Second pass on Gurugram."},
                {"pages": [92], "text": "Close spoken note."},
            ]
        }
        notes = notes_by_slide_key(script, plan)
        # notes_by_page would collapse both beats onto page 8; slide keys keep both.
        self.assertEqual(notes["brand:p8"], "First pass on Gurugram.")
        self.assertEqual(notes["brand:p8#2"], "Second pass on Gurugram.")
        self.assertEqual(notes["brand:p92"], "Close spoken note.")

    def test_generated_success_reconciles_placeholder_to_realized_key(self):
        planned = [
            SimpleNamespace(
                slide_key="generated:m13-what-we-are-not",
                page=None,
            )
        ]
        realized = [
            SimpleNamespace(
                slide_key="generated-a1b2c3",
                page=None,
            )
        ]
        topics = [
            ScriptTopic(
                topic_id=1,
                title="What we are not",
                pages=[],
                slide_keys=["generated:m13-what-we-are-not"],
                recipe_modules=["M13"],
            )
        ]
        script = {
            "sections": [
                {
                    "topic_id": 1,
                    "pages": [],
                    "slide_keys": ["generated:m13-what-we-are-not"],
                    "text": "We are a real institution.",
                }
            ],
            "cta": "",
        }

        updated, updated_topics = reconcile_realized_slide_mapping(
            script, topics, planned, realized
        )

        self.assertEqual(updated_topics[0].slide_keys, ["generated-a1b2c3"])
        self.assertEqual(updated_topics[0].pages, [])
        self.assertEqual(updated["sections"][0]["slide_keys"], ["generated-a1b2c3"])
        self.assertEqual(
            notes_by_slide_key(updated, realized),
            {"generated-a1b2c3": "We are a real institution."},
        )

    def test_generated_degradation_reconciles_to_restored_brand_page(self):
        """Exact failure mode: a generated beat degrades back to displaced p51."""
        planned = [
            SimpleNamespace(
                slide_key="generated:m13-what-we-are-not",
                page=None,
            )
        ]
        restored = BrandSlide(51, "M04", "The five Outclass challenges")
        topics = [
            ScriptTopic(
                topic_id=1,
                title="What we are not",
                pages=[],
                slide_keys=["generated:m13-what-we-are-not"],
                recipe_modules=["M13"],
            )
        ]
        script = {
            "sections": [
                {
                    "topic_id": 1,
                    "pages": [],
                    "slide_keys": ["generated:m13-what-we-are-not"],
                    "text": "We are not a glorified weekend bootcamp.",
                }
            ],
            "cta": "",
        }

        updated, updated_topics = reconcile_realized_slide_mapping(
            script, topics, planned, [restored]
        )

        self.assertEqual(updated_topics[0].slide_keys, ["brand:p51"])
        self.assertEqual(updated_topics[0].pages, [51])
        self.assertEqual(updated["sections"][0]["slide_keys"], ["brand:p51"])
        self.assertEqual(updated["sections"][0]["pages"], [51])
        self.assertEqual(
            notes_by_slide_key(updated, [restored]),
            {"brand:p51": "We are not a glorified weekend bootcamp."},
        )

    def test_dropped_generated_slide_removes_key_keeps_evidence_and_text(self):
        planned = [
            SimpleNamespace(slide_key="generated:m13-what-we-are-not", page=None, source="generated"),
            BrandSlide(89, "M13", "Acknowledged across industry and academia"),
        ]
        realized = [None, BrandSlide(89, "M13", "Acknowledged across industry and academia")]
        topics = [
            ScriptTopic(
                topic_id=1,
                title="What we are not",
                pages=[89],
                slide_keys=["generated:m13-what-we-are-not", "brand:p89"],
                recipe_modules=["M13"],
            )
        ]
        script = {
            "sections": [
                {
                    "topic_id": 1,
                    "pages": [89],
                    "slide_keys": ["generated:m13-what-we-are-not", "brand:p89"],
                    "text": "We are a real institution, acknowledged nationally.",
                }
            ],
            "cta": "",
        }

        updated, updated_topics = reconcile_realized_slide_mapping(
            script, topics, planned, realized
        )

        self.assertEqual(updated_topics[0].slide_keys, ["brand:p89"])
        self.assertEqual(updated_topics[0].pages, [89])
        self.assertEqual(updated["sections"][0]["slide_keys"], ["brand:p89"])
        self.assertEqual(updated["sections"][0]["pages"], [89])
        self.assertEqual(
            updated["sections"][0]["text"],
            "We are a real institution, acknowledged nationally.",
        )

    def test_none_at_non_generated_position_raises(self):
        planned = [BrandSlide(6, "M01", "Origin")]
        with self.assertRaises(ScriptFlowError):
            reconcile_realized_slide_mapping(
                {"sections": [], "cta": ""},
                [ScriptTopic(topic_id=1, title="Origin", pages=[6], slide_keys=["brand:p6"])],
                planned,
                [None],
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
        self.assertIn("never more than 20 minutes", user)

    def test_long_sessions_cap_spoken_script_at_twenty_minutes(self):
        messages = script_messages(
            audience_cluster="A",
            duration="T5",
            channel="CH4",
            intent="I2",
            temperature="X3",
            context_note="",
            modules=[],
            sequence=["M01"],
            facts=[],
            word_budget=2400,
        )
        user = messages[1]["content"]
        self.assertIn("20-minute spoken script", user)
        self.assertIn("never more than 20 minutes", user)
        self.assertIn("2400", user)

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

    def test_topic_flow_labels_opening_body_and_close(self):
        topics = [
            ScriptTopic(topic_id=1, title="Origin", pages=[1], labels=["Cover"]),
            ScriptTopic(topic_id=2, title="Proof", pages=[20], labels=["Student proof"]),
            ScriptTopic(topic_id=3, title="Visit", pages=[92], labels=["Visit campus"]),
        ]
        messages = script_messages(
            audience_cluster="A",
            duration="T1",
            channel="CH3",
            intent="I2",
            temperature="X3",
            context_note="",
            modules=[],
            sequence=["M01", "M07", "M14"],
            facts=[],
            word_budget=240,
            topic_flow=topics,
        )
        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertIn("Role: OPENING", user)
        self.assertIn("Role: BODY", user)
        self.assertIn("Role: CLOSE", user)
        self.assertIn("This is the first thing the listener hears", system)
        self.assertIn("Add no new argument or evidence", system)
        self.assertIn("blank line", system)
        self.assertIn("wall of text", system)


class ParagraphizeTests(unittest.TestCase):
    def test_keeps_existing_blank_lines(self):
        text = "First beat lands here.\n\nSecond beat follows after a pause."
        self.assertEqual(paragraphize_spoken_text(text), text)

    def test_splits_a_spoken_blob_every_two_sentences(self):
        text = (
            "You already know the ranking story. That is not why this room exists. "
            "Look at who taught last week's class. Then look at where those students went."
        )
        result = paragraphize_spoken_text(text)
        self.assertEqual(result.count("\n\n"), 1)
        self.assertIn("ranking story", result.split("\n\n")[0])
        self.assertIn("taught last week's class", result.split("\n\n")[1])
        self.assertEqual(count_script_words({"sections": [{"text": result}], "cta": ""}), 28)

    def test_leaves_short_copy_alone(self):
        text = "Come sit in a class this Saturday."
        self.assertEqual(paragraphize_spoken_text(text), text)


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
    def setUp(self):
        for name in ("script_plan_enabled", "flow_check_enabled", "listener_enabled"):
            patcher = mock.patch.object(
                __import__("backend.pipeline.runner", fromlist=["settings"]).settings,
                name,
                False,
            )
            patcher.start()
            self.addCleanup(patcher.stop)

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
            deck_use_case="",
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
            script_plan_json="",
            quality_trace_json="",
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
        topic = ScriptTopic(
            topic_id=1,
            title="Open",
            pages=[1],
            slide_keys=["brand:p1"],
            recipe_modules=["M01"],
        )
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
            "backend.pipeline.runner.plan_with_generated_slides",
            side_effect=lambda db, plan, *args, **kwargs: list(plan),
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

    def test_role_payloads_use_effort_and_conditional_verbosity(self):
        cases = {
            "script_planner": ("openai/gpt-5.6-sol", "max", "high"),
            "script_writer": ("openai/gpt-5.6-sol", "max", "high"),
            "voice_judge": ("z-ai/glm-5.3-flash", "high", ""),
            "flow_judge": ("deepseek/deepseek-v4.1-flash", "high", ""),
            "listener": ("moonshotai/kimi-k3", "medium", ""),
        }
        for role, (model, effort, verbosity) in cases.items():
            with self.subTest(role=role):
                payload = _request_payload(
                    [{"role": "user", "content": "x"}],
                    role=role,
                )
                self.assertEqual(payload["model"], model)
                self.assertEqual(payload["reasoning"], {"effort": effort})
                self.assertNotIn("enabled", payload["reasoning"])
                if verbosity:
                    self.assertEqual(payload["verbosity"], verbosity)
                else:
                    self.assertNotIn("verbosity", payload)
                self.assertNotIn("max_tokens", payload)

    def test_role_model_override_and_max_tokens_only_when_passed(self):
        payload = _request_payload(
            [{"role": "user", "content": "x"}],
            role="script_writer",
            model="openai/custom",
            max_tokens=500,
        )
        self.assertEqual(payload["model"], "openai/custom")
        self.assertEqual(payload["max_tokens"], 500)

    def test_role_none_unchanged_from_legacy(self):
        legacy = _request_payload([{"role": "user", "content": "Write the pitch."}])
        with_none = _request_payload(
            [{"role": "user", "content": "Write the pitch."}],
            role=None,
        )
        self.assertEqual(legacy, with_none)

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
