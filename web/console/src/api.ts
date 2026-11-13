import { useCallback, useEffect, useRef, useState } from 'react';

/** Rows of the read-only dashboard API (src/engineering_control/dashboard). */
export interface ProjectSummary {
  project: string;
  /** What the administrator calls it; the name above is the identifier and never changes. */
  display_name: string;
  /** "registry": the workflows run for it and it cannot be removed. "console": made here. */
  origin: 'registry' | 'console';
  default_branch: string;
  source_repository: string;
  deployment_repository: string;
  self_contained: boolean;
  /** Items of the backlog still to do. */
  open_items: number;
  /** Runs of the project's work, and how many of them are under way. */
  runs: number;
  active_runs: number;
  last_activity_at: string | null;
}

/** A flow of the library: what a section can be assigned. */
export interface Flow {
  key: string;
  name: string;
  summary: string;
  /** Whether starting an item of a section with this flow runs something on this platform. */
  runs_items?: boolean;
  /** What the section can run by itself to produce items, as the button names it. */
  section_action?: string | null;
  /** Whether the flow looks at the project's running application, which then has to be named. */
  needs_application?: boolean;
}

/** What a flow that runs on this platform consists of, and which version is in force. */
export interface FlowEngine {
  key: string;
  name: string;
  summary: string;
  /** 0 is the platform's own; null for a flow made here that was never put in force. */
  version: number | null;
  /** The version being worked on, when there is one. */
  draft: number | null;
  builtin: boolean;
  /** What starting the flow for a section is called, when a section can run it by itself. */
  section_action: string | null;
  publishes: boolean;
  tasks: number;
  agents: number;
  prompts_changed: string[];
  budgets: { rounds: number | null; minutes: number | null; tokens: number | null };
}

/** A flow of the library, with what the engine knows of it when it runs here. */
export interface FlowListed extends Flow {
  engine: FlowEngine | null;
}

/** One setting of a task. */
export interface TaskParam {
  name: string;
  kind: 'bool' | 'int' | 'providers' | 'task' | 'choice' | 'models';
  default: unknown;
  doc: string;
  low: number | null;
  high: number | null;
  choices: string[];
}

/** A kind of task a flow can be built from. */
export interface TaskType {
  key: string;
  group: string;
  title: string;
  summary: string;
  where: 'sandbox' | 'platform' | 'control';
  account: boolean;
  writes: boolean;
  publishes: boolean;
  gate: boolean;
  approval: boolean;
  needs: string[];
  produces: string[];
  outcomes: string[];
  params: TaskParam[];
  prompt: string | null;
}

export type PromptPart =
  | { kind: 'fixed'; title: string; text: string }
  | { kind: 'editable'; name: string; title: string; text: string; default: string; changed: boolean };

/** A prompt as its parts in order: what the platform fixes and what a flow may word. */
export interface PromptShown {
  name: string;
  title: string;
  variables: { name: string; doc: string }[];
  parts: PromptPart[];
  sha256: string;
}

/** A task as it was set in one flow, kept under a name to be used in others. */
export interface SavedTask {
  id: string;
  name: string;
  task: string;
  with: Record<string, unknown>;
  wording: Record<string, string>;
}

export interface FlowCatalogue {
  format: string;
  tasks: TaskType[];
  prompts: PromptShown[];
  budgets: TaskParam[];
  providers: string[];
  /** The models an agent's task can be set to, by subscription, with the levels each has. */
  models: Record<string, ModelChoices>;
  saved: SavedTask[];
}

export interface FlowNode {
  id: string;
  task: string;
  with: Record<string, unknown>;
  next: Record<string, string | null>;
}

/** A flow as a document: tasks, where each outcome leads, wording of prompts, budgets. */
export interface FlowDocument {
  format: string;
  key: string;
  name: string;
  summary: string;
  start: string;
  tasks: FlowNode[];
  prompts: Record<string, Record<string, string>>;
  budgets: Record<string, number | null>;
  /** For a flow that produces items: what starting an item of its section does. */
  items?: 'request';
  /** What starting the flow for a section is called. */
  action?: string;
  /** Where the designer drew each task. */
  layout?: Record<string, { x: number; y: number }>;
}

