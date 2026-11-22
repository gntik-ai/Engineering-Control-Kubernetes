import { projectScope } from './router';
import { useCallback, useEffect, useRef, useState } from 'react';

/** Rows of the read-only dashboard API (src/engineering_control/dashboard). */
export interface ProjectSummary {
  project: string;
  /** What the administrator calls it; the name above is the identifier and never changes. */
  display_name: string;
  description: string | null;
  /** "registry": the workflows run for it and it cannot be removed. "console": made here. */
  origin: 'registry' | 'console';
  default_branch: string;
  source_repository: string;
  repository_url: string;
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
  review_action?: string | null;
  key: string;
  name: string;
  summary: string;
  /** 0 is the platform's own; null for a flow made here that was never put in force. */
  version: number | null;
  /** The version being worked on, when there is one. */
  draft: number | null;
  builtin: boolean;
  /** A part: it runs only inside the flows that run it. */
  part?: boolean;
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
  owner?: string | null;
  engine: FlowEngine | null;
}

/** A flow the platform knows from the start that was deleted; it can be put back. */
export interface FlowRemoved {
  key: string;
  name: string;
  summary: string;
  /** Whether it is a flow that runs here, or only a name of the library. */
  runs: boolean;
}

/** The library of flows, and the platform's own that were deleted. */
export interface FlowLibrary extends Listing<FlowListed> {
  removed?: FlowRemoved[];
}

/** What deleting a flow did. */
export interface FlowGone {
  key: string;
  name: string;
  builtin: boolean;
  versions: number;
  evaluations: number;
  /** The sections that had it and are left without a flow. */
  sections: { project: string; section: string; section_name: string }[];
}

