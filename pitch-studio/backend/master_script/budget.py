"""Per-generation Engine 3 accounting, including child-thread requests."""
from contextvars import ContextVar, copy_context
from functools import wraps

active_budget = ContextVar("engine3_budget", default=None)
OUTPUT_LIMITS = {"ms_planner": 6000, "ms_evidence": 4500,
                 "ms_voice": 8000, "ms_editor": 8000}


def submit_with_context(pool, function, *args, **kwargs):
    return pool.submit(copy_context().run, function, *args, **kwargs)


def budgeted_generation(function):
    @wraps(function)
    def run(db, target, set_status, *, dry_run=False):
        if dry_run or active_budget.get() is not None:
            return function(db, target, set_status, dry_run=dry_run)
        from backend.config import DATA_DIR, settings
        from backend.master_script.budget_transport import BudgetedTransport, fetch_openrouter_prices
        from backend.pipeline import llm
        roles = ("ms_planner", "ms_evidence", "ms_voice", "ms_editor")
        models = {llm.role_defaults(role)["model"] for role in roles}
        models.add(settings.openrouter_model)
        if settings.ms_editor_fallback_model:
            models.add(settings.ms_editor_fallback_model)
        # Re-entry for the same generation shares its persisted allowance and cache.
        directory = DATA_DIR / "engine3_runs" / (type(target).__name__ + "-" + str(target.id))
        try:
            transport = BudgetedTransport(directory, budget_usd=12,
                prices=fetch_openrouter_prices(sorted(models)))
        except Exception as exc:
            target.error = "Engine 3 budget setup failed: " + str(exc)
            set_status("failed")
            db.commit()
            return None
        token = active_budget.set(transport)
        try:
            return function(db, target, set_status, dry_run=False)
        finally:
            active_budget.reset(token)
    return run


def compact_global_rules(doc):
    # Keep opening rules and the complete delivery/restrictions appendix.
    # Section prose and audience tables are supplied separately, once.
    raw = doc.get("source_globals", "")
    start = raw.find("The case on one page")
    end = raw.find("Delivery notes and open items")
    if start < 0 or end < start:
        return raw  # Unknown imports must not silently lose instructions.
    return raw[:start] + "\n" + raw[end:]
