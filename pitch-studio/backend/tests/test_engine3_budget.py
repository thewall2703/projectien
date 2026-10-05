from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from backend.master_script.budget import active_budget, compact_global_rules, submit_with_context
from backend.pipeline import llm


def test_budget_covers_imported_chat_and_thread_workers():
    budget = Mock()
    budget.call.return_value = {"ok": True}
    token = active_budget.set(budget)
    try:
        with patch.object(llm, "_post_openrouter", side_effect=AssertionError("unbudgeted")):
            with ThreadPoolExecutor(max_workers=2) as pool:
                result = submit_with_context(pool, llm.chat_json,
                    [{"role": "user", "content": "JSON please"}], role="ms_editor",
                    max_tokens=18000).result()
        assert result == {"ok": True}
        assert budget.call.call_args.kwargs["tokens"] == 8000
        assert budget.call.call_args.kwargs["request_override"]["messages"][-1]["content"] == "JSON please"
    finally:
        active_budget.reset(token)
    assert active_budget.get() is None


def test_unknown_format_keeps_everything():
    assert compact_global_rules({"source_globals": "custom rules"}) == "custom rules"


def test_compact_rules_preserves_intro_and_entire_appendix():
    doc = {"source_globals": "RULES\nThe case on one page\nDUPLICATE SECTIONS\nDelivery notes and open items\nALL RESTRICTIONS"}
    assert compact_global_rules(doc) == "RULES\n\nDelivery notes and open items\nALL RESTRICTIONS"


def test_text_pricing_includes_long_context_and_cache_tiers():
    from backend.master_script.budget_transport import _text_endpoint_rates
    rates = _text_endpoint_rates({"prompt": "0.001", "completion": "0.002",
        "web_search": "1", "discount": 0.5, "input_cache_write_1h": "0.005",
        "overrides": [{"min_prompt_tokens": 100, "prompt": "0.003", "completion": "0.004"}]})
    assert rates["prompt"] == "0.003"
    assert rates["completion"] == "0.004"
    assert rates["input_cache_write"] == "0.005"


def test_pricing_excludes_opt_in_tiers():
    import httpx
    from backend.master_script.budget_transport import fetch_openrouter_prices
    endpoints = [{"tag": "openai/flex", "pricing": {"prompt": "0.000005", "completion": "0.000025"}},
                 {"tag": "openai", "pricing": {"prompt": "0.00001", "completion": "0.00005"}}]
    catalog = fetch_openrouter_prices(["test/model"], http_get=lambda *a, **k: httpx.Response(200, json={"data": {"endpoints": endpoints}}))
    assert catalog["models"]["test/model"]["max_price"] == {"prompt": 10, "completion": 50}


def test_parallel_calls_wait_for_reservations_to_settle(tmp_path):
    import threading
    import httpx
    from backend.master_script.budget_transport import BudgetedTransport
    entered, release = threading.Event(), threading.Event()
    calls = []
    prices = {"test/model": {"currency": "USD", "verified_max_rates": True,
        "source": "test", "pricing": {"prompt": "0", "completion": "0.006"}}}
    def post(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}], "usage": {"cost": 0.1}})
    transport = BudgetedTransport(tmp_path, budget_usd=1, prices=prices,
        http_post=post, reservation_wait_seconds=2)
    with patch.object(llm, "role_defaults", return_value={"model": "test/model", "effort": "high", "verbosity": "", "timeout": 1}):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(transport.call, "first", "ms_voice", "system", {"n": 1}, 100)
            assert entered.wait(2)
            second = pool.submit(transport.call, "second", "ms_voice", "system", {"n": 2}, 100)
            # The first call consumes most of the allowance until it settles.
            assert transport.report()["committed_usd"] == "0.600"
            import time
            time.sleep(0.15)
            assert not second.done()
            assert len(calls) == 1
            release.set()
            assert first.result() == {"ok": True}
            assert second.result() == {"ok": True}
    assert len(calls) == 2
    assert float(transport.report()["committed_usd"]) == 0.2


def test_writer_omits_large_provenance_but_preserves_claims():
    import json
    from backend.master_script import voice
    card = {"id": 4, "claim": "Approved story", "figure": "12", "figure_label": "terminals",
            "checkability": "source material" * 10000}
    original = dict(card)
    with patch.object(voice, "chat_json", return_value={"text": "Draft"}) as chat:
        voice.write_section({"section_id": "4D"}, cards=[card], locked={}, style_guide="", pratham_block="")
    payload = json.loads(chat.call_args.args[0][-1]["content"])
    assert len(json.dumps(payload)) < 2000
    assert payload["cards"][0]["claim"] == card["claim"]
    assert payload["cards"][0]["figure"] == "12"
    assert card == original  # Full provenance remains intact for the source auditor.
