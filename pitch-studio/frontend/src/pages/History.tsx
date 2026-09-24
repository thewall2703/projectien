import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { EmptyState, ErrorBanner, Skeleton, StatusBadge } from "../components/ui";
import { generationAxisLabels, personaLabel } from "../labels";
import type { Generation, RecipeOption, User } from "../types";

export default function History({ currentUser }: { currentUser: User }) {
  const [rows, setRows] = useState<Generation[]>([]);
  const [recipes, setRecipes] = useState<RecipeOption[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .listGenerations()
      .then((data) => setRows(data as Generation[]))
      .catch((err) => setError(err instanceof Error ? err.message : "Failed"))
      .finally(() => setLoading(false));
    api.recipes().then((data) => setRecipes(data as RecipeOption[])).catch(() => setRecipes([]));
  }, []);

  return (
    <div className="mx-auto max-w-4xl px-6 py-10 md:px-10">
      <p className="kicker">Library</p>
      <h1 className="mt-2 font-display text-4xl tracking-tight text-black">History</h1>
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
            return (
              <Link
                key={row.id}
                to={`/result/${row.id}`}
                className="glass-panel block p-5 transition hover:bg-white/70"
              >
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p className="font-medium text-black">
                      {personaLabel(recipes, row.recipe_ref, row) || `Pitch ${row.id}`}
                    </p>
                    <p className="mt-1 text-sm text-grey">
                      {[labels.audience, labels.duration, labels.channel, labels.intent]
                        .filter(Boolean)
                        .join(" · ")}
                    </p>
                    {currentUser.is_admin && row.user_email && (
                      <p className="mt-1 text-xs text-grey">{row.user_email}</p>
                    )}
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
            title="No pitches yet"
            description="Generate a pitch to see it here."
            action={
              <Link to="/generate" className="btn-accent">
                Generate
              </Link>
            }
          />
        )}
      </div>
    </div>
  );
}
