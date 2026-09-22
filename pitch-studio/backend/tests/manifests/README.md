# Source-deck style examples

Style exemplars extracted from the Masters' Union brand deck, used by the slide
renderer tests as fixtures. Each `p*.json` describes how a real brand-deck page
is reconstructed from a template + values, so the fidelity harness can rebuild
it and pixel-diff against the cached page image.

- `section_divider.light.json` / `section_divider.dark.json` — the serialised
  `SlideManifest` for the section-divider template (the extracted *style*:
  geometry, type tokens, per-size tracking, colours). Regenerated from
  `backend.pipeline.slide_templates.section_divider_manifest`.
- `p019.json`, `p052.json` — reconstruction of real section-divider pages
  (p19, p52). Each carries the template id, variant, source page number, the
  slot *values* (content only), and the fidelity masks/tolerances used when
  comparing against the cached render (`data/files/brand-deck/pages/pNNN.jpg`).

Text is compared loosely on purpose: the licensed display serif is not bundled
yet and the source pages include a large raster brush stroke and the wordmark
logo that the vector furniture does not reproduce. Those regions are masked and
the remaining structure is compared within tolerance. See `test_slide_fidelity`.
