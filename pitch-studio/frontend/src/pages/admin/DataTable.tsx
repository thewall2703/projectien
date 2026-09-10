import { useEffect, useState, type ReactNode } from "react";
import { api } from "../../api";
import { Button, EmptyState, ErrorBanner, Skeleton } from "../../components/ui";
import type { FieldConfig } from "../../types";

type Props = {
  title: string;
  path: string;
  fields: FieldConfig[];
  idKey?: string;
  extra?: ReactNode;
};

function emptyRow(fields: FieldConfig[]): Record<string, unknown> {
  const row: Record<string, unknown> = {};
  for (const field of fields) {
    row[field.key] = field.type === "number" ? 0 : field.type === "checkbox" ? false : "";
  }
  return row;
}

export default function DataTable({ title, path, fields, extra }: Props) {
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

  const visible = fields.filter((field) => field.key !== "id").slice(0, 4);

  return (
    <div>
      <div className="flex items-center justify-between gap-4">
        <h1 className="font-serif text-3xl">{title}</h1>
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
      <div className="mt-5 overflow-x-auto rounded-xl bg-white">
        <table className="min-w-full text-left text-sm">
          <thead className="border-b border-ink/10 text-ink/50">
            <tr>
              {visible.map((field) => (
                <th key={field.key} className="px-4 py-3">
                  {field.label}
                </th>
              ))}
              <th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody>
            {loading &&
              Array.from({ length: 5 }).map((_, index) => (
                <tr key={`sk-${index}`} className="border-t border-ink/5">
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
                <tr key={String(row.id)} className="border-t border-ink/5">
                  {visible.map((field) => (
                    <td key={field.key} className="max-w-xs truncate px-4 py-3">
                      {String(row[field.key] ?? "")}
                    </td>
                  ))}
                  <td className="px-4 py-3 text-right">
                    <button className="mr-3 text-accent" type="button" onClick={() => { setCreating(false); setDraft(row); }}>
                      Edit
                    </button>
                    <Button
                      variant="ghost"
                      className="px-0 py-0 text-danger"
                      loading={removing === String(row.id)}
                      onClick={() => remove(row.id as string | number)}
                    >
                      Delete
                    </Button>
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
        <div className="fixed inset-0 z-10 flex justify-end bg-ink/30">
          <div className="h-full w-full max-w-lg overflow-y-auto bg-white p-6">
            <h2 className="font-serif text-2xl">{creating ? "Create" : "Edit"}</h2>
            <div className="mt-4 space-y-3">
              {fields.map((field) => (
                <label key={field.key} className="block text-sm">
                  {field.label}
                  {field.type === "textarea" ? (
                    <textarea
                      className="mt-1 w-full rounded border border-ink/15 px-3 py-2"
                      rows={5}
                      value={String(draft[field.key] ?? "")}
                      onChange={(e) => setDraft({ ...draft, [field.key]: e.target.value })}
                    />
                  ) : field.type === "select" ? (
                    <select
                      className="mt-1 w-full rounded border border-ink/15 px-3 py-2"
                      value={String(draft[field.key] ?? "")}
                      onChange={(e) => setDraft({ ...draft, [field.key]: e.target.value })}
                    >
                      {(field.options || []).map((option) => (
                        <option key={option} value={option}>
                          {option}
                        </option>
                      ))}
                    </select>
                  ) : field.type === "checkbox" ? (
                    <input
                      className="ml-2"
                      type="checkbox"
                      checked={Boolean(draft[field.key])}
                      onChange={(e) => setDraft({ ...draft, [field.key]: e.target.checked })}
                    />
                  ) : (
                    <input
                      className="mt-1 w-full rounded border border-ink/15 px-3 py-2"
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
            <div className="mt-6 flex gap-3">
              <Button variant="accent" loading={saving} onClick={save}>
                Save
              </Button>
              <Button disabled={saving} onClick={() => setDraft(null)}>
                Cancel
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
