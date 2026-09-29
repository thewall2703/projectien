"""Explicit user corrections retained when a source PDF is replaced."""
import re


def apply_approved_overrides(doc: dict) -> dict:
    sections = {s['id']: s for s in doc['sections']}
    faculty = sections['4B']
    for section in doc['sections']:
        section['original_source_text'] = section.get('source_text', '')
    old = 'Forty practitioners, thirty full-time PhD faculty, thirty visiting international.'
    new = ('Forty percent practitioners, thirty percent full-time PhD faculty, '
           'thirty percent visiting international faculty.')
    faculty['source_text'] = re.sub(
        r'Forty practitioners,\s*thirty full-time PhD faculty,\s*thirty visiting\s*international\.',
        new, faculty['source_text'])
    for lock in faculty['locked']:
        if lock['text'] == old:
            lock['text'] = new
            lock['authority'] = 'explicit_user_correction'
    doc['numbers_you_may_say'] = re.sub(
        r'Faculty: forty practitioners, thirty full-time PhD, thirty visiting international\.',
        'Faculty: 40% practitioners, 30% full-time PhD faculty, 30% visiting international faculty.',
        doc.get('numbers_you_may_say', ''))
    challenges, labs = sections['4C'], sections['4D']
    # Move the source's Food Lab passage intact; do not invent new source prose.
    match = re.search(r'Slide 35, Food Lab\..*?(?=\nSlide 36|\nSlides 36)', challenges['source_text'], re.S)
    if match:
        passage = match.group(0).strip()
        challenges['source_text'] = challenges['source_text'][:match.start()] + challenges['source_text'][match.end():]
        labs['source_text'] += '\n\n' + passage + '\n'
    challenges['source_text'] = re.sub(r'(?m)^3\. Food Lab:.*\n?', '', challenges['source_text'])
    challenges['premises'] = [p for p in challenges['premises'] if not re.search(r'^(?:3\. )?Food Lab:', p)]
    labs['premises'].append('Food Lab: build, launch and run a real cloud kitchen. No simulations.')
    challenges['slides'] = [n for n in challenges['slides'] if n != 35]
    labs['slides'] = sorted(set([35, *labs['slides']]))
    doc['user_overrides'] = [
        {'subject': 'faculty composition', 'correction': new},
        {'subject': 'Food Lab', 'correction': 'A lab in section 4D; retain the approved story and evidence.'},
    ]
    doc['original_source_globals'] = doc.get('source_globals', '')
    doc['source_globals'] = re.sub(
        r'Faculty: forty practitioners, thirty full-time PhD, thirty\s*visiting international\.',
        'Faculty: 40% practitioners, 30% full-time PhD faculty, 30% visiting international faculty.',
        doc.get('source_globals', ''))
    return doc
