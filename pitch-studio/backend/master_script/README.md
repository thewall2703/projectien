# Master Script: third generation mode

This mode has its own planning, evidence and writing orchestration. It does not
call Classic's script planner/writer or the Vision Modules content planner.
It shares the database, audience/duration inputs, voice corpus, LLM transport,
deck rendering and result format. The existing modes remain selectable.

## Source responsibilities

1. **Planner: what to say and why.** The supplied Brand Deck Master Script is
   authoritative. The planner receives full section text, audience routing and
   global rules. It emits a section argument, word budget, locked passages and
   precise evidence requests. Its deck plan follows Master Script sections.
2. **Evidence: why to believe it.** Retrieval is restricted to approved nouns and
   cards with source references and supporting excerpts. It returns usable cards
   or a recorded gap. Rejected cards and unapproved subjects cannot be selected
   through a fallback. The planner chooses evidence before the writer runs.
3. **Voice: how Pratham would say it.** Pratham's passages supply cadence,
   vocabulary and explanation patterns. They are not an extra facts database.
   The writer preserves the approved argument, examples and locked placeholders.

Execution order: Planner → Evidence → Planner selects proof → Voice → validation.
The Evidence model audits the final wording without seeing style references.
This is a second task for Engine 3, not a fourth model. Deterministic checks cover
locked passages, prohibited wording, numbers and word budget. Failed or incomplete
grounding audits stop publication. There are at most two targeted rewrite attempts.

## Model recommendations

Starting configuration, checked against the live OpenRouter catalog on 2026-09-27:

| Engine | OpenRouter model | Reason to evaluate it |
|---|---|---|
| Planner | `openai/gpt-6-astra` | Complex reasoning over argument, audience constraints and document rules |
| Evidence | `google/gemini-3.8-flash` | Multimodal extraction and repeated evidence selection tasks |
| Voice | `anthropic/claude-opus-5.5` | Following detailed writing instructions and producing clear prose |

