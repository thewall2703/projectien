import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";
import { Link, Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { api } from "./api";
import type { User } from "./types";
import Login from "./pages/Login";
import Landing from "./pages/Landing";
import Generate from "./pages/Generate";
import Result from "./pages/Result";
import History from "./pages/History";
import ScriptTesting from "./pages/ScriptTesting";
import AskTranscripts from "./pages/AskTranscripts";
import Modules from "./pages/admin/Modules";
import Facts from "./pages/admin/Facts";
import Recipes from "./pages/admin/Recipes";
import Assets from "./pages/admin/Assets";
import FounderQuotes from "./pages/admin/FounderQuotes";
import VoiceTranscripts from "./pages/admin/VoiceTranscripts";
import QaReview from "./pages/admin/QaReview";
import Objections from "./pages/admin/Objections";
import Users from "./pages/admin/Users";
import MediaTesting from "./pages/admin/MediaTesting";
import BrandDeckTesting from "./pages/admin/BrandDeckTesting";
import GeneratedSlides from "./pages/admin/GeneratedSlides";
import { GoogleG, Spinner, Wordmark } from "./components/ui";

const ADMIN_LINKS = [
  { to: "/admin/modules", label: "Modules" },
  { to: "/admin/facts", label: "Facts" },
  { to: "/admin/recipes", label: "Recipes" },
  { to: "/admin/assets", label: "Assets" },
  { to: "/admin/media-testing", label: "Media testing" },
  { to: "/admin/brand-deck-testing", label: "Brand deck testing" },
  { to: "/admin/generated-slides", label: "Generated slides" },
  { to: "/admin/founder-quotes", label: "Founder voice" },
  { to: "/admin/voice-transcripts", label: "Style guide" },
  { to: "/admin/qa-review", label: "AMA Q&A" },
  { to: "/admin/objections", label: "Objections" },
  { to: "/admin/users", label: "Users" },
] as const;

function navLinkClass({ isActive }: { isActive: boolean }) {
  return `rounded-lg px-3 py-1.5 text-sm transition-colors ${
    isActive ? "bg-black/5 font-semibold text-black" : "text-grey hover:bg-black/[0.03] hover:text-black"
  }`;
}

type MenuAnchor = {
  top: number;
  left: number;
  width: number;
  right: number;
};

/**
 * Menus must render outside the frosted navbar. Nested backdrop-filter
 * creates a backdrop root, so a dropdown inside glass-nav cannot blur the page.
 */
function FloatingGlassMenu({
  open,
  anchor,
  align = "left",
  minWidth,
  ignoreRef,
  onClose,
  children,
}: {
  open: boolean;
  anchor: MenuAnchor | null;
  align?: "left" | "right";
  minWidth: number;
  ignoreRef?: RefObject<HTMLElement | null>;
  onClose: () => void;
  children: ReactNode;
}) {
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: MouseEvent) => {
      const target = event.target as Node;
      if (menuRef.current?.contains(target)) return;
      if (ignoreRef?.current?.contains(target)) return;
      onClose();
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("mousedown", onPointer);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onPointer);
      window.removeEventListener("keydown", onKey);
    };
  }, [open, onClose, ignoreRef]);

  if (!open || !anchor || typeof document === "undefined") return null;

  const style: CSSProperties =
    align === "right"
      ? { top: anchor.top, right: Math.max(12, window.innerWidth - anchor.right), minWidth }
      : { top: anchor.top, left: Math.max(12, anchor.left), minWidth };

  return createPortal(
    <div ref={menuRef} role="menu" className="glass-menu fixed z-[80] overflow-hidden rounded-2xl py-2" style={style}>
      {children}
    </div>,
    document.body,
  );
}

