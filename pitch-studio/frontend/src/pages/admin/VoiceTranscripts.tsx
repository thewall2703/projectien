import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api";
import { AXES } from "../../axes";
import { axisLabel } from "../../labels";
import { Button, EmptyState, ErrorBanner, Skeleton, StatusBadge } from "../../components/ui";
import type { QaExtractionRun, RecipeOption, StyleGuide, StyleTranscriptRow } from "../../types";

function errMessage(err: unknown) {
  return err instanceof Error ? err.message : "Something went wrong";
}

type PersonaOption = {
  label: string;
  cluster: string;
  recipeCount: number;
};

function uniquePersonas(recipes: RecipeOption[]): PersonaOption[] {
  const byLabel = new Map<string, PersonaOption>();
  for (const recipe of recipes) {
    const label = (recipe.audience_label || "").trim();
    if (!label) continue;
    const existing = byLabel.get(label);
    if (existing) {
      existing.recipeCount += 1;
      continue;
    }
    byLabel.set(label, {
      label,
      cluster: recipe.audience_cluster || "",
      recipeCount: 1,
    });
  }
  return Array.from(byLabel.values()).sort((a, b) => {
    const clusterCmp = a.cluster.localeCompare(b.cluster);
    if (clusterCmp !== 0) return clusterCmp;
    return a.label.localeCompare(b.label);
  });
}

