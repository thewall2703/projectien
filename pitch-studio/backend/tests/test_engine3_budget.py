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
