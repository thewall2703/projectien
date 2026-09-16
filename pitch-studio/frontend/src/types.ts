export type AxisOption = {
  code: string;
  label: string;
  description: string;
};

export type User = {
  id: number;
  email: string;
  is_admin: boolean;
  created_at?: string;
};

export type ModuleRow = {
  id: string;
  name: string;
  job: string;
  core_content: string;
  flex_points: string;
  sources: string;
  owner: string;
  sort_order: number;
  edited: boolean;
};

export type FactRow = {
  id: number;
  fact: string;
  value: string;
  source: string;
  status: string;
  note: string;
  module_ids: string;
  edited: boolean;
};

export type RecipeRow = {
  id: number;
  ref: string;
  audience_label: string;
  audience_cluster: string;
  duration: string;
  channel: string;
  intent: string;
  temperature: string | null;
  module_sequence: string;
  word_budget: number;
  priority: string;
  owner: string;
  edited: boolean;
};

export type RecipeOption = {
  ref: string;
  audience_label: string;
  audience_cluster: string;
  duration: string;
  channel: string;
  intent: string;
  module_sequence: string;
  priority: string;
  word_budget: number;
  valid: boolean;
};

export type InterpretPersonaCandidate = {
  recipe_ref: string;
  confidence: number;
  rationale: string;
};

export type InterpretResult = {
  audience_cluster: string;
  duration: string;
  channel: string;
  intent: string;
  temperature: string;
  recipe_ref: string;
  persona_candidates: InterpretPersonaCandidate[];
  summary: string;
  notes: string;
};

export type DeckSlide = {
  layout: string;
  /** 1-based page in the Masters' Union brand deck. */
  page?: number | null;
  title: string;
  module_id: string;
  image_url?: string | null;
};

export type DeckSpec = {
  slides: DeckSlide[];
};

export type AssetRow = {
  id: number;
  type: string;
  title: string;
  url: string | null;
  source_url?: string;
  file_key?: string;
  file_status?: string;
  matrix_ref?: string;
  module_ids: string;
  audiences: string;
  status: string;
  notes: string;
  extract_status?: string;
  edited: boolean;
};

export type ReportPassage = {
  asset_id: number;
  title: string;
  module_ids?: string;
  start_page?: number | null;
  end_page?: number | null;
  text: string;
};

export type ObjectionRow = {
  id: number;
  question: string;
  who_asks: string;
  move: string;
  answer: string;
  status: string;
  edited: boolean;
  source_name?: string;
  source_transcript_id?: number;
  source_candidate_id?: number;
};

export type FounderQuoteRow = {
  id: number;
  text: string;
  verbatim: boolean;
  topic: string;
  module_ids: string;
  source_name: string;
  source_file_id?: string;
  source_url?: string;
  start_sec: number;
  end_sec: number;
  speaker: string;
  status: string;
  edited: boolean;
};

export type ScriptSection = {
  module_id?: string;
  topic_id?: number;
  topic_title?: string;
  pages?: number[];
  heading: string;
  text: string;
};

export type Generation = {
  id: number;
  user_id: number;
  audience_cluster: string;
  duration: string;
  channel: string;
  intent: string;
  temperature: string;
  context_note: string;
  recipe_ref: string;
  module_sequence: string;
  status: string;
  script_json: string;
  deck_spec_json: string;
  pptx_path: string;
  asset_ids: string;
  objection_ids: string;
  founder_quote_ids?: string;
  report_asset_ids?: string;
  validation_report: string;
  error: string;
  created_at: string;
  script: { sections: ScriptSection[]; cta: string } | null;
  deck_spec?: DeckSpec | null;
  assets: AssetRow[];
  objections: ObjectionRow[];
  founder_quotes?: FounderQuoteRow[];
  report_passages?: ReportPassage[];
  recommended_videos?: RecommendedMedia[];
  recommended_pictures?: RecommendedMedia[];
};

export type RecommendedMedia = {
  asset_id: number;
  parent_asset_id: number;
  media_kind: string;
  title: string;
  source_url: string;
  thumbnail_url: string;
  preview_url: string;
  confidence: number;
  rationale: string;
};

export type AxesResponse = {
  audience_clusters: AxisOption[];
  durations: AxisOption[];
  channels: AxisOption[];
  intents: AxisOption[];
  temperatures: AxisOption[];
};

