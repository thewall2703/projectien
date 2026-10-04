"""Isolated, synchronous benchmark transport; never retries or changes models.

Prices are USD/token (request is USD/request), NOT USD/million tokens. Supply
{model: {currency: 'USD', verified_max_rates: True, source: '...', pricing:
{prompt: ..., completion: ..., input_cache_read: ..., input_cache_write: ...}}}
or the catalog returned by fetch_openrouter_prices(). The explicit attestation
means rates bound ALL eligible providers, including cache surcharges. Optional
request/internal_reasoning rates default to zero. Unknown nonzero rate types
are rejected. Live discovery uses provider endpoints, not advertised minima.

Use one directory for ALL benchmark and comparison calls: independent directories
cannot share a cap. Outstanding/ambiguous requests permanently retain their
reservation (no automatic release). Reservations are conservative estimates, not
a provider-enforced spending limit; an unexpectedly larger reported bill halts
all subsequent paid calls. Cache hits remain available after a halt.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import math
import os
import re
from pathlib import Path
import time
from typing import Any, Callable
from urllib.parse import quote
import uuid

import httpx

from backend.pipeline import llm

TRANSPORT_VERSION = "engine3-budget-v1"
HARD_CAP_USD = Decimal("25")
_RATE_KEYS = {"prompt", "completion", "input_cache_read", "input_cache_write", "request", "internal_reasoning"}


class TransportError(llm.LLMError):
    """Request failed; consult persisted artifacts, not exception response text."""


class BudgetError(TransportError):
    pass


def _amount(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"Invalid {label}")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"Invalid {label}") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"Invalid {label}")
    return result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _normalize_apostrophe_escapes(content: str) -> str:
    return re.sub(r"\\[\s\S]", lambda m: "'" if m.group() == "\\'" else m.group(), content)


def _extract_pilot_json(content: str) -> dict:
    """Recover only the observed non-JSON apostrophe escape, never infer content.

    Scan escape pairs so a valid literal backslash before an apostrophe is not
    changed. Raw provider output is still persisted. Other malformed JSON fails.
    """
    try:
        return llm._extract_json(content)
    except (ValueError, llm.LLMError):
        normalized = _normalize_apostrophe_escapes(content)
        if normalized == content:
            raise
        return llm._extract_json(normalized)


def _atomic(path: Path, value: Any) -> None:
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as file:
            file.write(_json(value))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def _rates(pricing: dict) -> dict[str, str]:
    if not isinstance(pricing, dict) or not {"prompt", "completion"} <= pricing.keys():
        raise ValueError("Missing prompt/completion pricing")
    for key, value in pricing.items():
        rate = _amount(value, "pricing rate")
        if key not in _RATE_KEYS and rate:
            raise ValueError("Unsupported nonzero pricing dimension")
    return {key: str(_amount(pricing.get(key, 0), "pricing rate")) for key in sorted(_RATE_KEYS)}


def _text_endpoint_rates(pricing):
    """Rates for text-only calls; include every long-context tier conservatively."""
    ignored = {"web_search", "image", "audio", "input_audio_cache", "discount"}
    base = {k: v for k, v in pricing.items() if k not in ignored | {"overrides", "input_cache_write_1h"}}
    if "input_cache_write_1h" in pricing:
        base["input_cache_write"] = str(max(_amount(base.get("input_cache_write", 0), "cache"),
                                             _amount(pricing["input_cache_write_1h"], "cache")))
    tiers = [_rates(base)]
    for override in pricing.get("overrides", []):
        extra = {k: v for k, v in override.items() if k != "min_prompt_tokens"}
        tiers.append(_rates({**base, **extra}))
    return {k: str(max(Decimal(t[k]) for t in tiers)) for k in _RATE_KEYS}


def fetch_openrouter_prices(models: list[str], *, http_get: Callable | None = None) -> dict:
    """GET live endpoint maxima without credentials or paid inference calls.

    Fails closed on missing endpoints/prices or unsupported nonzero dimensions.
    Endpoint snapshots cannot guarantee future rates; fetch immediately before a
    run. Currency is USD per OpenRouter's pricing API contract.
    """
    get = http_get or httpx.get
    catalog = {"currency": "USD", "retrieved_at": datetime.now(timezone.utc).isoformat(), "models": {}}
    for model in models:
        if not isinstance(model, str) or not model or model.count("/") != 1:
            raise ValueError("Use an explicit author/model ID for pricing discovery")
        url = "https://openrouter.ai/api/v1/models/" + quote(model, safe="/") + "/endpoints"
        response = get(url, timeout=30)
        if response.status_code != 200:
            raise TransportError("Pricing discovery HTTP failure")
        try:
            endpoints = response.json()["data"]["endpoints"]
            if not isinstance(endpoints, list) or not endpoints:
                raise ValueError("No pricing endpoints")
            # Route only at the cheapest available base text price. Do not
            # change models or allow a premium endpoint to consume the allowance.
            endpoints = [e for e in endpoints if str(e.get("tag", "")).rsplit("/", 1)[-1] not in {"flex", "fast", "ultrafast", "priority"}]
            if not endpoints:
                raise ValueError("No standard-tier endpoints")
            cheapest = min(endpoints, key=lambda e: _amount(e["pricing"]["prompt"], "prompt") + _amount(e["pricing"]["completion"], "completion"))
            ceiling = {k: _amount(cheapest["pricing"][k], k) for k in ("prompt", "completion")}
            eligible = [e for e in endpoints if all(_amount(e["pricing"][k], k) <= ceiling[k] for k in ceiling)]
            rates = [_text_endpoint_rates(endpoint["pricing"]) for endpoint in eligible]
        except (KeyError, TypeError, ValueError):
            raise TransportError("Unusable endpoint pricing catalog") from None
        catalog["models"][model] = {
            "currency": "USD", "verified_max_rates": True, "source": url,
            "retrieved_at": catalog["retrieved_at"], "endpoint_count": len(eligible),
            "max_price": {k: float(v * 1000000) for k, v in ceiling.items()},
            "pricing": {key: str(max(Decimal(rate[key]) for rate in rates)) for key in _RATE_KEYS},
        }
    return catalog


class BudgetedTransport:
    def __init__(self, directory, budget_usd=None, *, prices: dict, http_post: Callable | None = None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.http_post = http_post or httpx.post
        cap = None if budget_usd is None else _amount(budget_usd, "budget cap")
        if cap is not None and cap > HARD_CAP_USD:
            raise BudgetError("Use an explicit audited authorization to exceed $25")
        if not isinstance(prices, dict):
            raise ValueError("Explicit verified pricing is required")
        if "models" in prices and prices.get("currency") != "USD":
            raise ValueError("Pricing currency must be USD")
        entries = prices.get("models", prices)
        if not isinstance(entries, dict):
            raise ValueError("Invalid pricing catalog")
        self.prices = {}
        for model, entry in entries.items():
            if not isinstance(model, str) or not isinstance(entry, dict):
                raise ValueError("Invalid model pricing")
            if entry.get("currency") != "USD" or entry.get("verified_max_rates") is not True or not entry.get("source"):
                raise ValueError("Pricing needs USD currency, source, and verified_max_rates=True")
            self.prices[model] = {**entry, "pricing": _rates(entry.get("pricing"))}
        # Validate metadata too; do not persist NaN or mutable caller-owned objects.
        self.prices = json.loads(_json(self.prices))
        with self._locked():
            path = self.directory / "ledger.json"
            if path.exists():
                ledger = self._load()
                if cap is not None:
                    if cap > Decimal(ledger["cap_usd"]):
                        raise BudgetError("A persisted budget cap cannot be increased without authorization")
                    ledger["cap_usd"] = str(cap)
                    ledger["halted"] = ledger["halted"] or self._total(ledger) > cap
            else:
                cap = HARD_CAP_USD if cap is None else cap
                ledger = {"schema": 1, "currency": "USD", "cap_usd": str(cap), "halted": False, "attempts": [], "cache_hits": []}
            self._save(ledger)

    @contextmanager
    def _locked(self):
        with (self.directory / "budget.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _load(self):
        try:
            ledger = json.loads((self.directory / "ledger.json").read_text())
            cap = _amount(ledger["cap_usd"], "persisted cap")
            ceiling = HARD_CAP_USD
            authorizations = ledger.get("budget_authorizations", [])
            if not isinstance(authorizations, list):
                raise ValueError("Invalid authorizations")
            seen = set()
            for authorization in authorizations:
                ident = authorization["id"]
                if not isinstance(ident, str) or not ident.strip() or ident in seen:
                    raise ValueError("Invalid authorization ID")
                seen.add(ident)
                before = _amount(authorization["previous_cap_usd"], "previous cap")
                committed = _amount(authorization["committed_usd"], "committed")
                allowance = _amount(authorization["additional_spend_usd"], "allowance")
                after = _amount(authorization["cap_usd"], "authorized cap")
                if before > ceiling or committed > before or allowance <= 0 or after != committed + allowance:
                    raise ValueError("Invalid authorization accounting")
                ceiling = after
            if ledger["schema"] != 1 or ledger["currency"] != "USD" or cap > ceiling or not isinstance(ledger["halted"], bool):
                raise ValueError("Invalid ledger")
            if not isinstance(ledger["attempts"], list) or not isinstance(ledger["cache_hits"], list):
                raise ValueError("Invalid ledger")
            self._total(ledger)
            return ledger
        except (KeyError, TypeError, ValueError):
            raise BudgetError("Invalid budget ledger; refusing paid calls") from None

    @staticmethod
    def _total(ledger):
        return sum((_amount(item["charged_usd"], "ledger charge") for item in ledger["attempts"]), Decimal(0))

    def _save(self, ledger):
        _atomic(self.directory / "ledger.json", ledger)

    def report(self) -> dict:
        """Snapshot includes paid/held charges, outstanding attempts and cache hits."""
        with self._locked():
            ledger = self._load()
            ledger["committed_usd"] = str(self._total(ledger))
            ledger["remaining_usd"] = str(max(Decimal(0), Decimal(ledger["cap_usd"]) - self._total(ledger)))
            return ledger

    def authorize_additional_spend(self, amount, authorization_id):
        """Explicit user-approved allowance from current commitment, not a reset.

        Replaying the same authorization is idempotent. Ambiguous charges and
        previous attempts remain intact; a halted ledger cannot be unhalted here.
        """
        allowance = _amount(amount, "additional spend")
        if allowance <= 0 or not isinstance(authorization_id, str) or not authorization_id.strip():
            raise ValueError("Positive allowance and explicit authorization ID required")
        with self._locked():
            ledger = self._load()
            records = ledger.setdefault("budget_authorizations", [])
            previous = next((r for r in records if r["id"] == authorization_id), None)
            if previous:
                if Decimal(previous["additional_spend_usd"]) != allowance:
                    raise BudgetError("Authorization ID already used with a different amount")
                return previous
            if ledger["halted"] or any(a["status"] == "reserved" for a in ledger["attempts"]):
                raise BudgetError("Cannot extend a halted ledger or one with in-flight requests")
            committed = self._total(ledger)
            record = {"id": authorization_id, "authorized_at": datetime.now(timezone.utc).isoformat(),
                      "previous_cap_usd": ledger["cap_usd"], "committed_usd": str(committed),
                      "additional_spend_usd": str(allowance), "cap_usd": str(committed + allowance)}
            records.append(record)
            ledger["cap_usd"] = record["cap_usd"]
            self._save(ledger)
            return record

    def _cached(self, path, fingerprint, ledger):
        try:
            cached = json.loads(path.read_text())
            attempt = next(item for item in ledger["attempts"] if item["id"] == cached["attempt_id"])
            if cached["fingerprint"] != fingerprint or attempt["status"] != "success" or attempt["fingerprint"] != fingerprint:
                return None
            result = _extract_pilot_json(cached["content"])
            if result != cached["result"]:
                return None
            return result
        except (OSError, ValueError, KeyError, TypeError, StopIteration, llm.LLMError):
            return None

    def call(self, name, role, system, payload, tokens=12000, *, response_format=None, reasoning_effort=None, request_override=None, timeout_override=None):
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
            raise ValueError("tokens must be a positive integer")
        request = llm._request_payload([
            {"role": "system", "content": system},
            {"role": "user", "content": _json(payload)},
        ], role=role, max_tokens=tokens)
        if reasoning_effort is not None:
            if reasoning_effort not in {"low", "medium", "high"}:
                raise ValueError("Unsupported pilot reasoning effort")
            request["reasoning"] = {"effort": reasoning_effort}
        if response_format is not None:
            # Include the exact schema in reservations and content-addressed caching.
            request["response_format"] = json.loads(_json(response_format))
        if request_override is not None:
            request = json.loads(_json(request_override))
            request["max_tokens"] = tokens
        # No provider/model fallback, retries, tools, or streaming in this transport.
        request["provider"] = {"allow_fallbacks": False}
        request["usage"] = {"include": True}
        model = request["model"]
        price = self.prices.get(model)
        if price is None:
            raise BudgetError("Model has no verified maximum pricing")
        if price.get("max_price"):
            request["provider"]["max_price"] = price["max_price"]
        # This transport prices text only, with no paid search/tools or media.
        if request.get("tools") or request.get("plugins") or any(not isinstance(m.get("content"), str) for m in request["messages"]):
            raise BudgetError("Budgeted Engine 3 requests must be text-only without tools")
        rates = {key: Decimal(value) for key, value in price["pricing"].items()}
        # Entire UTF-8 JSON plus framing allowance, not characters or token heuristics.
        input_bound = len(_json(request).encode("utf-8")) + 1024
        input_rate = rates["prompt"] + max(rates["prompt"], rates["input_cache_read"], rates["input_cache_write"])
        reserve = input_bound * input_rate + tokens * (rates["completion"] + rates["internal_reasoning"]) + rates["request"]
        fingerprint = hashlib.sha256(_json({"version": TRANSPORT_VERSION, "request": request, "source_payload": payload}).encode()).hexdigest()
        cache = self.directory / (fingerprint + ".cache.json")
        timeout = float(timeout_override or llm.role_defaults(role)["timeout"])
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Invalid role timeout")
        timeout = min(timeout, 240.0)
        attempt_id = uuid.uuid4().hex
        started = time.monotonic()
        with self._locked():
            ledger = self._load()
            result = self._cached(cache, fingerprint, ledger)
            if result is not None:
                ledger["cache_hits"].append({"name": str(name), "role": role, "fingerprint": fingerprint, "cost_usd": "0", "latency_seconds": time.monotonic() - started})
                self._save(ledger)
                return result
            if ledger["halted"] or self._total(ledger) + reserve > Decimal(ledger["cap_usd"]):
                raise BudgetError("Budget exhausted or halted; no request sent")
            ledger["attempts"].append({
                "id": attempt_id, "name": str(name), "role": role, "model": model,
                "fingerprint": fingerprint, "status": "reserved", "charged_usd": str(reserve),
                "reservation_usd": str(reserve), "input_token_bound": input_bound,
                "max_tokens": tokens, "price": price, "started_at": datetime.now(timezone.utc).isoformat(),
            })
            self._save(ledger)  # Must be durable BEFORE any network call.
        status, usage, cost, content, result = "ambiguous", {}, None, None, None
        failure_detail = ""
        error = None
        artifact = {"request": request, "fingerprint": fingerprint}
        try:
            response = self.http_post(llm.OPENROUTER_URL, headers=llm._headers(), json=request, timeout=timeout)
            artifact.update(http_status=response.status_code, raw_response=response.text)
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and isinstance(body.get("usage"), dict):
                # Store usage separately from the raw response, never request headers.
                usage = body["usage"]
                value = usage.get("cost")
                if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                    try:
                        cost = _amount(value, "usage cost")
                    except ValueError:
                        pass
            if response.status_code < 200 or response.status_code >= 300:
                status = "http_error"
                failure_detail = f"; HTTP {response.status_code}"
                provider_error = body.get("error") if isinstance(body, dict) else None
                metadata = provider_error.get("metadata") if isinstance(provider_error, dict) else None
                if response.status_code == 402 and isinstance(metadata, dict) and metadata.get("limit_source") == "openrouter_key_limit":
                    failure_detail += "; OpenRouter API-key spending limit exhausted; increase that key's allowance before retrying"
                if (response.status_code == 404 and isinstance(provider_error, dict)
                        and provider_error.get("message") == "No endpoints found that satisfy the max price for this request"
                        and isinstance(metadata, dict)
                        and metadata.get("failed_routing_step") == "Filter by Max Price"):
                    status, cost = "routing_rejected", Decimal(0)
                raise TransportError("HTTP failure; consult ledger")
            if cost is None:
                status = "missing_cost"
                raise TransportError("Missing valid numeric usage.cost; reservation retained")
            status = "invalid_response"
            if not isinstance(body, dict) or body.get("error"):
                raise TransportError("Invalid provider response")
            choices = body.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise TransportError("Missing response choice")
            if choices[0].get("finish_reason") == "length" or choices[0].get("native_finish_reason") in ("length", "max_tokens"):
                status = "length"
                raise TransportError("Output hit token limit; response charged, not cached")
            if choices[0].get("finish_reason") != "stop":
                raise TransportError("Response did not finish successfully")
            content = llm._message_content(body)
            result = _extract_pilot_json(content)
            artifact["json_normalization"] = "apostrophe_escape" if _normalize_apostrophe_escapes(content) != content else None
            _json(result)  # Reject non-finite JSON values before marking success.
            status = "success"
        except Exception:
            # Never include exception text: HTTP exceptions may contain auth/URLs.
            error = TransportError(f"Benchmark call failed ({status}{failure_detail}); see raw artifact and ledger")
        finally:
            # Preserve raw provider bytes as text even if usage contains NaN/Infinity.
            artifact["status"] = status
            try:
                _atomic(self.directory / (attempt_id + ".raw.json"), artifact)
            finally:
                with self._locked():
                    ledger = self._load()
                    attempt = next(item for item in ledger["attempts"] if item["id"] == attempt_id)
                    charge = reserve if cost is None else cost
                    if status == "http_error":
                        charge = max(reserve, charge)
                    attempt.update(status=status, charged_usd=str(charge), actual_cost_usd=None if cost is None else str(cost),
                                   latency_seconds=time.monotonic() - started,
                                   usage={key: value for key, value in usage.items() if key.endswith("tokens") and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0})
                    ledger["halted"] = ledger["halted"] or self._total(ledger) > Decimal(ledger["cap_usd"])
                    self._save(ledger)
                    if ledger["halted"]:
                        error = BudgetError("Reported spend exceeded cap; transport halted")
                    if error is None and status == "success":
                        _atomic(cache, {"fingerprint": fingerprint, "attempt_id": attempt_id, "content": content, "result": result})
        if error is not None:
            raise error from None
        return result
