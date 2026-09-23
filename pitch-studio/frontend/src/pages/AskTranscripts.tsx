import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Button, ErrorBanner } from "../components/ui";
import type { AskResponse, AskSegment, AskSource } from "../types";

function renderInline(text: string, keyPrefix: string) {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return (
        <strong key={`${keyPrefix}-b-${index}`} className="font-semibold text-black">
          {part.slice(2, -2)}
        </strong>
      );
    }
    return <span key={`${keyPrefix}-t-${index}`}>{part}</span>;
  });
}

function MarkdownAnswer({
  text,
  segments,
  activeSegment,
  onSelectSegment,
}: {
  text: string;
  segments: AskSegment[];
  activeSegment: number | null;
  onSelectSegment: (index: number) => void;
}) {
  const blocks = useMemo(() => {
    const lines = (text || "").replace(/\r\n/g, "\n").split("\n");
    const out: Array<{ type: "p" | "ul"; items: string[] }> = [];
    let paragraph: string[] = [];
    let list: string[] = [];

    const flushParagraph = () => {
      if (!paragraph.length) return;
      out.push({ type: "p", items: [paragraph.join(" ").trim()] });
      paragraph = [];
    };
    const flushList = () => {
      if (!list.length) return;
      out.push({ type: "ul", items: [...list] });
      list = [];
    };

    for (const raw of lines) {
      const line = raw.trim();
      if (!line) {
        flushParagraph();
        flushList();
        continue;
      }
      const bullet = line.match(/^[-*]\s+(.+)$/);
      if (bullet) {
        flushParagraph();
        list.push(bullet[1]);
        continue;
      }
      flushList();
      paragraph.push(line);
    }
    flushParagraph();
    flushList();
    return out;
  }, [text]);

  if (segments.length > 0) {
    return (
      <div className="space-y-3 text-[17px] leading-8 text-black/85">
        <p className="text-xs text-grey">Click a line to jump to its source.</p>
        {segments.map((segment, index) => {
          const clickable = (segment.targets || []).length > 0;
          const active = activeSegment === index;
          return (
            <button
              key={`seg-${index}`}
              type="button"
              disabled={!clickable}
              onClick={() => clickable && onSelectSegment(index)}
              className={`block w-full rounded-xl px-3 py-2 text-left transition ${
                active
                  ? "bg-amber-100 ring-1 ring-amber-300"
                  : clickable
                    ? "hover:bg-amber-50"
                    : "cursor-default"
              }`}
            >
              {renderInline(segment.text, `seg-${index}`)}
            </button>
          );
        })}
      </div>
    );
  }

  if (!text.trim()) return null;

  return (
    <div className="space-y-4 text-[17px] leading-8 text-black/85">
      {blocks.map((block, index) =>
        block.type === "ul" ? (
          <ul key={`ul-${index}`} className="list-disc space-y-2 pl-5">
            {block.items.map((item, itemIndex) => (
              <li key={`li-${index}-${itemIndex}`}>{renderInline(item, `li-${index}-${itemIndex}`)}</li>
            ))}
          </ul>
        ) : (
          <p key={`p-${index}`}>{renderInline(block.items[0] || "", `p-${index}`)}</p>
        ),
      )}
    </div>
  );
}

function SourcePassage({
  sourceIndex,
  text,
  highlightLines,
  flashLines,
}: {
  sourceIndex: number;
  text: string;
  highlightLines: number[];
  flashLines: number[];
}) {
  const lines = useMemo(
    () =>
      (text || "")
        .replace(/\r\n/g, "\n")
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean),
    [text],
  );
  const highlightSet = useMemo(() => new Set(highlightLines), [highlightLines]);
  const flashSet = useMemo(() => new Set(flashLines), [flashLines]);
  if (!lines.length) return null;
  return (
    <div className="mt-3 space-y-2">
      {lines.map((line, index) => {
        const cited = highlightSet.has(index);
        const flashed = flashSet.has(index);
        return (
          <p
            key={`${sourceIndex}-${index}`}
            id={`ask-source-${sourceIndex}-line-${index}`}
            className={`rounded-md px-2 py-1 text-sm leading-6 transition ${
              flashed
                ? "bg-amber-300 text-black ring-2 ring-amber-400"
                : cited
                  ? "bg-amber-100 text-black"
                  : "text-black/75"
            }`}
          >
            {line}
          </p>
        );
      })}
    </div>
  );
}

