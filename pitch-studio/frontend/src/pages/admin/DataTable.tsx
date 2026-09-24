import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../../api";
import Modal from "../../components/Modal";
import { Button, EmptyState, ErrorBanner, formatStatusLabel, Skeleton, Spinner } from "../../components/ui";
import type { FieldConfig } from "../../types";

type Props = {
  title: string;
  path: string;
  fields: FieldConfig[];
  idKey?: string;
  extra?: ReactNode;
  createLabel?: string;
  editLabel?: string;
  searchable?: boolean;
  searchPlaceholder?: string;
  entityLabel?: string;
};

function emptyRow(fields: FieldConfig[]): Record<string, unknown> {
  const row: Record<string, unknown> = {};
  for (const field of fields) {
    row[field.key] = field.type === "number" ? 0 : field.type === "checkbox" ? false : "";
  }
  return row;
}

function isEmptyCell(value: unknown): boolean {
  if (value == null) return true;
  if (typeof value === "string") return value.trim() === "";
  return false;
}

function formatCell(field: FieldConfig, value: unknown): string {
  if (isEmptyCell(value)) return "—";
  const text = String(value);
  if (field.optionLabels?.[text]) return field.optionLabels[text];
  if (field.key === "status" || field.key === "file_status" || field.key === "extract_status") {
    return formatStatusLabel(text);
  }
  return text;
}

function EditIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path
        d="M11.3 2.3a1 1 0 0 1 1.4 0l1 1a1 1 0 0 1 0 1.4l-8.2 8.2L3 13.5l.6-2.5 8.2-8.2Z"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function DeleteIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path
        d="M2.5 4h11M6 4V2.75h4V4M5 4.5l.6 8.25a1 1 0 0 0 1 .9h2.8a1 1 0 0 0 1-.9L11 4.5M6.75 7v4M9.25 7v4"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export default function DataTable({
  title,
  path,
  fields,
  extra,
  createLabel = "Create",
  editLabel = "Edit",
  searchable = false,
  searchPlaceholder = "Search…",
  entityLabel,
}: Props) {
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState<string>("");
  const [query, setQuery] = useState("");
  const [pendingDelete, setPendingDelete] = useState<Record<string, unknown> | null>(null);

  const noun = entityLabel || title.replace(/s$/i, "").toLowerCase() || "item";

  const reload = () =>
    api
      .list<Record<string, unknown>>(path)
      .then(setRows)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load"))
      .finally(() => setLoading(false));

  useEffect(() => {
    setLoading(true);
    reload();
  }, [path]);

  useEffect(() => {
    if (!draft) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [draft]);

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    setError("");
    try {
      if (creating) {
        await api.create(path, draft);
      } else {
        await api.update(`${path}/${draft.id}`, draft);
      }
      setDraft(null);
      setCreating(false);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  const confirmRemove = async () => {
    if (!pendingDelete?.id) return;
    const id = pendingDelete.id as string | number;
    setRemoving(String(id));
    setError("");
    try {
      await api.remove(`${path}/${id}`);
      setPendingDelete(null);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    } finally {
      setRemoving("");
    }
  };

  const visible = useMemo(() => {
    const candidates = fields.filter((field) => field.key !== "id");
    const withData = candidates.filter((field) => rows.some((row) => !isEmptyCell(row[field.key])));
    const pool = withData.length > 0 ? withData : candidates;
    return pool.slice(0, 4);
  }, [fields, rows]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((row) =>
      fields.some((field) => {
        const value = row[field.key];
        if (isEmptyCell(value)) return false;
        return formatCell(field, value).toLowerCase().includes(needle);
      }),
    );
  }, [rows, fields, query]);

  return (
    <div className="mx-auto w-full max-w-[90rem] px-6 py-10 md:px-10">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <h1 className="font-display text-3xl tracking-tight text-black">{title}</h1>
        <div className="flex flex-wrap items-center gap-2">
          {searchable && (
            <input
              className="field !w-64 !py-2"
              type="search"
              value={query}
              placeholder={searchPlaceholder}
              onChange={(event) => setQuery(event.target.value)}
              aria-label={searchPlaceholder}
            />
          )}
          {extra}
          <Button
            variant="accent"
            className="px-3 py-2"
            onClick={() => {
              setCreating(true);
              setDraft(emptyRow(fields));
            }}
          >
            New
          </Button>
        </div>
      </div>
      <div className="mt-3">
        <ErrorBanner message={error} />
      </div>
      <div className="admin-table-panel mt-5">
        <table className="w-full min-w-full table-auto text-left text-sm">
          <thead className="border-b border-black/15 bg-black/[0.04]">
            <tr>
              {visible.map((field) => (
                <th key={field.key} className="px-4 py-3 font-semibold text-black">
                  {field.label}
                </th>
              ))}
              <th className="w-28 px-4 py-3 text-right font-semibold text-black">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading &&
              Array.from({ length: 5 }).map((_, index) => (
                <tr key={`sk-${index}`} className="border-t border-black/5">
                  {visible.map((field) => (
                    <td key={field.key} className="px-4 py-3">
                      <Skeleton className="h-4 w-28" />
                    </td>
                  ))}
                  <td className="px-4 py-3">
                    <Skeleton className="ml-auto h-4 w-16" />
                  </td>
                </tr>
              ))}
            {!loading &&
              filtered.map((row) => (
                <tr key={String(row.id)} className="border-t border-black/5">
                  {visible.map((field) => (
                    <td key={field.key} className="max-w-[18rem] truncate px-4 py-3 text-black">
                      {formatCell(field, row[field.key])}
                    </td>
                  ))}
                  <td className="px-4 py-3 text-right">
                    <button
                      className="mr-1 inline-flex h-8 w-8 items-center justify-center rounded-lg text-grey-dark hover:bg-black/5 hover:text-black"
                      type="button"
                      aria-label="Edit"
                      onClick={() => {
                        setCreating(false);
                        setDraft(row);
                      }}
                    >
                      <EditIcon />
                    </button>
                    <button
                      className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-danger hover:bg-danger/10 disabled:opacity-50"
                      type="button"
                      aria-label="Delete"
                      disabled={removing === String(row.id)}
                      onClick={() => setPendingDelete(row)}
                    >
                      {removing === String(row.id) ? <Spinner /> : <DeleteIcon />}
                    </button>
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
        {!loading && filtered.length === 0 && !error && (
          <div className="p-6">
            <EmptyState
              title={query.trim() ? `No ${title.toLowerCase()} match` : `No ${title.toLowerCase()} yet`}
              description={query.trim() ? "Try a different search." : "Create a row to get started."}
            />
          </div>
        )}
      </div>

      <Modal
        open={Boolean(pendingDelete)}
        title={`Delete ${noun}?`}
        onClose={() => {
          if (!removing) setPendingDelete(null);
        }}
        footer={
          <div className="flex justify-end gap-3">
            <Button disabled={Boolean(removing)} onClick={() => setPendingDelete(null)}>
              Cancel
            </Button>
            <Button
              className="!bg-danger !text-white hover:!bg-danger/90"
              variant="accent"
              loading={Boolean(removing)}
              onClick={confirmRemove}
            >
              Delete
            </Button>
          </div>
        }
      >
        <p className="text-sm leading-relaxed text-grey-dark">
          Are you sure you want to delete
          {pendingDelete?.title || pendingDelete?.fact
            ? ` “${String(pendingDelete.title || pendingDelete.fact)}”`
            : ` this ${noun}`}
          ? This action cannot be undone.
        </p>
      </Modal>

      {draft && (
        <div
          className="fixed inset-0 z-50 flex justify-end bg-black/40 backdrop-blur-sm"
          onClick={() => !saving && setDraft(null)}
        >
          <div
            className="flex h-full w-full max-w-lg flex-col border-l border-black/10 bg-[#faf8f3] shadow-[-24px_0_60px_-28px_rgba(43,40,35,0.45)]"
            role="dialog"
            aria-modal="true"
            aria-label={creating ? createLabel : editLabel}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-4 border-b border-black/10 px-6 py-5">
              <div>
                <p className="kicker">{title}</p>
                <h2 className="mt-1 font-display text-2xl tracking-tight text-black">
                  {creating ? createLabel : editLabel}
                </h2>
              </div>
              <button
                type="button"
                className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-grey-dark hover:bg-black/5 hover:text-black"
                aria-label="Close"
                disabled={saving}
                onClick={() => setDraft(null)}
              >
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
                  <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                </svg>
              </button>
            </div>

            <div className="flex-1 space-y-5 overflow-y-auto px-6 py-5">
              {fields.map((field) => (
                <label key={field.key} className="block">
                  <span className="text-xs font-semibold uppercase tracking-[0.14em] text-grey">{field.label}</span>
                  {field.type === "textarea" ? (
                    <textarea
                      className="field mt-2 resize-none !border-black/10 !bg-white"
                      rows={4}
                      value={String(draft[field.key] ?? "")}
                      onChange={(e) => setDraft({ ...draft, [field.key]: e.target.value })}
                    />
                  ) : field.type === "select" ? (
                    <select
                      className="field mt-2 !border-black/10 !bg-white"
                      value={String(draft[field.key] ?? "")}
                      onChange={(e) => setDraft({ ...draft, [field.key]: e.target.value })}
                    >
                      {(field.options || []).map((option) => (
                        <option key={option} value={option}>
                          {field.optionLabels?.[option] || formatStatusLabel(option)}
                        </option>
                      ))}
                    </select>
                  ) : field.type === "checkbox" ? (
                    <input
                      className="ml-2 mt-2"
                      type="checkbox"
                      checked={Boolean(draft[field.key])}
                      onChange={(e) => setDraft({ ...draft, [field.key]: e.target.checked })}
                    />
                  ) : (
                    <input
                      className="field mt-2 !border-black/10 !bg-white"
                      type={field.type === "number" ? "number" : "text"}
                      value={String(draft[field.key] ?? "")}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          [field.key]: field.type === "number" ? Number(e.target.value) : e.target.value,
                        })
                      }
                    />
                  )}
                </label>
              ))}
            </div>

            <div className="flex items-center justify-end gap-3 border-t border-black/10 bg-[#f5f2eb] px-6 py-4">
              <Button disabled={saving} onClick={() => setDraft(null)}>
                Cancel
              </Button>
              <Button variant="accent" loading={saving} onClick={save}>
                Save
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
