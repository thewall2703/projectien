from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend.models import Recipe

_MODULE_RE = re.compile(r"^M\d+$")

WORD_BUDGETS = {
    "T0": 90,
    "T1": 280,
    "T2": 700,
    "T3": 1400,
    "T4": 3500,
    "T5": 4500,
}

DURATION_ORDER = ["T0", "T1", "T2", "T3", "T4", "T5"]
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

FALLBACK_BY_INTENT = {
    "I1": ["M01", "M04", "M14"],
    "I2": ["M01", "M04", "M07", "M05", "M14"],
    "I3": ["M13", "M07", "M02", "M14"],
    "I4": ["M01", "M02", "M08", "M12", "M14"],
    "I5": ["M02", "M07", "M11", "M14"],
    "I6": ["M02", "M07", "M13", "M14"],
}


@dataclass
class ResolvedRecipe:
    ref: str
    module_sequence: list[str]
    word_budget: int


def parse_sequence(raw: str) -> list[str]:
    return [part.strip() for part in raw.replace(" ", "").split(">") if part.strip()]


def is_valid_sequence(seq: list[str]) -> bool:
    return bool(seq) and all(_MODULE_RE.match(part) for part in seq)


def _priority_key(recipe: Recipe) -> int:
    return PRIORITY_ORDER.get(recipe.priority or "P2", 9)


def _pick(recipes: list[Recipe]) -> Recipe | None:
    if not recipes:
        return None
    return sorted(recipes, key=_priority_key)[0]


def _compose_fallback(cluster: str, duration: str, intent: str, temperature: str) -> list[str]:
    sequence = list(FALLBACK_BY_INTENT.get(intent, FALLBACK_BY_INTENT["I1"]))
    if duration in {"T0", "T1"}:
        head = [mid for mid in sequence if mid != "M14"][:2]
        sequence = head + ["M14"]
    if duration in {"T3", "T4", "T5"} and "M09" not in sequence:
        if "M14" in sequence:
            sequence.insert(sequence.index("M14"), "M09")
        else:
            sequence.append("M09")
    if temperature == "X4" and "M13" not in sequence:
        if "M14" in sequence:
            sequence.insert(sequence.index("M14"), "M13")
        else:
            sequence.append("M13")
    _ = cluster
    return sequence


def resolve_recipe(
    db: Session,
    audience_cluster: str,
    duration: str,
    channel: str,
    intent: str,
    temperature: str = "X2",
    recipe_ref: str | None = None,
) -> ResolvedRecipe:
    if recipe_ref:
        chosen = db.query(Recipe).filter(Recipe.ref == recipe_ref).first()
        if chosen:
            seq = parse_sequence(chosen.module_sequence)
            if is_valid_sequence(seq):
                return ResolvedRecipe(
                    ref=chosen.ref,
                    module_sequence=seq,
                    word_budget=chosen.word_budget or WORD_BUDGETS.get(chosen.duration, 280),
                )
    recipes = db.query(Recipe).all()
    exact = [
        recipe
        for recipe in recipes
        if recipe.audience_cluster == audience_cluster
        and recipe.duration == duration
        and recipe.channel == channel
        and recipe.intent == intent
    ]
    chosen = _pick(exact)
    if chosen is None:
        relaxed = [
            recipe
            for recipe in recipes
            if recipe.audience_cluster == audience_cluster
            and recipe.duration == duration
            and recipe.intent == intent
        ]
        chosen = _pick(relaxed)
    if chosen is None:
        same_intent = [
            recipe
            for recipe in recipes
            if recipe.audience_cluster == audience_cluster and recipe.intent == intent
        ]
        if same_intent:
            duration_index = DURATION_ORDER.index(duration) if duration in DURATION_ORDER else 1

            def distance(recipe: Recipe) -> tuple[int, int]:
                other = DURATION_ORDER.index(recipe.duration) if recipe.duration in DURATION_ORDER else 99
                return (abs(other - duration_index), _priority_key(recipe))

            chosen = sorted(same_intent, key=distance)[0]
    if chosen is None:
        sequence = _compose_fallback(audience_cluster, duration, intent, temperature)
        return ResolvedRecipe(
            ref="AUTO",
            module_sequence=sequence,
            word_budget=WORD_BUDGETS.get(duration, 280),
        )
    budget = chosen.word_budget or WORD_BUDGETS.get(duration, 280)
    return ResolvedRecipe(
        ref=chosen.ref,
        module_sequence=parse_sequence(chosen.module_sequence),
        word_budget=budget,
    )
