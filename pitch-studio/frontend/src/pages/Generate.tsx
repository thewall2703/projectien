import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { AXES } from "../axes";
import { Button, ErrorBanner, Skeleton } from "../components/ui";
import { axisLabel } from "../labels";
import type { AxisOption, Generation, RecipeOption } from "../types";

const STEPS = [
  "audience",
  "persona",
  "duration",
  "channel",
  "intent",
  "temperature",
  "context",
  "review",
] as const;

type StepId = (typeof STEPS)[number];

const STEP_TITLES: Record<StepId, string> = {
  audience: "Who is this for?",
  persona: "Which persona?",
  duration: "How long?",
  channel: "Which channel?",
  intent: "What's the intent?",
  temperature: "How warm are they?",
  context: "Any context?",
  review: "Ready to generate?",
};

function personaOptionLabel(recipe: RecipeOption, siblings: RecipeOption[]): string {
  const clash = siblings.filter((row) => row.audience_label === recipe.audience_label).length > 1;
  const name = recipe.audience_label || "Untitled persona";
  if (!clash) return recipe.valid ? name : `${name} (needs fix)`;
  const durationName = axisLabel(AXES.durations, recipe.duration);
  const suffix = durationName ? ` · ${durationName}` : "";
  return recipe.valid ? `${name}${suffix}` : `${name}${suffix} (needs fix)`;
}

function OptionCard({
  label,
  description,
  selected,
  disabled,
  onSelect,
}: {
  label: string;
  description?: string;
  selected: boolean;
  disabled?: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onSelect}
      className={`glass-panel w-full p-5 text-left transition ${
        selected ? "ring-2 ring-black" : "hover:bg-white/70"
      } ${disabled ? "opacity-50" : ""}`}
    >
      <p className="font-display text-xl text-black md:text-2xl">{label}</p>
      {description && <p className="mt-2 text-sm leading-relaxed text-grey">{description}</p>}
    </button>
  );
}

