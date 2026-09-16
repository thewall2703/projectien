import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api";
import { Button, EmptyState, ErrorBanner, Skeleton, StatusBadge } from "../../components/ui";
import type { QaCandidate, QaExtractionRun } from "../../types";

function errMessage(err: unknown) {
  return err instanceof Error ? err.message : "Something went wrong";
}

function evidenceExcerpt(raw: string): string {
  try {
    const parsed = JSON.parse(raw || "{}") as { chunk_excerpt?: string };
    return parsed.chunk_excerpt || "";
  } catch {
    return "";
  }
}

export default function QaReview() {
  const [candidates, setCandidates] = useState<QaCandidate[]>([]);
  const [runs, setRuns] = useState<QaExtractionRun[]>([]);
  const [filter, setFilter] = useState<"pending" | "approved" | "rejected" | "all">("pending");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [question, setQuestion] = useState("");
  const [whoAsks, setWhoAsks] = useState("");
  const [move, setMove] = useState("");
  const [answer, setAnswer] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [loading, setLoading] = useState(true);

  const selected = useMemo(
    () => candidates.find((row) => row.id === selectedId) || null,
    [candidates, selectedId],
  );

  const reload = async () => {
    const [candidateRows, runRows] = await Promise.all([
      api.listQaCandidates(filter === "all" ? undefined : { status: filter }),
      api.listQaExtractions(),
    ]);
    setCandidates(candidateRows);
    setRuns(runRows);
    if (candidateRows.length && !candidateRows.some((row) => row.id === selectedId)) {
      setSelectedId(candidateRows[0].id);
    }
    if (!candidateRows.length) setSelectedId(null);
  };

  useEffect(() => {
    setLoading(true);
    reload()
      .catch((err) => setError(errMessage(err)))
      .finally(() => setLoading(false));
  }, [filter]);

  useEffect(() => {
    if (!selected) {
      setQuestion("");
      setWhoAsks("");
      setMove("");
      setAnswer("");
      setNote("");
      return;
    }
    setQuestion(selected.proposed_question);
    setWhoAsks(selected.proposed_who_asks);
    setMove(selected.proposed_move);
    setAnswer(selected.proposed_answer);
    setNote(selected.review_note || "");
  }, [selected?.id]);

  const approve = async () => {
    if (!selected) return;
    setBusy("approve");
    setError("");
    setSuccess("");
    try {
      await api.approveQaCandidate(selected.id, {
        question,
        who_asks: whoAsks,
        move,
        answer,
        review_note: note,
      });
      setSuccess(
        selected.match_type === "enrich"
          ? "Enriched Q&A approved and applied to the library."
          : "New Q&A approved and added to the library.",
      );
      await reload();
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setBusy("");
    }
  };

  const reject = async () => {
    if (!selected) return;
    setBusy("reject");
    setError("");
    setSuccess("");
    try {
      await api.rejectQaCandidate(selected.id, note);
      setSuccess("Candidate rejected.");
      await reload();
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="mx-auto max-w-6xl px-6 py-10 md:px-10">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="font-display text-3xl tracking-tight text-black">AMA Q&A review</h1>
          <p className="mt-2 max-w-2xl text-sm text-grey-dark">
            Review extracted questions from AMA transcripts. Approving a new item adds it to Objections;
            approving an enrichment overwrites the matched Q&A after your edits.
          </p>
        </div>
        <Link className="text-sm font-medium text-black underline-offset-4 hover:underline" to="/admin/voice-transcripts">
          Back to style transcripts
        </Link>
      </div>

      <div className="mt-4">
        <ErrorBanner message={error} />
        {success && <p className="mt-2 text-sm text-black">{success}</p>}
      </div>

      <div className="mt-6 flex flex-wrap gap-2">
        {(["pending", "approved", "rejected", "all"] as const).map((value) => (
          <button
            key={value}
            type="button"
            className={`rounded-full border px-3 py-1.5 text-sm capitalize ${
              filter === value ? "border-black bg-black text-white" : "border-black/10 text-black"
            }`}
            onClick={() => setFilter(value)}
          >
            {value}
          </button>
        ))}
      </div>

      {runs.length > 0 && (
        <div className="glass-panel mt-6 overflow-x-auto">
          <div className="px-4 py-3">
            <h2 className="font-display text-xl text-black">Recent extraction runs</h2>
          </div>
          <table className="min-w-full text-left text-sm">
            <thead className="border-b border-black/10 text-grey">
              <tr>
                <th className="px-4 py-3 font-medium">Transcript</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 font-medium">Candidates</th>
                <th className="px-4 py-3 font-medium">Stage</th>
              </tr>
            </thead>
            <tbody>
              {runs.slice(0, 8).map((run) => (
                <tr key={run.id} className="border-t border-black/5">
                  <td className="px-4 py-3 text-black">{run.transcript_name || `#${run.style_transcript_id}`}</td>
                  <td className="px-4 py-3">
                    <StatusBadge status={run.status} />
                  </td>
                  <td className="px-4 py-3 text-black">{run.candidate_count}</td>
                  <td className="px-4 py-3 text-grey">{run.error || run.stage || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="mt-8 grid gap-6 lg:grid-cols-[320px_minmax(0,1fr)]">
        <div className="glass-panel max-h-[70vh] overflow-y-auto">
          {loading && (
            <div className="space-y-3 p-4">
              {Array.from({ length: 4 }).map((_, index) => (
                <Skeleton key={index} className="h-16 w-full rounded-xl" />
              ))}
            </div>
          )}
          {!loading && candidates.length === 0 && (
            <div className="p-6">
              <EmptyState
                title="No candidates"
                description="Extract Q&As from a style transcript to populate this queue."
              />
            </div>
          )}
          {!loading &&
            candidates.map((row) => (
              <button
                key={row.id}
                type="button"
                className={`block w-full border-b border-black/5 px-4 py-3 text-left ${
                  selectedId === row.id ? "bg-black/5" : "hover:bg-black/[0.03]"
                }`}
                onClick={() => setSelectedId(row.id)}
              >
                <div className="flex items-center justify-between gap-2">
                  <StatusBadge status={row.status} />
                  <span className="text-xs text-grey">{Math.round(row.confidence * 100)}%</span>
                </div>
                <p className="mt-2 line-clamp-2 text-sm font-medium text-black">{row.proposed_question}</p>
                <p className="mt-1 text-xs text-grey">
                  {row.match_type === "enrich" ? "Enrich existing" : "New"} · {row.transcript_name || "AMA"}
                </p>
              </button>
            ))}
        </div>

        <div className="glass-panel space-y-5 p-6">
          {!selected && !loading && (
            <EmptyState title="Select a candidate" description="Pick a Q&A on the left to review evidence and approve." />
          )}
          {selected && (
            <>
              <div className="flex flex-wrap items-center gap-3">
                <StatusBadge status={selected.status} />
                <span className="rounded-full border border-black/10 px-2.5 py-1 text-xs capitalize text-grey-dark">
                  {selected.match_type}
                </span>
                <span className="text-xs text-grey">{selected.transcript_name}</span>
              </div>

              {selected.match_type === "enrich" && (
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="rounded-2xl border border-black/8 bg-white/50 p-4">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-grey">Current</p>
                    <p className="mt-2 text-sm font-medium text-black">{selected.prior_question || "—"}</p>
                    <p className="mt-3 whitespace-pre-wrap text-sm leading-6 text-grey-dark">
                      {selected.prior_answer || "—"}
                    </p>
                  </div>
                  <div className="rounded-2xl border border-black/8 bg-white/50 p-4">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-grey">Proposed</p>
                    <p className="mt-2 text-sm font-medium text-black">{selected.proposed_question}</p>
                    <p className="mt-3 whitespace-pre-wrap text-sm leading-6 text-grey-dark">
                      {selected.proposed_answer}
                    </p>
                  </div>
                </div>
              )}

              <label className="block text-sm text-grey-dark">
                Question
                <textarea
                  className="field mt-1 min-h-[72px]"
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  disabled={selected.status !== "pending"}
                />
              </label>
              <label className="block text-sm text-grey-dark">
                Who asks
                <input
                  className="field mt-1"
                  value={whoAsks}
                  onChange={(e) => setWhoAsks(e.target.value)}
                  disabled={selected.status !== "pending"}
                />
              </label>
              <label className="block text-sm text-grey-dark">
                Move
                <input
                  className="field mt-1"
                  value={move}
                  onChange={(e) => setMove(e.target.value)}
                  disabled={selected.status !== "pending"}
                />
              </label>
              <label className="block text-sm text-grey-dark">
                Answer
                <textarea
                  className="field mt-1 min-h-[160px]"
                  value={answer}
                  onChange={(e) => setAnswer(e.target.value)}
                  disabled={selected.status !== "pending"}
                />
              </label>
              <label className="block text-sm text-grey-dark">
                Review note
                <input
                  className="field mt-1"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  disabled={selected.status !== "pending"}
                />
              </label>

              <div className="rounded-2xl border border-black/8 bg-white/40 p-4">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-grey">Transcript evidence</p>
                <p className="mt-2 text-sm text-grey-dark">
                  <span className="font-medium text-black">Q:</span> {selected.question_verbatim || "—"}
                </p>
                <p className="mt-2 text-sm text-grey-dark">
                  <span className="font-medium text-black">A:</span> {selected.answer_verbatim || "—"}
                </p>
                {evidenceExcerpt(selected.evidence_json) && (
                  <p className="mt-3 whitespace-pre-wrap text-xs leading-5 text-grey">
                    {evidenceExcerpt(selected.evidence_json)}
                  </p>
                )}
              </div>

              {selected.status === "pending" && (
                <div className="flex flex-wrap gap-3">
                  <Button variant="accent" loading={busy === "approve"} disabled={Boolean(busy)} onClick={approve}>
                    Approve
                  </Button>
                  <Button loading={busy === "reject"} disabled={Boolean(busy)} onClick={reject}>
                    Reject
                  </Button>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
