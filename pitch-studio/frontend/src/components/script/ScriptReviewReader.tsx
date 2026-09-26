import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import Modal from "../Modal";
import { Button } from "../ui";
import type {
  ScriptTestParagraph,
  ScriptTestReviewDocument,
  ScriptTestSection,
  ScriptTestSentence,
} from "../../types";

export type ReviewFeedbackItem = {
  id: number;
  target_id: string;
  reviewer_user_id: number;
  reviewer_email: string;
  comment: string;
  created_at: string;
};

type FeedbackTarget =
  | { kind: "sentence"; targetId: string; referenceText: string }
  | { kind: "paragraph"; targetId: string; referenceText: string };

type ScriptFeedbackContextValue = {
  feedback: ReviewFeedbackItem[];
  currentUserId: number;
  coarse: boolean;
  openTarget: (target: FeedbackTarget) => void;
};

const ScriptFeedbackContext = createContext<ScriptFeedbackContextValue | null>(null);

function useIsCoarsePointer() {
  return useMemo(() => {
    if (typeof window === "undefined") return false;
    return window.matchMedia("(pointer: coarse)").matches || window.innerWidth < 768;
  }, []);
}

function useScriptFeedback() {
  const value = useContext(ScriptFeedbackContext);
  if (!value) {
    throw new Error("useScriptFeedback must be used within ScriptFeedbackProvider");
  }
  return value;
}

function SentenceControl({
  sentence,
  count,
  coarse,
  onOpen,
}: {
  sentence: ScriptTestSentence;
  count: number;
  coarse: boolean;
  onOpen: () => void;
}) {
  return (
    <button
      type="button"
      className={`script-sentence relative inline rounded-sm border-b border-transparent px-0.5 text-left transition hover:border-black/20 hover:bg-brand-yellow/20 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-black ${
        count > 0 ? "bg-brand-yellow/10" : ""
      }`}
      aria-label={`Comment on sentence: ${sentence.text.slice(0, 80)}`}
      onDoubleClick={(event) => {
        if (coarse) return;
        event.preventDefault();
        onOpen();
      }}
      onClick={() => {
        if (coarse) onOpen();
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen();
        }
      }}
    >
      {sentence.text}
      {count > 0 && (
        <span className="ml-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-black px-1 text-[10px] font-semibold text-white align-super">
          {count}
        </span>
      )}{" "}
    </button>
  );
}

function ParagraphBlock({
  paragraph,
  feedback,
  coarse,
  onOpen,
}: {
  paragraph: ScriptTestParagraph;
  feedback: ReviewFeedbackItem[];
  coarse: boolean;
  onOpen: (target: FeedbackTarget) => void;
}) {
  const paragraphCount = feedback.filter((item) => item.target_id === paragraph.id).length;
  const showParagraphFeedback = paragraph.sentences.length > 3;
  return (
    <div className={showParagraphFeedback ? "space-y-3" : undefined}>
      <p className="text-base leading-7 text-grey-dark md:leading-8">
        {paragraph.sentences.map((sentence) => {
          const count = feedback.filter((item) => item.target_id === sentence.id).length;
          return (
            <SentenceControl
              key={sentence.id}
              sentence={sentence}
              count={count}
              coarse={coarse}
              onOpen={() =>
                onOpen({
                  kind: "sentence",
                  targetId: sentence.id,
                  referenceText: sentence.text,
                })
              }
            />
          );
        })}
      </p>
      {showParagraphFeedback && (
        <div className="flex justify-start md:justify-end">
          <button
            type="button"
            className="inline-flex min-h-11 items-center gap-2 rounded-full border border-black/15 bg-white/60 px-4 text-sm text-grey-dark transition hover:border-black/40 hover:bg-white"
            onClick={() =>
              onOpen({
                kind: "paragraph",
                targetId: paragraph.id,
                referenceText: paragraph.text,
              })
            }
          >
            Give paragraph feedback
            {paragraphCount > 0 && (
              <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-black px-1.5 text-[11px] font-semibold text-white">
                {paragraphCount}
              </span>
            )}
          </button>
        </div>
      )}
    </div>
  );
}

/** Paragraphs + sentence controls for one review section (no section title). */
export function ReviewSectionBody({
  section,
  feedback,
}: {
  section: ScriptTestSection;
  feedback: ReviewFeedbackItem[];
}) {
  const { coarse, openTarget } = useScriptFeedback();
  return (
    <div className="space-y-8">
      {section.paragraphs.map((paragraph) => (
        <ParagraphBlock
          key={paragraph.id}
          paragraph={paragraph}
          feedback={feedback}
          coarse={coarse}
          onOpen={openTarget}
        />
      ))}
    </div>
  );
}

function SectionBlock({
  section,
  feedback,
}: {
  section: ScriptTestSection;
  feedback: ReviewFeedbackItem[];
}) {
  const title = section.topic_title?.trim() || section.heading || `Section ${section.index + 1}`;
  return (
    <article className="space-y-5">
      <h3 className="font-display text-2xl tracking-tight text-black md:text-3xl">{title}</h3>
      <ReviewSectionBody section={section} feedback={feedback} />
    </article>
  );
}