function sourceLabel(source: AskSource) {
  const kind =
    source.source_type === "style"
      ? "Meeting"
      : source.source_type === "media"
        ? "Video"
        : source.source_type === "quote"
          ? "Founder quote"
          : source.source_type === "drive"
            ? "Drive transcript"
            : source.source_type;
  return `${kind} · ${source.source_name}`;
}

export default function AskTranscripts() {
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<AskResponse | null>(null);
  const [activeSegment, setActiveSegment] = useState<number | null>(null);
  const [flash, setFlash] = useState<{ sourceIndex: number; lines: number[] } | null>(null);
  const flashTimer = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (flashTimer.current != null) window.clearTimeout(flashTimer.current);
    };
  }, []);

  const onSelectSegment = (segmentIndex: number) => {
    if (!result) return;
    const segment = result.segments[segmentIndex];
    if (!segment?.targets?.length) return;
    const target = segment.targets[0];
    setActiveSegment(segmentIndex);
    setFlash({ sourceIndex: target.source_index, lines: target.line_indexes || [0] });
    const line = (target.line_indexes && target.line_indexes[0]) ?? 0;
    const node = document.getElementById(`ask-source-${target.source_index}-line-${line}`);
    node?.scrollIntoView({ behavior: "smooth", block: "center" });
    if (flashTimer.current != null) window.clearTimeout(flashTimer.current);
    flashTimer.current = window.setTimeout(() => setFlash(null), 2600);
  };

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const next = question.trim();
    if (!next || loading) return;
    setLoading(true);
    setError("");
    setActiveSegment(null);
    setFlash(null);
    try {
      const data = await api.askTranscripts(next);
      setResult(data);
    } catch (err) {
      setResult(null);
      setError(err instanceof Error ? err.message : "Failed to answer");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="mx-auto max-w-3xl px-6 py-10 md:px-10">
      <p className="kicker">Library</p>
      <h1 className="mt-2 font-display text-4xl tracking-tight text-black">Ask the transcripts</h1>
      <p className="mt-3 max-w-2xl text-sm leading-6 text-grey">
        Search across meeting uploads, video transcripts, and founder voice. Click an answer line to
        jump to the exact supporting source — cited lines are marked in yellow.
      </p>

      <form onSubmit={onSubmit} className="glass-panel mt-8 space-y-4 p-5 md:p-6">
        <label className="block">
          <span className="text-xs font-semibold uppercase tracking-[0.16em] text-grey">Question</span>
          <textarea
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            rows={4}
            placeholder="What does Pratham say about placements, or why learn by doing?"
            className="mt-2 w-full resize-y rounded-xl border border-black/10 bg-white/70 px-4 py-3 text-base text-black outline-none ring-black/10 focus:ring-2"
          />
        </label>
        <div className="flex items-center gap-3">
          <Button type="submit" loading={loading} disabled={!question.trim()}>
            {loading ? "Reading the corpus…" : "Ask"}
          </Button>
        </div>
      </form>

      <div className="mt-4">
        <ErrorBanner message={error} />
      </div>

      {result && (
        <div className="mt-8 space-y-8">
          <section className="glass-panel p-5 md:p-7">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-grey">Answer</p>
            <div className="mt-4">
              <MarkdownAnswer
                text={result.answer_markdown}
                segments={result.segments || []}
                activeSegment={activeSegment}
                onSelectSegment={onSelectSegment}
              />
            </div>
            {result.highlights.length > 0 && (
              <div className="mt-6 flex flex-wrap gap-2">
                {result.highlights.map((item) => (
                  <span
                    key={item}
                    className="rounded-full bg-black/[0.04] px-3 py-1 text-sm font-medium text-black"
                  >
                    {item}
                  </span>
                ))}
              </div>
            )}
          </section>

          {result.sources.length > 0 && (
            <section>
              <p className="text-xs font-semibold uppercase tracking-[0.16em] text-grey">Sources</p>
              <div className="mt-3 space-y-3">
                {result.sources.map((source, index) => (
                  <article
                    key={`${source.source_type}-${index}`}
                    id={`ask-source-${index}`}
                    className="glass-panel p-4"
                  >
                    <div className="flex items-center justify-between gap-3">
                      <p className="text-sm font-semibold text-black">{sourceLabel(source)}</p>
                      <p className="text-xs text-grey">{Math.round(source.score * 100)}% match</p>
                    </div>
                    <SourcePassage
                      sourceIndex={index}
                      text={source.text}
                      highlightLines={source.highlight_lines || []}
                      flashLines={flash?.sourceIndex === index ? flash.lines : []}
                    />
                  </article>
                ))}
              </div>
            </section>
          )}
        </div>
      )}
    </div>
  );
}