/** What the rules say about a flow, and its prompts part by part. */
export interface FlowChecked {
  document: FlowDocument;
  sha256: string;
  problems: FlowProblem[];
  prompts: PromptShown[];
}

export interface FlowProblem {
  level: 'error' | 'warning';
  rule: string;
  where: string | null;
  text: string;
}

export interface FlowVersionRow {
  version: number;
  state: 'draft' | 'published';
  note: string | null;
  sha256: string;
  saved_at: string | null;
  published_at: string | null;
  errors: number;
}

/** One version of a flow with its document, what validation says, and its prompts. */
export interface FlowVersionShown extends FlowVersionRow {
  document: FlowDocument;
  problems: FlowProblem[];
  prompts: PromptShown[];
}

export interface FlowDetail {
  key: string;
  /** Null for a flow made here that was never put in force. */
  in_force: FlowVersionShown | null;
  draft: FlowVersionShown | null;
  versions: FlowVersionRow[];
  builtin: boolean;
  /** The sections that have this flow. */
  used_by: { project: string; project_name: string; section: string; section_name: string }[];
}

/** A flow the supervisor proposed, before it is decided. */
export interface ProposedFlow extends FlowChecked {
  action_id: string;
  outcome: string;
  summary: string | null;
  reason: string | null;
  detail: string | null;
  at: string | null;
  exists: boolean;
  in_force: number | null;
  on_paper: Simulation;
}

/** What starts a section's flow without anyone asking. */
export interface Trigger {
  id: string;
  section: string;
  section_name: string;
  kind: 'schedule' | 'push' | 'item';
  spec: Record<string, unknown>;
  says: string;
  enabled: boolean;
  next_due_at: string | null;
  last_fired_at: string | null;
  last_result: string | null;
  fired: number;
}

export interface SimulatedStep {
  task: string;
  type: string;
  title: string;
  where: 'sandbox' | 'platform' | 'control';
  provider: string | null;
  round: number | null;
  entry: number | null;
  outcome: string;
  /** Whether the outcome is the task's usual one, taken because none was told. */
  assumed: boolean;
  prompt: { name: string; sha256: string; changed: boolean } | null;
  times: number;
  note: string | null;
}

export interface Simulation {
  steps: SimulatedStep[];
  ends: { how: 'done' | 'ended' | 'failed' | 'discarded' | 'invalid' | 'long'; text: string };
  problems: FlowProblem[];
  totals?: { sandboxes: number; agents: Record<string, number>; rounds: number; of_rounds: number };
  of_run: {
    id: string;
    title: string;
    state: string;
    outcome: string | null;
    flow_version: number;
    known: number;
  } | null;
}

/** A run that can be gone through again in the evaluation lab. */
export interface ReplayCandidate {
  run_id: string;
  title: string;
  project: string | null;
  project_name: string | null;
  state: string;
  outcome: string | null;
  detail: string | null;
  commit: string;
  flow_version: number;
  created_at: string | null;
  minutes: number;
  tokens: number;
}

/** What a run came to, in numbers that can be put next to another run's. */
export interface Measured {
  run_id: string;
  state: string;
  outcome: string | null;
  detail: string | null;
  flow_version: number;
  through: boolean;
  rounds: number;
  path: { name: string; outcome: string | null; state: string }[];
  sandboxes: number;
  minutes: number;
  tokens: number;
  counts: Record<string, number> | null;
  files: number;
}

export interface EvaluationCase {
  subject: { n: number; title: string; project: string | null; project_name: string | null; commit: string; of_run: string };
  baseline: Measured | null;
  candidate: Measured | null;
  skipped: string | null;
  verdict: 'better' | 'same' | 'worse' | null;
}

interface EvaluationTotal {
  cases: number;
  through: number;
  rounds: number;
  minutes: number;
  tokens: number;
}

