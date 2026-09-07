import { FormEvent, useState } from "react";
import { api } from "../api";
import type { User } from "../types";

export default function Login({ onSignedIn }: { onSignedIn: (user: User) => void }) {
  const [email, setEmail] = useState("admin@example.com");
  const [password, setPassword] = useState("changeme");
  const [error, setError] = useState("");

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    try {
      const user = (await api.login(email, password)) as User;
      onSignedIn(user);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign in failed");
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center px-6">
      <form onSubmit={submit} className="w-full max-w-md space-y-5 rounded-xl bg-white p-8 shadow-sm">
        <div>
          <p className="text-xs uppercase tracking-widest text-accent">Masters' Union</p>
          <h1 className="font-serif text-3xl">Pitch Studio</h1>
          <p className="mt-2 text-sm text-ink/60">Sign in to generate a script, deck, and asset pack.</p>
        </div>
        <label className="block text-sm">
          Email
          <input
            className="mt-1 w-full rounded border border-ink/15 px-3 py-2"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            type="email"
            required
          />
        </label>
        <label className="block text-sm">
          Password
          <input
            className="mt-1 w-full rounded border border-ink/15 px-3 py-2"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            type="password"
            required
          />
        </label>
        {error && <p className="text-sm text-red-700">{error}</p>}
        <button className="w-full rounded bg-accent px-4 py-2 text-white" type="submit">
          Sign in
        </button>
      </form>
    </div>
  );
}
