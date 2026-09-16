import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../../api";
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
}: Props) {
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState<string>("");

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

  const remove = async (id: string | number) => {
    if (!window.confirm("Delete this row?")) return;
    setRemoving(String(id));
    setError("");
    try {
      await api.remove(`${path}/${id}`);
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

  return (
    <div className="mx-auto max-w-7xl px-6 py-10 md:px-10">
      <div className="flex items-center justify-between gap-4">
        <h1 className="font-display text-3xl tracking-tight text-black">{title}</h1>
        <div className="flex gap-2">
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
      <div className="glass-panel mt-5 overflow-x-auto">
        <table className="w-full table-fixed text-left text-sm">
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
              rows.map((row) => (
                <tr key={String(row.id)} className="border-t border-black/5">
                  {visible.map((field) => (
                    <td key={field.key} className="truncate px-4 py-3 text-black">
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
                      onClick={() => remove(row.id as string | number)}
                    >
                      {removing === String(row.id) ? <Spinner /> : <DeleteIcon />}
                    </button>
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
        {!loading && rows.length === 0 && !error && (
          <div className="p-6">
            <EmptyState title={`No ${title.toLowerCase()} yet`} description="Create a row to get started." />
          </div>
        )}
      </div>
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