export function ScriptFeedbackProvider({
  feedback,
  currentUserId,
  onSaveFeedback,
  onUpdateFeedback,
  children,
}: {
  feedback: ReviewFeedbackItem[];
  currentUserId: number;
  onSaveFeedback: (payload: {
    target_kind: "sentence" | "paragraph";
    target_id: string;
    comment: string;
  }) => Promise<void>;
  onUpdateFeedback: (feedbackId: number, comment: string) => Promise<void>;
  children: ReactNode;
}) {
  const coarse = useIsCoarsePointer();
  const [target, setTarget] = useState<FeedbackTarget | null>(null);
  const [editingFeedback, setEditingFeedback] = useState<ReviewFeedbackItem | null>(null);
  const [comment, setComment] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [savedFlash, setSavedFlash] = useState(false);

  const openTarget = useCallback((nextTarget: FeedbackTarget) => {
    setEditingFeedback(null);
    setComment("");
    setError("");
    setTarget(nextTarget);
  }, []);

  const prior = useMemo(() => {
    if (!target) return [];
    return feedback
      .filter((item) => item.target_id === target.targetId)
      .slice()
      .sort((a, b) => a.created_at.localeCompare(b.created_at));
  }, [feedback, target]);

  const close = () => {
    if (saving) return;
    setTarget(null);
    setEditingFeedback(null);
    setComment("");
    setError("");
  };

  const save = async () => {
    if (!target || !comment.trim()) return;
    setSaving(true);
    setError("");
    try {
      if (editingFeedback) {
        await onUpdateFeedback(editingFeedback.id, comment.trim());
      } else {
        await onSaveFeedback({
          target_kind: target.kind,
          target_id: target.targetId,
          comment: comment.trim(),
        });
      }
      setTarget(null);
      setEditingFeedback(null);
      setComment("");
      setSavedFlash(true);
      window.setTimeout(() => setSavedFlash(false), 1800);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save feedback");
    } finally {
      setSaving(false);
    }
  };

  const value = useMemo(
    () => ({ feedback, currentUserId, coarse, openTarget }),
    [feedback, currentUserId, coarse, openTarget],
  );

  return (
    <ScriptFeedbackContext.Provider value={value}>
      {savedFlash && (
        <p className="text-sm font-medium text-black" role="status">
          Feedback saved
        </p>
      )}
      {children}
      <Modal
        open={Boolean(target)}
        title={target?.kind === "paragraph" ? "Paragraph feedback" : "Sentence feedback"}
        onClose={close}
        footer={
          <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
            <Button variant="ghost" className="w-full sm:w-auto" disabled={saving} onClick={close}>
              Cancel
            </Button>
            <Button
              variant="accent"
              className="w-full sm:w-auto"
              loading={saving}
              disabled={!comment.trim() || saving}
              onClick={save}
            >
              {editingFeedback ? "Update feedback" : "Save feedback"}
            </Button>
          </div>
        }
      >
        {target && (
          <div className="space-y-5">
            <blockquote className="rounded-2xl border border-black/10 bg-black/[0.03] px-4 py-3 text-sm leading-relaxed text-grey-dark">
              {target.referenceText}
            </blockquote>
            {prior.length > 0 && (
              <div className="space-y-3">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-grey">
                  Previous feedback
                </p>
                <ul className="space-y-3">
                  {prior.map((item) => (
                    <li key={item.id} className="rounded-2xl border border-black/8 bg-white/70 px-4 py-3">
                      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-grey">
                        <span>{item.reviewer_email || `User ${item.reviewer_user_id}`}</span>
                        <div className="flex items-center gap-3">
                          <span>{new Date(item.created_at).toLocaleString()}</span>
                          {item.reviewer_user_id === currentUserId && (
                            <button
                              type="button"
                              className="font-medium text-black underline-offset-4 hover:underline"
                              disabled={saving}
                              onClick={() => {
                                setEditingFeedback(item);
                                setComment(item.comment);
                                setError("");
                              }}
                            >
                              Edit
                            </button>
                          )}
                        </div>
                      </div>
                      <p className="mt-2 text-sm leading-relaxed text-black">{item.comment}</p>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <label className="block">
              <span className="kicker">{editingFeedback ? "Edit your feedback" : "Your feedback"}</span>
              <textarea
                data-modal-autofocus
                className="field mt-3 min-h-[120px]"
                value={comment}
                onChange={(event) => setComment(event.target.value)}
                onKeyDown={(event) => event.stopPropagation()}
                placeholder="What should change about this line?"
                required
              />
            </label>
            {error && <p className="text-sm text-danger">{error}</p>}
          </div>
        )}
      </Modal>
    </ScriptFeedbackContext.Provider>
  );
}

export default function ScriptReviewReader({
  document,
  feedback,
  currentUserId,
  onSaveFeedback,
  onUpdateFeedback,
}: {
  document: ScriptTestReviewDocument;
  feedback: ReviewFeedbackItem[];
  currentUserId: number;
  onSaveFeedback: (payload: {
    target_kind: "sentence" | "paragraph";
    target_id: string;
    comment: string;
  }) => Promise<void>;
  onUpdateFeedback: (feedbackId: number, comment: string) => Promise<void>;
}) {
  return (
    <ScriptFeedbackProvider
      feedback={feedback}
      currentUserId={currentUserId}
      onSaveFeedback={onSaveFeedback}
      onUpdateFeedback={onUpdateFeedback}
    >
      <div className="space-y-8">
        <div className="glass-panel px-4 py-3 text-sm text-grey-dark">
          Double-click a sentence to comment. On mobile, tap a sentence.
        </div>
        <div className="space-y-14">
          {document.sections.map((section) => (
            <SectionBlock key={section.index} section={section} feedback={feedback} />
          ))}
        </div>
        {document.cta && (
          <p className="border-t border-black/8 pt-6 text-base font-medium leading-7 text-black md:leading-8">
            {document.cta}
          </p>
        )}
      </div>
    </ScriptFeedbackProvider>
  );
}
