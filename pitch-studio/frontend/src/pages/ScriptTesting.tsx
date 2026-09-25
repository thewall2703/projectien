import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import GenerateWizard, { type GenerateWizardPayload } from "../components/generate/GenerateWizard";
import ScriptReviewReader from "../components/script/ScriptReviewReader";
import { Button, EmptyState, ErrorBanner, Skeleton, Spinner, StatusBadge } from "../components/ui";
import { generationAxisLabels, personaLabel } from "../labels";
import type { RecipeOption, ScriptTestRun, ScriptTestRunListItem, User } from "../types";

function statusMessage(status: string): string {
  switch (status) {
    case "queued":
      return "Queued — waiting to start…";
    case "planning":
      return "Planning the story…";
    case "generating_script":
      return "Writing the script…";
    case "validating":
      return "Validating facts and structure…";
    case "reviewing_flow":
      return "Checking flow with a test listener…";
    case "failed":
      return "Script test failed";
    case "done":
      return "Ready for review";
    default:
      return status.replaceAll("_", " ");
  }
}

function ScriptTestingList({
  onNew,
}: {
  onNew: () => void;
}) {
  const [rows, setRows] = useState<ScriptTestRunListItem[]>([]);
  const [recipes, setRecipes] = useState<RecipeOption[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.listScriptTests(), api.recipes()])
      .then(([tests, recipeData]) => {
        if (cancelled) return;
        setRows(tests);
        setRecipes(recipeData as RecipeOption[]);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="mx-auto max-w-4xl px-6 py-10 md:px-10">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="kicker">Library</p>
          <h1 className="mt-2 font-display text-4xl tracking-tight text-black">Script testing</h1>
          <p className="mt-3 max-w-2xl text-sm leading-relaxed text-grey">
            Generate a script with the same inputs as a normal pitch, then review sentences and paragraphs
            with your team — no deck or media.
          </p>
        </div>
        <Button variant="accent" className="min-h-11 w-full sm:w-auto" onClick={onNew}>
          New script test
        </Button>
      </div>
      <div className="mt-3">
        <ErrorBanner message={error} />
      </div>
      <div className="mt-8 space-y-3">
        {loading &&
          Array.from({ length: 4 }).map((_, index) => (
            <div key={index} className="glass-panel p-5">
              <div className="flex items-center justify-between gap-3">
                <div className="space-y-2">
                  <Skeleton className="h-5 w-48" />
                  <Skeleton className="h-4 w-64" />
                </div>
                <Skeleton className="h-6 w-20 rounded-full" />
              </div>
            </div>
          ))}
        {!loading &&
          rows.map((row) => {
            const labels = generationAxisLabels(row);
            const title =
              personaLabel(recipes, row.recipe_ref, row) || labels.audience || `Script test ${row.id}`;
            return (
              <Link
                key={row.id}
                to={`/script-testing/${row.id}`}
                className="glass-panel block min-h-11 p-5 transition hover:bg-white/70"
              >
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p className="font-medium text-black">{title}</p>
                    <p className="mt-1 text-sm text-grey">
                      {[labels.audience, labels.duration, labels.channel, labels.intent]
                        .filter(Boolean)
                        .join(" · ")}
                    </p>
                    <p className="mt-1 text-xs text-grey">
                      {row.created_by_email || `User ${row.created_by_user_id}`}
                      {row.rating_count > 0 && row.average_rating != null
                        ? ` · ${row.average_rating.toFixed(1)}/10 (${row.rating_count})`
                        : ""}
                    </p>
                  </div>
                  <div className="flex items-center gap-3">
                    <StatusBadge status={row.status} />
                    <span className="text-xs text-grey">{new Date(row.created_at).toLocaleString()}</span>
                  </div>
                </div>
              </Link>
            );
          })}
        {!loading && rows.length === 0 && !error && (
          <EmptyState
            title="No script tests yet"
            description="Generate a script to start collecting sentence and paragraph feedback."
            action={
              <Button variant="accent" onClick={onNew}>
                New script test
              </Button>
            }
          />
        )}
      </div>
    </div>
  );
}

