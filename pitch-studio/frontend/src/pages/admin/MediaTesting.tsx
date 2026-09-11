import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api";
import { AXES } from "../../axes";
import { Button, EmptyState, ErrorBanner, Skeleton, Spinner, StatusBadge } from "../../components/ui";
import { axisLabel, personaLabel } from "../../labels";
import type { MediaCandidate, MediaIndexList, MediaIndexRow, RecipeOption } from "../../types";

type ListItem =
  | { kind: "indexed"; row: MediaIndexRow }
  | { kind: "candidate"; row: MediaCandidate };

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

function videoPrepareLabel(fileStatus: string) {
  return fileStatus === "stored" ? "Analyze & index video" : "Download, analyze & index";
}

function isActiveJob(row?: Pick<MediaIndexRow, "job_status"> | null) {
  return row?.job_status === "queued" || row?.job_status === "running";
}

function hasExtract(row: MediaIndexRow) {
  if (row.media_kind === "video") {
    return Boolean(row.transcript.trim() || row.visual_description.trim());
  }
  return Boolean(row.visual_description.trim());
}

function displayStatus(row: MediaIndexRow) {
  if (row.vision_frozen) return "frozen";
  if (isActiveJob(row) || row.status === "processing") return "processing";
  if (row.stale) return "stale";
  return row.status || "draft";
}

function errMessage(err: unknown) {
  return err instanceof Error ? err.message : "Something went wrong";
}

