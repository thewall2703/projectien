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

  return (
    <div className="mx-auto max-w-6xl px-6 py-10 md:px-10">
      <h1 className="font-display text-3xl tracking-tight text-black">Style guide</h1>
      <p className="mt-2 max-w-2xl text-sm text-grey-dark">
        Add AMA or webinar transcripts to the library first. When you are ready, select unindexed
        transcripts, choose the personas they apply to, and index them.
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-4">
        <ErrorBanner message={error} />
        {success && <p className="text-sm text-black">{success}</p>}
        <Link className="text-sm font-medium text-black underline-offset-4 hover:underline" to="/admin/qa-review">
          Open AMA Q&A review
        </Link>
      </div>

      <div className="glass-panel mt-6 space-y-4 p-6">
        <div>
          <p className="kicker">Step 1</p>
          <h2 className="mt-2 font-display text-xl text-black">Add a meeting transcript</h2>
          <p className="mt-1 text-sm text-grey">
            The VTT is stored in the library without changing any style guide.
          </p>
        </div>
        <label className="block text-sm text-grey-dark">
          Meeting name
          <input
            className="field mt-1"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="AMA 12 March"
          />
        </label>
        <label className="block text-sm text-grey-dark">
          Transcript
          <textarea
            className="field mt-1 min-h-[180px]"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Paste WEBVTT or plain text"
          />
        </label>
        <div className="flex flex-wrap items-center gap-3">
          <input
            ref={fileRef}
            type="file"
            accept=".vtt,.txt,text/vtt,text/plain"
            className="text-sm text-grey-dark"
            onChange={(e) => onFile(e.target.files?.[0] || null)}
          />
          <Button variant="accent" loading={busy === "upload"} disabled={!canSubmit} onClick={submit}>
            {busy === "upload" ? "Adding to library…" : "Add to library"}
          </Button>
        </div>
      </div>

      <div className="glass-panel mt-8 space-y-5 p-6">
        <div>
          <p className="kicker">Step 2</p>
          <h2 className="mt-2 font-display text-xl text-black">Index stored transcripts</h2>
          <p className="mt-1 text-sm text-grey">
            Choose one or more unindexed meetings and every persona whose style guide should use them.
          </p>
        </div>
        {unindexedRows.length ? (
          <>
            <div className="rounded-xl border border-black/10">
              <ul className="divide-y divide-black/5">
                {unindexedRows.map((row) => {
                  const checked = selectedTranscriptIds.includes(row.id);
                  return (
                    <li key={row.id}>
                      <label className="flex min-h-11 cursor-pointer items-center gap-3 px-4 py-3 hover:bg-black/[0.02]">
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
                            {row.text_length.toLocaleString()} chars · {row.status === "failed" ? "Indexing failed — retry available" : row.status}
                          </span>
                        </span>
                        <StatusBadge status={row.status} />
                      </label>
                    </li>
                  );
                })}
              </ul>
            </div>
            <div>
              <p className="text-sm text-grey-dark">Applicable personas</p>
              <p className="mt-1 text-xs text-grey">
                The selected meetings will enrich each selected persona. Duration and channel variants
                under the same persona share one guide.
              </p>
              <div className="mt-3">
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
            <div className="flex flex-wrap items-center gap-3">
              <Button
                variant="accent"
                loading={busy === "index"}
                disabled={!canIndex}
                onClick={indexSelected}
              >
                {busy === "index"
                  ? "Indexing transcripts…"
                  : `Index selected${selectedTranscriptIds.length ? ` (${selectedTranscriptIds.length})` : ""}`}
              </Button>
              <span className="text-xs text-grey">
                Indexing can take several minutes and updates the selected persona guides.
              </span>
            </div>
          </>
        ) : (
          <EmptyState
            title="No unindexed transcripts"
            description="Add a meeting VTT above. It will appear here when it is ready to index."
          />
        )}
      </div>

      <div className="glass-panel mt-8 overflow-x-auto">
        <div className="flex items-center justify-between px-4 py-3">
          <h2 className="font-display text-xl text-black">Transcript library</h2>
        </div>
        <table className="min-w-full text-left text-sm">
          <thead className="border-b border-black/10 text-grey">
            <tr>
              <th className="px-4 py-3 font-medium">Name</th>
              <th className="px-4 py-3 font-medium">Personas</th>
              <th className="px-4 py-3 font-medium">Status</th>
              <th className="px-4 py-3 font-medium">Length</th>
              <th className="px-4 py-3 font-medium">Q&A extraction</th>
              <th className="px-4 py-3 font-medium">Uploaded</th>
              <th className="px-4 py-3 font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading &&
              Array.from({ length: 3 }).map((_, index) => (
                <tr key={`sk-${index}`} className="border-t border-black/5">
                  {Array.from({ length: 7 }).map((__, cell) => (
                    <td key={cell} className="px-4 py-3">
                      <Skeleton className="h-4 w-28" />
                    </td>
                  ))}
                </tr>
              ))}
            {!loading &&
              rows.map((row) => {
                const run = latestRunFor(row.id);
                const editingThis = editingRowId === row.id;
                return (
                  <tr key={row.id} className="border-t border-black/5 align-top">
                    <td className="max-w-xs truncate px-4 py-3 text-black">{row.name}</td>
                    <td className="px-4 py-3">
                      {editingThis ? (
                        <div className="min-w-[280px] space-y-3">
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
                      ) : row.persona_labels?.length ? (
                        <div className="flex max-w-sm flex-wrap gap-1.5">
                          {row.persona_labels.map((label) => (
                            <span
                              key={label}
                              className="rounded-full border border-black/10 bg-black/[0.03] px-2.5 py-0.5 text-xs text-black"
                            >
                              {label}
                            </span>
                          ))}
                        </div>
                      ) : (
                        <span className="text-xs text-grey">None assigned</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <StatusBadge status={row.status} />
                    </td>
                    <td className="px-4 py-3 text-black">{row.text_length.toLocaleString()} chars</td>
                    <td className="px-4 py-3 text-grey">
                      {run ? (
                        <span>
                          <StatusBadge status={run.status} />{" "}
                          <span className="text-xs">
                            {run.candidate_count ? `${run.candidate_count} candidates` : run.stage || ""}
                          </span>
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="px-4 py-3 text-grey">
                      {row.created_at ? new Date(row.created_at).toLocaleString() : "—"}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex flex-col gap-2">
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
                            Extract Q&As
                          </Button>
                        )}
                        {row.status !== "processed" && (
                          <span className="text-xs text-grey">Select in Step 2 to index</span>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
          </tbody>
        </table>
        {!loading && rows.length === 0 && (
          <div className="p-6">
            <EmptyState
              title="No transcripts yet"
              description="Add a meeting name and WEBVTT file above to start the transcript library."
            />
          </div>
        )}
      </div>

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
