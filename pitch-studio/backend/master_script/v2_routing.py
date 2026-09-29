"""Source-backed PG routing for the v2 introduction, with auditable provenance."""


def apply_v2_routing(doc: dict) -> dict:
    if doc.get('version') != 'v2':
        return doc
    raw = doc.get('source_globals', '')
    if 'Audience routing' not in raw or 'PGP SMG' not in raw:
        return doc  # Partial fixtures/imports cannot establish source-backed routing.
    start = raw.index('Audience routing')
    end = raw.index('PGP SMG', start)
    # Preserve the actual table, including its deliberate slide-order changes.
    doc['audience_routing_source'] = raw[start:end]
    for route in doc['routing']:
        if route['id'] != 'pg_students':
            route.setdefault('provenance', 'builtin_fallback; consult source_globals for v2 routing')
            continue
        route.update({
            'provenance': 'Master Script v2, PG/Exec audience table and sections 3, 5, 7',
            'open_with': "Wouldn't you rather learn business by actually doing it?",
            'ask': 'Talk to the admissions office / visit campus; apply',
            'section_order': ['1', '2', '3', '5', '4A', '4B', '4C', '4D', '4E', '4F', '6', '7'],
            'required_coverage': {
                '3': 'PlaySuper founders joined at twenty-seven; age relevance from slide 17.',
                '5': 'Career transitions by function and reported 3.03x average rise over pre-programme salaries, with cohort scope.',
                '7': 'Sixteen-month TBM for experienced professionals; distinguish twenty-four-month Young Leaders Cohort for fresh graduates; one admissions action.',
            },
        })
    return doc
