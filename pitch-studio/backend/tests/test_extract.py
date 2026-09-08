from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from backend.extract import passages_from_extract, pick_report_passages
from backend.pipeline.prompts import script_messages


class PassageTests(unittest.TestCase):
    def test_prefers_chunks_over_pages(self):
        payload = {
            "chunks": [{"text": "chunk one", "start_page": 1, "end_page": 2}],
            "pages": [{"page": 1, "text": "page one", "duplicate": False}],
        }
        passages = passages_from_extract(payload, asset_id=21, title="Placement 2024")
        self.assertEqual(len(passages), 1)
        self.assertEqual(passages[0]["text"], "chunk one")
        self.assertEqual(passages[0]["start_page"], 1)

    def test_skips_duplicate_pages_when_no_chunks(self):
        payload = {
            "pages": [
                {"page": 1, "text": "Keep me", "duplicate": False},
                {"page": 2, "text": "Skip me", "duplicate": True},
            ]
        }
        passages = passages_from_extract(payload, asset_id=1, title="R")
        self.assertEqual([item["text"] for item in passages], ["Keep me"])


class PickTests(unittest.TestCase):
    def test_prefers_module_overlap(self):
        ready = SimpleNamespace(
            id=21,
            type="report",
            title="PGP TBM Placement Report 2024",
            module_ids="M07,M13",
            extract_status="ready",
            extract_json=json.dumps(
                {"chunks": [{"text": "Bain hired three PGP TBM graduates.", "start_page": 4, "end_page": 4}]}
            ),
        )
        other = SimpleNamespace(
            id=9,
            type="report",
            title="Entrepreneurship Report",
            module_ids="M04,M06",
            extract_status="ready",
            extract_json=json.dumps(
                {"chunks": [{"text": "Student ventures raised seed rounds.", "start_page": 2, "end_page": 2}]}
            ),
        )
        selected = pick_report_passages([other, ready], ["M01", "M07", "M14"], limit=1)
        self.assertEqual(len(selected), 1)
        self.assertIn("Bain", selected[0]["text"])


class PromptReportTests(unittest.TestCase):
    def test_script_messages_include_report_evidence(self):
        messages = script_messages(
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I2",
            temperature="X3",
            context_note="",
            modules=[],
            sequence=["M07"],
            facts=[],
            word_budget=200,
            report_passages=[
                {
                    "title": "PGP TBM Placement Report 2024",
                    "start_page": 4,
                    "end_page": 4,
                    "text": "Bain hired three PGP TBM graduates.",
                }
            ],
        )
        user = messages[1]["content"]
        self.assertIn("REPORT EVIDENCE", user)
        self.assertIn("PGP TBM Placement Report 2024", user)
        self.assertIn("Bain hired three PGP TBM graduates.", user)
        self.assertIn("Use at least one concrete detail", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
