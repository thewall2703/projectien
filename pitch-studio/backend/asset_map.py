from __future__ import annotations

ALL_MODULES = ",".join(f"M{index:02d}" for index in range(1, 15))

REPORT_MODULE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("placement", "careers", "ug outcomes", "outcome"), "M07,M13"),
    (("entrepreneurship", "dropshipping", "ccc", "case competition"), "M04,M06"),
    (("prospectus", "pgp bharat", "brochure"), "M02,M10"),
    (("mu ventures", "muif"), "M11"),
    (("haryana", "private universities act"), "M02,M13"),
    (("brand deck",), ALL_MODULES),
]

MATRIX_REF_RULES: list[tuple[tuple[str, ...], str]] = [
    (("brand deck",), "R-10"),
    (("ug prospectus",), "R-08"),
    (("prospectus",), "R-08"),
    (("pgp bharat",), "R-09"),
    (("placement", "careers", "ug outcomes"), "R-01"),
    (("entrepreneurship",), "R-04"),
    (("dropshipping",), "R-07"),
    (("mu ventures",), "R-05"),
    (("muif",), "R-06"),
    (("haryana",), "R-13"),
    (("case competition",), "R-14"),
    (("ccc",), "R-07"),
]


def _first_match(title: str, rules: list[tuple[tuple[str, ...], str]]) -> str:
    lowered = title.lower()
    for needles, value in rules:
        if any(needle in lowered for needle in needles):
            return value
    return ""


def infer_module_ids(asset_type: str, title: str) -> str:
    if asset_type != "report":
        return ""
    return _first_match(title, REPORT_MODULE_RULES)


def infer_matrix_ref(asset_type: str, title: str) -> str:
    if asset_type != "report":
        return ""
    return _first_match(title, MATRIX_REF_RULES)