/** A version of a flow tried on past work. */
export interface Evaluation {
  id: string;
  flow: string;
  candidate_version: number;
  baseline_version: number;
  state: 'running' | 'finished' | 'stopped';
  note: string | null;
  cases: number;
  done: number;
  created_at: string | null;
  finished_at: string | null;
  totals: { baseline: EvaluationTotal; candidate: EvaluationTotal };
  verdicts: { better: number; same: number; worse: number };
  items?: EvaluationCase[];
}

export interface EvaluationLab {
  items: Evaluation[];
  candidates: ReplayCandidate[];
}

/** What runs used: tokens their agents reported, and minutes in sandboxes. */
export interface UsageRow {
  runs: number;
  succeeded: number;
  failed: number;
  tokens: number;
  cached: number;
  minutes: number;
  sandboxes: number;
}

export interface Usage {
  days: number;
  since: string | null;
  projects: (UsageRow & { project: string; project_name: string })[];
  flows: (UsageRow & { key: string; version: number; evaluation: boolean })[];
}

export interface RunUsage {
  tokens: number;
  input: number;
  output: number;
  cached: number;
  minutes: number;
  sandboxes: number;
}

/**
 * One kind of work on a project. `flow` is null while none is assigned. The description,
 * the two "when" texts, the examples and the labels are its routing guide: what the
 * supervisor reads to decide whether a piece of work belongs here.
 */
export interface Section {
  key: string;
  name: string;
  description: string;
  use_when: string;
  avoid_when: string;
  /** An item carrying one of these (a GitHub label) belongs here without a judgement. */
  labels: string[];
  examples: string[];
  kind: 'core' | 'custom';
  flow: Flow | null;
}

/** Where a project's work is done, and what was brought over from the original system. */
export interface WorkStatus {
  origin: 'registry' | 'console';
  /** False while the original system is still the one that works on the project. */
  here: boolean;
  cut_over_at: string | null;
  /** Set while no work may start on the project. */
  paused?: { at: string; reason: string | null; by: 'administrator' | 'supervisor' | null } | null;
  brought_over: {
    items_proposed: number;
    items_accepted: number;
    issues_marked: number;
    records_kept: Record<string, number>;
  } | null;
}

/** What a file of the original system's records would bring over. */
export interface RecordsPreview {
  findings: Record<string, number>;
  findings_to_propose: number;
  enhancements: Record<string, number>;
  enhancements_to_propose: number;
  enhancements_to_accept: number;
  issues_known: number;
  issues_in_backlog: number;
  issues_under_way: number;
  records: Record<string, number>;
  exported_at: string | null;
  /** Runs the original system still had under way for the project when the file was written. */
  in_flight: { run: string; workflow: string; status: string }[];
  in_flight_count: number;
}

export interface ProjectDetail {
  project: ProjectSummary;
  sections: Section[];
  work?: WorkStatus;
  /** Where the project's application runs, and whether a test identity is stored for it. */
  application?: { url: string; hosts: string[]; identity: boolean } | null;
}

export interface ItemFacts {
  kind?: 'finding' | 'enhancement' | 'clean-up' | 'ui-finding' | 'ux-finding';
  severity?: 'low' | 'medium' | 'high' | 'critical';
  lens?: string;
  component?: string;
  risk?: string;
  persona?: string;
  pull_request?: { number: number; url: string };
  /** What the original system had done about the item, when it was brought over. */
  original?: { pull_request?: string; delivery?: string; closure?: string; [key: string]: unknown };
  /** Why the supervisor set the item aside. */
  skipped_reason?: string;
  [key: string]: unknown;
}