/** One setting of a task. */
export interface TaskParam {
  name: string;
  kind:
    | 'bool'
    | 'int'
    | 'providers'
    | 'task'
    | 'choice'
    | 'models'
    | 'condition'
    | 'text'
    | 'line'
    | 'commands'
    | 'agent'
    | 'part';
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
  where: 'sandbox' | 'bench' | 'model' | 'platform' | 'control' | 'part' | 'review';
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
  /** For a kind kept only for the flows that have it: what took its place. */
  retired?: string;
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

/** A task of the administrator's own: a kind of task with its settings, kept under a name. */
export interface SavedTask {
  id: string;
  name: string;
  /** What it is for, in the administrator's words. */
  description?: string;
  task: string;
  with: Record<string, unknown>;
  wording: Record<string, string>;
}

/** How a project is installed and how its tests are run, as command lines. */
export interface CommandProfile { id: string; name: string; global: boolean; install: string[]; tests: string[]; }
export interface ManifestCommand { line: string; description: string; timeout: number; }
export interface ManifestCommands {
  project: string;
  name: string;
  using: 'manifest' | 'override' | 'profile' | 'unregistered';
  install: ManifestCommand[];
  tests: ManifestCommand[];
  note: string | null;
}
export interface CommandCatalog {
  items: CommandProfile[];
  manifests?: ManifestCommands[];
  task_commands?: {name: string; flow: string | null; task: string; draft: boolean; commands: string[]}[];
}
export interface ProjectCommands {
  profile?: CommandProfile;
  install: string[];
  tests: string[];
  /** Whether the administrator wrote them; else they are the platform's own file's. */
  own: boolean;
  note: string | null;
}

/** Where a task is used: a flow, as it stands (its draft when it has one), and its tasks. */
export interface TaskUse {
  flow: string;
  name: string;
  tasks: string[];
  draft: boolean;
}

/** Every task a flow can be built from, with the flows that use it. */
export interface TaskLibrary {
  kinds: (TaskType & { hidden: boolean; used_by: TaskUse[] })[];
  saved: (SavedTask & { saved_at: string | null; used_by: TaskUse[] })[];
}

/** What writing a task into the flows that use it did. */
export interface TaskApplied {
  name: string;
  flows: { flow: string; name: string; tasks: string[]; version: number; errors: number }[];
  unchanged: string[];
}

export interface FlowCatalogue {
  format: string;
  tasks: TaskType[];
  prompts: PromptShown[];
  budgets: TaskParam[];
  providers: string[];
  /** The models an agent's task can be set to, by subscription, with the levels each has. */
  models: Record<string, ModelChoices>;
  api_models?: {provider: string; model: string}[];
  decision_models?: {provider: string; model: string; questions: string[]}[];
  /** What an If can ask about, and the ways each is compared. */
  conditions?: ConditionCatalogue;
  saved: SavedTask[];
  /** The platform's kinds of task that are not offered in the designer's list. */
  hidden?: string[];
  /** The agents a task can name. */
  agents?: { id: string; name: string }[];
  /** Flows a new flow can be started from. */
  starters?: { key: string; name: string; summary: string; part?: boolean }[];
  /** The parts a flow can run: flows marked as parts that are in force, with the ways each is left. */
  parts?: { key: string; name: string; summary: string; exits: string[]; writes: boolean }[];
}

/** How an agent works: in a sandbox on a subscription, and when it is asked over an API. */
export interface AgentSettings {
  providers?: string[];
  models?: Record<string, { model?: string; effort?: string }>;
  minutes?: number;
  api?: { provider?: string; model?: string; then_provider?: string; then_model?: string; effort?: string };
}

/** One who does a task, as the administrator defined it. */
export interface AgentShown {
  id: string;
  name: string;
  description: string;
  instructions: string;
  settings: AgentSettings;
  skills: string[];
  skill_names: string[];
  used_by: { flow: string; name: string; tasks: string[]; draft: boolean }[];
}

/** Named instructions an agent can be given. */
export interface SkillShown {
  id: string;
  name: string;
  description: string;
  text: string;
  agents: string[];
}

export interface AgentsListing {
  agents: AgentShown[];
  skills: SkillShown[];
  roles?: EngineRole[];
}

export interface EngineRole {
  prompt: PromptShown;
  tasks: {key: string; name: string; description: string}[];
  used_by: (AgentShown['used_by'][number] & {custom_wording: boolean})[];
}

/** Something the platform knows about a run, that an If of a flow can ask about. */
export interface ConditionFact {
  key: string;
  group: string;
  title: string;
  kind: 'flag' | 'number' | 'text' | 'list' | 'choice' | 'level' | 'paths' | 'outcome';
  doc: string;
  /** For a flag: how it reads when it does not hold. */
  negative: string;
  needs: string[];
  choices: string[];
}

/** One test of a condition: a fact, how it is compared, and with what. */
export interface ConditionTest {
  fact: string;
  is: string;
  value?: string | number;
  /** For how a task ended: which task. */
  of?: string;
}

export interface FlowCondition {
  match: 'all' | 'any';
  tests: ConditionTest[];
}

export interface ConditionCatalogue {
  facts: ConditionFact[];
  operators: Record<string, { key: string; words: string; value: boolean }[]>;
  max_tests: number;
  default: FlowCondition;
}

export interface FlowNode {
  id: string;
  task: string;
  with: Record<string, unknown>;
  next: Record<string, string | null>;
  /** The task of the administrator's own it was made from, when it was. */
  saved?: string;
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
  /** A part: a flow other flows run with the task Run a part. It does not run by itself. */
  part?: boolean;
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
  /** For a part: the ways it can be left, which the flows that run it lead on from. */
  exits?: string[] | null;
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
  diff: string;
  impact: { sections: unknown[]; review_assignments: unknown[]; callers: unknown[]; active_runs: string[] } | null;
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
  where: 'sandbox' | 'bench' | 'model' | 'platform' | 'control';
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
  /** By the clock, from the first step that started to the last that ended, in minutes. */
  elapsed: number;
  steps: number;
  /** How many of its steps asked a model over an API, and what those questions cost. */
  asked: number;
  cost: number;
  counts: Record<string, number> | null;
  files: number;
}

export interface EvaluationCase {
  subject: { n: number; title: string; project: string | null; project_name: string | null; commit: string; of_run: string };
  baseline: Measured | null;
  candidate: Measured | null;
  skipped: string | null;
  verdict: 'better' | 'same' | 'worse' | null;
  /** How many files both changes touch, when both changed any. */
  common: number | null;
}

interface EvaluationTotal {
  cases: number;
  through: number;
  rounds: number;
  minutes: number;
  tokens: number;
  elapsed: number;
  asked: number;
  cost: number;
}

/** A version of a flow tried on past work. */
export interface Evaluation {
  id: string;
  flow: string;
  /** The flow whose past runs are gone through: the flow tried, or another of its sort. */
  of: string;
  of_name: string;
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
  /** The flows whose past runs this flow can be tried on: itself first. */
  sources: { key: string; name: string; runs: number }[];
  /** The flow whose runs `candidates` are. */
  of: string;
  candidates: ReplayCandidate[];
}

/** How much of a project's code is summarised, and which accounts could be asked for more. */
export interface ProjectSummaries {
  files: number;
  /** Files that were asked about and got no summary. */
  without: number;
  latest: string | null;
  models: string[];
  /** The accounts that could be asked; batch: whether their provider takes batches. */
  accounts: { provider: string; name: string; models: string[]; batch?: boolean; batch_price?: number }[];
  agents: { id: string; name: string }[];
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
  /** What the run's calls over an API cost, where the accounts say what their models cost. */
  cost?: number;
  /** How many of its steps asked a model over an API. */
  asked?: number;
}

/**
 * One kind of work on a project. `flow` is null while none is assigned. The description,
 * the two "when" texts, the examples and the labels are its routing guide: what the
 * supervisor reads to decide whether a piece of work belongs here.
 */
export interface Section {
  flow_version?: number | null;
  operation_key?: string | null;
  flow_key?: string | null;
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
  arguments?: Record<string, unknown>;
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
  epic_plan: boolean;
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

/** The start page: a number of days, the oldest first, and what is under way now. */
export interface Overview {
  days: number;
  /** On each day: the items of the backlog that were done, and the US dollars spent. */
  series: { day: string; resolved: number; cost: number }[];
  resolved: number;
  cost: number;
  /** The runs that have not ended; `total` counts the ones that are not listed too. */
  runs: { items: Run[]; total: number };
  /** The queues under way first, then the ones that finished last. */
  queues: { items: Queue[]; total: number; not_finished: number };
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
  incidents?: Incident[];
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
  kind: 'run_failed' | 'run_stalled' | 'queue_paused' | 'review_attention';
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
  usage?: {
    tokens: number;
    input: number;
    output: number;
    cached: number;
    /** Of the tokens read, the ones the provider kept in its cache, where it says so. */
    cache_write?: number;
    /** The question was asked in a batch, at the provider's lower price. */
    batch?: boolean;
    /** For a step that asked a model over an API: where, and what it cost if known. */
    cost?: number;
    provider?: string;
    model?: string;
    account?: string;
  } | null;
  /** What the step was given: the round, which wording of which prompt, which model. */
  input?: {
    round?: number;
    /** Which wording, and the agent the task named with a hash of what it was told. */
    prompt?: { name?: string; sha256?: string; agent?: string; agent_sha256?: string };
    /** The model the flow chose for the step's agent, when it chose one. */
    model?: { provider: string; model?: string; effort?: string };
    /** The Ifs the flow passed after the step: which way each went, and what it asked. */
    chose?: { task: string; way: string; asked: string }[];
  } | null;
  /** What the step produced; for an agent that reports, its report. */
  output?: { report?: string; [key: string]: unknown } | null;
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
    /** The pull request the run opened; `merged` once the run merged it. */
    pull_request?: { number: number; url: string; sha?: string; merged?: string | boolean };
    /** Whether the flow goes on to merge what it publishes, once GitHub's checks pass. */
    merges?: boolean;
    base_branch?: string;
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
  /** A subscription sign-in for agents in sandboxes, or an API key a model is asked with. */
  kind: 'subscription' | 'api';
  /** For an account with an API key: which of the provider's addresses it is used at. */
  endpoint?: string;
  endpoint_name?: string;
  host?: string;
  /** The models the provider listed for the key when it was last asked. */
  models?: string[];
  catalog_error?: string | null;
  catalog_updated_at?: string | null;
  decision_models?: number;
  /** US dollars per million tokens, by model. */
  prices?: Record<string, ModelPrice>;
}

/** Questions handed to a provider as one batch: answered within hours, at a lower price. */
export interface Batch {
  id: string;
  provider: string;
  provider_name: string;
  model: string;
  account: string | null;
  /** submitting, waiting, stopping (asked to stop), ended or failed. */
  state: 'submitting' | 'waiting' | 'stopping' | 'ended' | 'failed';
  /** Why it was stopped: its time was over ("late"), or its run was stopped. */
  stopping: string | null;
  requests: number;
  answered: number;
  detail: string | null;
  run_id: string | null;
  run: string | null;
  step: string | null;
  created_at: string | null;
  submitted_at: string | null;
  ended_at: string | null;
  deadline_at: string | null;
}

export interface Batches {
  items: Batch[];
  /** How many are out at a provider right now. */
  out: number;
}

/** What trying a provider's cache showed: each time the model was asked, and what it read. */
export interface CacheTrial {
  account: string;
  provider: string;
  model: string;
  /** How the provider caches: by itself, or where a request marks it. */
  how: string;
  asked: { what: string; input: number; cache_write: number; cached: number; output: number }[];
  works: boolean;
  with_another_shape: boolean;
  /** Whether a question of another shape read from the cache when the shapes were only said. */
  when_only_said: boolean;
}

/** What reading the list prices did, account by account. */
export interface PricesListed {
  /** The public lists that could be read. */
  lists: string[];
  read_at: string;
  accounts: {
    name: string;
    provider: string;
    /** Models that have the list's price now, of which some are new and some changed. */
    priced: number;
    added: number;
    changed: number;
    /** Models whose price was written by hand, and is kept. */
    yours: number;
    /** Models neither list has a price for. */
    unlisted: number;
  }[];
  priced: number;
  changed: number;
}

export interface ModelPrice {
  input: number;
  output: number;
  cached?: number;
  /** Written by hand: not replaced when the list prices are read again. */
  own?: boolean;
}

/** A kind of account, and how the administrator signs one in or gives it its key. */
export interface ProviderType {
  key: string;
  name: string;
  client: string | null;
  credential: 'token' | 'auth_file' | 'api_key' | null;
  available: boolean;
  steps: string[];
  note: string;
  kind: 'subscription' | 'api';
  endpoints?: { key: string; name: string; host: string }[];
  /** For a provider asked over its API: how it keeps what it read, and whether it takes batches. */
  cache?: string;
  batch?: boolean;
  /** What a token costs in a batch, as a part of what it costs asked at once. */
  batch_price?: number;
}

export interface Accounts extends Listing<Account> {
  providers: ProviderType[];
  /** False when this installation has nowhere to store a sign-in. */
  sign_in_available: boolean;
  /** False when this installation has no key to seal API keys with. */
  keys_available: boolean;
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

export interface ProjectListing extends Listing<ProjectSummary> {
  default_project: string | null;
}

export interface Paged<T> extends Listing<T> {
  page: number;
  pages: number;
  total: number;
}

export type Params = Record<string, string | number | null | undefined>;

export function apiUrl(path: string, params: Params = {}): string {
  const query = new URLSearchParams();
  const owner = projectScope();
  if (owner && /^(flows|agents|command-profiles)(\/|$)/.test(path)) query.set('scope_project', owner);
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== '') query.set(key, String(value));
  }
  const search = query.toString();
  return `/api/dashboard/${path}${search ? `?${search}` : ''}`;
}

function returnToSignIn(): never {
  window.location.assign('/login');
  throw new DOMException('Signed out', 'AbortError');
}

export async function getJson<T>(url: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal, credentials: 'same-origin', headers: { Accept: 'application/json' } });
  if (response.status === 401) {
    returnToSignIn();
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
    returnToSignIn();
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
export function useResource<T>(
  url: string,
  intervalMs: number | null = null,
  { keepPreviousData = false }: { keepPreviousData?: boolean } = {},
): Resource<T> {
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
    setState((previous) => ({ ...previous, loading: true, data: changed && !keepPreviousData ? null : previous.data }));
    getJson<T>(url, controller.signal)
      .then((data) => setState({ data, error: null, loading: false, fetchedAt: Date.now() }))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        const message = error instanceof Error ? error.message : 'The dashboard API is unreachable.';
        setState((previous) => ({ ...previous, error: message, loading: false }));
      });
    return () => controller.abort();
  }, [url, tick, keepPreviousData]);

  useEffect(() => {
    if (!intervalMs || state.loading) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') reload();
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs, reload, state.loading]);

  return { ...state, reload };
}