function LibraryMenu({ isAdmin }: { isAdmin: boolean }) {
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<MenuAnchor | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const location = useLocation();

  const measure = () => {
    const node = buttonRef.current;
    if (!node) return;
    const rect = node.getBoundingClientRect();
    setAnchor({
      top: rect.bottom + 8,
      left: rect.left,
      right: rect.right,
      width: rect.width,
    });
  };

  useEffect(() => {
    setOpen(false);
  }, [location.pathname]);

  useLayoutEffect(() => {
    if (!open) return;
    measure();
    const onMove = () => measure();
    window.addEventListener("resize", onMove);
    window.addEventListener("scroll", onMove, true);
    return () => {
      window.removeEventListener("resize", onMove);
      window.removeEventListener("scroll", onMove, true);
    };
  }, [open]);

  const libraryActive =
    location.pathname.startsWith("/history") ||
    (isAdmin && location.pathname.startsWith("/script-testing")) ||
    location.pathname.startsWith("/ask") ||
    location.pathname.startsWith("/admin");

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className={`rounded-lg px-3 py-1.5 text-sm transition-colors ${
          libraryActive
            ? "bg-black/5 font-semibold text-black"
            : "text-grey hover:bg-black/[0.03] hover:text-black"
        }`}
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((value) => !value)}
      >
        Library
      </button>
      <FloatingGlassMenu
        open={open}
        anchor={anchor}
        minWidth={220}
        ignoreRef={buttonRef}
        onClose={() => setOpen(false)}
      >
        <Link
          role="menuitem"
          to="/history"
          className={`block px-4 py-2 text-sm hover:bg-black/5 ${
            location.pathname.startsWith("/history") ? "bg-black/5 font-semibold text-black" : "text-black"
          }`}
          onClick={() => setOpen(false)}
        >
          History
        </Link>
        <Link
          role="menuitem"
          to="/ask"
          className={`block px-4 py-2 text-sm hover:bg-black/5 ${
            location.pathname.startsWith("/ask") ? "bg-black/5 font-semibold text-black" : "text-black"
          }`}
          onClick={() => setOpen(false)}
        >
          Ask the library
        </Link>
        {isAdmin && (
          <>
            <div className="my-1 border-t border-black/8" />
            <p className="px-4 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-grey">
              Admin
            </p>
            <Link
              role="menuitem"
              to="/script-testing"
              className={`block px-4 py-2 text-sm hover:bg-black/5 ${
                location.pathname.startsWith("/script-testing")
                  ? "bg-black/5 font-semibold text-black"
                  : "text-black"
              }`}
              onClick={() => setOpen(false)}
            >
              Script testing
            </Link>
            {ADMIN_LINKS.map((item) => (
              <Link
                key={item.to}
                role="menuitem"
                to={item.to}
                className={`block px-4 py-2 text-sm hover:bg-black/5 ${
                  location.pathname.startsWith(item.to) ? "bg-black/5 font-semibold text-black" : "text-black"
                }`}
                onClick={() => setOpen(false)}
              >
                {item.label}
              </Link>
            ))}
          </>
        )}
      </FloatingGlassMenu>
    </>
  );
}

function UserChip({ user, onLogout }: { user: User; onLogout: () => void }) {
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<MenuAnchor | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const initial = (user.email?.[0] || "?").toUpperCase();

  const measure = () => {
    const node = buttonRef.current;
    if (!node) return;
    const rect = node.getBoundingClientRect();
    setAnchor({
      top: rect.bottom + 8,
      left: rect.left,
      right: rect.right,
      width: rect.width,
    });
  };

  useLayoutEffect(() => {
    if (!open) return;
    measure();
    const onMove = () => measure();
    window.addEventListener("resize", onMove);
    window.addEventListener("scroll", onMove, true);
    return () => {
      window.removeEventListener("resize", onMove);
      window.removeEventListener("scroll", onMove, true);
    };
  }, [open]);

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className="inline-flex items-center gap-2 rounded-full border border-black/10 bg-white/80 py-1 pl-1 pr-3 text-sm shadow-sm hover:bg-white"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        <span className="flex h-7 w-7 items-center justify-center rounded-full bg-[#4285F4] text-xs font-semibold text-white">
          {initial}
        </span>
        <span className="hidden max-w-[140px] truncate sm:inline">{user.email}</span>
      </button>
      <FloatingGlassMenu
        open={open}
        anchor={anchor}
        align="right"
        minWidth={180}
        ignoreRef={buttonRef}
        onClose={() => setOpen(false)}
      >
        <p className="truncate px-4 py-2 text-xs text-grey">{user.email}</p>
        <button
          type="button"
          className="block w-full px-4 py-2 text-left text-sm text-black hover:bg-black/5"
          onClick={() => {
            setOpen(false);
            onLogout();
          }}
        >
          Sign out
        </button>
      </FloatingGlassMenu>
    </>
  );
}

