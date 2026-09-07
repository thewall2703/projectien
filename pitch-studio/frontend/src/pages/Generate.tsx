import { FormEvent, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import type { AxesResponse, Generation } from "../types";

function AxisSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { code: string; label: string; description: string }[];
}) {
  const current = options.find((item) => item.code === value);
  return (
    <label className="block">
      <span className="text-sm font-medium">{label}</span>
      <select
        className="mt-1 w-full rounded border border-ink/15 bg-white px-3 py-2"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((item) => (
          <option key={item.code} value={item.code}>
            {item.code} — {item.label}
          </option>
        ))}
      </select>
      {current && <p className="mt-1 text-xs text-ink/55">{current.description}</p>}
    </label>
  );
}

export default function Generate() {
  const navigate = useNavigate();
  const [axes, setAxes] = useState<AxesResponse | null>(null);
  const [audience, setAudience] = useState("A");
  const [duration, setDuration] = useState("T1");
  const [channel, setChannel] = useState("CH1");
  const [intent, setIntent] = useState("I2");
  const [temperature, setTemperature] = useState("X3");
  const [context, setContext] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.axes().then((data) => setAxes(data as AxesResponse)).catch((err) => setError(String(err)));
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const generation = (await api.createGeneration({
        audience_cluster: audience,
        duration,
        channel,
        intent,
        temperature,
        context_note: context,
      })) as Generation;
      navigate(`/result/${generation.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Generation failed");
    } finally {
      setBusy(false);
    }
  };

  if (!axes) {
    return <p className="text-ink/50">Loading axes…</p>;
  }

  return (
    <div className="max-w-3xl">
      <h1 className="font-serif text-3xl">Build a pitch</h1>
      <p className="mt-2 text-ink/60">Pick the five axes. The recipe chooses the modules; the model writes the words.</p>
      <form onSubmit={submit} className="mt-6 grid gap-5">
        <AxisSelect label="Audience" value={audience} onChange={setAudience} options={axes.audience_clusters} />
        <AxisSelect label="Duration" value={duration} onChange={setDuration} options={axes.durations} />
        <AxisSelect label="Channel" value={channel} onChange={setChannel} options={axes.channels} />
        <AxisSelect label="Intent" value={intent} onChange={setIntent} options={axes.intents} />
        <AxisSelect label="Temperature" value={temperature} onChange={setTemperature} options={axes.temperatures} />
        <label className="block">
          <span className="text-sm font-medium">Context note</span>
          <textarea
            className="mt-1 w-full rounded border border-ink/15 px-3 py-2"
            rows={4}
            value={context}
            onChange={(e) => setContext(e.target.value)}
            placeholder="What specifically matters in this conversation?"
          />
        </label>
        {error && <p className="text-sm text-red-700">{error}</p>}
        <button className="rounded bg-accent px-5 py-2 text-white disabled:opacity-50" disabled={busy} type="submit">
          {busy ? "Starting…" : "Generate pitch"}
        </button>
      </form>
    </div>
  );
}
