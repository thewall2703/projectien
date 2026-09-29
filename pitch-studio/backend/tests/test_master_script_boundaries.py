"""The third generator cannot silently cross its source/approval boundaries."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from backend.database import Base
from backend.models import MasterNoun, EvidenceCard
from backend.master_script.evidence import retrieve_candidates, _rerank, cards_for_plan, fulfil_requests
from backend.master_script.source import load_master_script, resolve_route, locked_lines_for_sections
from backend.master_script.planner import _section_brief, plan_sections, _has_section_anchor
from backend.master_script.deck_plan import build_master_deck
from backend.master_script.grounding import audit_grounding


class EvidenceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        for name, approved in [('Makers Lab', True), ('Bloomberg Lab', False)]:
            noun = MasterNoun(canonical=name, approved=approved)
            self.db.add(noun)
            self.db.flush()
            self.db.add(EvidenceCard(noun_id=noun.id, claim=name+' provides equipment',
                                     source_type='report',source_ref='1',checkability=name+' provides equipment'))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def retrieve(self, noun, need='equipment'):
        return retrieve_candidates(self.db, {'noun':noun,'need':need}, embed_fn=lambda _: [[]])

    def test_unapproved_or_unknown_noun_cannot_fall_back(self):
        self.assertEqual(self.retrieve('Bloomberg Lab'), [])
        self.assertEqual(self.retrieve('Unknown Lab', 'Makers Lab equipment'), [])

    def test_general_request_only_returns_approved_nouns(self):
        cards = self.retrieve('')
        self.assertEqual(len(cards), 1)
        self.assertIn('Makers', cards[0]['claim'])

    def test_revoke_takes_effect_immediately(self):
        self.db.query(MasterNoun).update({MasterNoun.approved:False})
        self.db.commit()
        self.assertEqual(self.retrieve(''), [])

    def test_missing_provenance_cannot_be_used(self):
        self.db.query(EvidenceCard).update({EvidenceCard.checkability:''})
        self.db.commit()
        self.assertEqual(self.retrieve('Makers Lab'), [])

    def test_approved_story_is_preferred_and_keeps_provenance(self):
        noun=self.db.query(MasterNoun).filter_by(canonical='Makers Lab').one()
        story=EvidenceCard(noun_id=noun.id,kind='approved_story',claim='Students build and test prototypes.',
                           source_type='human_approved_story',source_ref='story:N0020',
                           checkability=json.dumps({'sources':[{'text':'Students build and test prototypes.'}]}),
                           strength=.95)
        self.db.add(story);self.db.commit()
        cards=self.retrieve('Makers Lab')
        self.assertEqual(cards[0]['id'],story.id)
        self.assertIn('sources',json.loads(cards[0]['checkability']))
        noun.approved=False;self.db.commit()
        self.assertEqual(self.retrieve('Makers Lab'),[])

    def test_failed_or_partial_rerank_does_not_approve_candidates(self):
        cards=self.retrieve('Makers Lab')
        with patch('backend.master_script.evidence.chat_json',side_effect=RuntimeError('offline')):
            self.assertEqual(_rerank({},cards)['verdict'],'insufficient')
        with patch('backend.master_script.evidence.chat_json',return_value={'verdict':'use','cards':[]}):
            result=_rerank({},cards)
            self.assertEqual(result['verdict'],'insufficient')
            self.assertFalse(any(c['verdict']=='use' for c in result['cards']))

    def test_rejected_and_wrong_section_cards_cannot_be_selected(self):
        fulfilled=[{'section_id':'4D','verdict':'use','cards':[{'id':1,'verdict':'insufficient'}, {'id':2,'verdict':'use'}]}]
        plan={'sections':[{'section_id':'1','chosen_cards':[{'card_id':2}]},
                          {'section_id':'4D','chosen_cards':[{'card_id':1}]}]}
        result=cards_for_plan(fulfilled,plan)
        self.assertEqual(result['1'],[])
        self.assertNotIn(1,[c['id'] for c in result['4D']])

    def test_database_session_stays_on_owning_thread(self):
        result=fulfil_requests(self.db,[{'noun':'Makers Lab','need':'equipment','section_id':'4D'}],
                              dry_run=True,embed_fn=lambda _: [[]],follow_up=False)
        self.assertEqual(result[0]['verdict'],'use')


class SourceBoundaryTests(unittest.TestCase):
    def test_active_v2_source_and_pending_locks_survive_import(self):
        from backend.master_script.source import DEFAULT_JSON_PATH
        doc=load_master_script()
        self.assertEqual(doc.version, 'v2')
        self.assertEqual(DEFAULT_JSON_PATH.name, 'master_script.v2.json')
        self.assertEqual(doc.raw['source_pdf'], 'Brand Deck Master Script v2.pdf')
        self.assertTrue(all(s['source_text'].strip() for s in doc.sections.values()))
        self.assertIn('Macro Questions', doc.section('4A')['source_text'])
        self.assertNotIn('Manoj Kohli, Chairman of the Board of Governors', doc.section('7')['source_text'])
        self.assertIn('Manoj Kohli, Chairman of the Board of Governors', doc.raw['source_globals'])
        pending=[row for row in doc.section('4A')['locked'] if row['pending_signoff']]
        self.assertEqual(len(pending), 1)
        self.assertNotIn(pending[0]['id'], locked_lines_for_sections(doc, ['4A']))

    def test_food_lab_is_one_canonical_lab_story(self):
        repo=Path(__file__).resolve().parents[3]
        source=json.loads((repo/'pitch-studio/data/master_script/master_script.v2.json').read_text())
        sections={row['id']:row for row in source['sections']}
        self.assertNotIn('Food Lab', ' '.join(sections['4C']['premises']))
        self.assertIn('Food Lab', ' '.join(sections['4D']['premises']))
        stories=json.loads((repo/'output/master_script/approved_research/stories.json').read_text())['stories']
        self.assertEqual([row['item'] for row in stories].count('Food Lab'),1)
        self.assertNotIn('Food Lab Challenge',[row['item'] for row in stories])
        story=next(row for row in stories if row['item']=='Food Lab')['proposed_story']
        self.assertIn('actual kitchen with permits in place',story)
        self.assertIn('final showcase',story)

    def test_faculty_composition_is_percentages_not_headcounts(self):
        source=Path(__file__).resolve().parents[2]/'data/master_script/master_script.v2.json'
        doc=load_master_script(source)
        section=doc.section('4B')
        locks={row['id']:row['text'] for row in section['locked']}
        self.assertEqual(
            locks['S4B.1'],
            'Forty percent practitioners, thirty percent full-time PhD faculty, '
            'thirty percent visiting international faculty.',
        )
        self.assertNotIn('Forty practitioners,', section['source_text'])

    def test_planner_drops_unanchored_content_and_keeps_source_opening(self):
        doc=load_master_script(Path('/missing/test-master.json'))
        route=resolve_route(doc,duration='T4',deck_use_case='school_fair')
        sid=route.sections[0]
        quote='Students learn through practice with real businesses.'
        doc.sections[sid]['source_text']=quote
        response={'open_with':'Let us diagnose your career gaps.', 'ask':'Book a paid consultation.',
                  'sections':[{'section_id':sid,'script_cues':[
                      {'cue':'Ask why they want to resign.','source_quote':'Invented audience motivation'},
                      {'cue':'Explain learning through practice.','source_quote':quote}],
                      'evidence_requests':[{'noun':'Unknown Lab','need':'Invent a student success'}]}],
                  'evidence_requests':[{'section_id':sid,'noun':'Unknown Lab','need':'Invent a story'}]}
        with patch('backend.master_script.planner.chat_json',return_value=response):
            plan=plan_sections(doc,route,topic_flow=[],word_budget=400)
        self.assertEqual(plan['open_with'],route.open_with)
        self.assertEqual(plan['ask'],route.ask)
        first=plan['sections'][0]
        self.assertEqual(first['heading'],doc.section(sid)['title'])
        self.assertEqual([c['cue'] for c in first['script_cues']],['Explain learning through practice.'])
        self.assertFalse(plan['evidence_requests'])

    def test_source_anchor_cannot_come_from_another_section(self):
        self.assertFalse(_has_section_anchor({'source_quote':'A quote about another learning lab.'},
                                           {'source_text':'Founding facts belong here.'}))

    def test_grounding_audit_requires_complete_checks(self):
        doc=load_master_script(Path('/missing/test-master.json'))
        script={'sections':[{'ms_section_id':'1','text':'An unsupported claim'}], 'cta':'Visit us'}
        with patch('backend.master_script.grounding.chat_json', return_value={'checks':[]}):
            self.assertEqual(audit_grounding(script,doc,{})[0].code,'grounding_unavailable')
        with patch('backend.master_script.grounding.chat_json', return_value={'checks':[
            {'section_id':'1','verdict':'unsupported','issues':['Unsupported claim']},
            {'section_id':'cta','verdict':'supported','issues':[]}
        ]}):
            self.assertEqual(audit_grounding(script,doc,{})[0].code,'unsupported_claim')

    def test_pending_signoff_never_becomes_usable_lock(self):
        doc=load_master_script(Path('/missing/test-master.json'))
        doc.sections['1']['locked']=[{'id':'S1.1','text':'Draft claim','pending_signoff':True},
                                     {'id':'S1.2','text':'Approved claim'}]
        self.assertEqual(locked_lines_for_sections(doc,['1']),{'S1.2':'Approved claim'})
        self.assertEqual([r['id'] for r in _section_brief(doc,'1')['locked']],['S1.2'])

    def test_deck_plan_uses_master_sections_without_database_or_vm(self):
        doc=load_master_script(Path('/missing/test-master.json'))
        route=resolve_route(doc,duration='T4',deck_use_case='school_fair')
        slides,topics,trace=build_master_deck(doc,route,'T4')
        self.assertEqual([t.section for t in topics],route.sections)
        self.assertEqual(trace['source'],'master_script')
        self.assertEqual(len({s.page for s in slides}),len(slides))
        self.assertTrue(all(t.pages for t in topics))

    def test_parser_keeps_subsections_separate(self):
        scripts=Path(__file__).resolve().parents[3]/'scripts/master_script'
        sys.path.insert(0,str(scripts))
        from parse_master_script import split_section_chunks, deterministic_skeleton
        text='Section 4. Learning Model (slides 19 to 54)\nIntro\n4A. The model (slides 19 to 21)\nThe one thing. Model.\n4B. Inclass (slides 22 to 28)\nThe one thing. Faculty.\nSection 5. Outcomes (slides 55 to 64)\nResults.'
        chunks=split_section_chunks(text)
        self.assertIn('Faculty',chunks['4B'])
        self.assertNotIn('Faculty',chunks['4A'])
        doc=deterministic_skeleton(text)
        self.assertEqual(next(s for s in doc['sections'] if s['id']=='4B')['title'],'Inclass')


if __name__=='__main__':
    unittest.main()