export default function Generate() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [direction, setDirection] = useState(1);
  const [recipes, setRecipes] = useState<RecipeOption[]>([]);
  const [recipesLoading, setRecipesLoading] = useState(true);
  const [selected, setSelected] = useState("");
  const [audience, setAudience] = useState("");
  const [duration, setDuration] = useState("");
  const [channel, setChannel] = useState("");
  const [intent, setIntent] = useState("");
  const [temperature, setTemperature] = useState("");
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
        setRecipes(recipeData as RecipeOption[]);
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setRecipesLoading(false));
  }, []);

  const personas = useMemo(
    () => recipes.filter((recipe) => recipe.audience_cluster === audience),
    [audience, recipes],
  );

  const chosen = recipes.find((recipe) => recipe.ref === selected);

  const go = (next: number) => {
    setDirection(next > step ? 1 : -1);
    setStep(next);
  };

  const canAdvance = (index: number): boolean => {
    const id = STEPS[index];
    if (id === "audience") return Boolean(audience);
    if (id === "persona") return true;
    if (id === "duration") return Boolean(duration);
    if (id === "channel") return Boolean(channel);
    if (id === "intent") return Boolean(intent);
    if (id === "temperature") return Boolean(temperature);
    if (id === "context") return true;
    return Boolean(temperature && (selected || (audience && duration && channel && intent)));
  };

  const selectAndAdvance = (after: () => void) => {
    after();
    window.setTimeout(() => {
      if (step < STEPS.length - 1) go(step + 1);
    }, 180);
  };

  const onAudience = (code: string) => {
    selectAndAdvance(() => {
      setAudience(code);
      setSelected("");
    });
  };

  const onPersona = (ref: string) => {
    selectAndAdvance(() => {
      if (!ref) {
        setSelected("");
        return;
      }
      const recipe = recipes.find((row) => row.ref === ref);
      if (recipe) applyRecipe(recipe);
    });
  };

  const matchRecipe = (next: { audience: string; duration: string; channel: string; intent: string }) => {
    const sameAxes = (recipe: RecipeOption) =>
      recipe.audience_cluster === next.audience &&
      recipe.duration === next.duration &&
      recipe.channel === next.channel &&
      recipe.intent === next.intent;
    return recipes.find((recipe) => sameAxes(recipe) && recipe.valid) || recipes.find(sameAxes);
  };

  const onDuration = (code: string) => {
    selectAndAdvance(() => {
      setDuration(code);
      const match = matchRecipe({ audience, duration: code, channel, intent });
      setSelected(match?.ref ?? "");
    });
  };

  const onChannel = (code: string) => {
    selectAndAdvance(() => {
      setChannel(code);
      const match = matchRecipe({ audience, duration, channel: code, intent });
      setSelected(match?.ref ?? "");
    });
  };

  const onIntent = (code: string) => {
    selectAndAdvance(() => {
      setIntent(code);
      const match = matchRecipe({ audience, duration, channel, intent: code });
      setSelected(match?.ref ?? "");
    });
  };

  const onTemperature = (code: string) => {
    selectAndAdvance(() => setTemperature(code));
  };

  const submit = async () => {
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

  const stepId = STEPS[step];
  const progress = ((step + 1) / STEPS.length) * 100;

  const slideVariants = {
    enter: (dir: number) => ({ x: dir > 0 ? 48 : -48, opacity: 0 }),
    center: { x: 0, opacity: 1 },
    exit: (dir: number) => ({ x: dir > 0 ? -48 : 48, opacity: 0 }),
  };

  const renderAxisOptions = (options: AxisOption[], value: string, onChange: (code: string) => void) => (
    <div className="grid max-h-[55vh] gap-3 overflow-y-auto pr-1 sm:grid-cols-2">
      {options.map((item) => (
        <OptionCard
          key={item.code}
          label={item.label}
          description={item.description}
          selected={value === item.code}
          onSelect={() => onChange(item.code)}
        />
      ))}
    </div>
  );

  return (
    <div className="mx-auto flex min-h-[calc(100vh-4rem)] max-w-4xl flex-col px-6 py-8 md:px-10">
      <div className="mb-8">
        <div className="flex items-center justify-between gap-4 text-xs text-grey">
          <span>
            Step {step + 1} of {STEPS.length}
          </span>
          <span className="capitalize">{stepId.replace("-", " ")}</span>
        </div>
        <div className="mt-3 h-1 overflow-hidden rounded-full bg-grey-light">
          <motion.div
            className="h-full bg-black"
            animate={{ width: `${progress}%` }}
            transition={{ duration: 0.35 }}
          />
        </div>
      </div>

      <div className="relative flex-1">
        <AnimatePresence mode="wait" custom={direction}>
          <motion.div
            key={stepId}
            custom={direction}
            variants={slideVariants}
            initial="enter"
            animate="center"
            exit="exit"
            transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
            className="space-y-8"
          >
            <div>
              <p className="kicker">Generate</p>
              <h1 className="mt-3 font-display text-4xl tracking-tight text-black md:text-5xl">
                {STEP_TITLES[stepId]}
              </h1>
            </div>

            {recipesLoading && stepId !== "context" && stepId !== "review" && (
              <div className="grid gap-3 sm:grid-cols-2">
                {Array.from({ length: 4 }).map((_, index) => (
                  <Skeleton key={index} className="h-28 w-full rounded-2xl" />
                ))}
              </div>
            )}

            {!recipesLoading && stepId === "audience" &&
              renderAxisOptions(AXES.audience_clusters, audience, onAudience)}

            {!recipesLoading && stepId === "persona" && (
              <div className="grid max-h-[55vh] gap-3 overflow-y-auto pr-1 sm:grid-cols-2">
                <OptionCard
                  label="Skip persona"
                  description="Continue with axes only — generation will follow your selections."
                  selected={!selected}
                  onSelect={() => onPersona("")}
                />
                {personas.map((recipe) => (
                  <OptionCard
                    key={recipe.ref}
                    label={personaOptionLabel(recipe, personas)}
                    description={
                      recipe.valid
                        ? `${axisLabel(AXES.durations, recipe.duration)} · ${axisLabel(AXES.channels, recipe.channel)} · ${axisLabel(AXES.intents, recipe.intent)}`
                        : "This persona’s recipe still needs a module sequence."
                    }
                    selected={selected === recipe.ref}
                    disabled={!recipe.valid}
                    onSelect={() => onPersona(recipe.ref)}
                  />
                ))}
                {personas.length === 0 && (
                  <p className="text-sm text-grey sm:col-span-2">
                    No personas in this audience. You can continue without one.
                  </p>
                )}
              </div>
            )}

            {!recipesLoading && stepId === "duration" &&
              renderAxisOptions(AXES.durations, duration, onDuration)}

            {!recipesLoading && stepId === "channel" &&
              renderAxisOptions(AXES.channels, channel, onChannel)}

            {!recipesLoading && stepId === "intent" &&
              renderAxisOptions(AXES.intents, intent, onIntent)}

            {!recipesLoading && stepId === "temperature" &&
              renderAxisOptions(AXES.temperatures, temperature, onTemperature)}

            {stepId === "context" && (
              <div className="glass-panel p-6">
                <label className="block">
                  <span className="kicker">Context note</span>
                  <textarea
                    className="field mt-3 min-h-[160px]"
                    value={context}
                    onChange={(e) => setContext(e.target.value)}
                    placeholder="What specifically matters in this conversation?"
                    autoFocus
                  />
                </label>
              </div>
            )}

            {stepId === "review" && (
              <div className="glass-panel space-y-4 p-6">
                <dl className="grid gap-4 sm:grid-cols-2">
                  {[
                    ["Audience", axisLabel(AXES.audience_clusters, audience)],
                    ["Persona", chosen?.audience_label || "Axes only"],
                    ["Duration", axisLabel(AXES.durations, duration)],
                    ["Channel", axisLabel(AXES.channels, channel)],
                    ["Intent", axisLabel(AXES.intents, intent)],
                    ["Temperature", axisLabel(AXES.temperatures, temperature)],
                  ].map(([label, value]) => (
                    <div key={label}>
                      <dt className="text-[11px] font-semibold uppercase tracking-[0.18em] text-grey">{label}</dt>
                      <dd className="mt-1 text-base text-black">{value || "—"}</dd>
                    </div>
                  ))}
                </dl>
                {context && (
                  <div>
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-grey">Context</p>
                    <p className="mt-1 text-sm text-grey-dark">{context}</p>
                  </div>
                )}
                <ErrorBanner message={error} />
                <Button
                  variant="accent"
                  className="w-full py-3 text-base sm:w-auto"
                  loading={busy}
                  disabled={!canAdvance(step)}
                  onClick={submit}
                >
                  Generate now
                </Button>
              </div>
            )}
          </motion.div>
        </AnimatePresence>
      </div>

      <div className="mt-10 flex items-center justify-between gap-4 border-t border-black/8 pt-6">
        <Button variant="ghost" disabled={step === 0 || busy} onClick={() => go(step - 1)}>
          Back
        </Button>
        {stepId !== "review" && (
          <Button
            variant="accent"
            disabled={!canAdvance(step) || recipesLoading}
            onClick={() => go(step + 1)}
          >
            Next
          </Button>
        )}
      </div>
    </div>
  );
}