export type FieldConfig = {
  key: string;
  label: string;
  type?: "text" | "textarea" | "select" | "number" | "checkbox";
  options?: string[];
  optionLabels?: Record<string, string>;
};

export type MediaImageAsset = {
  id: number;
  title: string;
  file_status: string;
  content_type: string;
};

export type MediaRecommendation = {
  recipe_ref: string;
  audience_cluster: string;
  duration: string;
  channel: string;
  intent: string;
  temperatures: string[];
  confidence: number;
  rationale: string;
};

export type MediaFeedback = {
  verdicts: Record<string, { verdict: string; note: string; by: string; at: string }>;
  added: { recipe_ref: string; note: string; at: string }[];
};

export type MediaIndexRow = {
  id: number;
  asset_id: number;
  media_kind: string;
  transcript: string;
  visual_description: string;
  image_keys: string[];
  vision: string;
  vision_hash: string;
  indexed_vision_hash: string;
  recommended_at: string | null;
  vision_frozen: boolean;
  status: string;
  stale: boolean;
  job_status?: string;
  job_stage?: string;
  job_error?: string;
  job_type?: string;
  created_at?: string | null;
  updated_at?: string | null;
  asset_title: string;
  asset_source_url: string;
  asset_file_status: string;
  asset_content_type?: string;
  context_index?: string;
  recommendations: { generated_at: string; vision_hash: string; items: MediaRecommendation[] };
  feedback: MediaFeedback;
  image_assets: MediaImageAsset[];
};

export type MediaCandidate = {
  asset_id: number;
  title: string;
  type: string;
  source_url: string;
  file_status: string;
};

export type MediaIndexList = {
  items: MediaIndexRow[];
  candidates: MediaCandidate[];
};

export type DeckTopicPage = {
  page: number;
  label: string;
  module_id: string;
  image_url: string;
};

export type DeckTopicRow = {
  id: number;
  sort_order: number;
  title: string;
  pages: number[];
  page_items: DeckTopicPage[];
  summary: string;
  module_ids: string;
  vision: string;
  vision_hash: string;
  indexed_vision_hash: string;
  recommended_at: string | null;
  vision_frozen: boolean;
  status: string;
  stale: boolean;
  source_hash: string;
  job_status?: string;
  job_stage?: string;
  job_error?: string;
  job_type?: string;
  created_at?: string | null;
  updated_at?: string | null;
  context_index?: string;
  recommendations: { generated_at: string; vision_hash: string; items: MediaRecommendation[] };
  feedback: MediaFeedback;
};

export type DeckTopicList = {
  items: DeckTopicRow[];
  asset_id: number;
  asset_title: string;
  job_status?: string;
  job_stage?: string;
  job_error?: string;
  job_type?: string;
};

export type StyleTranscriptRow = {
  id: number;
  name: string;
  status: string;
  created_at: string | null;
  text_length: number;
  persona_labels: string[];
};

export type StyleTranscriptCreateResult = {
  guide_version: number;
  quotes_kept: number;
  quotes_skipped: number;
  transcript_id: number;
  persona_labels: string[];
  guides_updated: string[];
};

export type QaExtractionRun = {
  id: number;
  style_transcript_id: number;
  transcript_hash: string;
  status: string;
  stage: string;
  error: string;
  candidate_count: number;
  job_id: number;
  created_at: string | null;
  finished_at: string | null;
  transcript_name: string;
};

export type QaCandidate = {
  id: number;
  run_id: number;
  style_transcript_id: number;
  fingerprint: string;
  match_type: string;
  matched_objection_id: number;
  confidence: number;
  status: string;
  question_verbatim: string;
  answer_verbatim: string;
  proposed_question: string;
  proposed_who_asks: string;
  proposed_move: string;
  proposed_answer: string;
  evidence_json: string;
  prior_question: string;
  prior_who_asks: string;
  prior_move: string;
  prior_answer: string;
  applied_objection_id: number;
  review_note: string;
  error: string;
  created_at: string | null;
  reviewed_at: string | null;
  transcript_name: string;
};

export type StyleGuide = {
  version: number;
  guide_text: string;
  persona_label: string;
  source_transcript_ids?: string;
};