function ScrollToTop() {
  const { pathname } = useLocation();
  useLayoutEffect(() => {
    window.scrollTo(0, 0);
  }, [pathname]);
  return null;
}

function Shell({ user, onLogout, children }: { user: User; onLogout: () => void; children: ReactNode }) {
  return (
    <div className="app-canvas">
      <ScrollToTop />
      <header className="glass-nav sticky top-0 z-40">
        <div className="mx-auto flex h-16 max-w-7xl items-center justify-between gap-4 px-5 md:px-8">
          <div className="flex min-w-0 items-center gap-6 md:gap-10">
            <Wordmark size="sm" />
            <nav className="flex items-center gap-1">
              <NavLink to="/generate" className={navLinkClass}>
                Generate
              </NavLink>
              <LibraryMenu isAdmin={user.is_admin} />
            </nav>
          </div>
          <div className="flex items-center gap-3">
            <span className="hidden items-center gap-1.5 text-xs text-grey md:inline-flex">
              <GoogleG className="h-3.5 w-3.5" />
              Signed in
            </span>
            <UserChip user={user} onLogout={onLogout} />
          </div>
        </div>
      </header>
      <main className="min-w-0">{children}</main>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    api
      .me()
      .then((data) => setUser(data as User))
      .catch(() => setUser(null))
      .finally(() => setReady(true));
  }, []);

  if (!ready) {
    return (
      <div className="app-canvas flex flex-col items-center justify-center gap-3 text-grey">
        <Spinner className="h-6 w-6" />
        <p className="text-sm">Loading Pitch Studio…</p>
      </div>
    );
  }

  const logout = async () => {
    await api.logout();
    setUser(null);
  };

  return (
    <Routes>
      <Route path="/login" element={user ? <Navigate to="/" replace /> : <Login onSignedIn={setUser} />} />
      <Route
        path="/*"
        element={
          !user ? (
            <Navigate to="/login" replace />
          ) : (
            <Shell user={user} onLogout={logout}>
              <Routes>
                <Route path="/" element={<Landing />} />
                <Route path="/generate" element={<Generate />} />
                <Route path="/result/:id" element={<Result currentUser={user} />} />
                <Route path="/history" element={<History currentUser={user} />} />
                <Route path="/ask" element={<AskTranscripts />} />
                {user.is_admin && (
                  <>
                    <Route path="/script-testing" element={<ScriptTesting currentUser={user} />} />
                    <Route path="/script-testing/:id" element={<ScriptTesting currentUser={user} />} />
                    <Route path="/admin/modules" element={<Modules />} />
                    <Route path="/admin/facts" element={<Facts />} />
                    <Route path="/admin/recipes" element={<Recipes />} />
                    <Route path="/admin/assets" element={<Assets />} />
                    <Route path="/admin/media-testing" element={<MediaTesting />} />
                    <Route path="/admin/brand-deck-testing" element={<BrandDeckTesting />} />
                    <Route path="/admin/generated-slides" element={<GeneratedSlides />} />
                    <Route path="/admin/founder-quotes" element={<FounderQuotes />} />
                    <Route path="/admin/voice-transcripts" element={<VoiceTranscripts />} />
                    <Route path="/admin/qa-review" element={<QaReview />} />
                    <Route path="/admin/objections" element={<Objections />} />
                    <Route path="/admin/users" element={<Users />} />
                  </>
                )}
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
            </Shell>
          )
        }
      />
    </Routes>
  );
}
