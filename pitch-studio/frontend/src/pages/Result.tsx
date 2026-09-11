import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api";
import SlideStage from "../components/SlideStage";
import { ErrorBanner, Skeleton, Spinner } from "../components/ui";
import { generationAxisLabels, personaLabel } from "../labels";
import { moduleName } from "../modules";
import type { DeckSlide, FactRow, Generation, ObjectionRow, RecipeOption, RecommendedMedia } from "../types";

const PIPELINE = ["queued", "generating_script", "validating", "generating_deck", "rendering", "done"] as const;
const STAGGER_S = 0.14;

type TimelineId = "script" | "videos" | "photos" | "qa";

const TIMELINE: { id: TimelineId; label: string; sectionId: string }[] = [
  { id: "script", label: "Script & Deck", sectionId: "section-script" },
  { id: "videos", label: "Videos", sectionId: "section-videos" },
  { id: "photos", label: "Photos", sectionId: "section-photos" },
  { id: "qa", label: "Q&A", sectionId: "section-qa" },
];

function statusMessage(status: string): string {
  switch (status) {
    case "queued":
      return "Queued — waiting to start…";
    case "generating_script":
      return "Writing the script…";
    case "validating":
      return "Validating facts and structure…";
    case "generating_deck":
      return "Assembling the deck…";
    case "rendering":
      return "Rendering slides…";
    case "failed":
      return "Generation failed";
    case "done":
      return "Ready";
    default:
      return status.replaceAll("_", " ");
  }
}

function pipelineStage(status: string): string {
  const index = PIPELINE.indexOf(status as (typeof PIPELINE)[number]);
  if (index < 0) return status;
  return `Stage ${index + 1}/${PIPELINE.length}`;
}

