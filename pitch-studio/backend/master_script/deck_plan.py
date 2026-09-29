"""Master Script's own section/slide plan; no classic or VM content planning."""
from backend.master_script.source import MasterScriptDoc, RoutePlan
from backend.pipeline.brand_deck import BrandSlide, PAGE_LABELS, PAGE_MODULES
from backend.pipeline.deck import slide_ceiling_for
from backend.pipeline.script_flow import ScriptTopic


def build_master_deck(doc: MasterScriptDoc, route: RoutePlan, duration: str):
    pools = {sid: list((doc.section(sid) or {}).get('slides') or []) for sid in route.sections}
    chosen = {sid: [] for sid in route.sections}
    # Give each routed section one page before expanding any section. The
    # argument's section order remains authoritative even for compressed routes.
    budget = max(len(route.sections), slide_ceiling_for(duration))
    while budget and any(pools.values()):
        for sid in route.sections:
            if pools[sid] and budget:
                chosen[sid].append(pools[sid].pop(0))
                budget -= 1
    slides, topics = [], []
    for index, sid in enumerate(route.sections, 1):
        section = doc.section(sid) or {}
        title = str(section.get('title') or sid)
        section_slides = [BrandSlide(page, PAGE_MODULES.get(page, ''),
                                    PAGE_LABELS.get(page, f'Page {page}'), section=title)
                          for page in chosen[sid]]
        slides.extend(section_slides)
        modules = list(dict.fromkeys(s.module_id for s in section_slides if s.module_id))
        topics.append(ScriptTopic(topic_id=index, title=title, section=sid,
                                  pages=chosen[sid], slide_keys=[s.slide_key for s in section_slides],
                                  labels=[s.label for s in section_slides],
                                  summary=str(section.get('the_one_thing') or ''),
                                  module_ids=modules, recipe_modules=modules))
    return slides, topics, {'source': 'master_script', 'section_pages': chosen}
