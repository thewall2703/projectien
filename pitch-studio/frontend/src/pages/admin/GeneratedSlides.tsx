import { useEffect, useState } from "react";

import { api } from "../../api";
import { Button } from "../../components/ui";
import type { GeneratedSlideRow } from "../../types";

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Something went wrong";
}

export default function GeneratedSlides() {
  const [items, setItems] = useState<GeneratedSlideRow[]>([]);
  const [drafts, setDrafts] = useState<Record<number, Record<string, string | string[]>>>({});
  const [busyId, setBusyId] = useState<number | null>(null);
  const [message, setMessage] = useState("");

  const load = async () => {
    const rows = await api.list<GeneratedSlideRow>("/api/admin/generated-slides");
    setItems(rows);
    setDrafts(
      Object.fromEntries(rows.map((row) => [row.id, { ...row.slot_values }])),
    );
  };

  useEffect(() => {
    load().catch((error) => setMessage(errorMessage(error)));
  }, []);

  const save = async (item: GeneratedSlideRow) => {
    setBusyId(item.id);
    setMessage("");
    try {
      await api.update(`/api/admin/generated-slides/${item.id}`, {
        slot_values: drafts[item.id] || {},
      });
      await load();
      setMessage("Slide re-rendered");
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
      await load();
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

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-semibold">Generated slides</h1>
        <p className="mt-2 text-sm text-grey">
          Shared, content-addressed slides. Editing re-renders the image and protects
          the copy from automatic overwrites.
        </p>
        {message && <p className="mt-2 text-sm">{message}</p>}
      </div>

      {!items.length ? (
        <div className="rounded-2xl border border-black/10 bg-white p-8 text-sm text-grey">
          No generated slides yet.
        </div>
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
                  <span>{item.template_id}</span>
                  <span>·</span>
                  <span>{item.tone}</span>
                  <span>·</span>
                  <span>{item.status}</span>
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
                        {isList && (
                          <span className="ml-2 font-normal text-grey">(one item per line)</span>
                        )}
                      </span>
                      <textarea
                        value={isList ? (value as string[]).join("\n") : (value as string)}
                        rows={isList ? Math.max(3, (value as string[]).length) : key === "title" ? 2 : 3}
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
    </div>
  );
}
