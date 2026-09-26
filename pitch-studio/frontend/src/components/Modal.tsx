import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

export default function Modal({
  open,
  title,
  onClose,
  children,
  footer,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCloseRef.current();
    };
    window.addEventListener("keydown", onKey);

    const focusFrame = requestAnimationFrame(() => {
      const root = panelRef.current;
      if (!root) return;
      const focusable =
        root.querySelector<HTMLElement>("[data-modal-autofocus]") ??
        root.querySelector<HTMLElement>(
          "textarea, input, button, [tabindex]:not([tabindex='-1'])",
        );
      focusable?.focus();
    });

    return () => {
      cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
      previouslyFocused.current?.focus?.();
    };
  }, [open]);

  if (!open || typeof document === "undefined") return null;

  return createPortal(
    <div className="fixed inset-0 z-[90] flex items-end justify-center sm:items-center sm:p-6">
      <button
        type="button"
        aria-label="Close dialog"
        className="absolute inset-0 bg-black/40 backdrop-blur-[2px]"
        onClick={onClose}
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="relative z-[1] flex max-h-[92dvh] w-full max-w-xl flex-col rounded-t-3xl border border-black/10 bg-white shadow-2xl sm:max-h-[85vh] sm:rounded-3xl"
        onKeyDown={(event) => {
          // Keep Space / arrows inside fields from bubbling to page shortcuts.
          if (
            event.target instanceof HTMLElement &&
            event.target.closest("input, textarea, select, [contenteditable='true']")
          ) {
            event.stopPropagation();
          }
        }}
      >
        <div className="shrink-0 border-b border-black/8 px-5 py-4 sm:px-6">
          <h2 id={titleId} className="font-display text-2xl tracking-tight text-black">
            {title}
          </h2>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4 sm:px-6">{children}</div>
        {footer && (
          <div className="sticky bottom-0 shrink-0 border-t border-black/8 bg-white/95 px-5 py-4 backdrop-blur sm:px-6">
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