/** One piece of work wanted on a project: an issue of its repository, or written by hand. */
export interface WorkItem {
  id: string;
  project: string;
  project_name: string;
  /** The section it is routed to; null while it is not routed. */
  /**
   * `starts` says what starting the item does: "item" delivers it, "scope" asks its
   * section's flow to look at what it names, null when nothing can run for it.
   */
  section: { key: string; name: string; flow?: string | null; starts?: 'item' | 'scope' | null } | null;
  title: string;
  source: 'github' | 'manual';
  state: 'proposed' | 'accepted' | 'skipped' | 'done';
  /** Who chose the section: a label of the issue, the supervisor, or the administrator. */
  routed_by: 'label' | 'administrator' | 'supervisor' | null;
  /**
   * Who proposed an item that is not an issue: the supervisor, an audit (a finding), a
   * search for enhancements, or the original system it was brought over from.
   */
  proposed_by?: 'supervisor' | 'audit' | 'discovery' | 'original' | null;
  /** What an item that is not an issue asks for. A list carries only its beginning. */
  description?: string | null;
  /** What is known about the item: a finding's severity, an enhancement's risk, its pull request. */
  facts?: ItemFacts;
  /** The run that produced the item, when one did. */
  source_run_id?: string | null;
  routing_reason: string | null;
  repository: string | null;
  issue_number: number | null;
  issue_url: string | null;
  issue_state: 'open' | 'closed' | null;
  labels: string[];
  updated_at: string | null;
  /** The item's latest run, when it has one. */
  run?: { id: string; state: Run['state']; outcome: string | null } | null;
  /** The queue under way the item is in, when it is in one. */
  queue?: { id: string; name: string; state: Queue['state'] } | null;
}

/** Whether a project's backlog can be read from GitHub, and how the last reading went. */
export interface BacklogStatus {
  github: boolean;
  last_sync: { synced_at: string; open_issues: number; error: string | null } | null;
}

export interface SyncResult {
  imported: number;
  updated: number;
  closed: number;
  reopened: number;
  routed: number;
  open_issues: number;
  complete: boolean;
}

/** One thing waiting for the administrator, and how many items it covers. */
export interface InboxEntry {
  kind:
    | 'run_waiting'
    | 'account_sign_in'
    | 'supervisor_action'
    | 'queue_proposed'
    | 'queue_paused'
    | 'incident'
    | 'project_paused'
    | 'sync_failed'
    | 'section_without_flow'
    | 'unrouted'
    | 'undecided';
  /** For a queue the supervisor proposed, or one the platform paused. */
  queue_id?: string;
  queue?: string;
  /** For trouble with the work that is left to the administrator. */
  incident_id?: string;
  diagnosis?: string | null;
  /** For something the supervisor proposed: what it is, why, and why it waits. */
  action_id?: string;
  /** The tool the supervisor called: a proposed flow can be opened as a graph. */
  tool?: string;
  summary?: string | null;
  reason?: string | null;
  /** Null for an entry that is about an account, not a project. */
  project: string | null;
  project_name: string | null;
  account?: string;
  account_id?: string;
  run_id?: string;
  run?: string;
  outcome?: string | null;
  section: { key: string; name: string } | null;
  count: number;
  detail: string | null;
}

export interface Inbox {
  items: InboxEntry[];
  total: number;
}

/** One item's place in a queue, and what became of it. */
export interface QueueEntry {
  item_id: string;
  position: number;
  state: 'waiting' | 'running' | 'done' | 'failed' | 'skipped';
  /** Why it stands where it does, when the supervisor proposed the order. */
  reason: string | null;
  detail: string | null;
  title: string;
  issue_number: number | null;
  issue_url: string | null;
  item_state: WorkItem['state'];
  section: { key: string; name: string; flow?: string | null } | null;
  run: { id: string; state: Run['state']; outcome: string | null } | null;
}

