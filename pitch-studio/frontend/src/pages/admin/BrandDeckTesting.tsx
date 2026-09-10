import { useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { AXES } from "../../axes";
import SlideView from "../../components/SlideView";
import { Button, EmptyState, ErrorBanner, Skeleton, Spinner, StatusBadge } from "../../components/ui";
import { axisLabel, personaLabel } from "../../labels";
import type { DeckTopicList, DeckTopicRow, RecipeOption } from "../../types";

function isActiveJob(row?: Pick<DeckTopicList, "job_status"> | null) {
  return row?.job_status === "queued" || row?.job_status === "running";
}

function displayStatus(row: DeckTopicRow) {
  if (row.vision_frozen) return "frozen";
  if (isActiveJob(row) || row.status === "processing") return "processing";
  if (row.stale) return "stale";
  return row.status || "draft";
}

function pageRange(row: DeckTopicRow) {
  if (!row.pages.length) return "No pages";
  return row.pages.length === 1 ? `p${row.pages[0]}` : `p${row.pages[0]}–${row.pages[row.pages.length - 1]}`;
}

function errMessage(err: unknown) {
  return err instanceof Error ? err.message : "Something went wrong";
}

export default function BrandDeckTesting() {
  const [list, setList] = useState<DeckTopicList>({ items: [], asset_id: 0, asset_title: "Brand Deck" });
  const [recipes, setRecipes] = useState<RecipeOption[]>([]);
  const [current, setCurrent] = useState<DeckTopicRow | null>(null);
  const [visionDraft, setVisionDraft] = useState("");
  const [addRef, setAddRef] = useState("");
  const [addNote, setAddNote] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [visionSaved, setVisionSaved] = useState(false);
  const selectedIdRef = useRef(0);
  const pollRef = useRef(0);

  const applyCurrent = (row: DeckTopicRow) => {
    setCurrent(row);
    setVisionDraft(row.vision);
    selectedIdRef.current = row.id;
  };

  const reloadList = async (keepId?: number) => {
    const data = await api.deckTopicList();
    setList(data);
    if (keepId) {
      const next = data.items.find((row) => row.id === keepId);
      if (next) applyCurrent(next);
    }
    return data;
  };

  const watchJob = async (keepId?: number) => {
    const token = ++pollRef.current;
    setBusy("prepare");
    setError("");
    try {
      while (pollRef.current === token) {
        const data = await api.deckTopicList();
        if (pollRef.current !== token) return;
        setList(data);
        const next = data.items.find((row) => row.id === (keepId || selectedIdRef.current)) || data.items[0];
        if (next) applyCurrent(next);
        if (data.job_status === "error") {
          setError(data.job_error || "Brand deck indexing failed");
          return;
        }
        if (!isActiveJob(data)) return;
        await new Promise((resolve) => window.setTimeout(resolve, 2000));
      }
    } catch (err) {
      if (pollRef.current === token) setError(errMessage(err));
    } finally {
      if (pollRef.current === token) setBusy("");
    }
  };

  useEffect(() => {
    api
      .recipes()
      .then((data) => setRecipes(data as RecipeOption[]))
      .catch(() => undefined);
    reloadList()
      .then((data) => {
        if (data.items[0]) applyCurrent(data.items[0]);
        if (isActiveJob(data)) void watchJob(data.items[0]?.id);
      })
      .catch((err) => setError(errMessage(err)))
      .finally(() => setLoading(false));
    return () => {
      pollRef.current += 1;
    };
  }, []);

  const run = async (label: string, work: () => Promise<DeckTopicRow | void>) => {
    setBusy(label);
    setError("");
    try {
      const result = await work();
      if (result) applyCurrent(result);
      await reloadList(result?.id ?? current?.id);
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setBusy("");
    }
  };

  const prepare = async (force = false) => {
    setBusy("prepare");
    setError("");
    try {
      const data = await api.prepareDeckTopics(force);
      setList(data);
      if (data.items[0]) applyCurrent(data.items[0]);
      await watchJob(current?.id || data.items[0]?.id);
    } catch (err) {
      setError(errMessage(err));
      setBusy("");
    }
  };

  useEffect(() => {
    if (!current || current.vision_frozen) return;
    if (visionDraft.trim() === (current.vision || "").trim()) return;
    const handle = window.setTimeout(() => {
      const id = current.id;
      setBusy("vision");
      setVisionSaved(false);
      setError("");
      api
        .saveDeckTopicVision(id, visionDraft)
        .then((row) => {
          applyCurrent(row);
          setVisionSaved(true);
          return reloadList(row.id);
        })
        .catch((err) => setError(errMessage(err)))
        .finally(() => setBusy(""));
    }, 1500);
    return () => window.clearTimeout(handle);
  }, [visionDraft, current?.id, current?.vision, current?.vision_frozen]);

  useEffect(() => {
    if (!visionSaved) return;
    const handle = window.setTimeout(() => setVisionSaved(false), 2000);
    return () => window.clearTimeout(handle);
  }, [visionSaved]);

  const feedback = (ref: string, verdict: "yes" | "no") => {
    if (!current) return;
    run(`feedback-${ref}`, () => api.sendDeckTopicFeedback(current.id, ref, verdict));
  };

  const addUsecase = () => {
    if (!current || !addRef) return;
    run("add", async () => {
      const row = await api.addDeckTopicUsecase(current.id, addRef, addNote);
      setAddRef("");
      setAddNote("");
      return row;
    });
  };

  const freeze = () => {
    if (!current) return;
    run("freeze", () => (current.vision_frozen ? api.unfreezeDeckTopic(current.id) : api.freezeDeckTopic(current.id)));
  };

  const preparing = busy === "prepare" || isActiveJob(list);

  return (
    <div className="space-y-6">
      <div>
        <p className="kicker">Library</p>
        <h1 className="font-display text-3xl">Brand deck testing</h1>
        <p className="mt-2 max-w-3xl text-sm text-muted">
          Group the Brand Deck into topics, then attach the same vision and persona
          matching used for media. Generated pitch decks are unchanged in this version.
        </p>
      </div>
      <ErrorBanner message={error} />
      <div className="grid gap-6 lg:grid-cols-[280px_1fr]">
        <aside className="card max-h-[80vh] overflow-y-auto p-3">
          <div className="mb-3 space-y-2 px-1">
            <Button variant="accent" loading={preparing} onClick={() => prepare(list.items.length > 0)}>
              {list.items.length ? "Regroup topics" : "Prepare topics"}
            </Button>
            {preparing && (
              <p className="flex items-start gap-2 text-xs text-muted">
                <Spinner className="mt-0.5" />
                {list.job_stage || "Grouping brand deck pages…"}
              </p>
            )}
          </div>
          {loading && (
            <div className="space-y-2 p-1">
              {Array.from({ length: 6 }).map((_, index) => (
                <Skeleton key={index} className="h-16 w-full" />
              ))}
            </div>
          )}
          {!loading && list.items.length === 0 && !preparing && (
            <p className="p-3 text-sm text-muted">No topics yet. Prepare the Brand Deck to group its slides.</p>
          )}
          {list.items.map((row) => (
            <button
              key={row.id}
              type="button"
              onClick={() => applyCurrent(row)}
              className={`mb-2 w-full rounded-xl border px-3 py-3 text-left ${
                current?.id === row.id ? "border-accent bg-accent/5" : "border-line"
              }`}
            >
              <p className="truncate text-sm font-medium">{row.title || "Untitled topic"}</p>
              <p className="mt-1 flex items-center gap-2 text-xs text-muted">
                <span>{pageRange(row)}</span>
                <StatusBadge status={displayStatus(row)} />
              </p>
            </button>
          ))}
        </aside>
        <section className="space-y-6">
          {loading && (
            <div className="card space-y-4 p-6">
              <Skeleton className="h-8 w-64" />
              <Skeleton className="aspect-[16/9] w-full" />
            </div>
          )}
          {!loading && !current && (
            <EmptyState
              title="Select a topic"
              description="Prepare the Brand Deck, then choose a topic to attach a vision."
            />
          )}
          {current && (
            <>
              <div className="card space-y-4 p-6">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="font-display text-2xl">{current.title}</h2>
                    <p className="mt-1 text-sm text-muted">
                      {pageRange(current)}
                      {current.module_ids ? ` · ${current.module_ids}` : ""}
                    </p>
                  </div>
                  <StatusBadge status={displayStatus(current)} />
                </div>
                <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
                  {current.page_items.map((page, index) => (
                    <div key={page.page} className="overflow-hidden rounded-lg border border-line">
                      <div className="aspect-[16/9] bg-ink">
                        <SlideView
                          slide={{
                            layout: "image",
                            page: page.page,
                            title: page.label,
                            module_id: page.module_id,
                            image_url: page.image_url,
                          }}
                          index={index + 1}
                          total={current.page_items.length}
                          compact
                        />
                      </div>
                      <p className="truncate px-2 py-1.5 text-xs text-muted">
                        p{page.page} · {page.label}
                      </p>
                    </div>
                  ))}
                </div>
              </div>

              <div className="card space-y-3 p-6">
                <h3 className="font-display text-xl">Topic summary</h3>
                <p className="whitespace-pre-wrap text-sm leading-6">
                  {current.summary || "No summary yet. Prepare the Brand Deck to analyze these slides."}
                </p>
              </div>

              <div className="card space-y-3 p-6">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <h3 className="font-display text-xl">Vision</h3>
                  {busy === "vision" && (
                    <p className="flex items-center gap-2 text-sm text-muted">
                      <Spinner /> Saving vision…
                    </p>
                  )}
                  {visionSaved && busy !== "vision" && (
                    <p className="text-sm text-success">Indexed into context</p>
                  )}
                </div>
                <p className="text-sm text-muted">
                  Why should this topic be shown? Changes are indexed automatically and attached
                  to the topic summary below.
                </p>
                <textarea
                  className="field min-h-32"
                  value={visionDraft}
                  disabled={current.vision_frozen}
                  onChange={(event) => setVisionDraft(event.target.value)}
                />
                {current.stale && (
                  <p className="text-sm text-warn">Vision changed — indexing it into the context now.</p>
                )}
                <Button loading={busy === "freeze"} onClick={freeze}>
                  {current.vision_frozen ? "Unfreeze" : "Freeze vision"}
                </Button>
              </div>

              {current.context_index && (
                <div className="card space-y-3 p-6">
                  <h3 className="font-display text-xl">Context index</h3>
                  <pre className="whitespace-pre-wrap rounded-xl bg-paper p-4 text-sm leading-6">
                    {current.context_index}
                  </pre>
                </div>
              )}

              <div className="card space-y-4 p-6">
                <h3 className="font-display text-xl">Recommended usecases</h3>
                {current.recommendations.items.length === 0 && (
                  <p className="text-sm text-muted">
                    {displayStatus(current) === "ready"
                      ? "Topic is ready. Add a vision note to generate persona recommendations."
                      : "No recommendations yet. Prepare the topics, then enter a vision."}
                  </p>
                )}
                <div className="space-y-3">
                  {current.recommendations.items.map((item) => {
                    const verdict = current.feedback.verdicts[item.recipe_ref]?.verdict;
                    const name = personaLabel(recipes, item.recipe_ref) || item.recipe_ref;
                    const feedbackBusy = busy === `feedback-${item.recipe_ref}`;
                    return (
                      <div key={item.recipe_ref} className="rounded-xl border border-line p-4">
                        <div className="flex flex-wrap items-start justify-between gap-3">
                          <div>
                            <p className="font-medium">{name}</p>
                            <p className="mt-1 text-xs text-muted">
                              {item.recipe_ref} · {axisLabel(AXES.audience_clusters, item.audience_cluster)} ·{" "}
                              {axisLabel(AXES.durations, item.duration)} · {axisLabel(AXES.channels, item.channel)} ·{" "}
                              {axisLabel(AXES.intents, item.intent)}
                            </p>
                          </div>
                          <p className="text-xs text-muted">{Math.round(item.confidence * 100)}%</p>
                        </div>
                        {item.rationale && <p className="mt-2 text-sm">{item.rationale}</p>}
                        <div className="mt-3 flex gap-2">
                          <Button
                            variant={verdict === "yes" ? "accent" : "default"}
                            loading={feedbackBusy && verdict !== "no"}
                            onClick={() => feedback(item.recipe_ref, "yes")}
                          >
                            Yes
                          </Button>
                          <Button
                            variant={verdict === "no" ? "accent" : "default"}
                            loading={feedbackBusy && verdict !== "yes"}
                            onClick={() => feedback(item.recipe_ref, "no")}
                          >
                            No
                          </Button>
                        </div>
                      </div>
                    );
                  })}
                </div>
                <div className="border-t border-line pt-4">
                  <h4 className="text-sm font-medium">Add a missed usecase</h4>
                  <div className="mt-3 flex flex-wrap gap-3">
                    <select className="field max-w-sm" value={addRef} onChange={(event) => setAddRef(event.target.value)}>
                      <option value="">Choose a persona</option>
                      {recipes.map((recipe) => (
                        <option key={recipe.ref} value={recipe.ref}>
                          {recipe.audience_label || recipe.ref} ({recipe.ref})
                        </option>
                      ))}
                    </select>
                    <input
                      className="field max-w-sm"
                      placeholder="Why the model missed this"
                      value={addNote}
                      onChange={(event) => setAddNote(event.target.value)}
                    />
                    <Button loading={busy === "add"} disabled={!addRef} onClick={addUsecase}>
                      Add
                    </Button>
                  </div>
                  {current.feedback.added.length > 0 && (
                    <ul className="mt-3 space-y-1 text-sm">
                      {current.feedback.added.map((item) => (
                        <li key={`${item.recipe_ref}-${item.at}`}>
                          {personaLabel(recipes, item.recipe_ref) || item.recipe_ref}
                          {item.note ? ` — ${item.note}` : ""}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
