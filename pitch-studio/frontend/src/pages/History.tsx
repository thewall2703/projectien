import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { Generation } from "../types";

export default function History() {
  const [rows, setRows] = useState<Generation[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .listGenerations()
      .then((data) => setRows(data as Generation[]))
      .catch((err) => setError(err instanceof Error ? err.message : "Failed"));
  }, []);

  return (
    <div>
      <h1 className="font-serif text-3xl">History</h1>
      {error && <p className="mt-3 text-sm text-red-700">{error}</p>}
      <div className="mt-5 overflow-x-auto rounded-xl bg-white">
        <table className="min-w-full text-left text-sm">
          <thead className="border-b border-ink/10 text-ink/50">
            <tr>
              <th className="px-4 py-3">ID</th>
              <th className="px-4 py-3">Axes</th>
              <th className="px-4 py-3">Recipe</th>
              <th className="px-4 py-3">Status</th>
              <th className="px-4 py-3">Created</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} className="border-t border-ink/5">
                <td className="px-4 py-3">
                  <Link className="text-accent underline" to={`/result/${row.id}`}>
                    {row.id}
                  </Link>
                </td>
                <td className="px-4 py-3">
                  {row.audience_cluster} {row.duration} {row.channel} {row.intent} {row.temperature}
                </td>
                <td className="px-4 py-3">{row.recipe_ref}</td>
                <td className="px-4 py-3">{row.status}</td>
                <td className="px-4 py-3">{new Date(row.created_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