/** Items of one project to be worked on in order. A draft until it is started. */
export interface Queue {
  id: string;
  name: string;
  project: string;
  project_name: string;
  state: 'draft' | 'running' | 'paused' | 'finished';
  /** How many of its items run at the same time. */
  at_once: number;
  proposed_by: 'supervisor' | null;
  reason: string | null;
  pause_reason: string | null;
  counts: Record<QueueEntry['state'] | 'total', number>;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface QueueDetail extends Queue {
  /** Why nothing further starts right now, when a budget holds the queue back. */
  waiting_for: string | null;
  entries: QueueEntry[];
}

/** The limits of one scope and how much of them is used. Null does not limit. */
export interface BudgetRow {
  scope: 'platform' | 'project' | 'account';
  scope_id: string | null;
  at_once: number | null;
  per_day: number | null;
  running?: number;
  started_today: number;
  name?: string;
  display_name?: string;
  provider?: string;
}

export interface Budgets {
  platform: BudgetRow;
  projects: BudgetRow[];
  accounts: BudgetRow[];
}

/** One message of the conversation with the supervisor. */
export interface SupervisorMessage {
  id: string;
  role: 'administrator' | 'supervisor' | 'platform';
  channel: 'console' | 'chat' | 'platform';
  text: string;
  at: string | null;
}

/** The conversation as the panel shows it. `turn` is set while the supervisor answers. */
export interface SupervisorConversation {
  items: SupervisorMessage[];
  turn: { run_id: string; state: 'queued' | 'running'; detail: string | null; provider: string | null } | null;
  /** What the supervisor proposed and that waits for a decision. */
  pending: InboxEntry[];
}

export interface SupervisorSkill {
  key: string;
  name: string;
  description: string;
  body: string;
  enabled: boolean;
}

export type ToolMode = 'automatic' | 'confirm';

export interface SupervisorDefinition {
  /** 0 is the platform's own definition, before anything was saved. */
  version: number;
  prompt: string;
  skills: SupervisorSkill[];
  tools: Record<string, { enabled: boolean; mode: ToolMode }>;
  providers: string[];
  account_id: string | null;
  exclusive: boolean;
  models: Record<string, string>;
  /** How hard the model reasons, by provider. Not named: the model's own level. */
  efforts: Record<string, string>;
  /** Whether it looks at failed and stuck work by itself, and how often within an hour. */
  operations: { enabled: boolean; per_hour: number };
  note: string | null;
  saved_at: string | null;
}

/** Trouble with the work that the platform noticed, and what the supervisor made of it. */
export interface Incident {
  id: string;
  kind: 'run_failed' | 'run_stalled' | 'queue_paused';
  state: 'open' | 'looking' | 'handled' | 'waiting' | 'dismissed';
  summary: string;
  facts: { needs?: 'decision' | 'administrator'; queue_id?: string; [key: string]: unknown };
  project: string | null;
  project_name: string | null;
  run_id: string | null;
  item_id: string | null;
  diagnosis: string | null;
  confidence: 'high' | 'medium' | 'low' | null;
  at: string | null;
  closed_at: string | null;
}

export interface Incidents extends Listing<Incident> {
  settings: { enabled: boolean; per_hour: number };
}

export interface SupervisorTool {
  name: string;
  kind: 'read' | 'plan' | 'operate' | 'ask';
  title: string;
  description: string;
  modes: ToolMode[];
  default_mode: ToolMode;
}

export interface SupervisorSettings {
  definition: SupervisorDefinition;
  versions: { version: number; note: string | null; saved_at: string | null }[];
  catalogue: SupervisorTool[];
  providers: string[];
  accounts: { id: string; name: string; provider: string; state: string }[];
  /** The models and reasoning levels to choose among, by provider. */
  choices: Record<string, ModelChoices>;
}

/** What one provider's client takes: its models, each with the levels it has. */
export interface ModelChoices {
  models: { name: string; title: string; summary: string; efforts: string[] }[];
  efforts: string[];
}

/** One thing the supervisor did, or proposed, and what became of it. */
export interface SupervisorRecordEntry {
  id: string;
  tool: string;
  title: string;
  kind: SupervisorTool['kind'];
  outcome: 'read' | 'applied' | 'pending' | 'accepted' | 'rejected' | 'refused' | 'failed';
  summary: string | null;
  reason: string | null;
  detail: string | null;
  project: string | null;
  project_name: string | null;
  at: string | null;
  decided_at: string | null;
  decided_via: string | null;
}

/** One step of a run: one sandbox. */
export interface RunStep {
  id: string;
  position: number;
  name: string;
  state: 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'skipped';
  provider: string | null;
  attempt: number;
  exit_code: number | null;
  outcome: string | null;
  detail: string | null;
  started_at: string | null;
  finished_at: string | null;
  /** What the step's agent reported using, when an agent worked in it. */
  usage?: { tokens: number; input: number; output: number; cached: number } | null;
  /** What the step was given: the round, which wording of which prompt, which model. */
  input?: {
    round?: number;
    prompt?: { name: string; sha256: string };
    /** The model the flow chose for the step's agent, when it chose one. */
    model?: { provider: string; model?: string; effort?: string };
  } | null;
  log?: string;
}

/** What an audit or a search for enhancements made of one candidate. */
export interface ProducedEntry {
  number: number;
  title: string;
  severity?: string;
  lens?: string;
  risk?: string;
  persona?: string;
  /** In an audit of a whole repository: the area the finding was made in. */
  area?: string | null;
  disposition: string;
  notes: string[];
  duplicate_of: string | null;
  /** The backlog item it became, when it became one. */
  item_id: string | null;
}

/** What a run that produces items ended with. */
export interface RunResult {
  summary?: string | null;
  covered?: string[];
  not_covered?: string[];
  open_questions?: string[];
  findings?: ProducedEntry[];
  proposals?: ProducedEntry[];
  counts?: Record<string, number>;
  /** The areas a whole-repository audit was split into, and what came of each. */
  areas?: { name: string; paths: string[]; summary: string; blocked: boolean; candidates: number; confirmed: number }[];
}

/** Work done in sandboxes: trying an account, delivering an item, auditing, looking for enhancements. */
export interface Run {
  id: string;
  kind: 'account_check' | 'deliver_issue' | 'audit_code' | 'discover_enhancements' | 'evaluation' | 'flow';
  state: 'queued' | 'running' | 'waiting' | 'succeeded' | 'failed' | 'cancelled';
  title: string;
  outcome: string | null;
  detail: string | null;
  cancel_requested: boolean;
  project: string | null;
  project_name: string | null;
  /** What the run used so far. */
  usage?: RunUsage | null;
  data: {
    repository?: string;
    branch?: string;
    /** The flow the run follows, at the version it started with. */
    flow?: { key: string; version: number; sha256: string };
    /** Where the run stands among the tasks the platform does itself. */
    at?: string | null;
    /** Set for a run of the evaluation lab: which evaluation, and of which run's work. */
    evaluation?: { id: string; of_run: string };
    issue?: { number: number | null; title: string; url: string | null };
    scope?: { title: string; text: string };
    result?: RunResult;
    toolchain?: { note?: string | null };
    change?: {
      summary: string;
      files: { path: string; status: string }[];
      diff: string;
      diff_complete: boolean;
    };
    pull_request?: { number: number; url: string };
  };
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  steps?: RunStep[];
}

/** A subscription sign-in the agents work with. */
export interface Account {
  id: string;
  name: string;
  provider: string;
  provider_name: string;
  client: string | null;
  state: 'signed_out' | 'unchecked' | 'ready' | 'limited' | 'sign_in_required';
  state_reason: string | null;
  paused: boolean;
  max_concurrent: number;
  identity: string | null;
  signed_in_at: string | null;
  checked_at: string | null;
  last_used_at: string | null;
  limited_until: string | null;
  /** A sandbox is asking the account's client a question right now. */
  being_tried: boolean;
}

/** A kind of subscription, and how the administrator signs one in. */
export interface ProviderType {
  key: string;
  name: string;
  client: string | null;
  credential: 'token' | 'auth_file' | null;
  available: boolean;
  steps: string[];
  note: string;
}

export interface Accounts extends Listing<Account> {
  providers: ProviderType[];
  /** False when this installation has nowhere to store a sign-in. */
  sign_in_available: boolean;
}

/** A repository the GitHub App is installed on. */
export interface GitHubRepository {
  full_name: string;
  private: boolean;
  archived: boolean;
  default_branch: string;
  /** The project it is already connected to, if any. */
  project: string | null;
}

/** `configured` is false while the platform has no GitHub App to ask. */
export interface GitHubRepositories {
  configured: boolean;
  items: GitHubRepository[];
}

export interface Listing<T> {
  items: T[];
  refreshed_at?: string | null;
}
export interface Paged<T> extends Listing<T> {
  page: number;
  pages: number;
  total: number;
}

export type Params = Record<string, string | number | null | undefined>;

export function apiUrl(path: string, params: Params = {}): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== '') query.set(key, String(value));
  }
  const search = query.toString();
  return `/api/dashboard/${path}${search ? `?${search}` : ''}`;
}

