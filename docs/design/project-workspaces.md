# Project workspaces

The Project is the workspace for a single repository. The Superadmin signs in to the global default Project. The project sidebar provides **Global settings → Accounts** for the subscriptions and API keys shared across projects. Global settings also houses the shared Flows, Operations, Agents and Commands. Each project has its own automation library and flow assignments. The **Administrator Console** houses operational tools such as execution budgets.

| Phase | Deliverable | Acceptance criteria |
| --- | --- | --- |
| 0 | Baseline and migration rehearsal | Inventory the live Argo CD revision, schema, existing projects, issue states and completion dates. Rehearse both a physical database upgrade and a fresh install. Trace stored token usage, model prices and the source of settled charges separately. |
| 1 | Project identity and relationships | Persist name, description, repository identity and Architecture. Select Falcone on the existing installation, or the first project created on an empty installation. Support one changeable global default. Keep project-owned Epics, User Stories and technical Issues with same-project relationships. |
| 2 | Authentication and navigation | Every fresh sign-in, including a visit from a bookmark, opens the current default. Authenticated deep links still work. The gntik-ui project switcher and top-right Administrator Console link are usable at desktop and mobile widths. Existing global sections remain accessible. |
| 3 | Project tabs and planning | Home shows the name, description and repository URL. Epics, User Stories and Issues have searchable, paginated tables with project-wide count badges. Long descriptions open from Read More. Each Epic linked from a User Story opens its row in the Epics tab. Epics and User Stories have full CRUD. Every Story lists all its Epics, and every workspace Issue belongs to at least one Story. Reject changes that would orphan an active record. Architecture is an empty fifth tab. |
| 4 | Repository analysis and Description review | Start from a new name and repository URL or analyze an already-connected project in place. Save the source commit, documentation inventory, code context and all pages of open and closed issues, excluding pull requests. Resume interrupted scans. Generate an editable Description. Approval unlocks Epic analysis. |
| 5 | Epic and Story review | Analyze existing GitHub planning issues, import the valid ones and generate missing coverage. After the GitHub issue review, generate the next Epic or all remaining Epics in one model request. At the User Stories stage, generate the next story or all missing stories with one click. Bulk generation reviews groups of up to eight approved Epics. Each group gets one evidence pass and at most one overflow pass over covered outcomes. A batch with uncovered Epics must cover at least one to continue. Once every group has been reviewed and every Epic is covered by a saved story or pending proposal, generation ends. Admin approval of the last remaining story automatically opens the Issues stage. Home shows the active action and counts. The server checkpoints reviewed pages across sign-outs. Review proposals in the Epics or User Stories table and approve individually or by table before continuing. Review every Story's Epic links. Commit each accepted local identity before closing an open planning issue. Already-closed planning issues stay closed. |
| 6 | Technical Issues and GitHub approval | Map open and closed technical issues to one or more approved Stories, retain the original issue number, state and closure date, and generate local drafts for uncovered requirements. Allow the Superadmin to edit mappings. The Issues table supports individual and bulk approval. Only an explicit approval may publish a new issue. Store the approval and reconcile interrupted GitHub writes by identity. Pending work must be excluded from manual starts, queues, triggers and Supervisor execution. |
| 7 | Project Home statistics | Provide two daily charts for the last 7 or 30 UTC calendar days. Count resolved technical Issues using their actual closure dates. Plot measured tokens and estimated cost in EUR together. Show the share with missing prices, historical token-mix assumptions, and the exchange rate used. |
| 8 | Acceptance and release | Exercise an empty project and a project with mixed open and closed records. Test cross-project isolation, changed default, interrupted imports, approval retries, conflicting deletions and legacy automation. Build via GitHub Actions and verify the Argo CD revision, migration hook and application readiness. |

## Evidence needed before enabling external effects

