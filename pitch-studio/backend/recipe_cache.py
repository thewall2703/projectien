from __future__ import annotations

import time

from sqlalchemy.orm import Session

from backend.models import Recipe
from backend.pipeline.resolver import is_valid_sequence, parse_sequence
from backend.schemas import RecipeOption

_TTL_SECONDS = 120.0
_cached: list[RecipeOption] | None = None
_cached_at = 0.0


def invalidate_recipe_cache() -> None:
    global _cached, _cached_at
    _cached = None
    _cached_at = 0.0


def list_recipe_options(db: Session) -> list[RecipeOption]:
    global _cached, _cached_at
    now = time.monotonic()
    if _cached is not None and now - _cached_at < _TTL_SECONDS:
        return _cached
    options: list[RecipeOption] = []
    for recipe in db.query(Recipe).order_by(Recipe.ref).all():
        sequence = parse_sequence(recipe.module_sequence)
        options.append(
            RecipeOption(
                ref=recipe.ref,
                audience_label=recipe.audience_label,
                audience_cluster=recipe.audience_cluster,
                duration=recipe.duration,
                channel=recipe.channel,
                intent=recipe.intent,
                module_sequence=recipe.module_sequence,
                priority=recipe.priority or "P2",
                word_budget=recipe.word_budget or 0,
                valid=is_valid_sequence(sequence),
            )
        )
    _cached = options
    _cached_at = now
    return options