async function getJson<T>(url: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal, credentials: 'same-origin', headers: { Accept: 'application/json' } });
  if (response.status === 401) {
    // The session ended: the dashboard's own sign-in page brings the operator back here.
    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    throw new DOMException('Signed out', 'AbortError');
  }
  if (!response.ok) throw new Error(`The dashboard API answered ${response.status}.`);
  return (await response.json()) as T;
}

/** A refusal of the API, with the reason it gave. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/** A change: the session's CSRF token is fetched and sent with the body. */
/** Sent on the window after every change the administrator made. */
export const CHANGED = 'engineering-control:changed';

export async function postJson<T>(
  path: string,
  body: Record<string, unknown> = {},
  // A question that changes nothing (a check, a simulation) is asked quietly.
  { quiet = false }: { quiet?: boolean } = {},
): Promise<T> {
  const controller = new AbortController();
  const session = await getJson<{ csrf_token: string }>(apiUrl('session'), controller.signal);
  const response = await fetch(apiUrl(path), {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ ...body, csrf_token: session.csrf_token }),
  });
  const payload = (await response.json().catch(() => null)) as { detail?: unknown } | null;
  if (response.status === 401) {
    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    throw new DOMException('Signed out', 'AbortError');
  }
  if (!response.ok) {
    const detail = typeof payload?.detail === 'string' ? payload.detail : `The request was refused (${response.status}).`;
    throw new ApiError(detail, response.status);
  }
  // Something changed: whatever counts things (the inbox in the navigation) counts again.
  if (!quiet) window.dispatchEvent(new Event(CHANGED));
  return payload as T;
}