export default function MediaTesting() {
  const [list, setList] = useState<MediaIndexList>({ items: [], candidates: [] });
  const [recipes, setRecipes] = useState<RecipeOption[]>([]);
  const [selectedKey, setSelectedKey] = useState<string>("");
  const [current, setCurrent] = useState<MediaIndexRow | null>(null);
  const [visionDraft, setVisionDraft] = useState("");
  const [transcriptDraft, setTranscriptDraft] = useState("");
  const [addRef, setAddRef] = useState("");
  const [addNote, setAddNote] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [visionSaved, setVisionSaved] = useState(false);
  const selectedKeyRef = useRef("");
  const pollRef = useRef(0);

  const items: ListItem[] = useMemo(
    () => [
      ...list.items.map((row) => ({ kind: "indexed" as const, row })),
      ...list.candidates.map((row) => ({ kind: "candidate" as const, row })),
    ],
    [list],
  );

  const selected = items.find((item) =>
    item.kind === "indexed" ? `i-${item.row.id}` === selectedKey : `c-${item.row.asset_id}` === selectedKey,
  );

  const reloadList = async (keepId?: number) => {
    const data = await api.mediaIndexList();
    setList(data);
    if (keepId) {
      const next = data.items.find((row) => row.id === keepId);
      if (next) applyCurrent(next);
    }
    return data;
  };

  const applyCurrent = (row: MediaIndexRow) => {
    setCurrent(row);
    setVisionDraft(row.vision);
    setTranscriptDraft(row.transcript);
    setSelectedKey(`i-${row.id}`);
  };

  selectedKeyRef.current = selectedKey;

  const watchJob = async (id: number, label: string) => {
    const token = ++pollRef.current;
    setBusy(label);
    setError("");
    try {
      while (pollRef.current === token) {
        const row = await api.mediaIndexGet(id);
        if (pollRef.current !== token) return;
        if (selectedKeyRef.current === `i-${id}`) applyCurrent(row);
        setList((prev) => ({
          ...prev,
          items: prev.items.map((item) => (item.id === row.id ? row : item)),
        }));
        if (row.job_status === "error") {
          setError(row.job_error || "Indexing failed");
          await reloadList(row.id);
          return;
        }
        if (!isActiveJob(row)) {
          await reloadList(row.id);
          return;
        }
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
        const active = data.items.find((row) => isActiveJob(row));
        const first = active || data.items[0];
        if (first) applyCurrent(first);
        else if (data.candidates[0]) setSelectedKey(`c-${data.candidates[0].asset_id}`);
        if (active) {
          const label = active.job_type === "media_describe" ? "describe" : "prepare";
          void watchJob(active.id, label);
        }
      })
      .catch((err) => setError(errMessage(err)))
      .finally(() => setLoading(false));
    return () => {
      pollRef.current += 1;
    };
  }, []);

  const run = async (label: string, work: () => Promise<MediaIndexRow | void>) => {
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

  const prepare = async (assetId: number) => {
    setBusy("prepare");
    setError("");
    try {
      const row = await api.prepareMedia(assetId);
      applyCurrent(row);
      await reloadList(row.id);
      await watchJob(row.id, "prepare");
    } catch (err) {
      setError(errMessage(err));
      setBusy("");
    }
  };

  const saveTranscript = () => {
    if (!current) return;
    run("transcript", () => api.saveTranscript(current.id, transcriptDraft));
  };

  const loadTranscriptFile = async (file: File) => {
    const text = await file.text();
    setTranscriptDraft(text);
    if (!current) return;
    run("transcript", () => api.saveTranscript(current.id, text));
  };

  const generateDescription = async () => {
    if (!current) return;
    setBusy("describe");
    setError("");
    try {
      const row = await api.describeMedia(current.id);
      applyCurrent(row);
      await reloadList(row.id);
      await watchJob(row.id, "describe");
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
        .saveVision(id, visionDraft)
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
    run(`feedback-${ref}`, () => api.sendFeedback(current.id, ref, verdict));
  };

  const addUsecase = () => {
    if (!current || !addRef) return;
    run("add", async () => {
      const row = await api.addUsecase(current.id, addRef, addNote);
      setAddRef("");
      setAddNote("");
      return row;
    });
  };

  const freeze = () => {
    if (!current) return;
    run("freeze", () => (current.vision_frozen ? api.unfreezeMedia(current.id) : api.freezeMedia(current.id)));
  };

  const preparing = busy === "prepare" || (isActiveJob(current) && current?.job_type !== "media_describe");
  const describing = busy === "describe" || (isActiveJob(current) && current?.job_type === "media_describe");

  return (
    <div className="mx-auto max-w-6xl space-y-6 px-6 py-10 md:px-10">
      <div>
        <p className="kicker">Library</p>
        <h1 className="font-display text-3xl">Media testing</h1>
        <p className="mt-2 max-w-3xl text-sm text-muted">
          Select a video or photo set, then prepare it. Personas are recommended from the
          analysis automatically. Add a vision later if you want to refine those matches.
        </p>
      </div>
      <ErrorBanner message={error} />
      <div className="grid gap-6 lg:grid-cols-[280px_1fr]">
        <aside className="card scrollbar-none max-h-[80vh] overflow-y-auto p-3">
          {loading && (
            <div className="space-y-2 p-1">
              {Array.from({ length: 6 }).map((_, index) => (
                <Skeleton key={index} className="h-16 w-full" />
              ))}
            </div>
          )}
          {!loading && items.length === 0 && (
            <p className="p-3 text-sm text-muted">No video or photo assets yet.</p>
          )}
          {items.map((item) => {
            const key = item.kind === "indexed" ? `i-${item.row.id}` : `c-${item.row.asset_id}`;
            const title = item.kind === "indexed" ? item.row.asset_title : item.row.title;
            const kind = item.kind === "indexed" ? item.row.media_kind : item.row.type;
            const status = item.kind === "indexed" ? displayStatus(item.row) : "not indexed";
            return (
              <button
                key={key}
                type="button"
                onClick={() => {
                  setSelectedKey(key);
                  if (item.kind === "indexed") applyCurrent(item.row);
                  else setCurrent(null);
                }}
                className={`mb-2 w-full rounded-xl border px-3 py-3 text-left ${
                  selectedKey === key ? "border-accent bg-accent/5" : "border-line"
                }`}
              >
                <p className="truncate text-sm font-medium">{title}</p>
                <p className="mt-1 flex items-center gap-2 text-xs text-muted">
                  <span className="capitalize">{kind}</span>
                  <StatusBadge status={status} />
                </p>
              </button>
            );
          })}
        </aside>
        <section className="space-y-6">
          {loading && (
            <div className="card space-y-4 p-6">
              <Skeleton className="h-8 w-64" />
              <Skeleton className="h-4 w-40" />
              <Skeleton className="aspect-[16/9] w-full" />
            </div>
          )}
          {!loading && !selected && (
            <EmptyState title="Select a video or photo set" description="Choose an item from the library to prepare or review it." />
          )}
          {selected?.kind === "candidate" && (
            <div className="card space-y-4 p-6">
              <h2 className="font-display text-2xl">{selected.row.title}</h2>
              <p className="text-sm capitalize text-muted">{selected.row.type} · {selected.row.file_status || "pending"}</p>
              {selected.row.source_url && (
                <a className="text-sm text-accent underline" href={selected.row.source_url} target="_blank" rel="noreferrer">
                  Open source
                </a>
              )}
              <Button variant="accent" loading={preparing} onClick={() => prepare(selected.row.asset_id)}>
                {selected.row.type === "photo"
                  ? "Prepare & index photos"
                  : videoPrepareLabel(selected.row.file_status)}
              </Button>
              {preparing && (
                <PrepareProgress
                  kind={selected.row.type}
                  sourceUrl={selected.row.source_url}
                  stage={current?.job_stage}
                />
              )}
            </div>
          )}
          {current && (
            <>
              <div className="card space-y-4 p-6">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="font-display text-2xl">{current.asset_title}</h2>
                    <p className="mt-1 text-sm capitalize text-muted">
                      {current.media_kind} · {current.asset_file_status || "pending"}
                    </p>
                  </div>
                  <StatusBadge status={displayStatus(current)} />
                </div>
                <MediaPreview row={current} onSync={() => prepare(current.asset_id)} busy={preparing} />
                {(current.media_kind === "video" &&
                  (!current.transcript.trim() || !current.visual_description.trim())) ||
                (current.media_kind === "photo" && current.image_assets.length === 0) ? (
                  <Button variant="accent" loading={preparing} onClick={() => prepare(current.asset_id)}>
                    {current.media_kind === "photo"
                      ? "Prepare & index photos"
                      : videoPrepareLabel(current.asset_file_status)}
                  </Button>
                ) : null}
                {(preparing || describing) && (
                  <PrepareProgress
                    kind={current.media_kind}
                    sourceUrl={current.asset_source_url}
                    stage={current.job_stage}
                  />
                )}
              </div>

              {current.media_kind === "video" ? (
                <>
                  <div className="card space-y-3 p-6">
                    <h3 className="font-display text-xl">Audio transcript</h3>
                    <textarea
                      className="field min-h-40"
                      value={transcriptDraft}
                      disabled={current.vision_frozen}
                      onChange={(event) => setTranscriptDraft(event.target.value)}
                    />
                    <div className="flex flex-wrap items-center gap-3">
                      <Button loading={busy === "transcript"} disabled={current.vision_frozen} onClick={saveTranscript}>
                        Save transcript
                      </Button>
                      <label className={`btn cursor-pointer ${current.vision_frozen ? "opacity-50" : ""}`}>
                        Upload .txt / .json
                        <input
                          className="hidden"
                          type="file"
                          accept=".txt,.json,text/plain,application/json"
                          disabled={current.vision_frozen}
                          onChange={(event) => {
                            const file = event.target.files?.[0];
                            if (file) void loadTranscriptFile(file);
                            event.target.value = "";
                          }}
                        />
                      </label>
                    </div>
                  </div>
                  <div className="card space-y-3 p-6">
                    <h3 className="font-display text-xl">Visual narrative & relevance</h3>
                    <p className="whitespace-pre-wrap text-sm leading-6">
                      {current.visual_description ||
                        "No visual analysis yet. Prepare the video to analyze representative frames."}
                    </p>
                  </div>
                </>
              ) : (
                <div className="card space-y-3 p-6">
                  <h3 className="font-display text-xl">Visual description</h3>
                  <p className="text-sm text-muted">
                    {current.image_assets.length} stored image{current.image_assets.length === 1 ? "" : "s"}
                    {current.image_keys.length ? ` · ${current.image_keys.length} used for last description` : ""}
                  </p>
                  <p className="whitespace-pre-wrap text-sm leading-6">
                    {current.visual_description || "No description yet. Sync images, then generate one."}
                  </p>
                  <Button variant="accent" loading={describing} disabled={current.vision_frozen} onClick={generateDescription}>
                    Generate description
                  </Button>
                </div>
              )}

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
                  Optional. Personas are recommended from the media analysis automatically.
                  Add a vision to refine those matches.
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
                  <p className="text-sm text-muted">
                    Transcript or visual description, plus vision when you add one, used for usecase matching.
                  </p>
                  <pre className="whitespace-pre-wrap rounded-xl bg-paper p-4 text-sm leading-6">
                    {current.context_index}
                  </pre>
                </div>
              )}

              <div className="card space-y-4 p-6">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <h3 className="font-display text-xl">Recommended usecases</h3>
                  <Button
                    loading={busy === "reindex"}
                    disabled={current.vision_frozen || !hasExtract(current)}
                    onClick={() => run("reindex", () => api.reindexMedia(current.id))}
                  >
                    Refresh recommendations
                  </Button>
                </div>
                {current.recommendations.items.length === 0 && (
                  <p className="text-sm text-muted">
                    {hasExtract(current)
                      ? "No recommendations yet. Refresh to match personas from this analysis."
                      : "Prepare the media first. Personas are recommended from the analysis."}
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
                            {item.temperatures.length > 0 && (
                              <p className="mt-1 text-xs text-muted">
                                Temperatures: {item.temperatures.map((code) => axisLabel(AXES.temperatures, code)).join(", ")}
                              </p>
                            )}
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

function PrepareProgress({
  kind,
  sourceUrl,
  stage,
}: {
  kind: string;
  sourceUrl?: string;
  stage?: string;
}) {
  const isYoutube = Boolean(sourceUrl && youtubeEmbed(sourceUrl));
  return (
    <div className="flex items-start gap-3 rounded-xl border border-accent/20 bg-accent/5 px-4 py-3 text-sm">
      <Spinner className="mt-0.5 text-accent" />
      <div>
        <p className="font-medium text-accent">
          {stage || (kind === "photo" ? "Preparing photos…" : "Downloading and transcribing…")}
        </p>
        <p className="mt-1 text-muted">
          {kind === "photo"
            ? "Syncing the folder, then describing the images. Login and other pages stay available while this runs."
            : isYoutube
              ? "Downloading the YouTube source if needed, then analyzing its audio and representative frames."
              : "Downloading the Drive video if needed, then analyzing its audio, visuals, and story relevance."}
        </p>
      </div>
    </div>
  );
}

function MediaPreview({
  row,
  onSync,
  busy,
}: {
  row: MediaIndexRow;
  onSync: () => void;
  busy: boolean;
}) {
  if (row.media_kind === "video") {
    const drive = driveEmbed(row.asset_source_url);
    if (drive) {
      return (
        <div className="stage bg-ink">
          <iframe
            className="h-full w-full"
            src={drive}
            title={row.asset_title}
            allow="autoplay; fullscreen"
            allowFullScreen
          />
        </div>
      );
    }
    if (row.asset_file_status === "stored") {
      return (
        <div className="stage bg-ink">
          <video
            className="h-full w-full"
            src={`/api/assets/${row.asset_id}/file`}
            controls
            playsInline
          />
        </div>
      );
    }
    const embed = youtubeEmbed(row.asset_source_url);
    if (embed) {
      return (
        <div className="stage">
          <iframe
            className="h-full w-full"
            src={embed}
            title={row.asset_title}
            allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
            allowFullScreen
          />
        </div>
      );
    }
    return row.asset_source_url ? (
      <a className="text-sm text-accent underline" href={row.asset_source_url} target="_blank" rel="noreferrer">
        Open video
      </a>
    ) : (
      <p className="text-sm text-muted">No video URL yet.</p>
    );
  }
  if (row.image_assets.length === 0) {
    return (
      <EmptyState
        title="No stored images yet"
        description="Sync the folder first, then the photos will appear here."
        action={
          <Button loading={busy} onClick={onSync}>
            Prepare & index photos
          </Button>
        }
      />
    );
  }
  return <PhotoGallery key={row.asset_id} images={row.image_assets} />;
}

function PhotoGallery({
  images,
}: {
  images: MediaIndexRow["image_assets"];
}) {
  const PAGE_SIZE = 18;
  const CONCURRENT_LOADS = 3;
  const initialTarget = Math.min(PAGE_SIZE, images.length);
  const [targetCount, setTargetCount] = useState(initialTarget);
  const [mountedCount, setMountedCount] = useState(
    Math.min(CONCURRENT_LOADS, initialTarget),
  );

  const thumbnailSettled = () => {
    setMountedCount((count) => Math.min(count + 1, targetCount));
  };

  const loadMore = () => {
    const nextTarget = Math.min(targetCount + PAGE_SIZE, images.length);
    setTargetCount(nextTarget);
    setMountedCount((count) => Math.min(count + CONCURRENT_LOADS, nextTarget));
  };

  const visible = images.slice(0, mountedCount);
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
        {visible.map((image) => (
          <ImageThumb
            key={image.id}
            id={image.id}
            title={image.title}
            onSettled={thumbnailSettled}
          />
        ))}
      </div>
      {mountedCount < targetCount && (
        <p className="flex items-center gap-2 text-xs text-muted">
          <Spinner /> Loading previews… ({mountedCount}/{targetCount})
        </p>
      )}
      {mountedCount >= targetCount && targetCount < images.length && (
        <div className="flex items-center justify-between gap-3">
          <p className="text-xs text-muted">
            Showing {targetCount} of {images.length} images
          </p>
          <Button onClick={loadMore}>
            Load {Math.min(PAGE_SIZE, images.length - targetCount)} more
          </Button>
        </div>
      )}
    </div>
  );
}

function ImageThumb({
  id,
  title,
  onSettled,
}: {
  id: number;
  title: string;
  onSettled: () => void;
}) {
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const [fullLoaded, setFullLoaded] = useState(false);
  const [fullFailed, setFullFailed] = useState(false);
  const settledRef = useRef(false);

  const markSettled = () => {
    if (settledRef.current) return;
    settledRef.current = true;
    onSettled();
  };

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
        className="relative aspect-[4/3] overflow-hidden rounded-lg border border-line bg-paper"
        onClick={() => {
          setFullLoaded(false);
          setFullFailed(false);
          setOpen(true);
        }}
        aria-label={`View ${title}`}
      >
        {!loaded && !failed && (
          <Skeleton className="absolute inset-0 z-0 h-full w-full rounded-none" />
        )}
        {failed && (
          <span className="absolute inset-0 z-10 grid place-items-center px-3 text-xs text-muted">
            Preview unavailable
          </span>
        )}
        <img
          src={`/api/assets/${id}/thumbnail.jpg`}
          alt={title}
          loading="lazy"
          decoding="async"
          className={`relative z-10 h-full w-full object-cover ${failed ? "hidden" : "block"}`}
          onLoad={() => {
            setLoaded(true);
            markSettled();
          }}
          onError={() => {
            setFailed(true);
            setLoaded(false);
            markSettled();
          }}
        />
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-ink/90 p-4"
          role="dialog"
          aria-modal="true"
          aria-label={title}
          onClick={() => setOpen(false)}
        >
          {!fullLoaded && !fullFailed && <Spinner className="absolute h-8 w-8 text-white" />}
          {fullFailed && (
            <div className="rounded-xl bg-surface px-6 py-5 text-center">
              <p className="font-medium">Image preview unavailable</p>
              <p className="mt-1 text-sm text-muted">Close this view and try again.</p>
            </div>
          )}
          <img
            src={`/api/assets/${id}/thumbnail.jpg`}
            alt={title}
            className={`max-h-[90vh] max-w-[94vw] object-contain ${fullFailed ? "hidden" : "block"}`}
            onLoad={() => setFullLoaded(true)}
            onError={() => {
              setFullFailed(true);
              setFullLoaded(false);
            }}
            onClick={(event) => event.stopPropagation()}
          />
          <button
            type="button"
            className="btn-ghost absolute right-4 top-4 border-white/30 bg-ink/70 text-white"
            onClick={() => setOpen(false)}
          >
            Close
          </button>
        </div>
      )}
    </>
  );
}
