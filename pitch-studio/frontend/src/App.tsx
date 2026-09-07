import { useEffect, useState, type ReactNode } from "react";
import { Link, Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
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

function Shell({ user, onLogout, children }: { user: User; onLogout: () => void; children: ReactNode }) {
  const link = ({ isActive }: { isActive: boolean }) =>
    `text-sm ${isActive ? "text-accent font-semibold" : "text-ink/70 hover:text-ink"}`;
  return (
    <div className="min-h-screen">
      <header className="border-b border-ink/10 bg-white">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
          <Link to="/" className="font-serif text-xl">
            Pitch Studio
          </Link>
          <nav className="flex flex-wrap items-center gap-4">
            <NavLink to="/" className={link} end>
              Generate
            </NavLink>
            <NavLink to="/history" className={link}>
              History
            </NavLink>
            {user.is_admin && (
              <>
                <NavLink to="/admin/modules" className={link}>
                  Modules
                </NavLink>
                <NavLink to="/admin/facts" className={link}>
                  Facts
                </NavLink>
                <NavLink to="/admin/recipes" className={link}>
                  Recipes
                </NavLink>
                <NavLink to="/admin/assets" className={link}>
                  Assets
                </NavLink>
                <NavLink to="/admin/founder-quotes" className={link}>
                  Founder voice
                </NavLink>
                <NavLink to="/admin/objections" className={link}>
                  Objections
                </NavLink>
                <NavLink to="/admin/users" className={link}>
                  Users
                </NavLink>
              </>
            )}
            <span className="text-xs text-ink/50">{user.email}</span>
            <button className="text-sm text-ink/70 hover:text-ink" onClick={onLogout} type="button">
              Sign out
            </button>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const location = useLocation();

  useEffect(() => {
    api
      .me()
      .then((data) => setUser(data as User))
      .catch(() => setUser(null))
      .finally(() => setReady(true));
  }, [location.pathname]);

  if (!ready) {
    return <div className="p-10 text-ink/50">Loading…</div>;
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
