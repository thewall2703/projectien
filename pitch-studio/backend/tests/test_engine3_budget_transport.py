"""Offline-only tests: injected HTTP and fake role settings, never paid calls."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from backend.master_script import budget_transport as pilot

MODEL = "test/model"
PRICES = {MODEL: {
    "currency": "USD", "verified_max_rates": True, "source": "offline fixture",
    "pricing": {"prompt": "0.000001", "completion": "0.000002", "input_cache_read": "0.000001", "input_cache_write": "0.000003"},
}}


def defaults(role):
    return {"model": MODEL, "effort": "high", "verbosity": "", "timeout": 600}


def response(content='{"ok":true}', cost=0.01, finish="stop", status=200):
    usage = {"prompt_tokens": 7, "completion_tokens": 3, "cost": cost}
    if cost is None:
        del usage["cost"]
    return httpx.Response(status, json={"choices": [{"message": {"content": content}, "finish_reason": finish}], "usage": usage})


def concurrent_worker(directory, queue, release):
    def post(*args, **kwargs):
        queue.put("sent")
        release.wait(10)
        raise httpx.ReadTimeout("offline timeout")
    try:
        with patch.object(pilot.llm, "role_defaults", defaults), patch.object(pilot.llm, "_headers", return_value={}):
            transport = pilot.BudgetedTransport(directory, budget_usd=1, prices={MODEL: {
                **PRICES[MODEL], "pricing": {"prompt": "0", "completion": "0.006"},
            }}, http_post=post)
            transport.call("parallel", "ms_editor", "system", {}, 100)
    except pilot.BudgetError:
        queue.put("blocked")
    except pilot.TransportError:
        queue.put("held")


class PilotTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.roles = patch.object(pilot.llm, "role_defaults", defaults)
        self.roles.start()
        self.addCleanup(self.roles.stop)
        self.headers = patch.object(pilot.llm, "_headers", return_value={"Authorization": "Bearer offline-secret"})
        self.headers.start()
        self.addCleanup(self.headers.stop)
        self.calls = []

    def transport(self, body=None, budget=25, prices=None):
        def post(*args, **kwargs):
            self.calls.append(kwargs)
            return body if body is not None else response()
        return pilot.BudgetedTransport(self.directory, budget_usd=budget, prices=PRICES if prices is None else prices, http_post=post)

    def call(self, transport, payload=None, **kwargs):
        return transport.call("../../stage", "ms_editor", "system", {} if payload is None else payload, **kwargs)

    def test_success_resume_cache_and_safe_artifacts(self):
        transport = self.transport()
        self.assertEqual(self.call(transport), {"ok": True})
        resumed = self.transport()
        self.assertEqual(self.call(resumed), {"ok": True})
        self.assertEqual(len(self.calls), 1)
        report = resumed.report()
        self.assertEqual(report["committed_usd"], "0.01")
        self.assertEqual(len(report["cache_hits"]), 1)
        self.assertEqual(report["cache_hits"][0]["cost_usd"], "0")
        self.assertEqual(self.calls[0]["timeout"], 240)
        self.assertFalse(self.calls[0]["json"]["provider"]["allow_fallbacks"])
        self.assertEqual(report["attempts"][0]["usage"]["prompt_tokens"], 7)
        self.assertEqual(report["attempts"][0]["price"]["currency"], "USD")
        for artifact in self.directory.glob("*.json"):
            self.assertNotIn("offline-secret", artifact.read_text())
            self.assertNotIn("Authorization", artifact.read_text())
        self.assertEqual(len(list(self.directory.glob("*.raw.json"))), 1)

    def test_cache_invalidated_by_payload_tokens_role_request_and_version(self):
        transport = self.transport()
        self.call(transport)
        self.call(transport, {"source": "changed"})
        self.call(transport, tokens=100)
        with patch.object(pilot, "TRANSPORT_VERSION", "changed"):
            self.call(transport)
        with patch.object(pilot.llm, "role_defaults", lambda role: {**defaults(role), "effort": "low"}):
            self.call(transport)
        self.assertEqual(len(self.calls), 5)

    def test_apostrophe_escape_recovery_preserves_raw_and_cache(self):
        content = r'''{"quote":"Masters\' Union", "literal":"keep \\'", "line":"a\nb"}'''
        transport = self.transport(response(content))
        expected = {"quote": "Masters' Union", "literal": "keep \\'", "line": "a\nb"}
        self.assertEqual(self.call(transport), expected)
        self.assertEqual(self.call(transport), expected)
        self.assertEqual(len(self.calls), 1)
        raw = json.loads(next(self.directory.glob("*.raw.json")).read_text())
        self.assertEqual(raw["json_normalization"], "apostrophe_escape")
        self.assertEqual(json.loads(raw["raw_response"])["choices"][0]["message"]["content"], content)
        self.assertEqual(transport.report()["committed_usd"], "0.01")

    def test_apostrophe_recovery_does_not_repair_other_malformed_json(self):
        for content in (r'''{"x":"a\'b\q"}''', r'''{"x":"a\'b"''', '{"x":"a\\\'b\n"}'):
            transport = self.transport(response(content))
            with self.assertRaises(pilot.TransportError):
                self.call(transport)
        self.assertFalse(list(self.directory.glob("*.cache.json")))

    def test_schema_is_sent_and_changes_cache_identity(self):
        from backend.master_script.compiler_pilot import review_response_format
        schema = review_response_format()
        transport = self.transport()
        self.call(transport)
        self.call(transport, response_format=schema)
        self.call(transport, response_format=schema)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1]["json"]["response_format"], schema)
        self.assertTrue(schema["json_schema"]["strict"])
        changed = deepcopy(schema)
        changed["json_schema"]["name"] = "changed"
        self.call(transport, response_format=changed)
        self.assertEqual(len(self.calls), 3)

    def test_invalid_cache_is_not_reused(self):
        transport = self.transport()
        self.call(transport)
        cache = next(self.directory.glob("*.cache.json"))
        cache.write_text('{"fingerprint":"broken"}')
        self.call(transport)
        self.assertEqual(len(self.calls), 2)

    def test_timeout_retains_reservation_no_retry_and_reopening_does_not_reset(self):
        def post(*args, **kwargs):
            self.calls.append(kwargs)
            raise httpx.ReadTimeout("offline-secret")
        transport = pilot.BudgetedTransport(self.directory, prices=PRICES, http_post=post)
        with self.assertRaises(pilot.TransportError) as error:
            self.call(transport)
        self.assertNotIn("offline-secret", str(error.exception))
        self.assertEqual(len(self.calls), 1)
        attempt = self.transport().report()["attempts"][0]
        self.assertEqual(attempt["charged_usd"], attempt["reservation_usd"])
        self.assertEqual(attempt["status"], "ambiguous")
        self.assertFalse(list(self.directory.glob("*.cache.json")))

    def test_malformed_content_charged_and_not_cached(self):
        transport = self.transport(response("not JSON", cost=0.04))
        with self.assertRaises(pilot.TransportError):
            self.call(transport)
        report = transport.report()
        self.assertEqual(report["committed_usd"], "0.04")
        self.assertEqual(report["attempts"][0]["status"], "invalid_response")
        self.assertIn("not JSON", next(self.directory.glob("*.raw.json")).read_text())
        self.assertFalse(list(self.directory.glob("*.cache.json")))

    def test_nonfinite_output_and_unsuccessful_finish_are_not_cached(self):
        for body in (response('{"value":NaN}'), response(finish="error"), response(finish=None)):
            transport = self.transport(body)
            with self.assertRaises(pilot.TransportError):
                self.call(transport)
            self.assertEqual(transport.report()["attempts"][-1]["actual_cost_usd"], "0.01")
        self.assertFalse(list(self.directory.glob("*.cache.json")))

    def test_missing_and_invalid_spend_hold_reservation(self):
        for cost in (None, "0.001", True, -1):
            with self.subTest(cost=cost):
                transport = self.transport(response(cost=cost))
                with self.assertRaises(pilot.TransportError):
                    self.call(transport, {"cost": cost})
                attempt = transport.report()["attempts"][-1]
                self.assertEqual(attempt["charged_usd"], attempt["reservation_usd"])
                self.assertEqual(attempt["status"], "missing_cost")
        self.assertFalse(list(self.directory.glob("*.cache.json")))

    def test_length_checked_before_parse_but_charged(self):
        transport = self.transport(response("broken", cost=0.03, finish="length"))
        with patch.object(pilot.llm, "_extract_json", side_effect=AssertionError("must not parse")) as parse:
            with self.assertRaises(pilot.TransportError):
                self.call(transport)
        parse.assert_not_called()
        self.assertEqual(transport.report()["committed_usd"], "0.03")
        self.assertEqual(transport.report()["attempts"][0]["status"], "length")
        self.assertFalse(list(self.directory.glob("*.cache.json")))

    def test_larger_review_allowance_preserves_reasoning_and_reserves_full_cost(self):
        from backend.master_script.compiler_pilot import REVIEW_TOKEN_LIMITS
        tokens = REVIEW_TOKEN_LIMITS["ms_evidence"]
        transport = self.transport()
        transport.call("baseline_source", "ms_evidence", "system", {}, tokens)
        request = self.calls[0]["json"]
        self.assertEqual(request["max_tokens"], 24000)
        self.assertEqual(request["reasoning"], {"effort": "high"})
        attempt = transport.report()["attempts"][0]
        expected = Decimal(attempt["input_token_bound"]) * Decimal("0.000004") + Decimal(tokens) * Decimal("0.000002")
        self.assertEqual(Decimal(attempt["reservation_usd"]), expected)

    def test_larger_review_is_blocked_before_spending_when_reservation_cannot_fit(self):
        from backend.master_script.compiler_pilot import REVIEW_TOKEN_LIMITS
        transport = self.transport(budget=0.02)
        with self.assertRaises(pilot.BudgetError):
            transport.call("baseline_source", "ms_evidence", "system", {}, REVIEW_TOKEN_LIMITS["ms_evidence"])
        self.assertFalse(self.calls)
        self.assertFalse(transport.report()["attempts"])

    def test_live_length_response_still_fails_closed_with_higher_allowance(self):
        from backend.master_script.compiler_pilot import REVIEW_TOKEN_LIMITS
        # Reproduce the provider envelope and accounting from the failed live run.
        body = httpx.Response(200, json={"choices": [{"finish_reason": "length",
            "native_finish_reason": "MAX_TOKENS", "message": {"content": '{"partial":'}}],
            "usage": {"cost": 0.07605825, "completion_tokens": 9921,
                      "completion_tokens_details": {"reasoning_tokens": 8909}}})
        transport = self.transport(body)
        with patch.object(pilot.llm, "_extract_json", side_effect=AssertionError("Never parse truncation")) as parse:
            with self.assertRaises(pilot.TransportError):
                transport.call("baseline_source", "ms_evidence", "system", {}, REVIEW_TOKEN_LIMITS["ms_evidence"])
        parse.assert_not_called()
        self.assertEqual(len(self.calls), 1)  # No implicit paid retry.
        self.assertEqual(transport.report()["committed_usd"], "0.07605825")
        self.assertEqual(transport.report()["attempts"][0]["status"], "length")
        self.assertFalse(list(self.directory.glob("*.cache.json")))

    def test_http_error_retains_worst_case_even_with_low_numeric_cost(self):
        transport = self.transport(response(cost=0.001, status=500))
        with self.assertRaises(pilot.TransportError):
            self.call(transport)
        attempt = transport.report()["attempts"][0]
        self.assertEqual(attempt["charged_usd"], attempt["reservation_usd"])
        self.assertEqual(attempt["actual_cost_usd"], "0.001")
        self.assertEqual(len(self.calls), 1)

    def test_malformed_envelope_holds_reservation(self):
        transport = self.transport(httpx.Response(200, text="not JSON"))
        with self.assertRaises(pilot.TransportError):
            self.call(transport)
        attempt = transport.report()["attempts"][0]
        self.assertEqual(attempt["charged_usd"], attempt["reservation_usd"])

    def test_zero_known_cost_is_valid(self):
        transport = self.transport(response(cost=0))
        self.call(transport)
        self.assertEqual(transport.report()["committed_usd"], "0")

    def test_cap_cannot_increase_or_reset(self):
        self.transport(budget=1)
        with self.assertRaises(pilot.BudgetError):
            self.transport(budget=2)
        with self.assertRaises(pilot.BudgetError):
            self.transport(budget=26)
        self.transport(budget=0.5)
        with self.assertRaises(pilot.BudgetError):
            self.transport(budget=1)
        self.assertEqual(self.transport(budget=0.5).report()["cap_usd"], "0.5")

    def test_explicit_allowance_is_audited_and_idempotent(self):
        transport = self.transport()
        self.call(transport)
        before = transport.report()["attempts"]
        record = transport.authorize_additional_spend("16", "user-approved-16")
        self.assertEqual(record["cap_usd"], "16.01")
        self.assertEqual(transport.report()["remaining_usd"], "16.00")
        self.assertEqual(transport.report()["attempts"], before)
        self.call(transport, {"another": True})
        self.assertEqual(transport.authorize_additional_spend("16", "user-approved-16"), record)
        self.assertEqual(len(transport.report()["budget_authorizations"]), 1)
        self.assertEqual(transport.report()["remaining_usd"], "15.99")
        with self.assertRaises(pilot.BudgetError):
            transport.authorize_additional_spend("17", "user-approved-16")
        resumed = pilot.BudgetedTransport(self.directory, prices=PRICES)
        self.assertEqual(resumed.report()["cap_usd"], "16.01")

    def test_authorized_extension_preserves_ambiguous_charges_and_exceeds_initial_cap(self):
        transport = self.transport(response(status=500))
        with self.assertRaises(pilot.TransportError):
            self.call(transport)
        before = transport.report()
        transport.authorize_additional_spend("30", "explicit-user-authorization")
        resumed = pilot.BudgetedTransport(self.directory, prices=PRICES)
        self.assertEqual(resumed.report()["attempts"], before["attempts"])
        self.assertEqual(Decimal(resumed.report()["remaining_usd"]), 30)
        self.assertGreater(Decimal(resumed.report()["cap_usd"]), 25)

    def test_unauthorized_or_malformed_extended_ledger_is_rejected(self):
        self.transport()
        path = self.directory / "ledger.json"
        original = json.loads(path.read_text())
        for records in ([], [{"id": "made-up"}], "invalid"):
            ledger = {**original, "cap_usd": "30", "budget_authorizations": records}
            path.write_text(json.dumps(ledger))
            with self.assertRaises(pilot.BudgetError):
                pilot.BudgetedTransport(self.directory, prices=PRICES)

    def test_reasoning_override_is_local_and_changes_cache_identity(self):
        transport = self.transport()
        self.call(transport)
        self.call(transport, reasoning_effort="medium")
        self.call(transport, reasoning_effort="medium")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0]["json"]["reasoning"], {"effort": "high"})
        self.assertEqual(self.calls[1]["json"]["reasoning"], {"effort": "medium"})
        self.assertEqual(pilot.llm.role_defaults("ms_planner")["effort"], "high")
        with self.assertRaises(ValueError):
            self.call(transport, reasoning_effort="invalid")
        self.assertEqual(len(self.calls), 2)

    def test_allowance_cannot_unhalt_or_extend_inflight_requests(self):
        transport = self.transport()
        path = self.directory / "ledger.json"
        ledger = json.loads(path.read_text())
        ledger["halted"] = True
        path.write_text(json.dumps(ledger))
        with self.assertRaises(pilot.BudgetError):
            transport.authorize_additional_spend(16, "halted")
        ledger["halted"] = False
        ledger["attempts"] = [{"status": "reserved", "charged_usd": "1"}]
        path.write_text(json.dumps(ledger))
        with self.assertRaises(pilot.BudgetError):
            transport.authorize_additional_spend(16, "inflight")
        self.assertEqual(json.loads(path.read_text()), ledger)

    def test_http_key_limit_has_actionable_sanitized_diagnostic(self):
        transport = self.transport(httpx.Response(402, json={"error": {
            "message": "secret-provider-message", "metadata": {"limit_source": "openrouter_key_limit"}}}))
        with self.assertRaisesRegex(pilot.TransportError, "HTTP 402; OpenRouter API-key spending limit") as error:
            self.call(transport)
        self.assertNotIn("secret-provider-message", str(error.exception))
        attempt = transport.report()["attempts"][0]
        self.assertEqual(attempt["charged_usd"], attempt["reservation_usd"])
        self.assertEqual(len(self.calls), 1)

    def test_insufficient_budget_never_sends(self):
        transport = self.transport(budget=0.001)
        with self.assertRaises(pilot.BudgetError):
            self.call(transport)
        self.assertFalse(self.calls)
        self.assertFalse(transport.report()["attempts"])

    def test_overspend_halts_future_calls_and_retains_earlier_success(self):
        transport = self.transport(budget=0.1)
        self.call(transport)
        expensive = self.transport(response(cost=0.2), budget=0.1)
        with self.assertRaises(pilot.BudgetError):
            self.call(expensive, {"expensive": True})
        self.assertTrue(expensive.report()["halted"])
        self.assertEqual(expensive.report()["committed_usd"], "0.21")
        with self.assertRaises(pilot.BudgetError):
            self.call(self.transport(budget=0.1), {"new": True})
        self.assertEqual(self.call(expensive), {"ok": True})
        self.assertEqual(len(self.calls), 2)

    def test_reservation_is_durable_before_http_and_conservative(self):
        def post(*args, **kwargs):
            ledger = json.loads((self.directory / "ledger.json").read_text())
            attempt = ledger["attempts"][0]
            self.assertEqual(attempt["status"], "reserved")
            byte_count = len(pilot._json(kwargs["json"]).encode("utf-8"))
            self.assertGreater(attempt["input_token_bound"], byte_count)
            expected = Decimal(attempt["input_token_bound"]) * Decimal("0.000004") + Decimal("0.024")
            self.assertEqual(Decimal(attempt["reservation_usd"]), expected)
            return response()
        transport = pilot.BudgetedTransport(self.directory, prices=PRICES, http_post=post)
        self.call(transport, {"unicode": "नमस्ते"})

    def test_price_and_cap_validation(self):
        for cap in (float("nan"), float("inf"), -1, True):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                self.transport(budget=cap)
        for key, value in (("prompt", -1), ("completion", "NaN"), ("image", "1"), ("completion", True)):
            prices = deepcopy(PRICES)
            prices[MODEL]["pricing"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.transport(prices=prices)
        for field, value in (("currency", "EUR"), ("verified_max_rates", False), ("source", "")):
            prices = deepcopy(PRICES)
            prices[MODEL][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.transport(prices=prices)
        transport = self.transport(prices={})
        with self.assertRaises(pilot.BudgetError):
            self.call(transport)
        self.assertFalse(self.calls)

    def test_live_price_discovery_bounds_eligible_endpoints_without_auth(self):
        calls = []
        def get(url, **kwargs):
            calls.append((url, kwargs))
            return httpx.Response(200, json={"data": {"endpoints": [
                {"pricing": {"prompt": "0.001", "completion": "0.004", "input_cache_write": "0.005"}},
                {"pricing": {"prompt": "0.003", "completion": "0.002", "input_cache_write": "0.004"}},
            ]}})
        catalog = pilot.fetch_openrouter_prices([MODEL], http_get=get)
        price = catalog["models"][MODEL]
        self.assertEqual(price["pricing"]["prompt"], "0.001")
        self.assertEqual(price["max_price"], {"prompt": 1000.0, "completion": 4000.0})
        self.assertEqual(price["pricing"]["completion"], "0.004")
        self.assertEqual(price["pricing"]["input_cache_write"], "0.005")
        self.assertTrue(price["verified_max_rates"])
        self.assertNotIn("headers", calls[0][1])
        self.assertEqual(calls[0][1]["timeout"], 30)
        self.transport(prices=catalog)

    def test_discovery_rejects_missing_or_unsupported_prices(self):
        for endpoints in ([], [{"pricing": {"prompt": "0"}}], [{"pricing": {"prompt": "0", "completion": "0", "unknown_charge": "1"}}]):
            with self.subTest(endpoints=endpoints), self.assertRaises(pilot.TransportError):
                pilot.fetch_openrouter_prices([MODEL], http_get=lambda *a, **k: httpx.Response(200, json={"data": {"endpoints": endpoints}}))

    def test_cross_process_reservations_do_not_overcommit(self):
        context = multiprocessing.get_context("spawn")
        queue, release = context.Queue(), context.Event()
        workers = [context.Process(target=concurrent_worker, args=(str(self.directory), queue, release)) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            initial = {queue.get(timeout=15), queue.get(timeout=15)}
            self.assertEqual(initial, {"sent", "blocked"})
            release.set()
            self.assertEqual(queue.get(timeout=15), "held")
            for worker in workers:
                worker.join(15)
                self.assertEqual(worker.exitcode, 0)
            ledger = json.loads((self.directory / "ledger.json").read_text())
            self.assertEqual(len(ledger["attempts"]), 1)
            self.assertEqual(Decimal(ledger["attempts"][0]["charged_usd"]), Decimal("0.6"))
        finally:
            release.set()
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                worker.join(5)
            queue.close()


if __name__ == "__main__":
    unittest.main()
