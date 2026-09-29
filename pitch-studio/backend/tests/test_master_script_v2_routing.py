import copy
import unittest

from backend.master_script.source import load_master_script, resolve_route
from backend.master_script.v2_routing import apply_v2_routing


class V2AudienceRoutingTests(unittest.TestCase):
    def test_pg_outcomes_precede_learning_model(self):
        doc = load_master_script()
        route = resolve_route(doc, deck_use_case='pg_students', duration='T5')
        self.assertLess(route.sections.index('5'), route.sections.index('4A'))
        self.assertIn('admissions', route.ask)
        self.assertNotIn('48 hours', route.ask)

    def test_v2_has_source_provenance_and_required_audience_coverage(self):
        doc = apply_v2_routing(copy.deepcopy(load_master_script().raw))
        route = next(r for r in doc['routing'] if r['id'] == 'pg_students')
        self.assertIn('PG/', doc['audience_routing_source'])
        self.assertIn('PlaySuper', route['required_coverage']['3'])
        self.assertIn('3.03x', route['required_coverage']['5'])
        self.assertIn('Sixteen-month', route['required_coverage']['7'])
        self.assertEqual(apply_v2_routing(copy.deepcopy(doc)), doc)
