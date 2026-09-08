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
  module_id: string;
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
};
