import { FormEvent, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { AXES } from "../axes";
import { Button, ErrorBanner, Skeleton } from "../components/ui";
import { axisLabel } from "../labels";
import type { AxisOption, Generation, RecipeOption } from "../types";

function AxisSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: AxisOption[];
}) {
  const current = options.find((item) => item.code === value);
  return (
    <label className="block">
      <span className="kicker">{label}</span>
      <select className="field mt-2" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((item) => (
          <option key={item.code} value={item.code}>
            {item.label}
          </option>
        ))}
      </select>
      {current && <p className="mt-1 text-xs text-muted">{current.description}</p>}
    </label>
  );
}

function personaOptionLabel(recipe: RecipeOption, siblings: RecipeOption[]): string {
  const clash = siblings.filter((row) => row.audience_label === recipe.audience_label).length > 1;
  const name = recipe.audience_label || "Untitled persona";
  if (!clash) return recipe.valid ? name : `${name} (needs fix)`;
  const durationName = axisLabel(AXES.durations, recipe.duration);
  const suffix = durationName ? ` · ${durationName}` : "";
  return recipe.valid ? `${name}${suffix}` : `${name}${suffix} (needs fix)`;
}

export default function Generate() {
  const navigate = useNavigate();
  const [recipes, setRecipes] = useState<RecipeOption[]>([]);
  const [recipesLoading, setRecipesLoading] = useState(true);
  const [selected, setSelected] = useState("");
  const [audience, setAudience] = useState("A");
  const [duration, setDuration] = useState("T1");
  const [channel, setChannel] = useState("CH1");
  const [intent, setIntent] = useState("I2");
  const [temperature, setTemperature] = useState("X3");
  const [context, setContext] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const applyRecipe = (recipe: RecipeOption) => {
    setSelected(recipe.ref);
    setAudience(recipe.audience_cluster);
    setDuration(recipe.duration);
    setChannel(recipe.channel);
    setIntent(recipe.intent);
  };

  useEffect(() => {
    api
      .recipes()
      .then((recipeData) => {
        const rows = recipeData as RecipeOption[];
        setRecipes(rows);
        const first = rows.find((row) => row.valid) || rows[0];
        if (first) applyRecipe(first);
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setRecipesLoading(false));
  }, []);

  const personas = useMemo(
    () => recipes.filter((recipe) => recipe.audience_cluster === audience),
    [audience, recipes],
  );

  const chosen = recipes.find((recipe) => recipe.ref === selected);

  const matchRecipe = (next: { audience: string; duration: string; channel: string; intent: string }) => {
    const sameAxes = (recipe: RecipeOption) =>
      recipe.audience_cluster === next.audience &&
      recipe.duration === next.duration &&
      recipe.channel === next.channel &&
      recipe.intent === next.intent;
    return recipes.find((recipe) => sameAxes(recipe) && recipe.valid) || recipes.find(sameAxes);
  };

  const onAudience = (code: string) => {
    setAudience(code);
    const match =
      recipes.find((recipe) => recipe.audience_cluster === code && recipe.valid) ||
      recipes.find((recipe) => recipe.audience_cluster === code);
    if (match) applyRecipe(match);
    else setSelected("");
  };

  const onPersona = (ref: string) => {
    const recipe = recipes.find((row) => row.ref === ref);
    if (recipe) applyRecipe(recipe);
  };

  const onDuration = (code: string) => {
    setDuration(code);
    const match = matchRecipe({ audience, duration: code, channel, intent });
    setSelected(match?.ref ?? "");
  };

  const onChannel = (code: string) => {
    setChannel(code);
    const match = matchRecipe({ audience, duration, channel: code, intent });
    setSelected(match?.ref ?? "");
  };

  const onIntent = (code: string) => {
    setIntent(code);
    const match = matchRecipe({ audience, duration, channel, intent: code });
    setSelected(match?.ref ?? "");
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const body = selected
        ? { recipe_ref: selected, temperature, context_note: context }
        : {
            audience_cluster: audience,
            duration,
            channel,
            intent,
            temperature,
            context_note: context,
          };
      const generation = (await api.createGeneration(body)) as Generation;
      navigate(`/result/${generation.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Generation failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-4xl">
      <p className="kicker">Create</p>
      <h1 className="mt-2 font-display text-4xl">Build a pitch from the matrix</h1>
      <p className="mt-2 max-w-2xl text-muted">
        Pick a persona and the five axes. Names come from One Company — One Story; the recipe still locks the
        module order.
      </p>

      <form onSubmit={submit} className="mt-8 space-y-6">
        {recipesLoading && (
          <div className="card grid gap-5 p-5 md:grid-cols-2">
            {Array.from({ length: 6 }).map((_, index) => (
              <div key={index} className="space-y-2">
                <Skeleton className="h-3 w-20" />
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-3 w-40" />
              </div>
            ))}
          </div>
        )}
        <div className={`card grid gap-5 p-5 md:grid-cols-2 ${recipesLoading ? "hidden" : ""}`}>
          <AxisSelect label="Audience" value={audience} onChange={onAudience} options={AXES.audience_clusters} />
          <label className="block">
            <span className="kicker">Persona</span>
            <select
              className="field mt-2"
              value={selected}
              onChange={(e) => onPersona(e.target.value)}
              disabled={recipesLoading || personas.length === 0}
            >
              {personas.length === 0 && <option value="">No personas in this audience</option>}
              {!selected && personas.length > 0 && (
                <option value="">No matching persona for these axes</option>
              )}
              {personas.map((recipe) => (
                <option key={recipe.ref} value={recipe.ref} disabled={!recipe.valid}>
                  {personaOptionLabel(recipe, personas)}
                </option>
              ))}
            </select>
            <p className="mt-1 text-xs text-muted">
              {recipesLoading
                ? "Loading personas…"
                : chosen
                  ? chosen.valid
                    ? chosen.audience_label
                    : "This persona’s recipe still needs a module sequence."
                  : "No exact recipe for this combination — generation will follow the axes."}
            </p>
          </label>
          <AxisSelect label="Duration" value={duration} onChange={onDuration} options={AXES.durations} />
          <AxisSelect label="Channel" value={channel} onChange={onChannel} options={AXES.channels} />
          <AxisSelect label="Intent" value={intent} onChange={onIntent} options={AXES.intents} />
          <AxisSelect
            label="Temperature"
            value={temperature}
            onChange={setTemperature}
            options={AXES.temperatures}
          />
        </div>

        <div className="card p-5">
          <label className="block">
            <span className="kicker">Context note</span>
            <textarea
              className="field mt-2"
              rows={4}
              value={context}
              onChange={(e) => setContext(e.target.value)}
              placeholder="What specifically matters in this conversation?"
            />
          </label>
          {chosen && (
            <p className="mt-4 text-sm text-muted">
              {chosen.audience_label}
              {" · "}
              {axisLabel(AXES.audience_clusters, chosen.audience_cluster)}
              {" · "}
              {axisLabel(AXES.durations, chosen.duration)}
              {" · "}
              {axisLabel(AXES.channels, chosen.channel)}
              {" · "}
              {axisLabel(AXES.intents, chosen.intent)}
              {" · "}
              {axisLabel(AXES.temperatures, temperature)}
            </p>
          )}
        </div>

        <ErrorBanner message={error} />
        <Button variant="accent" type="submit" loading={busy} disabled={recipesLoading}>
          Generate pitch
        </Button>
      </form>
    </div>
  );
}
