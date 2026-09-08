import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api";
import DeckPreview from "../components/DeckPreview";
import { generationAxisLabels, personaLabel } from "../labels";
import { formatModuleSequence, moduleName } from "../modules";
import type { FactRow, Generation, RecipeOption } from "../types";

const STEPS = ["queued", "generating_script", "validating", "generating_deck", "rendering", "done"];
const TABS = ["Script", "Deck", "Assets & Evidence", "Q&A"] as const;

function highlight(text: string, values: string[]) {
  if (!values.length) return text;
  const escaped = values.map((value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).filter(Boolean);
  if (!escaped.length) return text;
  const regex = new RegExp(`(${escaped.join("|")})`, "g");
  return text.split(regex).map((part, index) =>
    values.includes(part) ? <mark key={index}>{part}</mark> : <span key={index}>{part}</span>,
  );
}

export default function Result() {
  const { id } = useParams();
  const [generation, setGeneration] = useState<Generation | null>(null);
  const [recipes, setRecipes] = useState<RecipeOption[]>([]);
  const [facts, setFacts] = useState<FactRow[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  const [tab, setTab] = useState<(typeof TABS)[number]>("Deck");

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    const tick = async () => {
      const data = (await api.getGeneration(Number(id))) as Generation;
      if (!cancelled) {
        setGeneration((prev) => {
          if (prev && prev.status === "done" && data.status === "done") return prev;
          return data;
        });
      }
      return data;
    };
    tick();
    const timer = window.setInterval(async () => {
      const data = await tick();
      if (data.status === "done" || data.status === "failed") {
        window.clearInterval(timer);
      }
    }, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [id]);

  useEffect(() => {
    api.recipes().then((data) => setRecipes(data as RecipeOption[])).catch(() => setRecipes([]));
  }, []);

  useEffect(() => {
    if (tab !== "Script" || !generation?.script || facts.length) return;
    api.list<FactRow>("/api/admin/facts").then(setFacts).catch(() => setFacts([]));
  }, [tab, generation?.script, facts.length]);

  const factValues = useMemo(
    () => facts.filter((fact) => fact.status === "verified" && fact.value.length > 3).map((fact) => fact.value),
    [facts],
  );

  if (!generation) return <p className="text-muted">Loading generation…</p>;

  const currentIndex = STEPS.indexOf(generation.status);
  const progress = generation.status === "failed" ? 100 : Math.max(8, ((currentIndex + 1) / STEPS.length) * 100);
  const axisNames = generationAxisLabels(generation);
  const personaName =
    personaLabel(recipes, generation.recipe_ref, generation) || axisNames.audience || "Resolving persona…";
  const modulePath = formatModuleSequence(generation.module_sequence);
  const axisRows = [
    ["Persona", personaName],
    ["Audience", axisNames.audience],
    ["Duration", axisNames.duration],
    ["Channel", axisNames.channel],
    ["Intent", axisNames.intent],
    ["Temperature", axisNames.temperature],
  ];

  return (
    <div className="space-y-8">
      <div>
        <p className="kicker">Pitch {generation.id}</p>
        <h1 className="mt-2 font-display text-4xl">{personaName}</h1>
        <dl className="mt-4 grid gap-3 sm:grid-cols-2">
          {axisRows.map(([label, value]) => (
            <div key={label}>
              <dt className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent">{label}</dt>
              <dd className="mt-1 text-sm text-ink">{value || "—"}</dd>
            </div>
          ))}
        </dl>
        <p className="mt-4 text-sm text-muted">
          {modulePath || "Resolving module recipe…"}
        </p>
      </div>

      <div>
        <div className="h-1.5 overflow-hidden rounded-full bg-line">
          <div
            className={`h-full ${generation.status === "failed" ? "bg-danger" : "bg-accent"}`}
            style={{ width: `${progress}%` }}
          />
        </div>
        <ol className="mt-3 flex flex-wrap gap-2 text-xs text-muted">
          {STEPS.map((step, index) => (
            <li key={step} className={index <= currentIndex ? "text-ink" : ""}>
              {step.replaceAll("_", " ")}
            </li>
          ))}
        </ol>
      </div>

      {generation.status === "failed" && (
        <div className="rounded-2xl border border-danger/20 bg-danger/5 p-4 text-sm text-danger">
          <p className="font-medium">{generation.error || "Generation failed"}</p>
          {generation.validation_report && <pre className="mt-2 whitespace-pre-wrap">{generation.validation_report}</pre>}
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        {TABS.map((item) => (
          <button
            key={item}
            type="button"
            className={`chip ${tab === item ? "chip-active" : ""}`}
            onClick={() => setTab(item)}
          >
            {item}
          </button>
        ))}
      </div>

      {tab === "Script" && generation.script && (
        <section className="space-y-6 animate-slide-up">
          {generation.script.sections.map((section) => (
            <article key={section.module_id} className="card p-6">
              <p className="kicker">{moduleName(section.module_id)}</p>
              <h2 className="mt-2 font-display text-2xl">{section.heading}</h2>
              <p className="mt-3 whitespace-pre-wrap leading-7">{highlight(section.text, factValues)}</p>
            </article>
          ))}
          {generation.script.cta && <p className="rounded-2xl bg-accent px-6 py-4 text-white">{generation.script.cta}</p>}
        </section>
      )}

      {tab === "Deck" && (
        <section className="space-y-4 animate-slide-up">
          <DeckPreview key={generation.id} slides={generation.deck_spec?.slides ?? []} />
          {generation.pptx_path && (
            <a className="btn-accent inline-block" href={`/api/generations/${generation.id}/deck.pptx`}>
              Download deck (.pptx)
            </a>
          )}
        </section>
      )}

      {tab === "Assets & Evidence" && (
        <section className="space-y-8 animate-slide-up">
          {generation.assets.length > 0 && (
            <div>
              <h2 className="font-display text-2xl">Assets</h2>
              <div className="mt-3 grid gap-4 md:grid-cols-3">
                {["video", "photo", "report"].map((type) => (
                  <div key={type} className="card p-4">
                    <h3 className="kicker">{type}s</h3>
                    <ul className="mt-3 space-y-2 text-sm">
                      {generation.assets
                        .filter((asset) => asset.type === type)
                        .map((asset) => {
                          const stored = asset.file_status === "stored" && asset.file_key;
                          const href = stored ? `/api/assets/${asset.id}/file` : asset.url || asset.source_url || "";
                          return (
                            <li key={asset.id}>
                              {href ? (
                                <a className="text-accent underline" href={href} target={stored ? undefined : "_blank"} rel="noreferrer">
                                  {asset.title}
                                </a>
                              ) : (
                                <span>
                                  {asset.title}
                                  {asset.file_status === "gap" ? " (not available)" : ""}
                                </span>
                              )}
                            </li>
                          );
                        })}
                    </ul>
                  </div>
                ))}
              </div>
            </div>
          )}
          {(generation.report_passages || []).length > 0 && (
            <div>
              <h2 className="font-display text-2xl">Report evidence</h2>
              <ul className="mt-3 space-y-2">
                {(generation.report_passages || []).map((passage, index) => (
                  <li key={`${passage.asset_id}-${index}`} className="card p-4 text-sm">
                    <p className="text-ink/80">
                      {passage.title}
                      {passage.start_page
                        ? ` · p.${passage.start_page}${
                            passage.end_page && passage.end_page !== passage.start_page ? `–${passage.end_page}` : ""
                          }`
                        : ""}
                    </p>
                    <p className="mt-1 text-muted">{passage.text}</p>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {(generation.founder_quotes || []).length > 0 && (
            <div>
              <h2 className="font-display text-2xl">Founder voice</h2>
              <ul className="mt-3 space-y-2">
                {(generation.founder_quotes || []).map((quote) => {
                  const minutes = Math.floor((quote.start_sec || 0) / 60);
                  const seconds = Math.floor((quote.start_sec || 0) % 60)
                    .toString()
                    .padStart(2, "0");
                  return (
                    <li key={quote.id} className="card p-4 text-sm">
                      <p className="text-ink/80">
                        Founder voice: {quote.speaker}, {quote.source_name || "transcript"} @ {minutes}:{seconds}
                      </p>
                      <p className="mt-1 italic text-muted">“{quote.text}”</p>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </section>
      )}

      {tab === "Q&A" && (
        <section className="animate-slide-up">
          <h2 className="font-display text-2xl">Objection prep</h2>
          <div className="mt-3 space-y-2">
            {generation.objections.map((item) => (
              <button
                type="button"
                key={item.id}
                className="card w-full p-4 text-left"
                onClick={() => setOpen(open === item.id ? null : item.id)}
              >
                <p className="font-medium">{item.question}</p>
                {open === item.id && (
                  <div className="mt-2 text-sm text-muted">
                    <p>
                      <strong>Move:</strong> {item.move}
                    </p>
                    <p className="mt-1">{item.answer}</p>
                  </div>
                )}
              </button>
            ))}
            {generation.objections.length === 0 && <p className="text-sm text-muted">No objections attached.</p>}
          </div>
        </section>
      )}
    </div>
  );
}
