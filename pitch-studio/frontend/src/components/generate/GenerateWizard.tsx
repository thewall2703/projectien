import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../../api";
import { AXES } from "../../axes";
import { Button, ErrorBanner, Skeleton } from "../ui";
import { axisLabel } from "../../labels";
import type { AxisOption, InterpretPersonaCandidate, InterpretResult, RecipeOption } from "../../types";

const STEPS = ["audience", "setting", "goal", "review"] as const;

type StepId = (typeof STEPS)[number];

const STEP_TITLES: Record<StepId, string> = {
  audience: "Who are you pitching to?",
  setting: "What's the setting?",
  goal: "What do you want from it?",
  review: "Ready to generate?",
};

const AUDIENCE_CHIPS = [
  "Prospective parents",
  "An investor evaluating us",
  "A potential hire",
  "A corporate partner",
  "A journalist",
];

const SETTING_CHIPS = [
  "A 30-minute Zoom call with a deck",
  "A quick 2-minute phone intro",
  "A 10-minute seated pitch in person",
  "A 90-minute campus walkthrough",
  "A short 30-second video message",
];

const GOAL_CHIPS = [
  "A first introduction — they don't know us",
  "Persuade a warm lead to commit",
  "Reassure them after they've decided",
  "Inform and update",
];

export type GenerateWizardPayload = {
  audience_cluster: string;
  duration: string;
  channel: string;
  intent: string;
  temperature: string;
  context_note: string;
  recipe_ref?: string;
  deck_use_case?: string;
};

const DECK_USE_CASES: { code: string; label: string }[] = [
  { code: "", label: "None (legacy recipe)" },
  { code: "school_fair", label: "School students at career fair" },
  { code: "pg_exec_fair", label: "PG / Exec aspirants at career fair" },
  { code: "pgp_smg", label: "PGP SMG aspirant" },
  { code: "ug_tbm", label: "UG TBM aspirant" },
  { code: "ug_dsai", label: "UG DSAI aspirant" },
  { code: "parents_undecided", label: "Parents, undecided" },
  { code: "parents_decided", label: "Parents, decided" },
];

function useCaseForPersona(label: string, temperature: string): string {
  const text = label.trim().toLowerCase();
  if (!text || text.startsWith("universal")) return "";
  if (text.includes("dsai") || text.includes("data science")) return "ug_dsai";
  if (text.includes("pgp smg") || text.includes("sports management")) return "pgp_smg";
  if (text.includes("tbm") && (/\bug\b/.test(text) || text.includes("school") || text.includes("class 11"))) {
    return "ug_tbm";
  }
  if (text.includes("parent")) return temperature === "X4" || temperature === "X5" ? "parents_decided" : "parents_undecided";
  if (text.includes("school student") || text.includes("school assembly") || text.includes("class 11")) {
    return "school_fair";
  }
  if (
    ["final-year", "final year", "working professional", "mid-career", "family business", "pg switch", "pg aspirant"].some(
      (token) => text.includes(token),
    )
  ) {
    return "pg_exec_fair";
  }
  return "";
}

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
  compact,
  onSelect,
}: {
  label: string;
  description?: string;
  selected: boolean;
  disabled?: boolean;
  compact?: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onSelect}
      className={`glass-panel w-full text-left transition ${compact ? "p-3.5" : "p-5"} ${
        selected ? "ring-2 ring-black" : "hover:bg-white/70"
      } ${disabled ? "opacity-50" : ""}`}
    >
      <p
        className={`font-display tracking-tight text-black ${
          compact ? "text-base md:text-lg" : "text-xl md:text-2xl"
        }`}
      >
        {label}
      </p>
      {description && (
        <p className={`leading-relaxed text-grey ${compact ? "mt-1.5 text-xs" : "mt-2 text-sm"}`}>
          {description}
        </p>
      )}
    </button>
  );
}

function ChipRow({
  chips,
  active,
  onPick,
}: {
  chips: string[];
  active: string;
  onPick: (value: string) => void;
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {chips.map((chip) => {
        const selected = active === chip;
        return (
          <button
            key={chip}
            type="button"
            onClick={() => onPick(chip)}
            className={`rounded-full border px-3 py-1.5 text-sm transition ${
              selected
                ? "border-black bg-black text-white"
                : "border-black/15 bg-white/50 text-grey-dark hover:border-black/40 hover:bg-white/80"
            }`}
          >
            {chip}
          </button>
        );
      })}
    </div>
  );
}

function briefKey(audience: string, setting: string, goal: string) {
  return `${audience.trim()}\n${setting.trim()}\n${goal.trim()}`;
}

