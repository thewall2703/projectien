import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { generationAxisLabels, personaLabel } from "../labels";
import type { Generation, RecipeOption } from "../types";

function statusClass(status: string) {
  if (status === "done") return "chip bg-success/10 text-success border-success/20";
  if (status === "failed") return "chip bg-danger/10 text-danger border-danger/20";
  return "chip";
}

export default function History() {
  const [rows, setRows] = useState<Generation[]>([]);
  const [recipes, setRecipes] = useState<RecipeOption[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .listGenerations()
      .then((data) => setRows(data as Generation[]))
      .catch((err) => setError(err instanceof Error ? err.message : "Failed"));
    api.recipes().then((data) => setRecipes(data as RecipeOption[])).catch(() => setRecipes([]));
  }, []);

  return (
    <div>
      <p className="kicker">Create</p>
      <h1 className="mt-2 font-display text-4xl">History</h1>
      {error && <p className="mt-3 text-sm text-danger">{error}</p>}
      <div className="mt-6 space-y-3">
        {rows.map((row) => {
          const labels = generationAxisLabels(row);
          return (
            <Link key={row.id} to={`/result/${row.id}`} className="card block p-5 hover:border-accent/40">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="font-medium">
                    {personaLabel(recipes, row.recipe_ref, row) || `Pitch ${row.id}`}
                  </p>
                  <p className="mt-1 text-sm text-muted">
                    {[labels.audience, labels.duration, labels.channel, labels.intent]
                      .filter(Boolean)
                      .join(" · ")}
                  </p>
                </div>
                <div className="flex items-center gap-3">
                  <span className={statusClass(row.status)}>{row.status.replaceAll("_", " ")}</span>
                  <span className="text-xs text-muted">{new Date(row.created_at).toLocaleString()}</span>
                </div>
              </div>
            </Link>
          );
        })}
        {rows.length === 0 && !error && <p className="text-sm text-muted">No pitches yet.</p>}
      </div>
    </div>
  );
}