/** Ends the dashboard session (the server revokes it) and returns to the sign-in page. */
export async function signOut(): Promise<void> {
  const controller = new AbortController();
  const session = await getJson<{ csrf_token: string }>(apiUrl('session'), controller.signal);
  const response = await fetch('/logout', {
    method: 'POST',
    credentials: 'same-origin',
    redirect: 'manual',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ csrf_token: session.csrf_token }).toString(),
  });
  // A followed redirect is opaque here; anything but an explicit error means signed out.
  if (response.status >= 400) throw new Error(`Sign-out failed (${response.status}); the session is still active.`);
  window.location.assign('/login');
}

export interface Resource<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** When the data on screen was fetched. */
  fetchedAt: number | null;
  reload: () => void;
}

/**
 * GET `url` now and every `intervalMs` while the tab is visible. The previous data stays
 * on screen during a refresh and after a failed one (with `error` set), never a blank table.
 */
export function useResource<T>(url: string, intervalMs: number | null = null): Resource<T> {
  const [state, setState] = useState<Omit<Resource<T>, 'reload'>>({
    data: null,
    error: null,
    loading: true,
    fetchedAt: null,
  });
  const [tick, setTick] = useState(0);
  const latest = useRef(url);
  const reload = useCallback(() => setTick((value) => value + 1), []);

  useEffect(() => {
    const changed = latest.current !== url;
    latest.current = url;
    const controller = new AbortController();
    setState((previous) => ({ ...previous, loading: true, data: changed ? null : previous.data }));
    getJson<T>(url, controller.signal)
      .then((data) => setState({ data, error: null, loading: false, fetchedAt: Date.now() }))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        const message = error instanceof Error ? error.message : 'The dashboard API is unreachable.';
        setState((previous) => ({ ...previous, error: message, loading: false }));
      });
    return () => controller.abort();
  }, [url, tick]);

  useEffect(() => {
    if (!intervalMs) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') reload();
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs, reload]);

  return { ...state, reload };
}