The normal GitHub scanner uses read permissions. Publishing a technical issue and closing an approved planning issue each use a short-lived Issues write token scoped to that repository. On 18 November 2026 the installed app created, read back and closed Falcone issue 1649 as `engineering-control[bot]`. The acceptance check used a repository-scoped Issues write token. [GitHub record](https://github.com/gntik-ai/falcone/issues/1649)

The Home estimate uses project-attributed usage, stored USD costs or model prices per million tokens, and ECB exchange rates. The optional finalized billing import retains distinct source-referenced charges for reconciliation.

## Preservation and recovery

Keep the existing project identifiers, repository identity, historical issue states and completion dates. Importing a technical issue may attach it to Stories without reopening or reclosing it. Closing an imported Epic or Story is a planning action and must be excluded from resolved-Issue counts. Keep a stable mapping from repository and issue number to the local record.

Persist each review stage and its approved inputs. A restart must resume at the last committed stage. A timeout after GitHub receives a create request requires reconciliation before another request can create an issue. Read-only scanning and local review remain usable when a write grant or a billing source is unavailable.

## Release verification

The acceptance suite exercises a repository with no planning issues and a repository with open and closed Epics, Stories and technical issues. It checks stage order, multiple parents, stable identity after a lost GitHub response, and retention of the original technical closure date. Chromium covers the five tabs at desktop and mobile widths. Both a fresh PostgreSQL installation and a physical upgrade from revision 0051 reached revision 0053 while retaining Falcone as the default.

The Home charts use UTC days. The daily figures table shows estimated amounts and exchange-rate dates. [Estimated project cost and optional finalized billing](../../deploy/README.md#estimated-project-cost)


## Review spending and coverage recovery

In **Home → Review token budget**, the administrator can authorize a cumulative token limit shared by automatic planning reviews, Epic and Story generation, technical issue linking, coverage checks and analyses of connections owned by the project. Existing projects start with their current review settings and no configured limit. Usage counts from the first authorization. Each request reserves capacity for its input and maximum output before contacting the provider. Confirmed provider usage settles the reservation. A request with unknown usage keeps its reservation.

If a request cannot fit, the workers wait at their saved checkpoints. The Home tab shows metered, reserved and available tokens and the capacity needed for the next request. Authorizing a sufficient total limit releases the spending hold. Reviews stopped by an operator keep their own Resume controls. A limit of zero holds new model reviews.

If an issue comparison returns an unknown issue ID, unknown Story ID or blank evidence, coverage records the condition and batch reference. It checkpoints supported matches and unaffected comparisons. Resuming retries unresolved pairs, and the normal approval process still governs proposals.

Technical issue matching checks the full text of each issue and Story before reusing a decision. Editing a Story reopens its eligible issue comparisons, including prior negative results. Editing one imported issue reopens its comparisons with eligible Stories. **Project Home → Preview changes** shows the number of reusable and outstanding pairs before starting a review.

The matcher sends the complete source for outstanding pairs. It groups fewer records when their text is long. A single pair larger than the usual batch travels on its own under the existing project token budget. On upgrade, a previous decision based on clipped text appears as outstanding in the preview. The matcher reuses compatible decisions based on complete text when the review starts or resumes. New paired pilots also use the complete source for their original read and include it in budget admission. Earlier frozen pilots resume with the evidence format they started with.

## Automatic planning reviews

Adding an Epic, User Story or technical Issue queues a project review in the same transaction as the record. The first import of a technical GitHub issue and approval of a planning proposal also enter this workflow. A repeated sync uses the existing identity. Releasing this feature starts with new additions. Existing planning can supply context immediately.

In **Home → Automatic planning reviews**, the project uses its repository-analysis model until the administrator selects another model from Accounts. The worker records usage against this project and keeps its place across sign-outs and restarts. Projects without a selection keep their work queued. The source row and Home history show questions, parent matches and links to resulting proposals.

The review checks the source text and looks for missing work across the project's Epics. It considers saved and proposed work, including closed technical Issues. A Story or draft Issue submitted without parents appears as a proposal while its parents are identified. Inspect ambiguous matches in the table, edit the record or its links, and the review resumes. The administrator can retry a review after addressing a failure.

Generated Stories and Issues show their reason and parents in the existing tables. Approving a generated Story triggers its technical Issue review. A generated Issue ends the chain with a text and parent check. GitHub publication uses the existing explicit approval and synchronization process.

## Project connections

Open Settings → Connections in the project sidebar. Dashboard opens the five project tabs. The shell uses the gntik-ui sidebar rail, collapsible Settings group and mobile drawer. A project can depend on several projects and act as a dependency for others. Each connection appears in both projects. Disconnect archives the relationship and retains its decision history.

A connection checks repository-qualified GitHub references in the retained issue history, including closed issues. It shows the original reference while planning is incomplete, then resolves it to an approved local record. The Issues table supports manual links to the other project or to a specific record, a connected-project filter and links back from the target.

When both sides have approved planning, select a model from Accounts for a comparison. The worker reviews records from each project against relevant planning records from its peer. It saves progress and decisions, and checks for new evidence. Analysis usage is attributed to the project that established the connection. Administrators can accept a link or a local edit, dismiss a suggestion, or send missing work to the owning project's planning table. That table handles approval and GitHub publication even after its repository import has completed.

## Flows and Stats delivery phases

Phases 9–14 extend the workspace roadmap above. The project Home retains its Resolved Issues and Cost & Usage summaries.

| Phase | Deliverable | Acceptance criterion |
| --- | --- | --- |
| 9 | Paginate the global and project Flow libraries with a searchable gntik-ui table | Search covers the entire visible library. Assignment and copy dialogs can select published definitions on any page. |
| 10 | Add editable review definitions and project assignments | Manual planning, project coverage, automatic planning, connection analysis and execution planning honor the assigned published definition. Running and paused reviews keep their version. |
| 11 | Attribute usage and run outcomes | Each new metered review response identifies a project and a logical run. Historical usage with incomplete identity stays visible. |
| 12 | Add Global Settings → Stats | Show daily API and subscription consumption, a flow/version table with daily drill-down, and a project table and chart in tokens or estimated EUR. |
| 13 | Consolidate operations reporting | Show daily starts and outcomes, executing runs, cached tokens, sandbox time, evaluations and resolved issues. Remove global reporting from the Flows and Administrator Console landing pages. |
| 14 | Verify and release | Exercise the UI and scoped assignments with fixtures, reconcile the totals, rehearse migration and check the production rollout and existing review pauses. |

## Study of lower-cost planning reviews

A read-only replay on 20 November 2026 used 332 public Falcone-serverless GitHub records. The controller's classifier identified 16 Epics, 58 User Stories and 258 technical issues. Of 14,964 possible Story–issue pairs, 134 were explicitly named parent links. Another 172 explicit links in the Falcone snapshot referenced those serverless Stories. These cross-repository links were counted separately and contributed no savings to the table below. [Falcone-serverless issues](https://github.com/gntik-ai/falcone-serverless/issues) · [Falcone issues](https://github.com/gntik-ai/falcone/issues)

The replay used the current evidence excerpt, matching instructions, response schema and page limits of six Stories and twelve issues. It held the text and ordering constant across variants, counted the system and user input with OpenAI's o200k_base tokenizer, and made zero provider requests. It modeled a cold inventory with no saved decisions or manual exclusions. Stable synthetic UUIDs stood in for the internal database IDs. Each page included its entire rectangle of candidates, even when that meant sending extra pairs.

| Candidate selection | Pages | Input tokens | Reduction | Explicit parent links retained out of 134 |
| --- | ---: | ---: | ---: | ---: |
| Current cold comparison | 220 | 2,500,736 | Baseline | 134 |
| Same pairs, reversible short IDs within each request | 220 | 2,421,182 | 3.2% | 134 |
| Four text-ranked Stories per issue | 84 | 860,322 | 65.6% | 124 |
| Eight text-ranked Stories per issue | 134 | 1,447,673 | 42.1% | 129 |
| Eight text-ranked Stories plus explicit parent links | 135 | 1,451,800 | 41.9% | 134 |
| Sixteen text-ranked Stories per issue | 187 | 2,097,253 | 16.1% | 132 |

The text-ranking test used BM25 after removing parent lines, ticket identifiers and issue references from both queries and indexed records. The graph variant deliberately restored the explicit links. Recovery of those 134 links verifies preservation of declared parents. Reviewers still need to judge semantic links, additional parents and previously unlinked issues. The sidebar Stats release supplies the EUR and token measurements for that pilot.

**Cost interpretation.** The graph replay removed 1,048,936 input tokens. At an uncached input tariff of €1 per million, that part alone would save about €1.05 on a cold pass. The example scales with the actual input tariff. Output length, provider prompt caching, any summarization costs and review corrections affect the total. The existing comparison cache can already make an unchanged repeat cost zero. The pilot must compare both approaches with the same cache state and provider. The study did not call a model or measure an invoice.

### Evidence pages for the same review

A second offline replay tested the first piece of an LLM Wiki. It replaced long descriptions with short excerpts from the source and measured the text a reviewer would receive. The issue and Story IDs, ranking, page order, tokenizer and matching instructions were the same as in the first replay. It made zero model calls.

| Evidence sent | Review batches | Input tokens |
| --- | ---: | ---: |
| All eligible pairs, current evidence | 220 | 2,500,736 |
| All eligible pairs, extractive wiki cards | 220 | 1,139,442 |
| Top eight Stories plus explicit parents, current evidence | 135 | 1,451,800 |
| Same graph candidates, extractive wiki cards | 135 | 639,834 |
| Same cards, followed by the original evidence for 34 of those batches | 169 | 1,005,202 |

Each card cites the repository, source issue number and a hash of the body. For long bodies the replay kept complete lines up to an 850-character allowance, preferring acceptance criteria, constraints and intent. Each excerpt carries its original line number. Short or unsuitable bodies use the ordinary review excerpt. Both graph runs selected the same 2,069 candidate pairs, including all 134 declared parents. Packing them into the incumbent rectangular batches exposed 7,114 pairs to the reviewer. A hash of the batch IDs selected the 34 rereads independently of their length. The input snapshot SHA-256 is `fc19aa3a35aa123d19e347703428caabb562c17c6fae45a0eb4582fc052c059c`.

Using cards cut the graph run's input by 55.9% before follow-up and by 30.8% in the 34-batch reread scenario. Rereading **every** batch would cost 2,091,634 input tokens, 44.1% more than graph retrieval with ordinary evidence. These are input counts for extractive cards. The actual rate of source rereads, accuracy on ambiguous links, output tokens, paid synthesis and refresh costs require a matched pilot.

### Graph and wiki tasks in review flows

The relationship graph answers “which records belong in this review?” A project wiki answers “what do their sources establish, and where can I verify it?” Both belong in the Flow Designer. Model-written pages are an optional experiment. A persistent page can bring together an Epic's intent, a component's constraints, decisions, acceptance criteria, related work and unresolved questions. The administrator can open the original source behind each claim. Original records and administrator decisions govern project state. This adapts the LLM Wiki pattern of ingesting sources, querying accumulated pages and checking them for stale or conflicting information. [LLM Wiki concept](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)

For example, Falcone issue #1431 cites Falcone-serverless Story #22 about weighted rollout and rollback. For an active connection, the graph can return that pair directly. The Story page can display its requirements for readiness, rollback and concurrent updates. A reviewer then checks which criteria #1431 addresses and whether any remain uncovered. A citation establishes a reference. The technical coverage decision still needs evidence for each claimed criterion. [Falcone issue #1431](https://github.com/gntik-ai/falcone/issues/1431) · [Falcone-serverless Story #22](https://github.com/gntik-ai/falcone-serverless/issues/22)

**Current integration point.** The task catalogue already describes settings, required inputs, outputs and outcomes. A review flow currently accepts a sequence of guidance nodes followed by one action-specific terminal task. At the start of a review the worker pins the flow identity and version and saves its instructions and output limit. Supporting these tasks requires extending the review validator, Flow Designer palette and durable review execution together. The published workflow determines the order and branches that the worker actually executes. [Task catalogue](../../src/engineering_control/engine/catalogue.py) · [Review flow validator and policy](../../src/engineering_control/engine/reviews.py) · [Run pinning](../../src/engineering_control/dashboard/review_flows.py) · [Flow Designer](../../web/console/src/pages/FlowDesigner.tsx)

#### Proposed tasks available to an administrator

| Task in the Flow Designer | Input and output | When a model is used |
| --- | --- | --- |
| Read project relationships | Project and changed record IDs → revisioned Epic, Story and issue relationships with evidence. Optionally include active, directly connected projects. | Database reads only. |
| Find related records | Relationships and source text → candidate pairs with reasons. Include explicit and approved neighbors, then rank text matches across Epics. An outcome can widen the search where evidence is weak. | Deterministic ranking. Compare shortlist sizes of 4, 8 and 16 in replay. |
| Reuse review decisions | Candidates and current evidence revisions → known positives, known negatives and pairs still to check. A fully reusable batch goes straight to the result. | Database reads only. |
| Read wiki evidence | Candidate IDs → current, cited evidence pages and a list of missing, stale or conflicting sources. | Reading pages and extracting source lines use zero model tokens. |
| Fetch original evidence | Missing or uncertain criteria, conflicting pages, audit samples or possible gaps → original source passages with reversible record IDs and revision anchors. | Source reads only. For a possible gap, widen the eligible search before proposing new work. |
| Compare evidence | Eligible unchecked pairs and their evidence → supported matches, partial coverage, uncertainty or a request for more evidence. Allow one bounded expansion before routing unresolved items to the administrator. | Uses the model selected from Accounts and the project's review budget. |
| Draft missing work | A gap confirmed against eligible existing work → a proposed Epic, Story or technical issue with its intended parents and evidence. | Uses the selected model only on the gap branch. |
| Complete the review | Checked results → the assigned action's existing local results, links or proposals. Shadow runs write a comparison report. | Existing approval and GitHub synchronization rules govern publication. |
| Check wiki sources | Changed revisions → affected pages, broken citations and visible conflicts. | Database and source reads only. |
| Build evidence page | Changed source text → a versioned page of verbatim, cited passages for a record. | Deterministic extraction. |
| Synthesize wiki page (optional) | Cited passages from frequently read records → a versioned component or Epic page, with claims linked to source revisions. | An explicit model task uses the project budget, Stop control and usage accounting. |

An issue review can start with **Read project relationships → Find related records → Reuse review decisions → Read wiki evidence → Compare evidence**. A fully reused batch finishes immediately. Uncertain matches fetch original evidence. Uncovered work can proceed to drafting after the wider check.

A global template connects the task types in a working order. Project copies can change the shortlist size, include an approved connection, bypass wiki evidence, or insert source expansion. Administrators publish and assign versions under the existing global and project Flows sections. A project can assign different flows to manual Epic, Story and issue reviews, coverage, automatic reviews and connection review. The Home buttons run the assigned flow. Preview follows its deterministic steps and shows fresh comparisons, reusable results and expected token ranges before paid work.

#### Running the evidence flow

Open **Project Home → Review related work** and choose **Preview related records** for a zero-token preview. The searchable table explains the selection, shows the existing decision for each pair and opens cited passages and original source text. In **Global Settings → Flows → Review related work**, the Designer previews an edited draft against a selected project. Project Flow assignments select the published version used for an actual run.

**Run paired comparison** takes its model from Accounts and a separate token cap. It selects one unchecked pair in stable hash order whose original and wiki requests fit the cap, then sends at most one original request and one wiki request. The project budget also applies. The run pins its source, task settings and version. Stop allows an admitted request to settle before pausing. An interrupted request with unconfirmed usage retains its reservation for reconciliation. Results and usage stay in the evidence run and Global Settings → Stats. A repeat with identical inputs reuses the saved comparison. Extractive pages are keyed by source revision and extraction settings.

Project Home → Review related work → **Review model verdicts** lists saved comparisons with an Unrelated or Unclear answer or a disagreement between formats. Open a comparison to read both frozen sources and the model’s evidence, then record or revise your own judgment. Cached repeats share the original comparison’s review. This review uses zero model tokens. Project links and GitHub approvals follow their existing workflows.

When Claude reaches its output limit or declines an answer with a final usage receipt, the comparison records the actual tokens and estimated cost, releases the reservation and stops. An interrupted stream without a final receipt retains its reservation. The run records the failed read, reason and provider reference when available.

Coverage, technical issue and automatic review flows can put relationship, ranking and decision-reuse tasks ahead of matching. These tasks prioritize the selected local pairs and preserve the exhaustive fallback. Cross-project recommendations continue through Connections. The paired comparison evaluates evidence quality separately from the approvals governing project records and GitHub.

#### Revised development phases

Phases 15–21 replace the earlier 15–20 proposal. The graph and wiki share task contracts and a reference set from the first phase. Phases 9–14 above cover the delivered Flows and Stats foundation.

| Phase | Deliverable | Acceptance criterion |
| --- | --- | --- |
| 15 | Specify the task contracts and establish both baselines. | Define inputs, outputs, outcomes, cost mode, scope and implementation version for each node. Replay the current flow, wiki cards alone, graph alone and graph plus cards on the same records. Collect administrator-confirmed positives, dismissals and unknowns. Keep near-duplicates together and hold out whole Epics or a later time window. Distinguish declared links from independently judged semantic links. |
| 16 | Execute and edit individual review tasks. | The palette, publisher and worker use the same compatible task definitions. Validate inputs on every incoming path and bound evidence expansion. Pin the executable node graph, parameters and contract versions per run. Record each node, attempt, inputs and outputs by reference. Stop, logout, restart and Resume return to the saved checkpoint. An interrupted or ambiguously billed call is reconciled before retry. Existing published definitions retain their behavior and paused runs retain their version. |
| 17 | Deliver the project graph and candidate selection as tasks. | Read existing PostgreSQL relationships, imported source references and current connection decisions. Separate human approval, rejection, source citations and model suggestions. Namespace GitHub numbers by repository. Preserve multiple parents, open and closed states, manual restrictions and cycle-safe traversal. Keep unresolved external references visible. Broaden the search for orphan, weak-evidence and audit cases, including cross-Epic matches. |
| 18 | Deliver wiki read, source-check and refresh tasks. | Store derived pages per project with source revisions, citations and a change history. Read relevant sections on demand. Refresh only affected pages after an edit, deletion or connection change. Use deterministic excerpts first. Compare an optional, budgeted model-written page for frequently reused components against that extractive baseline. Route stale or insufficient evidence to original sources before a coverage decision. |
| 19 | Connect incremental reuse, previews and task-level Stats. | Separate selection revisions from comparison revisions. Reuse a decision only for compatible evidence, scope, model and instructions. A new or unchecked pair stays eligible. An unchanged rerun makes zero matching calls, while an edited Story revisits affected prior negatives. Show tokens and estimated EUR for reading, comparing, synthesizing, refreshing and rereading evidence by project, flow version and task. Attribute each model call once. |
| 20 | Run isolated, capped comparisons on Falcone and Falcone-serverless. | Begin with human-labelled offline comparisons. A separately authorized shadow run takes a fixed project token allowance, the same chosen model, a frozen sample and independent Stop/Resume. First compare current review with graph retrieval, then graph alone with graph plus wiki on the same sample. Measure cold import, one edit and unchanged reruns. Keep shadow findings in the report and the project's regular review queue under its existing controls. |
| 21 | Roll out the winning flow per project and action. | Proposed gates are at least 25% lower total estimated EUR than the current flow, preservation of every explicit approved link, and held-out semantic recall within two percentage points of the current reviewer. Report missed and false links, orphan and external-link recall, reviewer time, and EUR per accepted link. A separate comparison must justify adding wiki synthesis. The administrator can publish and assign the winning version or restore the previous assignment. |

For the wiki decision, compare **total estimated EUR per accepted link** with and without the wiki. Include the Accounts input, cached-input and output rates, plus creation, refresh, source rereads and retries. Track reads per page between edits. A frequently read, stable Epic may recover its synthesis cost quickly. A rapidly changing issue may be cheaper to read directly. Promote the wiki only where incremental savings and the quality gates hold on a sufficiently large adjudicated sample. An underpowered or unpriced pilot leaves the choice open.
