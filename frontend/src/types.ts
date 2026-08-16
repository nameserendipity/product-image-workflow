export type WorkflowName = 'main' | 'sku' | 'detail';
export type SupplementCategory = WorkflowName | 'all';
export type GenerationMode = 'own_product' | 'competitor_reference';

export interface AgentState {
  reference_url: string | null;
  max_main_images: number | null;
  max_sku_images: number | null;
  max_detail_images: number | null;
  main_quantity_mode: 'default' | 'reference' | 'custom';
  awaiting: string;
  manifest_loaded: boolean;
  workflows: WorkflowName[];
  generation_enabled: boolean;
  generation_mode: GenerationMode;
}

export interface WorkflowProgress {
  analyzing: number;
  prompt_ready: number;
  generating: number;
  completed: number;
  failed: number;
}

export interface BatchStatus {
  mode: 'image_search' | 'direct_link' | 'direct_replace';
  run_mode: 'full' | 'collect_only';
  input_path: string | null;
  input_name: string | null;
  output_path: string | null;
  running: boolean;
  stop_requested: boolean;
  total: number;
  completed: number;
  collected: number;
  failed: number;
  stopped: number;
  current: number;
  valid: number;
  invalid: number;
  unsupported: number;
  missing_images: number;
  missing_links: number;
  pairing_conflicts: number;
  events: Array<Record<string, unknown>>;
}

export interface SupplementStatus {
  running: boolean;
  stop_requested: boolean;
  workbook: string | null;
  events: Array<Record<string, unknown>>;
  completed: number;
  failed: number;
}

export interface AppStatus {
  agent: AgentState;
  identity_source: 'uploaded_product' | 'collected_main';
  identity_ready: boolean;
  manifest_path: string | null;
  product_image: string | null;
  collecting: boolean;
  generating: boolean;
  collector_pid: number | null;
  collection_stop_requested: boolean;
  collection_paused: boolean;
  logs: string[];
  api_configured: boolean;
  vision_api_ready: boolean;
  vision_api_builtin: boolean;
  image_api_ready: boolean;
  vision_api_error: string | null;
  browser_choice: string;
  browser_label: string;
  browser_detected: boolean;
  browser_executable: string;
  generated_count: number;
  collected_summary: Record<WorkflowName | 'total', number>;
  missing_workflows: WorkflowName[];
  folders: {
    collected: boolean;
    generated: boolean;
  };
  workflow_progress: Record<WorkflowName, WorkflowProgress>;
  batch: BatchStatus;
  supplement: SupplementStatus;
  supplement_workbook: string | null;
}

export interface AgentReply {
  message: string;
}

export interface ActionResponse {
  reply: AgentReply;
  status: AppStatus;
}

export interface ChatMessage {
  id: number;
  role: 'agent' | 'user';
  text: string;
}

export type SharedPackageKind = 'complete' | 'main' | 'sku' | 'detail';

export interface SharedCatalogItem {
  product_key: string;
  platform: 'taobao' | 'tmall';
  product_id: string;
  preview_url: string;
  main_count: number;
  sku_count: number;
  detail_count: number;
  package_size: number;
  created_at: string;
  local_directory: string | null;
  available_packages: SharedPackageKind[];
}

export interface SharedLibraryPage {
  items: SharedCatalogItem[];
  next_cursor: string;
}
