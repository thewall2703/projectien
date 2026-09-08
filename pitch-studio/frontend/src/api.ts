const jsonHeaders = { "Content-Type": "application/json" };

let recipesCache: unknown = null;
let recipesInflight: Promise<unknown> | null = null;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const timeoutMs = path.includes("/auth/me") ? 25000 : 20000;
  const response = await fetch(path, {
    credentials: "include",
    signal: AbortSignal.timeout(timeoutMs),
    ...init,
  });
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
  logout: () => {
    recipesCache = null;
    recipesInflight = null;
    return request("/api/auth/logout", { method: "POST" });
  },
  me: () => request("/api/auth/me"),
  axes: () => request("/api/axes"),
  recipes: () => {
    if (recipesCache) return Promise.resolve(recipesCache);
    if (!recipesInflight) {
      recipesInflight = request("/api/recipes")
        .then((data) => {
          recipesCache = data;
          return data;
        })
        .finally(() => {
          recipesInflight = null;
        });
    }
    return recipesInflight;
  },
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
  extractAsset: (assetId: number, maxPages = 0) =>
    request(`/api/admin/extract-assets/${assetId}?max_pages=${maxPages}`, { method: "POST" }),
  createGeneration: (body: unknown) =>
    request("/api/generations", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify(body),
    }),
  getGeneration: (id: number) => request(`/api/generations/${id}`),
  listGenerations: () => request("/api/generations"),
};