/** Honest staged presentation: only Script & Deck is active while generating. */
function entryState(id: TimelineId, status: string): "pending" | "active" | "done" {
  if (status === "failed") return id === "script" ? "active" : "pending";
  if (status === "done") return "done";
  return id === "script" ? "active" : "pending";
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

function slidesForSection(pages: number[] | undefined, slides: DeckSlide[]): DeckSlide[] {
  if (!pages?.length || !slides.length) return [];
  const byPage = new Map<number, DeckSlide>();
  for (const slide of slides) {
    if (slide.page != null) byPage.set(slide.page, slide);
  }
  return pages.map((page) => byPage.get(page)).filter((slide): slide is DeckSlide => Boolean(slide));
}

export default function Result() {
  const { id } = useParams();
  const [generation, setGeneration] = useState<Generation | null>(null);
  const [recipes, setRecipes] = useState<RecipeOption[]>([]);
  const [facts, setFacts] = useState<FactRow[]>([]);
  const [loadError, setLoadError] = useState("");
  const [activeSection, setActiveSection] = useState<string>("section-script");
  const [openObjection, setOpenObjection] = useState<number | null>(null);
  const [navReady, setNavReady] = useState(false);

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
    if (!generation?.script || facts.length) return;
    api
      .list<FactRow>("/api/admin/facts")
      .then(setFacts)
      .catch(() => setFacts([]));
  }, [generation?.script, facts.length]);

  useEffect(() => {
    if (generation?.status !== "done") {
      setNavReady(false);
      return;
    }
    const totalMs = (TIMELINE.length - 1) * STAGGER_S * 1000 + 280;
    const timer = window.setTimeout(() => setNavReady(true), totalMs);
    return () => window.clearTimeout(timer);
  }, [generation?.status, generation?.id]);

  useEffect(() => {
    if (!navReady) return;
    const nodes = TIMELINE.map((item) => document.getElementById(item.sectionId)).filter(
      (node): node is HTMLElement => Boolean(node),
    );
    if (!nodes.length) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio);
        if (visible[0]?.target?.id) setActiveSection(visible[0].target.id);
      },
      { rootMargin: "-20% 0px -55% 0px", threshold: [0.15, 0.35, 0.55] },
    );
    nodes.forEach((node) => observer.observe(node));
    return () => observer.disconnect();
  }, [navReady, generation?.id]);

  const factValues = useMemo(
    () => facts.filter((fact) => fact.status === "verified" && fact.value.length > 3).map((fact) => fact.value),
    [facts],
  );

  if (!generation) {
    return (
      <div className="mx-auto max-w-5xl space-y-6 px-6 py-10">
        <ErrorBanner message={loadError} />
        <Skeleton className="h-4 w-24" />
        <Skeleton className="h-10 w-72" />
        <Skeleton className="aspect-[16/9] w-full" />
      </div>
    );
  }

  const done = generation.status === "done";
  const generating = generation.status !== "done" && generation.status !== "failed";
  const axisNames = generationAxisLabels(generation);
  const personaName =
    personaLabel(recipes, generation.recipe_ref, generation) || axisNames.audience || "Resolving persona…";
  const deckSlides = generation.deck_spec?.slides ?? [];

  const scrollTo = (sectionId: string) => {
    const node = document.getElementById(sectionId);
    node?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <div className="relative mx-auto flex max-w-7xl gap-8 px-4 py-8 md:px-8">
      <aside className="sticky top-24 hidden h-[calc(100vh-8rem)] w-52 shrink-0 lg:block xl:w-56">
        <TimelineSidebar
          status={generation.status}
          statusText={statusMessage(generation.status)}
          done={done}
          navReady={navReady}
          activeSection={activeSection}
          onNavigate={scrollTo}
        />
      </aside>

      <div className="min-w-0 flex-1 space-y-20 pb-24">
        {generating && (
          <div className="glass-panel flex items-center justify-between gap-3 px-4 py-3 lg:hidden">
            <p className="flex min-w-0 items-center gap-2 text-sm text-grey-dark">
              <Spinner />
              <span className="truncate">{statusMessage(generation.status)}</span>
            </p>
            <span className="shrink-0 text-[11px] uppercase tracking-[0.16em] text-grey">
              {pipelineStage(generation.status)}
            </span>
          </div>
        )}

        <header className="space-y-3">
          <p className="kicker">Pitch {generation.id}</p>
          <h1 className="font-display text-4xl tracking-tight text-black md:text-5xl">{personaName}</h1>
          <p className="text-sm text-grey">
            {[axisNames.audience, axisNames.duration, axisNames.channel, axisNames.intent, axisNames.temperature]
              .filter(Boolean)
              .join(" · ")}
          </p>
          {generation.pptx_path && (
            <a className="btn-accent inline-flex" href={`/api/generations/${generation.id}/deck.pptx`}>
              Download deck (.pptx)
            </a>
          )}
        </header>

        {generation.status === "failed" && (
          <div className="rounded-2xl border border-danger/20 bg-danger/5 p-4 text-sm text-danger">
            <p className="font-medium">{generation.error || "Generation failed"}</p>
            {generation.validation_report && (
              <pre className="mt-2 whitespace-pre-wrap">{generation.validation_report}</pre>
            )}
          </div>
        )}

        <section id="section-script" className="scroll-mt-28 space-y-16">
          <SectionHeading title="Script & Deck" />
          {!generation.script && generation.status !== "failed" && (
            <p className="flex items-center gap-2 text-sm text-grey">
              <Spinner /> {statusMessage(generation.status)}
            </p>
          )}
          {generation.script?.sections.map((section, index) => {
            const topicName = section.topic_title?.trim() || moduleName(section.module_id || "") || section.heading;
            const matched = slidesForSection(section.pages, deckSlides);
            return (
              <article
                key={`${section.topic_id || section.module_id || "section"}-${index}`}
                className="min-h-[calc(100vh-5rem)] space-y-8"
              >
                {matched.length > 0 ? (
                  <SlideStage slides={matched} />
                ) : (
                  <div className="glass-panel flex min-h-[40vh] items-center justify-center p-8 text-center text-grey">
                    {generation.status === "done"
                      ? "No deck slides linked to this topic."
                      : "Slides will appear as the deck is built…"}
                  </div>
                )}
                <div className="max-w-3xl pb-10">
                  <p className="kicker">{topicName}</p>
                  <h2 className="mt-2 font-display text-2xl text-black md:text-3xl">{section.heading}</h2>
                  <p className="mt-4 whitespace-pre-wrap text-base leading-8 text-grey-dark">
                    {highlight(section.text, factValues)}
                  </p>
                </div>
              </article>
            );
          })}
          {generation.script?.cta && (
            <p className="glass-panel inline-block px-6 py-4 text-base text-black">{generation.script.cta}</p>
          )}
        </section>

        <section id="section-videos" className="scroll-mt-28">
          <SectionHeading title="Videos" />
          <VideoCarousel videos={generation.recommended_videos || []} ready={done} />
        </section>

        <section id="section-photos" className="scroll-mt-28">
          <SectionHeading title="Photos" />
          <PhotoGrid pictures={generation.recommended_pictures || []} ready={done} />
        </section>

        <section id="section-qa" className="scroll-mt-28">
          <SectionHeading title="Q&A" />
          <ObjectionAccordion
            objections={generation.objections}
            openId={openObjection}
            setOpenId={setOpenObjection}
            ready={done}
          />
        </section>
      </div>
    </div>
  );
}

