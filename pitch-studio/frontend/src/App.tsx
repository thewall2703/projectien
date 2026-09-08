import { useEffect, useState, type ReactNode } from "react";
import { Link, Navigate, NavLink, Route, Routes } from "react-router-dom";
import { api } from "./api";
import type { User } from "./types";
import Login from "./pages/Login";
import Generate from "./pages/Generate";
import Result from "./pages/Result";
import History from "./pages/History";
import Modules from "./pages/admin/Modules";
import Facts from "./pages/admin/Facts";
import Recipes from "./pages/admin/Recipes";
import Assets from "./pages/admin/Assets";
import FounderQuotes from "./pages/admin/FounderQuotes";
import Objections from "./pages/admin/Objections";
import Users from "./pages/admin/Users";

function navClass({ isActive }: { isActive: boolean }) {
  return `block rounded-lg px-3 py-2 text-sm ${
    isActive ? "bg-accent/10 font-semibold text-accent" : "text-ink/70 hover:bg-paper hover:text-ink"
  }`;
}

function Shell({ user, onLogout, children }: { user: User; onLogout: () => void; children: ReactNode }) {
  return (
    <div className="min-h-screen md:flex">
      <header className="border-b border-line bg-surface md:hidden">
        <div className="flex items-center justify-between px-5 py-4">
          <Link to="/" className="font-display text-xl">
            Pitch Studio
          </Link>
          <span className="text-xs text-muted">{user.email}</span>
        </div>
        <nav className="flex flex-wrap gap-3 px-5 pb-4">
          <NavLink to="/" className={navClass} end>
            Generate
          </NavLink>
          <NavLink to="/history" className={navClass}>
            History
          </NavLink>
          {user.is_admin && (
            <>
              <NavLink to="/admin/modules" className={navClass}>
                Modules
              </NavLink>
              <NavLink to="/admin/recipes" className={navClass}>
                Recipes
              </NavLink>
              <NavLink to="/admin/assets" className={navClass}>
                Assets
              </NavLink>
            </>
          )}
        </nav>
      </header>
      <aside className="hidden w-64 shrink-0 flex-col border-r border-line bg-surface px-5 py-6 md:flex">
        <Link to="/" className="font-display text-2xl leading-none">
          Pitch Studio
        </Link>
        <p className="mt-1 text-xs uppercase tracking-[0.18em] text-muted">Masters' Union</p>
        <nav className="mt-8 space-y-6">
          <div>
            <p className="kicker">Create</p>
            <div className="mt-2 space-y-1">
              <NavLink to="/" className={navClass} end>
                Generate
              </NavLink>
              <NavLink to="/history" className={navClass}>
                History
              </NavLink>
            </div>
          </div>
          {user.is_admin && (
            <div>
              <p className="kicker">Library</p>
              <div className="mt-2 space-y-1">
                <NavLink to="/admin/modules" className={navClass}>
                  Modules
                </NavLink>
                <NavLink to="/admin/facts" className={navClass}>
                  Facts
                </NavLink>
                <NavLink to="/admin/recipes" className={navClass}>
                  Recipes
                </NavLink>
                <NavLink to="/admin/assets" className={navClass}>
                  Assets
                </NavLink>
                <NavLink to="/admin/founder-quotes" className={navClass}>
                  Founder voice
                </NavLink>
                <NavLink to="/admin/objections" className={navClass}>
                  Objections
                </NavLink>
                <NavLink to="/admin/users" className={navClass}>
                  Users
                </NavLink>
              </div>
            </div>
          )}
        </nav>
        <div className="mt-auto pt-8">
          <p className="truncate text-xs text-muted">{user.email}</p>
          <button className="mt-3 text-sm text-ink/70 hover:text-ink" onClick={onLogout} type="button">
            Sign out
          </button>
        </div>
      </aside>
      <main className="min-w-0 flex-1 px-6 py-8">{children}</main>
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
    return <div className="p-10 text-muted">Loading…</div>;
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
                <Route path="/" element={<Generate />} />
                <Route path="/result/:id" element={<Result />} />
                <Route path="/history" element={<History />} />
                {user.is_admin && (
                  <>
                    <Route path="/admin/modules" element={<Modules />} />
                    <Route path="/admin/facts" element={<Facts />} />
                    <Route path="/admin/recipes" element={<Recipes />} />
                    <Route path="/admin/assets" element={<Assets />} />
                    <Route path="/admin/founder-quotes" element={<FounderQuotes />} />
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
