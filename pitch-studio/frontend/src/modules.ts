export const MODULE_NAMES: Record<string, string> = {
  M01: "Origin",
  M02: "Institution",
  M03: "Model",
  M04: "Sequence",
  M05: "People proof",
  M06: "Venture proof",
  M07: "Outcome proof",
  M08: "Faculty",
  M09: "Campus",
  M10: "Programmes",
  M11: "Ecosystem",
  M12: "Culture",
  M13: "Honest",
  M14: "Ask",
};

export function moduleName(id: string | null | undefined): string {
  if (!id) return "";
  return MODULE_NAMES[id] || id;
}

export function formatModuleSequence(sequence: string | null | undefined): string {
  if (!sequence) return "";
  return sequence
    .split(">")
    .map((part) => moduleName(part.trim()))
    .filter(Boolean)
    .join(" · ");
}