function SectionHeading({ title }: { title: string }) {
  return <h2 className="mb-8 font-display text-3xl tracking-tight text-black md:text-4xl">{title}</h2>;
}

function TimelineSidebar({
  status,
  statusText,
  done,
  navReady,
  activeSection,
  onNavigate,
}: {
  status: string;
  statusText: string;
  done: boolean;
  navReady: boolean;
  activeSection: string;
  onNavigate: (sectionId: string) => void;
}) {
  return (
    <nav className="relative flex h-full flex-col py-2" aria-label="Output timeline">
      {TIMELINE.map((item, index) => {
        const state = entryState(item.id, status);
        const isLast = index === TIMELINE.length - 1;
        const scrollActive = navReady && activeSection === item.sectionId;
        const delay = index * STAGGER_S;

        return (
          <div key={item.id} className="relative flex gap-4">
            <div className="flex w-3 flex-col items-center">
              {done ? (
                <motion.span
                  className="mt-1.5 h-2.5 w-2.5 rounded-full bg-black"
                  initial={{ backgroundColor: "#E8E4DB", scale: 0.7 }}
                  animate={{ backgroundColor: "#0B0B0F", scale: 1 }}
                  transition={{ delay, duration: 0.28, ease: "easeOut" }}
                />
              ) : (
                <span
                  className={`mt-1.5 h-2.5 w-2.5 rounded-full ${
                    state === "active" ? "bg-grey" : "bg-grey-light"
                  }`}
                />
              )}
              {!isLast && (
                <span className="relative mt-1 w-px flex-1 min-h-[2.5rem] overflow-hidden bg-grey-light">
                  {done && (
                    <motion.span
                      className="absolute inset-0 origin-top bg-black/40"
                      initial={{ scaleY: 0 }}
                      animate={{ scaleY: 1 }}
                      transition={{ delay: delay + STAGGER_S * 0.35, duration: 0.28, ease: "easeOut" }}
                    />
                  )}
                </span>
              )}
            </div>
            <div className="min-w-0 flex-1 pb-8">
              {navReady ? (
                <button
                  type="button"
                  className={`text-left text-sm font-medium transition-colors ${
                    scrollActive ? "text-black" : "text-grey hover:text-black"
                  }`}
                  onClick={() => onNavigate(item.sectionId)}
                >
                  {item.label}
                </button>
              ) : done ? (
                <motion.span
                  className="block text-sm font-medium text-black"
                  initial={{ color: "#E8E4DB", opacity: 0.55 }}
                  animate={{ color: "#0B0B0F", opacity: 1 }}
                  transition={{ delay, duration: 0.28, ease: "easeOut" }}
                >
                  {item.label}
                </motion.span>
              ) : state === "active" ? (
                <motion.span
                  className="block text-sm font-medium"
                  animate={{ color: ["#0B0B0F", "#E8E4DB", "#0B0B0F"] }}
                  transition={{ duration: 1.4, repeat: Infinity, ease: "easeInOut" }}
                >
                  {item.label}
                </motion.span>
              ) : (
                <span className="block text-sm font-medium text-grey-light">{item.label}</span>
              )}
              {state === "active" && !done && (
                <motion.p
                  className="mt-2 text-xs leading-relaxed text-grey"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  key={status}
                >
                  {statusText}
                </motion.p>
              )}
            </div>
          </div>
        );
      })}
      {!done && status !== "failed" && (
        <p className="mt-auto text-[11px] uppercase tracking-[0.16em] text-grey">{pipelineStage(status)}</p>
      )}
    </nav>
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

function VideoCarousel({ videos, ready }: { videos: RecommendedMedia[]; ready: boolean }) {
  const scroller = useRef<HTMLDivElement>(null);
  const [canLeft, setCanLeft] = useState(false);
  const [canRight, setCanRight] = useState(false);

  const updateArrows = () => {
    const el = scroller.current;
    if (!el) return;
    const max = el.scrollWidth - el.clientWidth;
    setCanLeft(el.scrollLeft > 4);
    setCanRight(max > 4 && el.scrollLeft < max - 4);
  };

  useEffect(() => {
    if (!ready || !videos.length) return;
    const el = scroller.current;
    if (!el) return;
    updateArrows();
    el.addEventListener("scroll", updateArrows, { passive: true });
    window.addEventListener("resize", updateArrows);
    const frame = window.requestAnimationFrame(updateArrows);
    return () => {
      el.removeEventListener("scroll", updateArrows);
      window.removeEventListener("resize", updateArrows);
      window.cancelAnimationFrame(frame);
    };
  }, [ready, videos]);

  const scrollByCard = (direction: 1 | -1) => {
    const el = scroller.current;
    if (!el) return;
    const card = el.querySelector<HTMLElement>("article");
    const gap = 16;
    const amount = card ? card.getBoundingClientRect().width + gap : el.clientWidth * 0.85;
    el.scrollBy({ left: direction * amount, behavior: "smooth" });
  };

  if (!ready) {
    return <p className="text-sm text-grey">Videos unlock when generation finishes.</p>;
  }
  if (!videos.length) {
    return <p className="text-sm text-grey">No recommended videos for this usecase yet.</p>;
  }

  return (
    <div className="relative">
      <button
        type="button"
        aria-label="Previous videos"
        disabled={!canLeft}
        onClick={() => scrollByCard(-1)}
        className="btn absolute -left-2 top-1/2 z-10 hidden h-10 w-10 -translate-y-1/2 items-center justify-center rounded-full p-0 shadow-glass disabled:opacity-30 md:flex"
      >
        ‹
      </button>
      <button
        type="button"
        aria-label="Next videos"
        disabled={!canRight}
        onClick={() => scrollByCard(1)}
        className="btn absolute -right-2 top-1/2 z-10 hidden h-10 w-10 -translate-y-1/2 items-center justify-center rounded-full p-0 shadow-glass disabled:opacity-30 md:flex"
      >
        ›
      </button>
      <div
        ref={scroller}
        className="flex gap-4 overflow-x-auto pb-4 snap-x snap-mandatory"
        style={{ scrollbarWidth: "thin" }}
      >
        {videos.map((video) => {
          const embed = driveEmbed(video.source_url) || youtubeEmbed(video.source_url);
          return (
            <article
              key={video.asset_id}
              className="glass-panel w-[min(100%,22rem)] shrink-0 snap-start overflow-hidden"
            >
              {embed ? (
                <div className="aspect-video bg-black">
                  <iframe
                    className="h-full w-full"
                    src={embed}
                    title={video.title}
                    allow="autoplay; fullscreen"
                    allowFullScreen
                  />
                </div>
              ) : video.thumbnail_url ? (
                <div className="aspect-video bg-grey-light">
                  <img src={video.thumbnail_url} alt="" className="h-full w-full object-cover" />
                </div>
              ) : null}
              <div className="space-y-2 p-4">
                <h3 className="font-medium text-black">{video.title}</h3>
                {video.rationale ? <p className="text-sm text-grey">{video.rationale}</p> : null}
                {video.source_url ? (
                  <a className="text-sm text-grey-dark underline" href={video.source_url} target="_blank" rel="noreferrer">
                    Open source
                  </a>
                ) : null}
              </div>
            </article>
          );
        })}
      </div>
    </div>
  );
}

function PhotoGrid({ pictures, ready }: { pictures: RecommendedMedia[]; ready: boolean }) {
  if (!ready) {
    return <p className="text-sm text-grey">Photos unlock when generation finishes.</p>;
  }
  if (!pictures.length) {
    return <p className="text-sm text-grey">No recommended pictures for this usecase yet.</p>;
  }
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
      {pictures.slice(0, 5).map((picture) => (
        <RecommendedPicture key={picture.asset_id} picture={picture} />
      ))}
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
        className="overflow-hidden rounded-xl border border-black/8 bg-white/50 text-left"
        onClick={() => {
          setFullLoaded(false);
          setFullFailed(false);
          setOpen(true);
        }}
        aria-label={`View ${picture.title}`}
      >
        <div className="relative aspect-[4/3] bg-grey-light/50">
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
        <p className="truncate px-2 py-1.5 text-xs text-grey">{picture.title}</p>
      </button>
      <AnimatePresence>
        {open && (
          <motion.div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/90 p-4"
            role="dialog"
            aria-modal="true"
            aria-label={picture.title}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={() => setOpen(false)}
          >
            {!fullLoaded && !fullFailed && <Spinner className="absolute h-8 w-8 text-offwhite" />}
            {fullFailed && (
              <div className="rounded-xl bg-offwhite px-6 py-5 text-center" onClick={(event) => event.stopPropagation()}>
                <p className="font-medium">Image preview unavailable</p>
                {picture.source_url ? (
                  <a className="mt-2 inline-block text-sm underline" href={picture.source_url} target="_blank" rel="noreferrer">
                    Open in Drive
                  </a>
                ) : null}
              </div>
            )}
            {picture.preview_url && !fullFailed ? (
              <motion.img
                src={picture.preview_url}
                alt={picture.title}
                className={`max-h-[90vh] max-w-[94vw] object-contain ${fullLoaded ? "block" : "hidden"}`}
                initial={{ scale: 0.96, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                onLoad={() => setFullLoaded(true)}
                onError={() => setFullFailed(true)}
                onClick={(event) => event.stopPropagation()}
              />
            ) : null}
            <button
              type="button"
              className="btn-ghost absolute right-4 top-4 border border-white/20 bg-black/50 text-offwhite"
              onClick={() => setOpen(false)}
            >
              Close
            </button>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}

function ObjectionAccordion({
  objections,
  openId,
  setOpenId,
  ready,
}: {
  objections: ObjectionRow[];
  openId: number | null;
  setOpenId: (id: number | null) => void;
  ready: boolean;
}) {
  if (!ready) {
    return <p className="text-sm text-grey">Q&A unlocks when generation finishes.</p>;
  }
  if (!objections.length) {
    return <p className="text-sm text-grey">No objections attached.</p>;
  }
  return (
    <div className="space-y-3">
      {objections.map((item) => {
        const open = openId === item.id;
        return (
          <div key={item.id} className="glass-panel overflow-hidden">
            <button
              type="button"
              className="flex w-full items-start justify-between gap-4 p-5 text-left"
              onClick={() => setOpenId(open ? null : item.id)}
              aria-expanded={open}
            >
              <div>
                <p className="font-medium text-black">{item.question}</p>
                {item.who_asks && <p className="mt-1 text-xs text-grey">{item.who_asks}</p>}
              </div>
              <span className="text-grey">{open ? "−" : "+"}</span>
            </button>
            <AnimatePresence initial={false}>
              {open && (
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: "auto", opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={{ duration: 0.22 }}
                  className="overflow-hidden"
                >
                  <div className="space-y-2 border-t border-black/8 px-5 pb-5 pt-3 text-sm text-grey-dark">
                    {item.move && (
                      <p>
                        <span className="font-semibold text-black">Move:</span> {item.move}
                      </p>
                    )}
                    <p className="leading-7">{item.answer}</p>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        );
      })}
    </div>
  );
}
