import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api";
import type { FactRow, Generation } from "../types";

const STEPS = ["queued", "generating_script", "validating", "generating_deck", "rendering", "done"];

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
  const [facts, setFacts] = useState<FactRow[]>([]);
  const [open, setOpen] = useState<number | null>(null);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    const tick = async () => {
      const data = (await api.getGeneration(Number(id))) as Generation;
      if (!cancelled) setGeneration(data);
      return data;
    };
    tick();
    const timer = window.setInterval(async () => {
      const data = await tick();
      if (data.status === "done" || data.status === "failed") {
        window.clearInterval(timer);
      }
    }, 2000);
    api.list<FactRow>("/api/admin/facts").then(setFacts).catch(() => setFacts([]));
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [id]);

  const factValues = useMemo(
    () => facts.filter((fact) => fact.status === "verified" && fact.value.length > 3).map((fact) => fact.value),
    [facts],
  );

  if (!generation) return <p className="text-ink/50">Loading generation…</p>;

  const currentIndex = STEPS.indexOf(generation.status);

  return (
    <div className="space-y-8">
      <div>
        <p className="text-xs uppercase tracking-widest text-accent">
          {generation.recipe_ref || "resolving"} · {generation.module_sequence || "…"}
        </p>
        <h1 className="font-serif text-3xl">Pitch {generation.id}</h1>
        <p className="text-sm text-ink/55">
          {generation.audience_cluster} / {generation.duration} / {generation.channel} / {generation.intent} /{" "}
          {generation.temperature}
        </p>
      </div>
      <ol className="flex flex-wrap gap-3 text-xs">
        {STEPS.map((step, index) => (
          <li
            key={step}
            className={`rounded-full px-3 py-1 ${
              generation.status === "failed"
                ? "bg-red-100 text-red-800"
                : index <= currentIndex
                  ? "bg-accent text-white"
                  : "bg-ink/10 text-ink/50"
            }`}
          >
            {step.replaceAll("_", " ")}
          </li>
        ))}
      </ol>
      {generation.status === "failed" && (
        <div className="rounded border border-red-200 bg-red-50 p-4 text-sm text-red-900">
          <p className="font-medium">{generation.error || "Generation failed"}</p>
          {generation.validation_report && <pre className="mt-2 whitespace-pre-wrap">{generation.validation_report}</pre>}
        </div>
      )}
      {generation.script && (
        <section className="space-y-6">
          {generation.script.sections.map((section) => (
            <article key={section.module_id} className="rounded-xl bg-white p-6 shadow-sm">
              <p className="text-xs uppercase tracking-widest text-accent">{section.module_id}</p>
              <h2 className="font-serif text-2xl">{section.heading}</h2>
              <p className="mt-3 whitespace-pre-wrap leading-7">{highlight(section.text, factValues)}</p>
            </article>
          ))}
          {generation.script.cta && (
            <p className="rounded-xl bg-accent px-6 py-4 text-white">{generation.script.cta}</p>
          )}
        </section>
      )}
      {generation.pptx_path && (
        <a
          className="inline-block rounded bg-ink px-5 py-2 text-white"
          href={`/api/generations/${generation.id}/deck.pptx`}
        >
          Download deck (.pptx)
        </a>
      )}
      {generation.assets.length > 0 && (
        <section>
          <h2 className="font-serif text-2xl">Assets</h2>
          <div className="mt-3 grid gap-4 md:grid-cols-3">
            {["video", "photo", "report"].map((type) => (
              <div key={type} className="rounded-xl bg-white p-4">
                <h3 className="text-sm font-semibold uppercase tracking-wide">{type}s</h3>
                <ul className="mt-2 space-y-2 text-sm">
                  {generation.assets
                    .filter((asset) => asset.type === type)
                    .map((asset) => {
                      const stored = asset.file_status === "stored" && asset.file_key;
                      const href = stored
                        ? `/api/assets/${asset.id}/file`
                        : asset.url || asset.source_url || "";
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
        </section>
      )}
      {(generation.founder_quotes || []).length > 0 && (
        <section>
          <h2 className="font-serif text-2xl">Founder voice</h2>
          <ul className="mt-3 space-y-2 text-sm">
            {(generation.founder_quotes || []).map((quote) => {
              const minutes = Math.floor((quote.start_sec || 0) / 60);
              const seconds = Math.floor((quote.start_sec || 0) % 60)
                .toString()
                .padStart(2, "0");
              return (
                <li key={quote.id} className="rounded-xl bg-white p-4">
                  <p className="text-ink/80">
                    Founder voice: {quote.speaker}, {quote.source_name || "transcript"} @ {minutes}:{seconds}
                  </p>
                  <p className="mt-1 italic text-ink/70">“{quote.text}”</p>
                </li>
              );
            })}
          </ul>
        </section>
      )}
      {generation.objections.length > 0 && (
        <section>
          <h2 className="font-serif text-2xl">Objection prep</h2>
          <div className="mt-3 space-y-2">
            {generation.objections.map((item) => (
              <button
                type="button"
                key={item.id}
                className="w-full rounded-xl bg-white p-4 text-left"
                onClick={() => setOpen(open === item.id ? null : item.id)}
              >
                <p className="font-medium">{item.question}</p>
                {open === item.id && (
                  <div className="mt-2 text-sm text-ink/70">
                    <p>
                      <strong>Move:</strong> {item.move}
                    </p>
                    <p className="mt-1">{item.answer}</p>
                  </div>
                )}
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