function RatingPanel({
  run,
  currentUserId,
  onSaved,
}: {
  run: ScriptTestRun;
  currentUserId: number;
  onSaved: (next: ScriptTestRun) => void;
}) {
  const existing = run.ratings.find((row) => row.reviewer_user_id === currentUserId);
  const [value, setValue] = useState(existing ? String(existing.rating) : "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setValue(existing ? String(existing.rating) : "");
  }, [existing?.rating, existing?.id]);

  const parsed = Number(value);
  const valid =
    value.trim() !== "" &&
    Number.isFinite(parsed) &&
    parsed >= 0 &&
    parsed <= 10 &&
    Math.round(parsed * 10) === parsed * 10;
  const unchanged = existing != null && Math.abs(existing.rating - parsed) < 0.05;

  const save = async () => {
    if (!valid || unchanged) return;
    setSaving(true);
    setError("");
    try {
      const result = await api.saveScriptTestRating(run.id, parsed);
      const ratings = run.ratings.filter((row) => row.reviewer_user_id !== currentUserId).concat([
        {
          id: result.id,
          script_test_run_id: result.script_test_run_id,
          reviewer_user_id: result.reviewer_user_id,
          reviewer_email: result.reviewer_email,
          rating: result.rating,
          created_at: result.created_at,
          updated_at: result.updated_at,
        },
      ]);
      onSaved({
        ...run,
        ratings,
        average_rating: result.average_rating ?? null,
        rating_count: result.rating_count,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save rating");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="glass-panel space-y-4 p-5 md:p-6">
      <div>
        <p className="kicker">Rate this script</p>
        <h2 className="mt-2 font-display text-2xl tracking-tight text-black">Your score</h2>
      </div>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <label className="block flex-1">
          <span className="text-[11px] font-semibold uppercase tracking-[0.18em] text-grey">
            Your rating
          </span>
          <div className="mt-2 flex items-center gap-2">
            <input
              type="number"
              min={0}
              max={10}
              step={0.1}
              inputMode="decimal"
              className="field w-full max-w-[10rem]"
              value={value}
              onChange={(event) => setValue(event.target.value)}
              placeholder="0.0–10.0"
            />
            <span className="text-sm text-grey">/ 10</span>
          </div>
        </label>
        <Button
          variant="accent"
          className="min-h-11 w-full sm:w-auto"
          loading={saving}
          disabled={!valid || unchanged || saving}
          onClick={save}
        >
          {existing ? "Update rating" : "Save rating"}
        </Button>
      </div>
      <p className="text-sm text-grey">
        {run.rating_count > 0 && run.average_rating != null
          ? `Average ${run.average_rating.toFixed(1)} from ${run.rating_count} reviewer${
              run.rating_count === 1 ? "" : "s"
            }`
          : "No ratings yet"}
      </p>
      <ErrorBanner message={error} />
    </div>
  );
}

function formatElapsed(seconds: number): string {
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  if (mins <= 0) return `${secs}s`;
  return `${mins}m ${secs.toString().padStart(2, "0")}s`;
}

function ScriptTestingDetail({ currentUser }: { currentUser: User }) {
  const { id } = useParams();
  const navigate = useNavigate();
  const runId = Number(id);
  const [run, setRun] = useState<ScriptTestRun | null>(null);
  const [recipes, setRecipes] = useState<RecipeOption[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [elapsedSec, setElapsedSec] = useState(0);

  useEffect(() => {
    api.recipes().then((data) => setRecipes(data as RecipeOption[])).catch(() => setRecipes([]));
  }, []);

  useEffect(() => {
    if (!Number.isFinite(runId)) {
      setError("Invalid script test");
      setLoading(false);
      return;
    }
    let cancelled = false;
    let timer: number | undefined;

    const tick = async () => {
      try {
        const detail = await api.getScriptTest(runId);
        if (cancelled) return detail;
        setRun(detail);
        setError("");
        setLoading(false);
        return detail;
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load");
          setLoading(false);
        }
        return null;
      }
    };

    const poll = async () => {
      const detail = await tick();
      if (cancelled) return;
      // Keep polling through transient errors while the run is incomplete.
      const incomplete = !detail || (detail.status !== "done" && detail.status !== "failed");
      if (incomplete) {
        timer = window.setTimeout(poll, 2000);
      }
    };

    poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [runId]);

  useEffect(() => {
    if (!run || run.status === "done" || run.status === "failed") return;
    const started = new Date(run.created_at).getTime();
    const update = () => setElapsedSec(Math.max(0, Math.floor((Date.now() - started) / 1000)));
    update();
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, [run?.id, run?.status, run?.created_at]);

  const generating = run != null && run.status !== "done" && run.status !== "failed";
  const axisNames = useMemo(() => (run ? generationAxisLabels(run) : null), [run]);
  const personaName = run
    ? personaLabel(recipes, run.recipe_ref, run) || axisNames?.audience || "Resolving persona…"
    : "";

  if (loading && !run) {
    return (
      <div className="mx-auto max-w-4xl space-y-4 px-6 py-10 md:px-10">
        <Skeleton className="h-8 w-40" />
        <Skeleton className="h-12 w-72" />
        <Skeleton className="h-64 w-full rounded-3xl" />
      </div>
    );
  }

  if (!run) {
    return (
      <div className="mx-auto max-w-4xl px-6 py-10 md:px-10">
        <ErrorBanner message={error || "Script test not found"} />
        <div className="mt-6">
          <Link to="/script-testing" className="btn">
            Back to script testing
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl space-y-8 px-6 py-10 md:px-10">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 space-y-2">
          <p className="kicker">Script test {run.id}</p>
          <h1 className="font-display text-4xl tracking-tight text-black md:text-5xl">{personaName}</h1>
          <p className="text-sm text-grey">
            {[axisNames?.audience, axisNames?.duration, axisNames?.channel, axisNames?.intent, axisNames?.temperature]
              .filter(Boolean)
              .join(" · ")}
          </p>
          <p className="text-xs text-grey">
            Created by {run.created_by_email || `User ${run.created_by_user_id}`} ·{" "}
            {new Date(run.created_at).toLocaleString()}
          </p>
        </div>
        <div className="flex w-full flex-col gap-3 sm:w-auto sm:flex-row sm:items-center">
          <StatusBadge status={run.status} />
          <Button variant="accent" className="min-h-11 w-full sm:w-auto" onClick={() => navigate("/script-testing?new=1")}>
            New test
          </Button>
        </div>
      </div>

      <ErrorBanner message={error} />

      {generating && (
        <div className="glass-panel space-y-4 p-6">
          <p className="flex items-center gap-2 text-sm text-grey-dark">
            <Spinner /> {statusMessage(run.status)}
          </p>
          <p className="text-sm text-grey">
            Elapsed {formatElapsed(elapsedSec)}. Script generation usually takes a few minutes — this page
            updates automatically when it finishes.
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            {Array.from({ length: 4 }).map((_, index) => (
              <Skeleton key={index} className="h-24 w-full rounded-2xl" />
            ))}
          </div>
        </div>
      )}

      {run.status === "failed" && (
        <div className="space-y-4">
          <div className="rounded-2xl border border-danger/20 bg-danger/5 p-4 text-sm text-danger">
            <p className="font-medium">{run.error || "Script test failed"}</p>
            {run.validation_report && (
              <pre className="mt-2 whitespace-pre-wrap">{run.validation_report}</pre>
            )}
          </div>
          <Button variant="accent" onClick={() => navigate("/script-testing?new=1")}>
            Start a new test
          </Button>
        </div>
      )}

      {run.status === "done" && run.review_document && (
        <>
          <ScriptReviewReader
            document={run.review_document}
            feedback={run.feedback}
            currentUserId={currentUser.id}
            onSaveFeedback={async (payload) => {
              const item = await api.addScriptTestFeedback(run.id, payload);
              setRun((current) =>
                current
                  ? {
                      ...current,
                      feedback: [...current.feedback, item],
                    }
                  : current,
              );
            }}
            onUpdateFeedback={async (feedbackId, comment) => {
              const item = await api.updateScriptTestFeedback(run.id, feedbackId, comment);
              setRun((current) =>
                current
                  ? {
                      ...current,
                      feedback: current.feedback.map((row) => (row.id === item.id ? item : row)),
                    }
                  : current,
              );
            }}
          />
          <RatingPanel
            run={run}
            currentUserId={currentUser.id}
            onSaved={setRun}
          />
        </>
      )}

      {run.status === "done" && !run.review_document && (
        <div className="glass-panel space-y-3 p-6">
          <p className="text-sm text-grey-dark">
            Script finished, but the review document is missing. Refresh the page or start a new test.
          </p>
          <Button onClick={() => window.location.reload()}>Refresh</Button>
        </div>
      )}
    </div>
  );
}

export default function ScriptTesting({ currentUser }: { currentUser: User }) {
  const navigate = useNavigate();
  const { id } = useParams();
  const [composing, setComposing] = useState(() => {
    if (typeof window === "undefined") return false;
    return new URLSearchParams(window.location.search).get("new") === "1";
  });

  useEffect(() => {
    if (typeof window === "undefined") return;
    if (new URLSearchParams(window.location.search).get("new") === "1") {
      setComposing(true);
    }
  }, [id]);

  if (id) {
    return <ScriptTestingDetail currentUser={currentUser} />;
  }

  if (composing) {
    return (
      <GenerateWizard
        kicker="Script testing"
        submitLabel="Generate script"
        onSubmit={async (payload: GenerateWizardPayload) => {
          const run = await api.createScriptTest(payload);
          navigate(`/script-testing/${run.id}`);
        }}
      />
    );
  }

  return <ScriptTestingList onNew={() => setComposing(true)} />;
}
