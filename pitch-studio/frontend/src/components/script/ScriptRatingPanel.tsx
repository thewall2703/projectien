import { useEffect, useState } from "react";
import { Button, ErrorBanner } from "../ui";

export type ScriptRatingValue = {
  id: number;
  reviewer_user_id: number;
  reviewer_email: string;
  rating: number;
  created_at: string;
  updated_at: string;
};

export default function ScriptRatingPanel({
  currentUserId,
  ratings,
  averageRating,
  ratingCount,
  onSave,
}: {
  currentUserId: number;
  ratings: ScriptRatingValue[];
  averageRating: number | null;
  ratingCount: number;
  onSave: (rating: number) => Promise<{
    id: number;
    reviewer_user_id: number;
    reviewer_email: string;
    rating: number;
    created_at: string;
    updated_at: string;
    average_rating: number | null;
    rating_count: number;
  }>;
}) {
  const existing = ratings.find((row) => row.reviewer_user_id === currentUserId);
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
      await onSave(parsed);
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
        {ratingCount > 0 && averageRating != null
          ? `Average ${averageRating.toFixed(1)} from ${ratingCount} reviewer${
              ratingCount === 1 ? "" : "s"
            }`
          : "No ratings yet"}
      </p>
      <ErrorBanner message={error} />
    </div>
  );
}
