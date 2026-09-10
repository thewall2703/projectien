import type { DeckTopicList, DeckTopicRow, MediaIndexList, MediaIndexRow } from "./types";

const jsonHeaders = { "Content-Type": "application/json" };

let recipesCache: unknown = null;
let recipesInflight: Promise<unknown> | null = null;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const timeoutMs = path.includes("/auth/me")
    ? 25000
    : /\/(describe|reindex|sync|prepare)(\?|$)/.test(path) || path.includes("/sync-assets") || path.includes("/deck-topics/prepare")
      ? 1200000
      : 20000;
  const response = await fetch(path, {
    credentials: "include",
    signal: AbortSignal.timeout(timeoutMs),
    ...init,
  });
  if (response.status === 401) {
    throw new Error("unauthorized");
  }
  if (!response.ok) {
    const raw = await response.text();
    let detail: unknown = raw || response.statusText;
    try {
      const body = JSON.parse(raw) as { detail?: unknown };
      if (body.detail !== undefined) detail = body.detail;
    } catch {
      // keep raw text
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
  mediaIndexList: () => request<MediaIndexList>("/api/admin/media-index"),
  mediaIndexGet: (id: number) => request<MediaIndexRow>(`/api/admin/media-index/${id}`),
  mediaIndexCreate: (assetId: number) =>
    request<MediaIndexRow>("/api/admin/media-index", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ asset_id: assetId }),
    }),
  prepareMedia: (assetId: number) =>
    request<MediaIndexRow>("/api/admin/media-index/prepare", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ asset_id: assetId }),
    }),
  saveTranscript: (id: number, transcript: string) =>
    request<MediaIndexRow>(`/api/admin/media-index/${id}/transcript`, {
      method: "PUT",
      headers: jsonHeaders,
      body: JSON.stringify({ transcript }),
    }),
  describeMedia: (id: number) =>
    request<MediaIndexRow>(`/api/admin/media-index/${id}/describe`, { method: "POST" }),
  saveVision: (id: number, vision: string) =>
    request<MediaIndexRow>(`/api/admin/media-index/${id}/vision`, {
      method: "PUT",
      headers: jsonHeaders,
      body: JSON.stringify({ vision }),
    }),
  reindexMedia: (id: number) =>
    request<MediaIndexRow>(`/api/admin/media-index/${id}/reindex`, { method: "POST" }),
  sendFeedback: (id: number, recipeRef: string, verdict: "yes" | "no", note = "") =>
    request<MediaIndexRow>(`/api/admin/media-index/${id}/feedback`, {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ recipe_ref: recipeRef, verdict, note }),
    }),
  addUsecase: (id: number, recipeRef: string, note = "") =>
    request<MediaIndexRow>(`/api/admin/media-index/${id}/add-usecase`, {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ recipe_ref: recipeRef, note }),
    }),
  freezeMedia: (id: number) =>
    request<MediaIndexRow>(`/api/admin/media-index/${id}/freeze`, { method: "POST" }),
  unfreezeMedia: (id: number) =>
    request<MediaIndexRow>(`/api/admin/media-index/${id}/unfreeze`, { method: "POST" }),
  syncAsset: (assetId: number, force = false) =>
    request(`/api/admin/assets/${assetId}/sync?force=${force ? "true" : "false"}`, { method: "POST" }),
  deckTopicList: () => request<DeckTopicList>("/api/admin/deck-topics"),
  deckTopicGet: (id: number) => request<DeckTopicRow>(`/api/admin/deck-topics/${id}`),
  prepareDeckTopics: (force = false) =>
    request<DeckTopicList>(`/api/admin/deck-topics/prepare?force=${force ? "true" : "false"}`, {
      method: "POST",
    }),
  saveDeckTopicVision: (id: number, vision: string) =>
    request<DeckTopicRow>(`/api/admin/deck-topics/${id}/vision`, {
      method: "PUT",
      headers: jsonHeaders,
      body: JSON.stringify({ vision }),
    }),
  reindexDeckTopic: (id: number) =>
    request<DeckTopicRow>(`/api/admin/deck-topics/${id}/reindex`, { method: "POST" }),
  sendDeckTopicFeedback: (id: number, recipeRef: string, verdict: "yes" | "no", note = "") =>
    request<DeckTopicRow>(`/api/admin/deck-topics/${id}/feedback`, {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ recipe_ref: recipeRef, verdict, note }),
    }),
  addDeckTopicUsecase: (id: number, recipeRef: string, note = "") =>
    request<DeckTopicRow>(`/api/admin/deck-topics/${id}/add-usecase`, {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ recipe_ref: recipeRef, note }),
    }),
  freezeDeckTopic: (id: number) =>
    request<DeckTopicRow>(`/api/admin/deck-topics/${id}/freeze`, { method: "POST" }),
  unfreezeDeckTopic: (id: number) =>
    request<DeckTopicRow>(`/api/admin/deck-topics/${id}/unfreeze`, { method: "POST" }),
};
