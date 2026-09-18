import type {
  DeckTopicList,
  DeckTopicRow,
  InterpretResult,
  MediaIndexList,
  MediaIndexRow,
  QaCandidate,
  QaExtractionRun,
  ScriptTestFeedback,
  ScriptTestRating,
  ScriptTestRun,
  ScriptTestRunListItem,
  StyleGuide,
  StyleTranscriptCreateResult,
  StyleTranscriptRow,
} from "./types";

const jsonHeaders = { "Content-Type": "application/json" };

let recipesCache: unknown = null;
let recipesInflight: Promise<unknown> | null = null;
let deckTopicsInflight: Promise<DeckTopicList> | null = null;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const timeoutMs = path.includes("/auth/")
    ? 25000
    : /\/(describe|reindex|sync|prepare)(\?|$)/.test(path) ||
        path.includes("/sync-assets") ||
        path.includes("/deck-topics/prepare") ||
        path.includes("/style-transcripts")
      ? 1200000
      : path.includes("/deck-topics") ||
          path.includes("/media-index") ||
          path.includes("/generations/interpret")
        ? 60000
        : 20000;
  let response: Response;
  try {
    response = await fetch(path, {
      credentials: "include",
      signal: AbortSignal.timeout(timeoutMs),
      ...init,
    });
  } catch (err) {
    const name = err instanceof DOMException ? err.name : "";
    if (name === "AbortError" || name === "TimeoutError") {
      throw new Error("The server did not respond. The database may be unreachable from this network.");
    }
    throw err;
  }
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
    deckTopicsInflight = null;
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
  interpretBrief: (body: { audience_text: string; setting_text: string; goal_text: string }) =>
    request<InterpretResult>("/api/generations/interpret", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify(body),
    }),
  getGeneration: (id: number) => request(`/api/generations/${id}`),
  listGenerations: () => request("/api/generations"),
  createScriptTest: (body: unknown) =>
    request<ScriptTestRun>("/api/script-tests", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify(body),
    }),
  listScriptTests: () => request<ScriptTestRunListItem[]>("/api/script-tests"),
  getScriptTest: (id: number) => request<ScriptTestRun>(`/api/script-tests/${id}`),
  addScriptTestFeedback: (
    id: number,
    body: { target_kind: "sentence" | "paragraph"; target_id: string; comment: string },
  ) =>
    request<ScriptTestFeedback>(`/api/script-tests/${id}/feedback`, {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify(body),
    }),
  saveScriptTestRating: (id: number, rating: number) =>
    request<ScriptTestRating & { average_rating: number | null; rating_count: number }>(
      `/api/script-tests/${id}/rating`,
      {
        method: "PUT",
        headers: jsonHeaders,
        body: JSON.stringify({ rating }),
      },
    ),
  recordVideoClick: (
    generationId: number,
    body: { asset_id: number; displayed_rank: number; interaction_type: "play" | "open_source" },
  ) =>
    request(`/api/generations/${generationId}/video-clicks`, {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify(body),
    }),
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
  deckTopicList: () => {
    if (!deckTopicsInflight) {
      deckTopicsInflight = request<DeckTopicList>("/api/admin/deck-topics").finally(() => {
        deckTopicsInflight = null;
      });
    }
    return deckTopicsInflight;
  },
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
  styleTranscriptList: () => request<StyleTranscriptRow[]>("/api/admin/style-transcripts"),
  styleTranscriptCreate: (name: string, text: string, personaLabels: string[]) =>
    request<StyleTranscriptCreateResult>("/api/admin/style-transcripts", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ name, text, persona_labels: personaLabels }),
    }),
  styleTranscriptSetPersonas: (transcriptId: number, personaLabels: string[]) =>
    request<StyleTranscriptRow>(`/api/admin/style-transcripts/${transcriptId}/personas`, {
      method: "PUT",
      headers: jsonHeaders,
      body: JSON.stringify({ persona_labels: personaLabels }),
    }),
  styleGuideGet: (personaLabel: string) =>
    request<StyleGuide>(
      `/api/admin/style-guide?persona_label=${encodeURIComponent(personaLabel)}`,
    ),
  styleGuideSave: (guideText: string, personaLabel: string) =>
    request<StyleGuide>("/api/admin/style-guide", {
      method: "PUT",
      headers: jsonHeaders,
      body: JSON.stringify({ guide_text: guideText, persona_label: personaLabel }),
    }),
  startQaExtraction: (transcriptId: number, force = false) =>
    request<QaExtractionRun>(
      `/api/admin/style-transcripts/${transcriptId}/extract-qa?force=${force ? "true" : "false"}`,
      { method: "POST" },
    ),
  listQaExtractions: (transcriptId?: number) =>
    request<QaExtractionRun[]>(
      transcriptId
        ? `/api/admin/qa-extractions?transcript_id=${transcriptId}`
        : "/api/admin/qa-extractions",
    ),
  listQaCandidates: (params?: { status?: string; runId?: number; transcriptId?: number }) => {
    const search = new URLSearchParams();
    if (params?.status) search.set("status", params.status);
    if (params?.runId != null) search.set("run_id", String(params.runId));
    if (params?.transcriptId != null) search.set("transcript_id", String(params.transcriptId));
    const query = search.toString();
    return request<QaCandidate[]>(`/api/admin/qa-candidates${query ? `?${query}` : ""}`);
  },
  approveQaCandidate: (
    id: number,
    body: {
      question?: string;
      who_asks?: string;
      move?: string;
      answer?: string;
      review_note?: string;
    } = {},
  ) =>
    request<QaCandidate>(`/api/admin/qa-candidates/${id}/approve`, {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify(body),
    }),
  rejectQaCandidate: (id: number, review_note = "") =>
    request<QaCandidate>(`/api/admin/qa-candidates/${id}/reject`, {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ review_note }),
    }),
};
