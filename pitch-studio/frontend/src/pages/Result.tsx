import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api";
import DeckPreview from "../components/DeckPreview";
import { ErrorBanner, Skeleton, Spinner } from "../components/ui";
import { generationAxisLabels, personaLabel } from "../labels";
import { formatModuleSequence, moduleName } from "../modules";
import type { FactRow, Generation, RecipeOption, RecommendedMedia } from "../types";

const STEPS = ["queued", "generating_script", "validating", "generating_deck", "rendering", "done"];
const TABS = ["Script", "Deck", "Assets & Evidence", "Q&A"] as const;

function slideRange(pages?: number[]) {
  if (!pages?.length) return "";
  const first = pages[0];
  const last = pages[pages.length - 1];
  if (pages.length === 1 || first === last) return `p${first}`;
  return `p${first}–${last}`;
}

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
  const [factsLoading, setFactsLoading] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const [tab, setTab] = useState<(typeof TABS)[number]>("Deck");
  const [loadError, setLoadError] = useState("");

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
    tick().catch((err) => setLoadError(err instanceof Error ? err.message : "Failed to load generation"));
    const timer = window.setInterval(async () => {
      try {
        const data = await tick();
        if (data.status === "done" || data.status === "failed") {
          window.clearInterval(timer);
        }
      } catch {
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
    setFactsLoading(true);
    api
      .list<FactRow>("/api/admin/facts")
      .then(setFacts)
      .catch(() => setFacts([]))
      .finally(() => setFactsLoading(false));
  }, [tab, generation?.script, facts.length]);

  const factValues = useMemo(
    () => facts.filter((fact) => fact.status === "verified" && fact.value.length > 3).map((fact) => fact.value),
    [facts],
  );

  if (!generation) {
    return (
      <div className="space-y-6">
        <ErrorBanner message={loadError} />
        <Skeleton className="h-4 w-24" />
        <Skeleton className="h-10 w-72" />
        <div className="grid gap-3 sm:grid-cols-2">
          {Array.from({ length: 6 }).map((_, index) => (
            <Skeleton key={index} className="h-12 w-full" />
          ))}
        </div>
        <Skeleton className="h-1.5 w-full" />
        <Skeleton className="aspect-[16/9] w-full" />
      </div>
    );
  }

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

      {tab === "Script" && !generation.script && (
        <p className="flex items-center gap-2 text-sm text-muted">
          <Spinner /> Writing the script…
        </p>
      )}
      {tab === "Script" && factsLoading && (
        <p className="flex items-center gap-2 text-sm text-muted">
          <Spinner /> Loading locked facts…
        </p>
      )}
      {tab === "Script" && generation.script && (
        <section className="space-y-6 animate-slide-up">
          {generation.script.sections.map((section, index) => {
            const topicName = section.topic_title?.trim() || moduleName(section.module_id || "");
            const range = slideRange(section.pages);
            return (
              <article key={`${section.topic_id || section.module_id || "section"}-${index}`} className="card p-6">
                <p className="kicker">
                  {topicName}
                  {range ? ` · ${range}` : ""}
                </p>
                <h2 className="mt-2 font-display text-2xl">{section.heading}</h2>
                <p className="mt-3 whitespace-pre-wrap leading-7">{highlight(section.text, factValues)}</p>
              </article>
            );
          })}
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
          <RecommendedVideos videos={generation.recommended_videos || []} />
          <RecommendedPictures pictures={generation.recommended_pictures || []} />
          {generation.assets.filter((asset) => asset.type === "report").length > 0 && (
            <div>
              <h2 className="font-display text-2xl">Reports</h2>
              <ul className="mt-3 space-y-2 text-sm">
                {generation.assets
                  .filter((asset) => asset.type === "report")
                  .map((asset) => {
                    const stored = asset.file_status === "stored" && asset.file_key;
                    const href = stored ? `/api/assets/${asset.id}/file` : asset.url || asset.source_url || "";
                    return (
                      <li key={asset.id} className="card p-4">
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

function youtubeEmbed(url: string): string | null {
  try {
    const parsed = new URL(url);
    if (parsed.hostname.includes("youtu.be")) {
      const id = parsed.pathname.replace("/", "").split("/")[0];
      return id ? `https://www.youtube.com/embed/${id}` : null;
    }
    const id = parsed.searchParams.get("v");
    return id ? `https://www.youtube.com/embed/${id}` : null;
  } catch {
    return null;
  }
}

function driveEmbed(url: string): string | null {
  try {
    const parsed = new URL(url);
    if (!parsed.hostname.includes("drive.google.com")) return null;
    const pathMatch = parsed.pathname.match(/\/file\/d\/([^/]+)/);
    const id = pathMatch?.[1] || parsed.searchParams.get("id");
    return id ? `https://drive.google.com/file/d/${id}/preview` : null;
  } catch {
    return null;
  }
}

function RecommendedVideos({ videos }: { videos: RecommendedMedia[] }) {
  return (
    <div>
      <h2 className="font-display text-2xl">Recommended videos</h2>
      {videos.length === 0 ? (
        <p className="mt-3 text-sm text-muted">No recommended videos for this usecase yet.</p>
      ) : (
        <div className="mt-3 grid gap-4 md:grid-cols-2">
          {videos.map((video) => {
            const embed = driveEmbed(video.source_url) || youtubeEmbed(video.source_url);
            return (
              <article key={video.asset_id} className="card overflow-hidden">
                {embed ? (
                  <div className="aspect-video bg-ink">
                    <iframe className="h-full w-full" src={embed} title={video.title} allow="autoplay; fullscreen" allowFullScreen />
                  </div>
                ) : null}
                <div className="space-y-2 p-4">
                  <h3 className="font-medium">{video.title}</h3>
                  {video.rationale ? <p className="text-sm text-muted">{video.rationale}</p> : null}
                  {video.source_url ? (
                    <a className="text-sm text-accent underline" href={video.source_url} target="_blank" rel="noreferrer">
                      Open source
                    </a>
                  ) : null}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}

function RecommendedPictures({ pictures }: { pictures: RecommendedMedia[] }) {
  return (
    <div>
      <h2 className="font-display text-2xl">Recommended pictures</h2>
      {pictures.length === 0 ? (
        <p className="mt-3 text-sm text-muted">No recommended pictures for this usecase yet.</p>
      ) : (
        <div className="mt-3 grid grid-cols-2 gap-3 md:grid-cols-5">
          {pictures.map((picture) => (
            <RecommendedPicture key={picture.asset_id} picture={picture} />
          ))}
        </div>
      )}
    </div>
  );
}

function RecommendedPicture({ picture }: { picture: RecommendedMedia }) {
  const [open, setOpen] = useState(false);
  const [thumbLoaded, setThumbLoaded] = useState(false);
  const [fullLoaded, setFullLoaded] = useState(false);
  const [fullFailed, setFullFailed] = useState(false);

  useEffect(() => {
    if (!thumbLoaded || !picture.preview_url) return;
    const image = new Image();
    image.src = picture.preview_url;
  }, [thumbLoaded, picture.preview_url]);

  useEffect(() => {
    if (!open) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [open]);

  return (
    <>
      <button
        type="button"
        className="overflow-hidden rounded-xl border border-line bg-paper text-left"
        onClick={() => {
          setFullLoaded(false);
          setFullFailed(false);
          setOpen(true);
        }}
        aria-label={`View ${picture.title}`}
      >
        <div className="relative aspect-[4/3] bg-paper">
          {!thumbLoaded && <Skeleton className="absolute inset-0 h-full w-full rounded-none" />}
          {picture.thumbnail_url ? (
            <img
              src={picture.thumbnail_url}
              alt={picture.title}
              className="h-full w-full object-cover"
              onLoad={() => setThumbLoaded(true)}
            />
          ) : null}
        </div>
        <p className="truncate px-2 py-1.5 text-xs text-muted">{picture.title}</p>
      </button>
      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-ink/90 p-4"
          role="dialog"
          aria-modal="true"
          aria-label={picture.title}
          onClick={() => setOpen(false)}
        >
          {!fullLoaded && !fullFailed && <Spinner className="absolute h-8 w-8 text-white" />}
          {fullFailed && (
            <div className="rounded-xl bg-surface px-6 py-5 text-center" onClick={(event) => event.stopPropagation()}>
              <p className="font-medium">Image preview unavailable</p>
              {picture.source_url ? (
                <a className="mt-2 inline-block text-sm text-accent underline" href={picture.source_url} target="_blank" rel="noreferrer">
                  Open in Drive
                </a>
              ) : null}
            </div>
          )}
          {picture.preview_url && !fullFailed ? (
            <img
              src={picture.preview_url}
              alt={picture.title}
              className={`max-h-[90vh] max-w-[94vw] object-contain ${fullLoaded ? "block" : "hidden"}`}
              onLoad={() => setFullLoaded(true)}
              onError={() => setFullFailed(true)}
              onClick={(event) => event.stopPropagation()}
            />
          ) : null}
          <button type="button" className="btn-ghost absolute right-4 top-4 border-white/30 bg-ink/70 text-white" onClick={() => setOpen(false)}>
            Close
          </button>
        </div>
      )}
    </>
  );
}
