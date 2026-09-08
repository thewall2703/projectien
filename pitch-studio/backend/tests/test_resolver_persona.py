from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import Recipe
from backend.pipeline.resolver import is_valid_sequence, parse_sequence, resolve_recipe


class ResolverPersonaTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        self.db = Session()
        self.db.add_all(
            [
                Recipe(
                    ref="A1-1",
                    audience_label="School student",
                    audience_cluster="A",
                    duration="T1",
                    channel="CH2",
                    intent="I2",
                    priority="P0",
                    module_sequence="M01>M04>M06>M12>M14",
                    word_budget=280,
                ),
                Recipe(
                    ref="A8-1",
                    audience_label="Family business",
                    audience_cluster="A",
                    duration="T1",
                    channel="CH2",
                    intent="I2",
                    priority="P0",
                    module_sequence="M04>M08>M11>M14",
                    word_budget=280,
                ),
                Recipe(
                    ref="B7-2",
                    audience_label="Team lead briefing",
                    audience_cluster="B",
                    duration="T3",
                    channel="CH3",
                    intent="I6",
                    priority="P0",
                    module_sequence="Fullcanon,deliverynotesattached",
                    word_budget=1400,
                ),
            ]
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_recipe_ref_picks_exact_persona(self) -> None:
        resolved = resolve_recipe(self.db, "A", "T1", "CH2", "I2", recipe_ref="A8-1")
        self.assertEqual(resolved.ref, "A8-1")
        self.assertEqual(resolved.module_sequence, ["M04", "M08", "M11", "M14"])

    def test_every_valid_ref_is_reachable(self) -> None:
        recipes = self.db.query(Recipe).all()
        for recipe in recipes:
            sequence = parse_sequence(recipe.module_sequence)
            if not is_valid_sequence(sequence):
                continue
            resolved = resolve_recipe(
                self.db,
                recipe.audience_cluster,
                recipe.duration,
                recipe.channel,
                recipe.intent,
                recipe_ref=recipe.ref,
            )
            self.assertEqual(resolved.ref, recipe.ref)
            self.assertEqual(resolved.module_sequence, sequence)

    def test_non_module_sequence_is_invalid(self) -> None:
        recipe = self.db.query(Recipe).filter(Recipe.ref == "B7-2").one()
        self.assertFalse(is_valid_sequence(parse_sequence(recipe.module_sequence)))