These role assignments are recommendations, not measured winners for this corpus.
Provider documentation supports the capabilities, not a claim that any model is
best at Pratham's voice:
[OpenAI Docs](https://developers.openai.com/api/docs/models/gpt-6-astra),
[Gemini models](https://ai.google.dev/gemini-api/docs/models),
[Claude Opus](https://www.anthropic.com/claude/opus).

Each model and timeout is independently configurable through `MS_PLANNER_*`,
`MS_EVIDENCE_*` and `MS_VOICE_*`. The extraction command uses low reasoning effort
and caches completed batches; the runtime evidence engine uses its configured effort.

After noun selection, compare candidates using identical held-out tasks:

- Planner: section coverage, correct audience route, useful evidence requests,
  preservation of restrictions, latency and cost.
- Evidence: correct source attribution, unsupported-claim rejection, handling of
  conflicting dates/figures, and correct abstention when only a mention exists.
- Voice: blind human preference against Pratham reference passages, naturalness,
  argument preservation and zero factual drift.

The existing `bakeoff.py` is only a preliminary smoke probe. Its generic samples
and dry runs do not constitute this evaluation and must not determine a winner.

## Current review deliverable

`output/master_script/noun_register.xlsx` contains 953 section-specific candidate
rows and 1,283 source mentions from all 49 Master Script pages and 92 deck pages.
The extractor reads deck images as well as text. There are 740 distinct spellings;
aliases and subjects repeated across sections are deliberately retained for review.
It includes people, companies, ventures, labs, equipment, programmes, metrics and
meaningful common-noun subjects. It is a candidate register, not verified evidence.
Extraction can still miss or misread small labels; source locations remain attached.

The three board members, Bloomberg Lab, Makers Lab and PwC AI Lab are present.
The user approved 27 rows covering 24 subjects. The connected database now contains
57 supporting evidence cards and 15 human-approved story cards. The story sheet
contains generated drafts and editable Human story versions; the latter are authoritative
for re-ingestion. Nine subjects remain partial or without usable supporting material.
Named student outcomes are optional: concrete student usefulness is sufficient.
Source restrictions and pending sign-off remain separate from subject approval.

## Ingestion sequence

Run from the repository root:

```sh
.venv/bin/python scripts/master_script/parse_master_script.py --dry-run
.venv/bin/python scripts/master_script/build_review_register.py
```

The parser's `--dry-run` means deterministic PDF extraction without LLM
structuring. It retains all section source text and checks locked quotations.
The resulting JSON is local under `pitch-studio/data/master_script/`; production
must receive this source artifact before the new mode can run.

After the user's selections are returned:

```sh
.venv/bin/python scripts/master_script/import_approved_nouns.py --dry-run
.venv/bin/python scripts/master_script/import_approved_nouns.py
.venv/bin/python scripts/master_script/noun_coverage.py
```

Coverage searches stored facts, reports, transcript chunks, stories and passages.
It produces a noun coverage table, every evidence card with its source and excerpt,
and a gaps sheet. Coverage and selection changes invalidate generation caches.
The import command adds approved subjects; it does not interpret blanks as revocation.

For a gap, ingest a text dossier with a dated source, actor, action, result, units
and scope. Then rerun coverage. A mention alone is insufficient proof. Do not
confuse a transcript speaker's claim with independently verified data.

## Improvements to prioritise next

- Entity aliases should share a stable canonical ID while keeping page-specific
  mentions. Review ambiguous spellings before merging them.
- Store structured dates, cohort, unit and metric definitions. Flag conflicting
  sources rather than choosing the largest or newest number automatically.
- Let the planner revise a weak claim when evidence is thin, rather than searching
  indefinitely for an example that appears to prove it.
- Keep source snapshots and model/prompt versions with runs. Persisted card IDs,
  sources, supporting excerpts, evidence gaps and model names already appear in
  the Master Script trace.
- Build the corpus-specific evaluation above before calling any model the best.

## Validation and remaining limits

79 focused tests pass across Master Script, its source/approval boundaries, the
existing script pipeline and vision-deck planning. The frontend production build
passes. Six broader Script Testing API tests fail because the existing router
requires admin access while their fixtures use non-admin reviewers. That router
restriction is also present in HEAD and was not changed here.

Approved nouns, evidence and stories have been ingested. A 3,008-word PG introduction
was generated through the standalone runner and passed its automated evidence audit.
Production deployment and broader audience/duration evaluation remain unverified.
The current orchestration still uses the Vision deck spine; the independent
Master Script deck planner exists but is not wired into flow.py yet.
The final grounding check is an LLM audit, not a guarantee.

## Approved story updates

Run `.venv/bin/python scripts/master_script/ingest_approved_stories.py` after updating
Human story in `output/master_script/approved_research/master_table.xlsx`.
This updates each stable `human_approved_story` card in place, retains prior text
in its history, preserves source snapshots and invalidates the generation cache.
Blank Human story cells do not revoke previously approved stories.
The planner receives the available subject catalog. Evidence retrieval prioritises
relevant approved stories, while retaining approval and source checks. The writer
uses employee perspective and keeps citations out of the spoken narrative.

## Deck-only planning and stage delivery

The shared rules in `editorial.py` constrain the planner to the Master Deck /
Master Script's arguments and section titles. Audience context may change selection,
order and compression, but cannot introduce career advice, invented motivations or
audience exercises. Model-produced cues and evidence requests require a matching
source quotation from their own section. This quote check establishes provenance;
the final model audit must still assess whether the proposed beat follows that quote.

Voice uses English vocabulary and phrasing from Pratham references, with original
stage delivery: setup, real tension, pause, payoff and restrained callbacks. Fictional
events, Hindi/Hinglish, copied television dialogue and automatic PR-approval claims
are excluded. Approved stories enrich deck topics through the evidence engine.

## Editorial agent memory

`pitch-studio/data/master_script/editorial_agent_context.md` is the durable editorial
handbook built from user review: authority order, the deletion test, screenplay meaning,
Pratham voice boundaries, spoken-language standards, recurring failure modes, the edit
procedure and the output contract. Presentation-specific corrections live under
`pitch-studio/data/master_script/editorial_profiles/`; this prevents one pitch's facts
from leaking into unrelated routes.

Load both layers with `load_editorial_context(profile)` from `editorial.py`. The PG
30-minute rebuild passes `pg_intro_30min.md` to its editorial repairs and independent
quality gates. The context is an instruction source only and must never be used as
evidence for a factual claim.

The editorial pass uses the independent `ms_editor` OpenRouter role. Its model and
request settings are configured with `MS_EDITOR_MODEL`,
`MS_EDITOR_REASONING_EFFORT`, `MS_EDITOR_VERBOSITY` and `MS_EDITOR_TIMEOUT`. Voice
generation remains on `ms_voice`; source and quality reviews remain separate so the
editor does not approve its own edits.

## Efficient generation path

Both production `flow.py` and the PG rebuild use `editor.py`:

1. Generate the source-backed plan and evidence, then write sections concurrently.
2. One whole-script editorial call returns exact replacements and their reasons.
3. Two independent review calls run concurrently: source fidelity, and delivery.
   Delivery reports separate spoken-clarity and screenplay verdicts.
4. Only mandatory findings trigger one consolidated editorial repair.
5. Both reviewers check the repaired draft once. Remaining failures stop for review.

The editorial stage uses three calls normally and six at most. There are no per-section
editor repair loops, no hidden manual-resolution override and no automatic retries in
this stage. It retains the existing models and reasoning settings. Production also
removes the separate transition-stitch call and the duplicate evidence follow-up;
evidence retrieval still has its own bounded follow-up.

The current 11-section PG runner uses 17 logical calls when clean, at most 20 with a
repair: planning, evidence, performance map, 11 parallel section calls and 3–6 editorial
calls. It admits no more than 20 uncached API calls, stops launching calls after 30
minutes and bounds each HTTP operation to at most 240 seconds. These are admission and
network-operation limits, not a guaranteed end-to-end wall-clock deadline. No dollar
cap is claimed. Production's total call count also depends on evidence requests.

Source review receives complete selected source sections and global restrictions.
Delivery review does not receive the factual corpus. Voice references are short excerpts;
factual authorities are never silently truncated. The editorial memory is included once
in the editor's system prompt. Exact replacement validation protects source metadata and
locked placeholders. Audits must account for every section, including the closing CTA.

Run `.venv/bin/python scripts/master_script/rebuild_pg_stage.py` for a new PG review.
Artifacts are saved under `output/master_script/pg_intro_30min/stage_v8_efficient/`.
Cache fingerprints include the full request, model, settings, prompt and source payload.
Only a draft passing all three verdicts and length checks replaces `current_output.md`.
The previous version is archived. Failed runs retain completed calls for resumption.

The efficient path has automated tests but has not yet been measured with a live
end-to-end generation. Runtime, cost and writing quality require that comparison;
call reduction alone is not evidence of equal editorial quality.
