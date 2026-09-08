import { AXES } from "./axes";
import type { AxisOption, RecipeOption } from "./types";

export function axisLabel(options: AxisOption[], code: string | null | undefined): string {
  if (!code) return "";
  return options.find((item) => item.code === code)?.label ?? code;
}

export function axisDescription(options: AxisOption[], code: string | null | undefined): string {
  if (!code) return "";
  return options.find((item) => item.code === code)?.description ?? "";
}

export function personaLabel(
  recipes: RecipeOption[],
  ref: string | null | undefined,
  generation?: {
    audience_cluster: string;
    duration: string;
    channel: string;
    intent: string;
  },
): string {
  if (ref && ref !== "AUTO") {
    const exact = recipes.find((recipe) => recipe.ref === ref);
    if (exact?.audience_label) return exact.audience_label;
  }
  if (generation) {
    const match = recipes.find(
      (recipe) =>
        recipe.audience_cluster === generation.audience_cluster &&
        recipe.duration === generation.duration &&
        recipe.channel === generation.channel &&
        recipe.intent === generation.intent,
    );
    if (match?.audience_label) return match.audience_label;
  }
  return "";
}

export function generationAxisLabels(generation: {
  audience_cluster: string;
  duration: string;
  channel: string;
  intent: string;
  temperature?: string;
}) {
  return {
    audience: axisLabel(AXES.audience_clusters, generation.audience_cluster),
    duration: axisLabel(AXES.durations, generation.duration),
    channel: axisLabel(AXES.channels, generation.channel),
    intent: axisLabel(AXES.intents, generation.intent),
    temperature: axisLabel(AXES.temperatures, generation.temperature ?? ""),
  };
}