export default function GenerateWizard({
  kicker = "Generate",
  submitLabel = "Generate now",
  submitting = false,
  onSubmit,
}: {
  kicker?: string;
  submitLabel?: string;
  submitting?: boolean;
  onSubmit: (payload: GenerateWizardPayload) => Promise<void> | void;
}) {
  const [step, setStep] = useState(0);
  const [direction, setDirection] = useState(1);
  const [recipes, setRecipes] = useState<RecipeOption[]>([]);
  const [recipesLoading, setRecipesLoading] = useState(true);

  const [audienceText, setAudienceText] = useState("");
  const [settingText, setSettingText] = useState("");
  const [goalText, setGoalText] = useState("");

  const [selected, setSelected] = useState("");
  const [audience, setAudience] = useState("");
  const [duration, setDuration] = useState("");
  const [channel, setChannel] = useState("");
  const [intent, setIntent] = useState("");
  const [temperature, setTemperature] = useState("");
  const [deckUseCase, setDeckUseCase] = useState("");
  const [context, setContext] = useState("");
  const [interpretSummary, setInterpretSummary] = useState("");
  const [interpretNotes, setInterpretNotes] = useState("");
  const [personaCandidates, setPersonaCandidates] = useState<InterpretPersonaCandidate[]>([]);
  const [interpretedKey, setInterpretedKey] = useState("");
  const [interpreting, setInterpreting] = useState(false);
  const [interpretFailed, setInterpretFailed] = useState(false);
  const [manualOpen, setManualOpen] = useState(false);
  const [openManualSection, setOpenManualSection] = useState<string | null>(null);

  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const interpretRequestId = useRef(0);

  const applyRecipe = (recipe: RecipeOption) => {
    setSelected(recipe.ref);
    setAudience(recipe.audience_cluster);
    setDuration(recipe.duration);
    setChannel(recipe.channel);
    setIntent(recipe.intent);
  };

  const applyInterpretation = (result: InterpretResult) => {
    setAudience(result.audience_cluster);
    setDuration(result.duration);
    setChannel(result.channel);
    setIntent(result.intent);
    setTemperature(result.temperature);
    setSelected(result.recipe_ref || "");
    setDeckUseCase(result.deck_use_case || "");
    setInterpretSummary(result.summary);
    setInterpretNotes(result.notes || "");
    setPersonaCandidates(result.persona_candidates || []);
  };

  const clearInterpretationAxes = () => {
    setSelected("");
    setAudience("");
    setDuration("");
    setChannel("");
    setIntent("");
    setTemperature("");
    setDeckUseCase("");
    setInterpretSummary("");
    setInterpretNotes("");
    setPersonaCandidates([]);
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

  const currentBriefKey = briefKey(audienceText, settingText, goalText);

  useEffect(() => {
    const recipe = recipes.find((row) => row.ref === selected);
    if (!selected || !recipe) return;
    setDeckUseCase(useCaseForPersona(recipe.audience_label, temperature));
  }, [selected, temperature, recipes]);

  useEffect(() => {
    if (STEPS[step] !== "review") return;
    if (!audienceText.trim() || !settingText.trim() || !goalText.trim()) return;
    if (currentBriefKey === interpretedKey) return;

    const requestId = ++interpretRequestId.current;
    setInterpreting(true);
    setInterpretFailed(false);
    setError("");

    api
      .interpretBrief({
        audience_text: audienceText.trim(),
        setting_text: settingText.trim(),
        goal_text: goalText.trim(),
      })
      .then((result) => {
        if (requestId !== interpretRequestId.current) return;
        applyInterpretation(result);
        setInterpretedKey(currentBriefKey);
        setManualOpen(false);
      })
      .catch(() => {
        if (requestId !== interpretRequestId.current) return;
        clearInterpretationAxes();
        setInterpretedKey(currentBriefKey);
        setInterpretFailed(true);
        setManualOpen(true);
      })
      .finally(() => {
        if (requestId === interpretRequestId.current) setInterpreting(false);
      });
  }, [step, currentBriefKey, interpretedKey, audienceText, settingText, goalText]);

  const personas = useMemo(
    () => recipes.filter((recipe) => !audience || recipe.audience_cluster === audience),
    [audience, recipes],
  );

  const chosen = recipes.find((recipe) => recipe.ref === selected);

  const go = (next: number) => {
    setDirection(next > step ? 1 : -1);
    setStep(next);
  };

  const canAdvance = (index: number): boolean => {
    const id = STEPS[index];
    if (id === "audience") return Boolean(audienceText.trim());
    if (id === "setting") return Boolean(settingText.trim());
    if (id === "goal") return Boolean(goalText.trim());
    return Boolean(temperature && (selected || (audience && duration && channel && intent)));
  };

  const matchRecipe = (next: { audience: string; duration: string; channel: string; intent: string }) => {
    const sameAxes = (recipe: RecipeOption) =>
      recipe.audience_cluster === next.audience &&
      recipe.duration === next.duration &&
      recipe.channel === next.channel &&
      recipe.intent === next.intent;
    return recipes.find((recipe) => sameAxes(recipe) && recipe.valid) || recipes.find(sameAxes);
  };

  const onManualAudience = (code: string) => {
    setAudience(code);
    setSelected("");
  };

  const onManualPersona = (ref: string) => {
    if (!ref) {
      setSelected("");
      return;
    }
    const recipe = recipes.find((row) => row.ref === ref);
    if (recipe) applyRecipe(recipe);
  };

  const onCandidatePersona = (ref: string) => {
    setSelected((current) => (current === ref ? "" : ref));
  };

  const formatConfidence = (score: number) => `${Math.round(Math.max(0, Math.min(1, score)) * 100)}%`;

  const candidateRows = personaCandidates
    .map((candidate) => {
      const recipe = recipes.find((row) => row.ref === candidate.recipe_ref);
      return recipe ? { candidate, recipe } : null;
    })
    .filter((row): row is { candidate: InterpretPersonaCandidate; recipe: RecipeOption } => Boolean(row));

  const onManualDuration = (code: string) => {
    setDuration(code);
    const match = matchRecipe({ audience, duration: code, channel, intent });
    setSelected(match?.ref ?? "");
  };

  const onManualChannel = (code: string) => {
    setChannel(code);
    const match = matchRecipe({ audience, duration, channel: code, intent });
    setSelected(match?.ref ?? "");
  };

  const onManualIntent = (code: string) => {
    setIntent(code);
    const match = matchRecipe({ audience, duration, channel, intent: code });
    setSelected(match?.ref ?? "");
  };

  const composedContextNote = () => {
    const parts = [context.trim(), interpretNotes.trim()].filter(Boolean);
    return parts.join("\n");
  };

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      const context_note = composedContextNote();
      const body: GenerateWizardPayload = {
        audience_cluster: audience,
        duration,
        channel,
        intent,
        temperature,
        context_note,
        ...(selected ? { recipe_ref: selected } : {}),
        ...(deckUseCase ? { deck_use_case: deckUseCase } : {}),
      };
      await onSubmit(body);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Generation failed");
    } finally {
      setBusy(false);
    }
  };

  const stepId = STEPS[step];
  const progress = ((step + 1) / STEPS.length) * 100;
  const isSubmitting = busy || submitting;

  const slideVariants = {
    enter: (dir: number) => ({ x: dir > 0 ? 48 : -48, opacity: 0 }),
    center: { x: 0, opacity: 1 },
    exit: (dir: number) => ({ x: dir > 0 ? -48 : 48, opacity: 0 }),
  };

  const renderAxisOptions = (options: AxisOption[], value: string, onChange: (code: string) => void) => (
    <div className="scrollbar-none grid w-full grid-cols-2 gap-2.5 overflow-y-auto sm:grid-cols-3">
      {options.map((item) => (
        <OptionCard
          key={item.code}
          compact
          label={item.label}
          description={item.description}
          selected={value === item.code}
          onSelect={() => onChange(item.code)}
        />
      ))}
    </div>
  );

  const renderTextStep = (
    value: string,
    onChange: (next: string) => void,
    chips: string[],
    placeholder: string,
  ) => (
    <div className="glass-panel space-y-5 p-6">
      <label className="block">
        <span className="kicker">Your answer</span>
        <textarea
          className="field mt-3 min-h-[140px]"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          autoFocus
        />
      </label>
      <div>
        <p className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-grey">Examples</p>
        <div className="min-h-[5.25rem]">
          <ChipRow chips={chips} active={value} onPick={onChange} />
        </div>
      </div>
    </div>
  );

  const toggleManualSection = (id: string) => {
    setOpenManualSection((current) => (current === id ? null : id));
  };

  const ManualSection = ({
    id,
    title,
    children,
  }: {
    id: string;
    title: string;
    children: ReactNode;
  }) => {
    const open = openManualSection === id;
    return (
      <div className="glass-panel overflow-hidden">
        <button
          type="button"
          className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left text-sm font-medium text-black hover:bg-white/40"
          onClick={() => toggleManualSection(id)}
          aria-expanded={open}
        >
          <span>{title}</span>
          <span className="text-grey" aria-hidden>
            {open ? "−" : "+"}
          </span>
        </button>
        {open && <div className="border-t border-black/8 px-3 py-3 sm:px-4">{children}</div>}
      </div>
    );
  };

  return (
    <div className="mx-auto flex max-w-4xl flex-col px-6 py-8 md:px-10">
      <div className="mb-8 shrink-0">
        <div className="flex items-center justify-between gap-4 text-xs text-grey">
          <span>
            Step {step + 1} of {STEPS.length}
          </span>
          <span className="capitalize">{stepId}</span>
        </div>
        <div className="mt-3 h-1 overflow-hidden rounded-full bg-grey-light">
          <motion.div
            className="h-full bg-black"
            animate={{ width: `${progress}%` }}
            transition={{ duration: 0.35 }}
          />
        </div>
      </div>

      <div className="relative">
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
            <div className="min-h-[5.5rem]">
              <p className="kicker">{kicker}</p>
              <h1 className="mt-3 font-display text-4xl tracking-tight text-black md:text-5xl">
                {STEP_TITLES[stepId]}
              </h1>
            </div>

            {stepId === "audience" &&
              renderTextStep(
                audienceText,
                setAudienceText,
                AUDIENCE_CHIPS,
                "e.g. parents considering the programme for their child",
              )}

            {stepId === "setting" &&
              renderTextStep(
                settingText,
                setSettingText,
                SETTING_CHIPS,
                "e.g. a 30-minute video call with a short deck",
              )}

            {stepId === "goal" &&
              renderTextStep(
                goalText,
                setGoalText,
                GOAL_CHIPS,
                "e.g. introduce us clearly and invite them to campus",
              )}

            {stepId === "review" && (
              <div className="space-y-5">
                {interpreting && (
                  <div className="glass-panel space-y-4 p-6">
                    <p className="text-sm text-grey">Reading your answers…</p>
                    <div className="grid gap-3 sm:grid-cols-2">
                      {Array.from({ length: 4 }).map((_, index) => (
                        <Skeleton key={index} className="h-16 w-full rounded-2xl" />
                      ))}
                    </div>
                  </div>
                )}

                {!interpreting && (
                  <div className="glass-panel space-y-4 p-6">
                    {interpretFailed && (
                      <p className="text-sm text-grey-dark">
                        Couldn't auto-read your answers — pick the settings below.
                      </p>
                    )}
                    {!interpretFailed && interpretSummary && (
                      <div>
                        <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-grey">
                          What we'll write
                        </p>
                        <p className="mt-2 text-lg leading-relaxed text-black">{interpretSummary}</p>
                      </div>
                    )}
                    {!interpretFailed && (
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
                            <dt className="text-[11px] font-semibold uppercase tracking-[0.18em] text-grey">
                              {label}
                            </dt>
                            <dd className="mt-1 text-base text-black">{value || "—"}</dd>
                          </div>
                        ))}
                      </dl>
                    )}

                    <label className="block">
                      <span className="kicker">Deck use case</span>
                      <select
                        className="field mt-3"
                        value={deckUseCase}
                        onChange={(e) => setDeckUseCase(e.target.value)}
                      >
                        {DECK_USE_CASES.map((row) => (
                          <option key={row.code || "none"} value={row.code}>
                            {row.label}
                          </option>
                        ))}
                      </select>
                    </label>

                    {!interpretFailed && candidateRows.length > 0 && (
                      <div>
                        <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-grey">
                          {selected ? "Persona confidence" : "Close persona matches — pick one"}
                        </p>
                        <div className="mt-3 grid gap-2 sm:grid-cols-2">
                          {candidateRows.map(({ candidate, recipe }) => {
                            const active = selected === recipe.ref;
                            return (
                              <button
                                key={recipe.ref}
                                type="button"
                                className={`rounded-2xl border px-4 py-3 text-left transition ${
                                  active
                                    ? "border-black bg-black text-white"
                                    : "border-black/10 bg-white/60 text-black hover:border-black/30"
                                }`}
                                onClick={() => onCandidatePersona(recipe.ref)}
                              >
                                <div className="flex items-start justify-between gap-3">
                                  <span className="text-sm font-medium leading-snug">
                                    {recipe.audience_label || recipe.ref}
                                  </span>
                                  <span className={`shrink-0 text-sm tabular-nums ${active ? "text-white/80" : "text-grey-dark"}`}>
                                    {formatConfidence(candidate.confidence)}
                                  </span>
                                </div>
                                {candidate.rationale && (
                                  <p className={`mt-1 text-xs leading-relaxed ${active ? "text-white/70" : "text-grey"}`}>
                                    {candidate.rationale}
                                  </p>
                                )}
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    )}

                    <label className="block">
                      <span className="kicker">Anything else we should know?</span>
                      <textarea
                        className="field mt-3 min-h-[120px]"
                        value={context}
                        onChange={(e) => setContext(e.target.value)}
                        placeholder="Optional details to fold into the pitch"
                      />
                    </label>

                    {manualOpen && (
                      <div className="space-y-2 border-t border-black/8 pt-4">
                        <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-grey">
                          Manual settings
                        </p>
                        <p className="text-xs text-grey">
                          Override audience, persona, duration, and other axes yourself.
                        </p>
                        {recipesLoading ? (
                          <div className="grid gap-3 sm:grid-cols-2">
                            {Array.from({ length: 4 }).map((_, index) => (
                              <Skeleton key={index} className="h-28 w-full rounded-2xl" />
                            ))}
                          </div>
                        ) : (
                          <>
                            <ManualSection id="audience" title="Audience">
                              {renderAxisOptions(AXES.audience_clusters, audience, onManualAudience)}
                            </ManualSection>
                            <ManualSection id="persona" title="Persona">
                              <div className="scrollbar-none grid max-h-[min(50vh,24rem)] w-full grid-cols-1 gap-2.5 overflow-y-auto sm:grid-cols-2">
                                <OptionCard
                                  compact
                                  label="Skip persona"
                                  description="Continue with axes only — generation will follow your selections."
                                  selected={!selected}
                                  onSelect={() => onManualPersona("")}
                                />
                                {personas.map((recipe) => (
                                  <OptionCard
                                    key={recipe.ref}
                                    compact
                                    label={personaOptionLabel(recipe, personas)}
                                    description={
                                      recipe.valid
                                        ? `${axisLabel(AXES.durations, recipe.duration)} · ${axisLabel(AXES.channels, recipe.channel)} · ${axisLabel(AXES.intents, recipe.intent)}`
                                        : "This persona’s recipe still needs a module sequence."
                                    }
                                    selected={selected === recipe.ref}
                                    disabled={!recipe.valid}
                                    onSelect={() => onManualPersona(recipe.ref)}
                                  />
                                ))}
                                {personas.length === 0 && (
                                  <p className="text-sm text-grey sm:col-span-2">
                                    No personas in this audience. You can continue without one.
                                  </p>
                                )}
                              </div>
                            </ManualSection>
                            <ManualSection id="duration" title="Duration">
                              {renderAxisOptions(AXES.durations, duration, onManualDuration)}
                            </ManualSection>
                            <ManualSection id="channel" title="Channel">
                              {renderAxisOptions(AXES.channels, channel, onManualChannel)}
                            </ManualSection>
                            <ManualSection id="intent" title="Intent">
                              {renderAxisOptions(AXES.intents, intent, onManualIntent)}
                            </ManualSection>
                            <ManualSection id="temperature" title="Temperature">
                              {renderAxisOptions(AXES.temperatures, temperature, setTemperature)}
                            </ManualSection>
                          </>
                        )}
                      </div>
                    )}

                    <ErrorBanner message={error} />
                    <div className="flex flex-wrap items-center justify-end gap-3 pt-2">
                      <Button
                        type="button"
                        disabled={isSubmitting || interpreting}
                        onClick={() => {
                          setManualOpen((open) => {
                            if (open) setOpenManualSection(null);
                            return !open;
                          });
                        }}
                      >
                        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden>
                          <path
                            d="M11.3 2.3a1 1 0 0 1 1.4 0l1 1a1 1 0 0 1 0 1.4l-8.2 8.2L3 13.5l.6-2.5 8.2-8.2Z"
                            stroke="currentColor"
                            strokeWidth="1.4"
                            strokeLinejoin="round"
                          />
                        </svg>
                        {manualOpen ? "Hide manual settings" : "Adjust manually"}
                      </Button>
                      <Button
                        variant="accent"
                        className="py-3 text-base"
                        loading={isSubmitting}
                        disabled={!canAdvance(step) || interpreting}
                        onClick={submit}
                      >
                        {submitLabel}
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            )}
          </motion.div>
        </AnimatePresence>
      </div>

      <div className="mt-8 flex shrink-0 items-center justify-between gap-4 border-t border-black/8 pt-6">
        <Button
          variant={step === 0 ? "ghost" : "default"}
          disabled={step === 0 || isSubmitting || interpreting}
          onClick={() => go(step - 1)}
        >
          Back
        </Button>
        {stepId !== "review" && (
          <Button variant="accent" disabled={!canAdvance(step)} onClick={() => go(step + 1)}>
            Next
          </Button>
        )}
      </div>
    </div>
  );
}
