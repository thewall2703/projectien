import { useEffect, useState } from "react";

import { api } from "../../api";
import { Button, EmptyState, StatusBadge } from "../../components/ui";
import type { GeneratedSlideAttemptRow, GeneratedSlideRow } from "../../types";

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Something went wrong";
}

const emptyFilters = {
  reviewStatus: "",
  outcome: "",
  templateId: "",
  generationId: "",
};

export default function GeneratedSlides() {
  const [view, setView] = useState<"library" | "attempts">("library");
  const [items, setItems] = useState<GeneratedSlideRow[]>([]);
  const [attempts, setAttempts] = useState<GeneratedSlideAttemptRow[]>([]);
  const [drafts, setDrafts] = useState<Record<number, Record<string, string | string[]>>>({});
  const [notes, setNotes] = useState<Record<number, string>>({});
  const [filters, setFilters] = useState(emptyFilters);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");

  const loadLibrary = async () => {
    const rows = await api.list<GeneratedSlideRow>("/api/admin/generated-slides");
    setItems(rows);
    setDrafts(Object.fromEntries(rows.map((row) => [row.id, { ...row.slot_values }])));
  };

  const setAttemptRows = (rows: GeneratedSlideAttemptRow[]) => {
    setAttempts(rows);
    setNotes(Object.fromEntries(rows.map((row) => [row.id, row.review_note || ""])));
  };

  const loadAttempts = async (activeFilters = filters) => {
    const query = new URLSearchParams();
    if (activeFilters.reviewStatus) query.set("review_status", activeFilters.reviewStatus);
    if (activeFilters.outcome.trim()) query.set("outcome", activeFilters.outcome.trim());
    if (activeFilters.templateId.trim()) query.set("template_id", activeFilters.templateId.trim());
    if (activeFilters.generationId.trim()) query.set("generation_id", activeFilters.generationId.trim());
    const suffix = query.toString();
    setAttemptRows(
      await api.list<GeneratedSlideAttemptRow>(
        `/api/admin/generated-slide-attempts${suffix ? `?${suffix}` : ""}`,
      ),
    );
  };

  useEffect(() => {
    setLoading(true);
    const request = view === "library" ? loadLibrary() : loadAttempts(emptyFilters);
    request
      .catch((error) => setMessage(errorMessage(error)))
      .finally(() => setLoading(false));
  }, [view]);

  const runAttemptLoad = (activeFilters = filters) => {
    setLoading(true);
    setMessage("");
    loadAttempts(activeFilters)
      .catch((error) => setMessage(errorMessage(error)))
      .finally(() => setLoading(false));
  };

  const save = async (item: GeneratedSlideRow) => {
    setBusyId(item.id);
    setMessage("");
    try {
      await api.update(`/api/admin/generated-slides/${item.id}`, {
        slot_values: drafts[item.id] || {},
      });
      await loadLibrary();
      setMessage("Slide re-rendered.");
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (item: GeneratedSlideRow) => {
    if (!window.confirm("Delete this generated slide? It will be regenerated if needed.")) return;
    setBusyId(item.id);
    setMessage("");
    try {
      await api.remove(`/api/admin/generated-slides/${item.id}`);
      await loadLibrary();
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setBusyId(null);
    }
  };

  const updateSlot = (id: number, key: string, value: string | string[]) => {
    setDrafts((current) => ({
      ...current,
      [id]: { ...(current[id] || {}), [key]: value },
    }));
  };

  const review = async (
    item: GeneratedSlideAttemptRow,
    reviewStatus: "approved" | "reviewed" | "rejected",
    useAsGuidance: boolean,
  ) => {
    const reviewNote = (notes[item.id] || "").trim();
    if (useAsGuidance && !reviewNote) {
      setMessage("Add a review note before approving this attempt as guidance.");
      return;
    }
    setBusyId(item.id);
    setMessage("");
    try {
      const updated = await api.update<GeneratedSlideAttemptRow>(
        `/api/admin/generated-slide-attempts/${item.id}/review`,
        {
          review_status: reviewStatus,
          review_note: reviewNote,
          use_as_guidance: useAsGuidance,
        },
      );
      setAttempts((current) =>
        current.map((attempt) => (attempt.id === updated.id ? updated : attempt)),
      );
      setNotes((current) => ({ ...current, [updated.id]: updated.review_note }));
      setMessage(useAsGuidance ? "Attempt approved for guidance." : "Attempt marked as reviewed.");
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setBusyId(null);
    }
  };

  const slotValue = (value: unknown) => {
    if (Array.isArray(value)) return value.map(String).join("\n");
    if (value && typeof value === "object") return JSON.stringify(value, null, 2);
    return String(value ?? "");
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-semibold">Generated slides</h1>
        <p className="mt-2 text-sm text-grey">
          Edit accepted library slides or review the append-only generation record.
        </p>
      </div>

      <div className="flex gap-2 border-b border-black/10">
        {([
          ["library", "Accepted library"],
          ["attempts", "Attempt review"],
        ] as const).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => {
              setView(key);
              setMessage("");
            }}
            className={`border-b-2 px-4 py-3 text-sm font-medium ${
              view === key ? "border-black text-black" : "border-transparent text-grey"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {message && (
        <p className="rounded-xl border border-black/10 bg-white px-4 py-3 text-sm" role="status">
          {message}
        </p>
      )}

      {view === "library" && (
        <>
          <p className="text-sm text-grey">
            Shared, accepted slides. Editing re-renders the image and protects the copy
            from automatic overwrites.
          </p>
          {!loading && !items.length ? (
            <EmptyState
              title="No accepted generated slides"
              description="Accepted slides will appear here after a successful generation."
            />
          ) : (
            <div className="grid gap-6 xl:grid-cols-2">
              {items.map((item) => (
                <article key={item.id} className="overflow-hidden rounded-2xl border border-black/10 bg-white">
                  <div className="aspect-video bg-black">
                    <img
                      src={`${item.image_url}?v=${encodeURIComponent(item.updated_at)}`}
                      alt={`Generated ${item.template_id} slide`}
                      className="h-full w-full object-contain"
                    />
                  </div>
                  <div className="space-y-4 p-5">
                    <div className="flex flex-wrap items-center gap-2 text-xs text-grey">
                      <span>{item.template_id}</span><span>·</span><span>{item.tone}</span>
                      <span>·</span><span>{item.status}</span>
                      {item.edited_by_human && (
                        <span className="rounded-full bg-black px-2 py-1 text-white">Human edited</span>
                      )}
                    </div>
                    {Object.entries(drafts[item.id] || {}).map(([key, value]) => {
                      const isList = Array.isArray(value);
                      return (
                        <label key={key} className="block text-sm font-medium">
                          <span className="mb-1 block capitalize">
                            {key.replaceAll("_", " ")}
                            {isList && <span className="ml-2 font-normal text-grey">(one per line)</span>}
                          </span>
                          <textarea
                            value={isList ? (value as string[]).join("\n") : (value as string)}
                            rows={isList ? Math.max(3, (value as string[]).length) : 3}
                            onChange={(event) =>
                              updateSlot(
                                item.id,
                                key,
                                isList
                                  ? event.target.value.split("\n").filter((line) => line.trim())
                                  : event.target.value,
                              )
                            }
                            className="w-full rounded-xl border border-black/15 px-3 py-2 font-normal"
                          />
                        </label>
                      );
                    })}
                    <div className="flex gap-3">
                      <Button loading={busyId === item.id} onClick={() => save(item)}>
                        Save &amp; re-render
                      </Button>
                      <button
                        type="button"
                        disabled={busyId === item.id}
                        onClick={() => remove(item)}
                        className="rounded-xl border border-red-300 px-4 py-2 text-sm font-medium text-red-700 disabled:opacity-50"
                      >
                        Delete
                      </button>
                    </div>
                  </div>
                </article>
              ))}
            </div>
          )}
        </>
      )}

      {view === "attempts" && (
        <>
          <div className="rounded-2xl border border-black/10 bg-white p-4">
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
              <label className="text-sm font-medium">
                <span className="mb-1 block">Review status</span>
                <select
                  value={filters.reviewStatus}
                  onChange={(event) => setFilters((value) => ({ ...value, reviewStatus: event.target.value }))}
                  className="w-full rounded-xl border border-black/15 px-3 py-2 font-normal"
                >
                  <option value="">All statuses</option>
                  <option value="pending">Pending</option>
                  <option value="reviewed">Reviewed</option>
                  <option value="approved">Approved guidance</option>
                  <option value="rejected">Rejected</option>
                </select>
              </label>
              <FilterInput
                label="Outcome"
                value={filters.outcome}
                placeholder="e.g. rendered"
                onChange={(outcome) => setFilters((value) => ({ ...value, outcome }))}
              />
              <FilterInput
                label="Template"
                value={filters.templateId}
                placeholder="Template ID"
                onChange={(templateId) => setFilters((value) => ({ ...value, templateId }))}
              />
              <FilterInput
                label="Generation"
                value={filters.generationId}
                placeholder="Generation ID"
                onChange={(generationId) => setFilters((value) => ({ ...value, generationId }))}
              />
            </div>
            <div className="mt-4 flex gap-3">
              <Button loading={loading} onClick={() => runAttemptLoad()}>Apply filters</Button>
              <Button
                variant="ghost"
                disabled={loading}
                onClick={() => {
                  setFilters(emptyFilters);
                  runAttemptLoad(emptyFilters);
                }}
              >
                Clear
              </Button>
            </div>
          </div>

          {!loading && !attempts.length ? (
            <EmptyState
              title="No generation attempts found"
              description="No attempts match these filters. Clear them or generate a deck first."
            />
          ) : (
            <div className="space-y-6">
              {attempts.map((item) => (
                <article key={item.id} className="overflow-hidden rounded-2xl border border-black/10 bg-white">
                  <div className="grid lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
                    <div className="flex min-h-64 items-center justify-center bg-black">
                      {item.image_url ? (
                        <img
                          src={item.image_url}
                          alt={`Attempt ${item.id} for ${item.template_id}`}
                          className="h-full max-h-[32rem] w-full object-contain"
                        />
                      ) : (
                        <p className="px-6 text-center text-sm text-white/60">
                          No image was produced for this attempt.
                        </p>
                      )}
                    </div>
                    <div className="space-y-5 p-5">
                      <div className="flex flex-wrap items-center gap-2">
                        <StatusBadge status={item.review_status} />
                        <StatusBadge status={item.outcome} />
                        <span className="text-xs text-grey">Gate: {item.gate || "—"}</span>
                      </div>
                      <section>
                        <p className="text-xs font-medium uppercase tracking-wide text-grey">Identity</p>
                        <p className="mt-1 text-sm">
                          Generation {item.generation_id} · {item.placeholder_key} · attempt {item.attempt_number}
                        </p>
                        <p className="mt-1 text-xs text-grey">
                          {item.template_id} · {item.tone} · {new Date(item.created_at).toLocaleString()}
                        </p>
                      </section>
                      <section>
                        <p className="text-xs font-medium uppercase tracking-wide text-grey">Claim</p>
                        <p className="mt-1 text-sm">{item.claim || "No claim recorded."}</p>
                      </section>
                      <section>
                        <p className="text-xs font-medium uppercase tracking-wide text-grey">Slot values</p>
                        {Object.keys(item.slot_values).length ? (
                          <dl className="mt-2 space-y-2">
                            {Object.entries(item.slot_values).map(([key, value]) => (
                              <div key={key}>
                                <dt className="text-xs font-medium capitalize text-grey">{key.replaceAll("_", " ")}</dt>
                                <dd className="whitespace-pre-wrap text-sm">{slotValue(value)}</dd>
                              </div>
                            ))}
                          </dl>
                        ) : <p className="mt-1 text-sm text-grey">No slot values recorded.</p>}
                      </section>
                      <section>
                        <p className="text-xs font-medium uppercase tracking-wide text-grey">Violations</p>
                        {item.violations.length ? (
                          <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-red-700">
                            {item.violations.map((violation, index) => (
                              <li key={`${item.id}-${index}`}>{violation}</li>
                            ))}
                          </ul>
                        ) : <p className="mt-1 text-sm text-grey">No violations recorded.</p>}
                      </section>
                      <label className="block text-sm font-medium">
                        <span className="mb-1 block">Review note</span>
                        <textarea
                          value={notes[item.id] || ""}
                          onChange={(event) =>
                            setNotes((value) => ({ ...value, [item.id]: event.target.value }))
                          }
                          rows={3}
                          placeholder="Explain what should be repeated or changed."
                          className="w-full rounded-xl border border-black/15 px-3 py-2 font-normal"
                        />
                      </label>
                      {item.reviewed_at && (
                        <p className="text-xs text-grey">
                          Reviewed {new Date(item.reviewed_at).toLocaleString()} by user {item.reviewer_user_id}
                          {item.use_as_guidance ? " · Used as guidance" : ""}
                        </p>
                      )}
                      <div className="flex flex-wrap gap-3">
                        <Button loading={busyId === item.id} onClick={() => review(item, "approved", true)}>
                          Approve as guidance
                        </Button>
                        <Button
                          variant="ghost"
                          disabled={busyId === item.id}
                          onClick={() => review(item, "reviewed", false)}
                        >
                          Reviewed without guidance
                        </Button>
                        <button
                          type="button"
                          disabled={busyId === item.id}
                          onClick={() => review(item, "rejected", false)}
                          className="rounded-xl border border-red-300 px-4 py-2 text-sm font-medium text-red-700 disabled:opacity-50"
                        >
                          Reject
                        </button>
                      </div>
                    </div>
                  </div>
                </article>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function FilterInput({
  label,
  value,
  placeholder,
  onChange,
}: {
  label: string;
  value: string;
  placeholder: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="text-sm font-medium">
      <span className="mb-1 block">{label}</span>
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="w-full rounded-xl border border-black/15 px-3 py-2 font-normal"
      />
    </label>
  );
}
