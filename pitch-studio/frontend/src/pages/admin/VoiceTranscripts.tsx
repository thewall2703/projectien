import { useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { Button, EmptyState, ErrorBanner, Skeleton, StatusBadge } from "../../components/ui";
import type { StyleGuide, StyleTranscriptRow } from "../../types";

function errMessage(err: unknown) {
  return err instanceof Error ? err.message : "Something went wrong";
}

export default function VoiceTranscripts() {
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [rows, setRows] = useState<StyleTranscriptRow[]>([]);
  const [guide, setGuide] = useState<StyleGuide>({ version: 0, guide_text: "" });
  const [guideDraft, setGuideDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [loading, setLoading] = useState(true);
  const fileRef = useRef<HTMLInputElement>(null);

  const reload = async () => {
    const [transcripts, current] = await Promise.all([api.styleTranscriptList(), api.styleGuideGet()]);
    setRows(transcripts);
    setGuide(current);
    if (!editing) setGuideDraft(current.guide_text);
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
      setSuccess(
        `Style guide updated to version ${result.guide_version} — ${result.quotes_kept} quotes added`,
      );
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

  const saveGuide = async () => {
    setBusy("guide");
    setError("");
    setSuccess("");
    try {
      const saved = await api.styleGuideSave(guideDraft);
      setGuide(saved);
      setGuideDraft(saved.guide_text);
      setEditing(false);
      setSuccess(`Style guide updated to version ${saved.version}`);
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setBusy("");
    }
  };

  const canSubmit = Boolean(name.trim() && text.trim()) && !busy;

  return (
    <div className="mx-auto max-w-6xl px-6 py-10 md:px-10">
      <h1 className="font-display text-3xl tracking-tight text-black">Style guide</h1>
      <p className="mt-2 max-w-2xl text-sm text-grey-dark">
        Upload or paste an AMA or webinar transcript. Each file enriches the cumulative Pratham style
        and structure guide used in script generation, and harvests founder quotes.
      </p>
      <div className="mt-3">
        <ErrorBanner message={error} />
        {success && <p className="mt-2 text-sm text-black">{success}</p>}
      </div>

      <div className="glass-panel mt-6 space-y-4 p-6">
        <label className="block text-sm text-grey-dark">
          Name
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
            {busy === "upload" ? "Distilling style guide…" : "Distill style guide"}
          </Button>
        </div>
      </div>

      <div className="glass-panel mt-8 overflow-x-auto">
        <div className="flex items-center justify-between px-4 py-3">
          <h2 className="font-display text-xl text-black">Uploaded transcripts</h2>
        </div>
        <table className="min-w-full text-left text-sm">
          <thead className="border-b border-black/10 text-grey">
            <tr>
              <th className="px-4 py-3 font-medium">Name</th>
              <th className="px-4 py-3 font-medium">Status</th>
              <th className="px-4 py-3 font-medium">Length</th>
              <th className="px-4 py-3 font-medium">Uploaded</th>
            </tr>
          </thead>
          <tbody>
            {loading &&
              Array.from({ length: 3 }).map((_, index) => (
                <tr key={`sk-${index}`} className="border-t border-black/5">
                  {Array.from({ length: 4 }).map((__, cell) => (
                    <td key={cell} className="px-4 py-3">
                      <Skeleton className="h-4 w-28" />
                    </td>
                  ))}
                </tr>
              ))}
            {!loading &&
              rows.map((row) => (
                <tr key={row.id} className="border-t border-black/5">
                  <td className="max-w-xs truncate px-4 py-3 text-black">{row.name}</td>
                  <td className="px-4 py-3">
                    <StatusBadge status={row.status} />
                  </td>
                  <td className="px-4 py-3 text-black">{row.text_length.toLocaleString()} chars</td>
                  <td className="px-4 py-3 text-grey">
                    {row.created_at ? new Date(row.created_at).toLocaleString() : "—"}
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
        {!loading && rows.length === 0 && (
          <div className="p-6">
            <EmptyState
              title="No transcripts yet"
              description="Paste or upload a WEBVTT or plain-text transcript to start the guide."
            />
          </div>
        )}
      </div>

      <div className="glass-panel mt-8 space-y-4 p-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="font-display text-xl text-black">
            Current style guide {guide.version > 0 ? `v${guide.version}` : "v0"}
          </h2>
          <div className="flex gap-2">
            {editing ? (
              <>
                <Button variant="accent" loading={busy === "guide"} onClick={saveGuide}>
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
              <Button onClick={() => setEditing(true)}>Edit</Button>
            )}
          </div>
        </div>
        {editing ? (
          <textarea
            className="field min-h-[280px]"
            value={guideDraft}
            onChange={(e) => setGuideDraft(e.target.value)}
          />
        ) : loading ? (
          <div className="space-y-2">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-5/6" />
            <Skeleton className="h-4 w-2/3" />
          </div>
        ) : guide.guide_text ? (
          <pre className="whitespace-pre-wrap text-sm leading-6 text-black">{guide.guide_text}</pre>
        ) : (
          <p className="text-sm text-grey">No style guide yet. Upload a transcript to create version 1.</p>
        )}
      </div>
    </div>
  );
}
