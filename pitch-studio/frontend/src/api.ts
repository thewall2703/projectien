const jsonHeaders = { "Content-Type": "application/json" };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "include", ...init });
  if (response.status === 401) {
    throw new Error("unauthorized");
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch {
      detail = await response.text();
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export const api = {
  login: (email: string, password: string) =>
    request("/api/auth/login", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ email, password }),
    }),
  logout: () => request("/api/auth/logout", { method: "POST" }),
  me: () => request("/api/auth/me"),
  axes: () => request("/api/axes"),
  list: <T>(path: string) => request<T[]>(path),
  create: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  update: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PUT", headers: jsonHeaders, body: JSON.stringify(body) }),
  remove: (path: string) => request(path, { method: "DELETE" }),
  reseed: () => request("/api/admin/reseed", { method: "POST" }),
  syncAssets: (types = "report") =>
    request(`/api/admin/sync-assets?types=${encodeURIComponent(types)}`, { method: "POST" }),
  ingestTranscripts: (force = false) =>
    request(`/api/admin/ingest-transcripts?force=${force ? "true" : "false"}`, { method: "POST" }),
  createGeneration: (body: unknown) =>
    request("/api/generations", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify(body),
    }),
  getGeneration: (id: number) => request(`/api/generations/${id}`),
  listGenerations: () => request("/api/generations"),
};