function PersonaMultiselect({
  options,
  selected,
  onChange,
  disabled,
  filter,
  onFilterChange,
}: {
  options: PersonaOption[];
  selected: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
  filter: string;
  onFilterChange: (value: string) => void;
}) {
  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const filtered = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return options;
    return options.filter((option) => {
      const cluster = axisLabel(AXES.audience_clusters, option.cluster).toLowerCase();
      return option.label.toLowerCase().includes(needle) || cluster.includes(needle);
    });
  }, [filter, options]);

  const toggle = (label: string) => {
    if (selectedSet.has(label)) {
      onChange(selected.filter((item) => item !== label));
      return;
    }
    onChange([...selected, label]);
  };

  const selectVisible = () => {
    const next = new Set(selected);
    for (const option of filtered) next.add(option.label);
    onChange(Array.from(next));
  };

  const clearAll = () => onChange([]);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <input
          className="field max-w-sm flex-1"
          type="search"
          value={filter}
          disabled={disabled}
          onChange={(e) => onFilterChange(e.target.value)}
          placeholder="Filter personas"
        />
        <Button disabled={disabled || filtered.length === 0} onClick={selectVisible}>
          Select visible
        </Button>
        <Button disabled={disabled || selected.length === 0} onClick={clearAll}>
          Clear
        </Button>
        <span className="text-xs text-grey">{selected.length} selected</span>
      </div>
      {selected.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {selected.map((label) => (
            <button
              key={label}
              type="button"
              disabled={disabled}
              className="rounded-full border border-black/10 bg-black/[0.03] px-3 py-1 text-xs text-black"
              onClick={() => toggle(label)}
              title="Remove"
            >
              {label} ×
            </button>
          ))}
        </div>
      )}
      <div className="max-h-64 overflow-y-auto rounded-xl border border-black/10">
        {filtered.length === 0 ? (
          <p className="px-4 py-6 text-sm text-grey">No personas match this filter.</p>
        ) : (
          <ul className="divide-y divide-black/5">
            {filtered.map((option) => {
              const checked = selectedSet.has(option.label);
              const clusterName = axisLabel(AXES.audience_clusters, option.cluster) || option.cluster;
              return (
                <li key={option.label}>
                  <label className="flex cursor-pointer items-start gap-3 px-4 py-3 hover:bg-black/[0.02]">
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={checked}
                      disabled={disabled}
                      onChange={() => toggle(option.label)}
                    />
                    <span className="min-w-0">
                      <span className="block text-sm font-medium text-black">{option.label}</span>
                      <span className="mt-0.5 block text-xs text-grey">
                        {clusterName}
                        {option.recipeCount > 1 ? ` · ${option.recipeCount} recipe variants` : ""}
                      </span>
                    </span>
                  </label>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}

export default function VoiceTranscripts() {
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [selectedFileName, setSelectedFileName] = useState("");
  const [pasteOpen, setPasteOpen] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [personaOptions, setPersonaOptions] = useState<PersonaOption[]>([]);
  const [selectedTranscriptIds, setSelectedTranscriptIds] = useState<number[]>([]);
  const [indexPersonas, setIndexPersonas] = useState<string[]>([]);
  const [indexPersonaFilter, setIndexPersonaFilter] = useState("");
  const [rows, setRows] = useState<StyleTranscriptRow[]>([]);
  const [runs, setRuns] = useState<QaExtractionRun[]>([]);
  const [guidePersona, setGuidePersona] = useState("");
  const [guide, setGuide] = useState<StyleGuide>({ version: 0, guide_text: "", persona_label: "" });
  const [guideDraft, setGuideDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [editingRowId, setEditingRowId] = useState<number | null>(null);
  const [editPersonas, setEditPersonas] = useState<string[]>([]);
  const [editFilter, setEditFilter] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [loading, setLoading] = useState(true);
  const fileRef = useRef<HTMLInputElement>(null);

  const reload = async (preferredGuidePersona?: string) => {
    const [transcripts, recipes, extractionRuns] = await Promise.all([
      api.styleTranscriptList(),
      api.recipes(),
      api.listQaExtractions(),
    ]);
    const personas = uniquePersonas(recipes as RecipeOption[]);
    setPersonaOptions(personas);
    setRows(transcripts);
    setRuns(extractionRuns);

    const nextGuidePersona =
      preferredGuidePersona ||
      guidePersona ||
      personas[0]?.label ||
      "";
    setGuidePersona(nextGuidePersona);
    if (nextGuidePersona) {
      const current = await api.styleGuideGet(nextGuidePersona);
      setGuide(current);
      if (!editing) setGuideDraft(current.guide_text);
    } else {
      setGuide({ version: 0, guide_text: "", persona_label: "" });
      if (!editing) setGuideDraft("");
    }
  };

  useEffect(() => {
    setLoading(true);
    reload()
      .catch((err) => setError(errMessage(err)))
      .finally(() => setLoading(false));
  }, []);

  const onFile = (file: File | null) => {
    if (!file) return;
    setSelectedFileName(file.name);
    const reader = new FileReader();
    reader.onload = () => {
      const value = typeof reader.result === "string" ? reader.result : "";
      setText(value);
      if (!name.trim()) {
        setName(file.name.replace(/\.(vtt|txt)$/i, ""));
      }
    };
    reader.readAsText(file);
  };

  const submit = async () => {
    setBusy("upload");
    setError("");
    setSuccess("");
    try {
      const result = await api.styleTranscriptCreate(name.trim(), text);
      setSuccess(`“${result.name}” was added to the transcript library. Select it below when you are ready to index it.`);
      setName("");
      setText("");
      setSelectedFileName("");
      setPasteOpen(false);
      if (fileRef.current) fileRef.current.value = "";
      await reload();
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setBusy("");
    }
  };

  const indexSelected = async () => {
    if (!selectedTranscriptIds.length || !indexPersonas.length) return;
    setBusy("index");
    setError("");
    setSuccess("");
    const transcriptIds = [...selectedTranscriptIds];
    const personas = [...indexPersonas];
    try {
      const result = await api.styleTranscriptIndex(transcriptIds, personas);
      setSuccess(
        `Indexed ${result.transcript_ids.length} transcript${result.transcript_ids.length === 1 ? "" : "s"} for ${result.guides_updated.join(", ")} — ${result.quotes_kept} quotes added.`,
      );
      setSelectedTranscriptIds([]);
      setIndexPersonas([]);
      setIndexPersonaFilter("");
      await reload(personas[0] || guidePersona);
    } catch (err) {
      setError(errMessage(err));
      await reload();
    } finally {
      setBusy("");
    }
  };

  const saveGuide = async () => {
    if (!guidePersona) return;
    setBusy("guide");
    setError("");
    setSuccess("");
    try {
      const saved = await api.styleGuideSave(guideDraft, guidePersona);
      setGuide(saved);
      setGuideDraft(saved.guide_text);
      setEditing(false);
      setSuccess(`Style guide for ${saved.persona_label || guidePersona} updated to version ${saved.version}`);
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setBusy("");
    }
  };

  const switchGuidePersona = async (label: string) => {
    setGuidePersona(label);
    setEditing(false);
    setError("");
    if (!label) {
      setGuide({ version: 0, guide_text: "", persona_label: "" });
      setGuideDraft("");
      return;
    }
    setBusy("switch-guide");
    try {
      const current = await api.styleGuideGet(label);
      setGuide(current);
      setGuideDraft(current.guide_text);
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setBusy("");
    }
  };

  const extractQa = async (transcriptId: number) => {
    setBusy(`extract-${transcriptId}`);
    setError("");
    setSuccess("");
    try {
      const run = await api.startQaExtraction(transcriptId, true);
      setSuccess(`Q&A extraction queued (run #${run.id}). Open AMA Q&A review when it finishes.`);
      await reload();
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setBusy("");
    }
  };

  const startEditPersonas = (row: StyleTranscriptRow) => {
    setEditingRowId(row.id);
    setEditPersonas([...(row.persona_labels || [])]);
    setEditFilter("");
    setError("");
    setSuccess("");
  };

  const saveEditPersonas = async () => {
    if (editingRowId == null) return;
    setBusy(`personas-${editingRowId}`);
    setError("");
    setSuccess("");
    const personasForSave = [...editPersonas];
    try {
      const updated = await api.styleTranscriptSetPersonas(editingRowId, personasForSave);
      setSuccess(
        `Updated personas for “${updated.name}” (${updated.persona_labels.join(", ") || "none"}). Style guides rebuilt.`,
      );
      setEditingRowId(null);
      setEditPersonas([]);
      await reload(personasForSave[0] || guidePersona);
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setBusy("");
    }
  };

  const latestRunFor = (transcriptId: number) =>
    runs.find((run) => run.style_transcript_id === transcriptId) || null;

  const canSubmit = Boolean(name.trim() && text.trim()) && !busy;
  const unindexedRows = rows.filter((row) => row.status !== "processed");
  const canIndex = selectedTranscriptIds.length > 0 && indexPersonas.length > 0 && !busy;
  const selectableUnindexedIds = unindexedRows
    .filter((row) => row.status !== "indexing")
    .map((row) => row.id);
  const allUnindexedSelected =
    selectableUnindexedIds.length > 0 &&
    selectableUnindexedIds.every((id) => selectedTranscriptIds.includes(id));

  return (
    <div className="mx-auto max-w-6xl px-6 py-10 md:px-10">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="kicker">Founder voice</p>
          <h1 className="mt-2 font-display text-4xl tracking-tight text-black">Transcript library</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-grey-dark">
            Upload meeting transcripts now and decide when—and for which personas—they should
            influence script style.
          </p>
        </div>
        <Link className="btn min-h-11" to="/admin/qa-review">
          Open AMA Q&amp;A review
        </Link>
      </div>

      {(error || success) && (
        <div className="mt-5">
          <ErrorBanner message={error} />
          {success && (
            <div className="rounded-xl border border-black/10 bg-white/60 px-4 py-3 text-sm text-black" role="status">
              {success}
            </div>
          )}
        </div>
      )}

      <div className="mt-8 grid gap-3 sm:grid-cols-2">
        <div className="rounded-2xl border border-black/10 bg-white/50 px-4 py-3">
          <div className="flex items-center gap-3">
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-black text-sm font-semibold text-white">1</span>
            <div>
              <p className="text-sm font-medium text-black">Add to library</p>
              <p className="text-xs text-grey">Store the meeting name and VTT safely.</p>
            </div>
          </div>
        </div>
        <div className="rounded-2xl border border-black/10 bg-white/50 px-4 py-3">
          <div className="flex items-center gap-3">
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-black text-sm font-semibold text-white">2</span>
            <div>
              <p className="text-sm font-medium text-black">Assign and index</p>
              <p className="text-xs text-grey">Choose meetings and applicable personas.</p>
            </div>
          </div>
        </div>
      </div>

      <div className="glass-panel mt-5 space-y-5 p-5 md:p-6">
        <div>
          <p className="kicker">Step 1</p>
          <h2 className="mt-2 font-display text-2xl text-black">Add a meeting transcript</h2>
          <p className="mt-1 text-sm text-grey">
            Nothing is indexed until you complete Step 2.
          </p>
        </div>
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(320px,0.8fr)]">
          <label className="block text-sm font-medium text-grey-dark">
            Meeting name
            <input
              className="field mt-2"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Raising capital AMA · 13 September"
            />
          </label>
          <div>
            <p className="text-sm font-medium text-grey-dark">Transcript file</p>
            <button
              type="button"
              className={`mt-2 flex min-h-28 w-full flex-col items-center justify-center rounded-2xl border border-dashed px-5 py-4 text-center transition ${
                dragActive
                  ? "border-black bg-brand-yellow/15"
                  : selectedFileName
                    ? "border-black/25 bg-white/75"
                    : "border-black/15 bg-white/40 hover:border-black/35 hover:bg-white/70"
              }`}
              disabled={Boolean(busy)}
              onClick={() => fileRef.current?.click()}
              onDragEnter={(event) => {
                event.preventDefault();
                setDragActive(true);
              }}
              onDragOver={(event) => event.preventDefault()}
              onDragLeave={() => setDragActive(false)}
              onDrop={(event) => {
                event.preventDefault();
                setDragActive(false);
                onFile(event.dataTransfer.files?.[0] || null);
              }}
            >
              <span className="text-sm font-medium text-black">
                {selectedFileName || "Drop a .vtt file here"}
              </span>
              <span className="mt-1 text-xs text-grey">
                {selectedFileName ? `${text.length.toLocaleString()} characters loaded` : "or click to choose a file"}
              </span>
            </button>
          </div>
          <input
            ref={fileRef}
            type="file"
            accept=".vtt,.txt,text/vtt,text/plain"
            className="hidden"
            onChange={(e) => onFile(e.target.files?.[0] || null)}
          />
        </div>
        <div>
          <button
            type="button"
            className="text-sm font-medium text-black underline-offset-4 hover:underline"
            onClick={() => setPasteOpen((open) => !open)}
          >
            {pasteOpen ? "Hide pasted transcript" : "No file? Paste transcript text instead"}
          </button>
          {pasteOpen && (
            <label className="mt-3 block text-sm text-grey-dark">
              Transcript text
              <textarea
                className="field mt-2 min-h-[160px]"
                value={text}
                onChange={(e) => {
                  setText(e.target.value);
                  setSelectedFileName("");
                  if (fileRef.current) fileRef.current.value = "";
                }}
                placeholder="Paste WEBVTT or plain text"
              />
            </label>
          )}
        </div>
        <div className="flex flex-col gap-2 border-t border-black/8 pt-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-xs text-grey">
            {canSubmit ? "Ready to add. You will choose personas in the next step." : "Add a meeting name and transcript file to continue."}
          </p>
          <Button
            variant="accent"
            className="min-h-11 w-full sm:w-auto"
            loading={busy === "upload"}
            disabled={!canSubmit}
            onClick={submit}
          >
            {busy === "upload" ? "Adding to library…" : "Add transcript"}
          </Button>
        </div>
      </div>

      <div id="transcript-indexing" className="glass-panel mt-8 scroll-mt-24 space-y-5 p-5 md:p-6">
        <div>
          <p className="kicker">Step 2</p>
          <h2 className="mt-2 font-display text-xl text-black">Index stored transcripts</h2>
          <p className="mt-1 text-sm text-grey">
            Choose one or more unindexed meetings and every persona whose style guide should use them.
          </p>
        </div>
        {unindexedRows.length ? (
          <>
            <div className="grid gap-6 lg:grid-cols-2">
              <div>
                <div className="mb-3 flex items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-medium text-black">1. Choose meetings</p>
                    <p className="mt-0.5 text-xs text-grey">{selectedTranscriptIds.length} selected</p>
                  </div>
                  {selectableUnindexedIds.length > 1 && (
                    <button
                      type="button"
                      className="text-xs font-medium text-black underline-offset-4 hover:underline"
                      disabled={Boolean(busy)}
                      onClick={() =>
                        setSelectedTranscriptIds(allUnindexedSelected ? [] : selectableUnindexedIds)
                      }
                    >
                      {allUnindexedSelected ? "Clear all" : "Select all"}
                    </button>
                  )}
                </div>
                <div className="max-h-[26rem] overflow-y-auto rounded-xl border border-black/10 bg-white/40">
                  <ul className="divide-y divide-black/5">
                    {unindexedRows.map((row) => {
                      const checked = selectedTranscriptIds.includes(row.id);
                      return (
                        <li key={row.id}>
                          <label
                            className={`flex min-h-14 cursor-pointer items-center gap-3 px-4 py-3 transition hover:bg-white/70 ${
                              checked ? "bg-brand-yellow/10" : ""
                            }`}
                          >
                            <input
                              type="checkbox"
                              checked={checked}
                              disabled={Boolean(busy) || row.status === "indexing"}
                              onChange={() =>
                                setSelectedTranscriptIds((current) =>
                                  checked
                                    ? current.filter((id) => id !== row.id)
                                    : [...current, row.id],
                                )
                              }
                            />
                            <span className="min-w-0 flex-1">
                              <span className="block truncate text-sm font-medium text-black">{row.name}</span>
                              <span className="mt-0.5 block text-xs text-grey">
                                {row.text_length.toLocaleString()} characters
                                {row.status === "failed" ? " · Previous attempt failed" : ""}
                              </span>
                            </span>
                            <StatusBadge status={row.status} />
                          </label>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              </div>
              <div>
                <div className="mb-3">
                  <p className="text-sm font-medium text-black">2. Choose applicable personas</p>
                  <p className="mt-0.5 text-xs text-grey">
                    All selected meetings will enrich these persona guides.
                  </p>
                </div>
                <PersonaMultiselect
                  options={personaOptions}
                  selected={indexPersonas}
                  onChange={setIndexPersonas}
                  disabled={Boolean(busy)}
                  filter={indexPersonaFilter}
                  onFilterChange={setIndexPersonaFilter}
                />
              </div>
            </div>
            <div className="flex flex-col gap-3 border-t border-black/8 pt-5 sm:flex-row sm:items-center sm:justify-between">
              <span className="text-xs text-grey">
                {selectedTranscriptIds.length && indexPersonas.length
                  ? `${selectedTranscriptIds.length} meeting${selectedTranscriptIds.length === 1 ? "" : "s"} will be indexed for ${indexPersonas.length} persona${indexPersonas.length === 1 ? "" : "s"}.`
                  : "Select at least one meeting and one persona to continue."}
              </span>
              <Button
                variant="accent"
                className="min-h-11 w-full sm:w-auto"
                loading={busy === "index"}
                disabled={!canIndex}
                onClick={indexSelected}
              >
                {busy === "index"
                  ? "Indexing transcripts…"
                  : `Index selected${selectedTranscriptIds.length ? ` (${selectedTranscriptIds.length})` : ""}`}
              </Button>
            </div>
          </>
        ) : (
          <EmptyState
            title="No unindexed transcripts"
            description="Add a meeting VTT above. It will appear here when it is ready to index."
          />
        )}
      </div>

      <section className="mt-10">
        <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
          <div>
            <p className="kicker">All meetings</p>
            <h2 className="mt-2 font-display text-2xl text-black">Transcript library</h2>
          </div>
          {!loading && <span className="text-sm text-grey">{rows.length} total</span>}
        </div>
        <div className="space-y-3">
          {loading &&
            Array.from({ length: 3 }).map((_, index) => (
              <div key={`sk-${index}`} className="glass-panel space-y-3 p-5">
                <div className="flex items-center justify-between gap-4">
                  <Skeleton className="h-5 w-56" />
                  <Skeleton className="h-6 w-20 rounded-full" />
                </div>
                <Skeleton className="h-4 w-80 max-w-full" />
              </div>
            ))}
          {!loading &&
            rows.map((row) => {
              const run = latestRunFor(row.id);
              const editingThis = editingRowId === row.id;
              return (
                <article key={row.id} className="glass-panel p-5">
                  <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <h3 className="truncate font-medium text-black">{row.name}</h3>
                        <StatusBadge status={row.status} />
                      </div>
                      <p className="mt-1 text-xs text-grey">
                        {row.text_length.toLocaleString()} characters
                        {row.created_at ? ` · Added ${new Date(row.created_at).toLocaleString()}` : ""}
                      </p>
                      {!editingThis && (
                        <div className="mt-3 flex flex-wrap gap-1.5">
                          {row.persona_labels?.length ? (
                            row.persona_labels.map((label) => (
                              <span
                                key={label}
                                className="rounded-full border border-black/10 bg-black/[0.03] px-2.5 py-1 text-xs text-black"
                              >
                                {label}
                              </span>
                            ))
                          ) : (
                            <span className="text-xs text-grey">
                              {row.status === "processed" ? "No personas assigned" : "Waiting to be indexed"}
                            </span>
                          )}
                        </div>
                      )}
                      {run && (
                        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-grey">
                          <span>Q&amp;A extraction:</span>
                          <StatusBadge status={run.status} />
                          <span>{run.candidate_count ? `${run.candidate_count} candidates` : run.stage || ""}</span>
                        </div>
                      )}
                    </div>
                    <div className="flex shrink-0 flex-wrap gap-2">
                      {!editingThis && row.status === "processed" && (
                        <Button disabled={Boolean(busy)} onClick={() => startEditPersonas(row)}>
                          Edit personas
                        </Button>
                      )}
                      {row.status === "processed" && (
                        <Button
                          loading={busy === `extract-${row.id}`}
                          disabled={Boolean(busy)}
                          onClick={() => extractQa(row.id)}
                        >
                          Extract Q&amp;As
                        </Button>
                      )}
                      {row.status !== "processed" && (
                        <button
                          type="button"
                          className="text-sm font-medium text-black underline-offset-4 hover:underline"
                          onClick={() => {
                            setSelectedTranscriptIds([row.id]);
                            document.getElementById("transcript-indexing")?.scrollIntoView({
                              behavior: "smooth",
                              block: "start",
                            });
                          }}
                        >
                          Select to index
                        </button>
                      )}
                    </div>
                  </div>
                  {editingThis && (
                    <div className="mt-5 space-y-3 border-t border-black/8 pt-5">
                      <p className="text-sm font-medium text-black">Edit applicable personas</p>
                      <PersonaMultiselect
                        options={personaOptions}
                        selected={editPersonas}
                        onChange={setEditPersonas}
                        disabled={Boolean(busy)}
                        filter={editFilter}
                        onFilterChange={setEditFilter}
                      />
                      <div className="flex flex-wrap gap-2">
                        <Button
                          variant="accent"
                          loading={busy === `personas-${row.id}`}
                          disabled={editPersonas.length === 0 || Boolean(busy)}
                          onClick={saveEditPersonas}
                        >
                          Save personas
                        </Button>
                        <Button
                          disabled={Boolean(busy)}
                          onClick={() => {
                            setEditingRowId(null);
                            setEditPersonas([]);
                          }}
                        >
                          Cancel
                        </Button>
                      </div>
                    </div>
                  )}
                </article>
              );
            })}
          {!loading && rows.length === 0 && (
            <EmptyState
              title="No transcripts yet"
              description="Add a meeting name and WEBVTT file above to start the transcript library."
            />
          )}
        </div>
      </section>

      <div className="glass-panel mt-8 space-y-4 p-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="font-display text-xl text-black">
            Style guide {guide.version > 0 ? `v${guide.version}` : "v0"}
            {guidePersona ? ` · ${guidePersona}` : ""}
          </h2>
          <div className="flex flex-wrap items-center gap-2">
            <select
              className="field min-w-[220px]"
              value={guidePersona}
              disabled={Boolean(busy) || personaOptions.length === 0}
              onChange={(e) => switchGuidePersona(e.target.value)}
            >
              {personaOptions.length === 0 && <option value="">No personas</option>}
              {personaOptions.map((option) => (
                <option key={option.label} value={option.label}>
                  {option.label}
                </option>
              ))}
            </select>
            {editing ? (
              <>
                <Button variant="accent" loading={busy === "guide"} disabled={!guidePersona} onClick={saveGuide}>
                  Save
                </Button>
                <Button
                  disabled={Boolean(busy)}
                  onClick={() => {
                    setGuideDraft(guide.guide_text);
                    setEditing(false);
                  }}
                >
                  Cancel
                </Button>
              </>
            ) : (
              <Button disabled={!guidePersona || Boolean(busy)} onClick={() => setEditing(true)}>
                Edit
              </Button>
            )}
          </div>
        </div>
        {editing ? (
          <textarea
            className="field min-h-[280px]"
            value={guideDraft}
            onChange={(e) => setGuideDraft(e.target.value)}
          />
        ) : loading || busy === "switch-guide" ? (
          <div className="space-y-2">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-5/6" />
            <Skeleton className="h-4 w-2/3" />
          </div>
        ) : guide.guide_text ? (
          <pre className="whitespace-pre-wrap text-sm leading-6 text-black">{guide.guide_text}</pre>
        ) : (
          <p className="text-sm text-grey">
            {guidePersona
              ? `No style guide yet for ${guidePersona}. Upload a transcript assigned to this persona.`
              : "No style guide yet. Upload a transcript to create version 1."}
          </p>
        )}
      </div>
    </div>
  );
}
