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
| 21 | Roll out the winning flow per project and action. | Choose on independently judged accuracy. Check exact scope, false links, missed links and the share sent for administrator review. Preserve the history of an approved link and allow a later source-backed correction. Track estimated EUR within the project allowance. A separate comparison must justify adding wiki synthesis. The administrator can publish and assign the winning version or restore the previous assignment. |

For the wiki decision, compare **total estimated EUR per accepted link** with and without the wiki. Include the Accounts input, cached-input and output rates, plus creation, refresh, source rereads and retries. Track reads per page between edits. A frequently read, stable Epic may recover its synthesis cost quickly. A rapidly changing issue may be cheaper to read directly. Promote the wiki only where incremental savings and the quality gates hold on a sufficiently large adjudicated sample. An underpowered or unpriced pilot leaves the choice open.

## OpenRouter and decision model roadmap

The Superadmin configures OpenRouter in **Global Settings → Accounts**. In the Flow Designer, a generation task selects a standard text model. A decision task selects its own provider, model and supported question types. A project can inherit these choices through its assigned global Flow or publish and assign a project-specific version. For example, Falcone could generate Stories with Claude and check issue-to-Story links with OpenRouter’s `liquid/d1.2`. The same decision task could also use OpenAI Decisions directly.

OpenRouter documents two distinct interfaces. Standard text calls use `/api/v1/chat/completions`. Native decision models use `/api/alpha/decisions` and can be discovered through the Models API with `output_modalities=decisions`. Model IDs, supported questions and prices can change. The selector should use the account catalog and show the capabilities needed by the task. [OpenRouter Decisions](https://openrouter.ai/docs/guides/features/decisions) · [OpenRouter structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs) · [OpenAI Decisions](https://developers.openai.com/api/docs/guides/decisions)

| Phase | Deliverable | Acceptance check |
| --- | --- | --- |
| 22 | Add OpenRouter to Accounts and model discovery | A Superadmin can save a sealed OpenRouter API key, check the account and refresh its catalog. Keep the full provider-qualified model IDs, input and output capabilities, endpoint and pricing units. A failed refresh shows the last successful update. |
| 23 | Support standard OpenRouter calls | Existing model-backed Flow tasks and project review selectors can use a compatible OpenRouter text model. Requests honor output limits, required schema support and the configured upstream routing policy. The run records the requested model and the route reported by the provider. |
| 24 | Add a native decision task with two providers | Adapt OpenAI’s `/v1/decisions` and OpenRouter’s alpha Decisions endpoint to the same task. Verify binary, choice and ordered-score support per model. The issue reviewer asks separately whether a link exists and how much of the Story the issue covers. Map per-question refusals, missing answers and insufficient evidence to administrator review. Keep the original issue and Story passages with each judgment. |
| 25 | Expose independent choices in the Flow Designer and project assignments | A global template or project Flow can choose different generation and decision models. Show the effective provider and model before a run. Pin the model, routing policy and rubric to the run version. Existing and paused runs use their saved configuration. Reuse review decisions only when the source and decision configuration are compatible. |
| 26 | Extend project budgets and Global Settings → Stats | Preview the applicable token or per-request tariff before dispatch. Attribute each receipt once to the project, Flow version, task, provider, endpoint and model. Retain OpenRouter’s reported credits where supplied, use the applicable rate for estimated EUR and identify missing prices. Verify cached input accounting, Stop/Resume and reconciliation after an interrupted call. |
| 27 | Compare decision models on frozen issue–Story pairs | Run the baseline and each candidate on the same original texts, with previous decisions hidden. Score the scope answer and the separate binary-link answer. Record the model, rubric, route, receipt, tokens and estimated EUR. Send conflicting answers to administrator review. Continue with phases 28–32 before assigning a model to automatic reviews. |

### Using the decision review

In Global Settings → Accounts, add an OpenRouter API account or use an existing OpenAI API account. Checking OpenRouter validates the key and refreshes its text and native Decisions catalogs. Prices are USD per million tokens with a separate per-request tariff where applicable. Global Settings → Stats shows the requested model, reported model and upstream, endpoint and estimated EUR when there is a receipt.

Assign **Review issue links with Decisions** to the project's **Review related work** action. The built-in Flow reads graph relationships, ranks candidates, then verifies one original issue–Story pair. In the Flow Designer, a project copy can select its own generation model, decision provider and model, and an optional ordered evidence score. Add **Read wiki evidence** and **Compare original and wiki evidence** before the decision task to compare all three judgments on the same pair. The default decision model is OpenAI `gpt-6-luna`. Changing the project assignment takes effect on a new run.

Open **Review related work** to preview the sources and decision tariff. Run the suggested pair or select **Review this pair**. The project and run token limits apply to both comparison and decision calls. A compatible saved judgment incurs zero additional model calls. An uncertain dispatch keeps its reservation, and a matching retry waits for reconciliation. Pending verdicts include agreements, disagreements and refusals. The administrator can record a judgment against the original passages. Saving a judgment has no link side effects.

For a blind replay, freeze predictions before joining administrator labels. `python scripts/score_decision_panel.py predictions.json reference.json` checks duplicate receipts, rejects overlap with tuning Epics and reports missed links, false links, abstentions, exact scope and estimated EUR per confirmed link. Keep unjudged pairs out of quality denominators. Before promotion, independently label cases held out by Epic, including agreements, orphans and connected projects. The frozen 32-pair pilot has four administrator judgments. It supports advisory review. A cost or quality comparison across native providers requires receipts from each selected account.

### Accuracy and correction of existing connections

An accepted connection can originate from an earlier controller suggestion. The graph may use that edge to find work worth examining. Acceptance by itself supplies no evidence about which Story requirements the issue delivers. A reviewer can recommend correcting a previously approved connection using the original issue and Story passages. Store any subsequent administrator decision with the previous judgment and both source revisions.

| Phase | Work | Acceptance check |
| --- | --- | --- |
| 28 | Establish the reference judgments | Give the reviewer the original issue and Story text, state, outcome and acceptance criteria. Hide stored connections, earlier judgments and model answers. Record a scope, a passage from each source and the reason. Keep ambiguous cases open. Show agreement with earlier judgments as a separate result. |
| 29 | Assemble a validation set held out by Epic | Reserve whole Epic families or a later time window before selecting a model or tuning its instructions. Include full, partial and unrelated pairs, unresolved cases, orphans, cross-project links, and plausible but wrong matches. Keep near-duplicates together. Have a second reviewer resolve disputed cases when there is sufficient evidence. |
| 30 | Compare retrieval, wiki evidence and decision tasks | Measure whether the graph retrieves independently identified links, including links absent from the stored graph. Separately compare the decision models on identical candidate pairs. Test original text, then wiki excerpts with a return to the original when necessary. Freeze model versions, routing, rubric, candidate list and sources for each comparison. |
| 31 | Evaluate errors, review workload and cost | Report exact scope, precision, recall, false and missed links, and abstentions with their denominators. Split out fully covered Stories, orphans and cross-project cases. Treat a binary/scope disagreement as requiring review. Sample agreements too. Record actual token usage and estimated EUR per resolved comparison and per supported link. |
| 32 | Roll out the selected Flow per project | Agree the acceptable false-link and missed-link rates and review workload before evaluating the held-out set. Compare against the current reviewer with uncertainty intervals. Prefer higher accuracy within the project budget. Use cost to choose between similarly accurate options. Keep advisory findings available for administrator approval and retain the previous Flow assignment for rollback. |

An evaluation run records findings separately from project link decisions. If the original sources support a correction, present the previous link, the proposed change and the passages that justify it. The administrator can confirm the correction from that record.

#### Advisory review of an issue–Story pair

Assign **Review issue links with a second opinion** to a project's **Review related work** action. On Project Home, **Preview related records** ranks up to 16 Stories per issue alongside existing links and the graph's wider search for weak matches. It builds extractive wiki cards locally. **Review this pair** runs the paid tasks for the selected pair under the displayed run cap and project allowance.

The first task sends the original issue and Story to OpenAI `gpt-6-luna`. A consistent full or partial verdict completes the review. For an unrelated, incomplete or contradictory verdict, the next task sends the same sources independently to OpenRouter `google/gemini-decision-1` with a criterion-by-criterion question. Both models can be configured in the Flow Designer. Each paid task records its own usage and can reuse a receipt for identical inputs.

**Pending** shows disagreements, incomplete judgments and cases where the models contradict a saved link. **Advisory results** shows consistent findings. Open either record to inspect the original sources and record a human judgment. Changes to project links require their existing approval action. To roll back, restore the previous assignment under the project's Flow assignments. Evidence runs retain the Flow version and model selections they started with.

#### Prerequisites in Story coverage

**Some** covers a direct contribution or a necessary prerequisite. A prerequisite supplies an identifiable input that a specific Story outcome needs. **All** requires direct coverage of every material requirement. A shared topic alone earns **None**. Use **Unclear** when the connection is plausible and the evidence is insufficient.

**Find related records** can follow a dependency named by a Story or by an issue with a declared parent, a manual Story link or a current full-coverage review. The task follows one hop. A newer human judgment takes precedence over a model verdict for the same sources. The selector can replace up to two text matches within the configured limit. The preview and saved comparison show the source revision and the passage that named the dependency. Administrators can disable prerequisite retrieval in the Flow Designer.

In the second-opinion Flow, the first model uses the direct-coverage rubric when the graph has no dependency passage. With a passage, it also judges whether the issue supplies a prerequisite. Both routes can reuse a receipt for identical sources and instructions. A negative or inconsistent verdict, or a conflict with a human judgment on the same source revisions, triggers an independent second opinion. This second model also checks for prerequisites established by the original issue and Story. A shared input alone does not establish dependency.

The review inbox flags disagreement with a previous administrator judgment and shows that judgment alongside the new verdicts. Human labels stay out of model requests. A prerequisite-only verdict of All requires review. Runs already in progress retain their pinned routing and instructions.

## Connecting Epics, User Stories and Issues: redesign

A profile of the live projects on 23 November 2026 showed why the connections disappoint. In Falcone the 64 Epics and 737 User Stories were written from a 2,000-character description of the repository, a Story sat under 3.6 Epics on average and up to ten, one Epic held 273 Stories, 543 Stories had no issue and 279 issues that are no bugs had no Story. A link is two identifiers: nothing says who made it or why. Falcone-serverless, whose Stories were written by people with acceptance criteria, had six orphans in 258 issues.

The administrator agreed these rules and phases. Every change to existing records is a proposal that the administrator approves.

- A User Story has one primary Epic and a technical Issue one primary User Story. Any other connection is a related link, kept apart.
- A link is a record: who made it (stated in the text, the administrator, or a model), the passages on both sides that justify it, its strength, its status and its history.
- A User Story cites the document lines or the issues it groups. One with neither is unsupported.
- An issue's parent is chosen from a ranked shortlist in one question, which may answer that none fits or that the issue is a bug. Only open orphans are resolved; a bug stands apart from the plan and is labelled as one.
- Documentation is read incrementally, from `openspec/` and `docs-site/` as well as the usual folders.

| Phase | Deliverable | Acceptance criterion |
| --- | --- | --- |
| R0 | Unblock stuck review runs and measure planning health. | A run that was cut off can be released, a refused call holds nothing, and each project has a dated baseline of its numbers. |
| R1 | Links as records with reasons, and the primary parent. | Every link shows who made it and why. Each Story has one primary Epic and each issue at most one primary Story. |
| R2 | Orphan worklist and bulk choice of the parent. Bug label and filter. | The open orphans of a project get an answer in one run within a stated budget, and are linked on approval. |
| R3 | Consolidate the existing plan. | A primary Epic for each User Story under several, chosen by approval in bulk; Epics counted by their primary Stories; a missing Story drafted from an answer; finished Stories listed. Merging Stories and splitting an Epic were measured and found unnecessary. |
| R4 | Documentation inventory with history and diff. | A scan is kept per commit, and added, modified and deleted documents are listed between two scans without a model call. |
| R5 | Signals: identifiers, an embedding model and a reranker, joined recall, a labelled set to measure against. | The true parent is among the top 20 candidates for at least 95% of held-out links and ranked first for at least 85%. |
| R6 | Scorer in shadow mode: one learned judgment from all signals that abstains where it cannot rule the other candidates out. Asking decision models about what it abstains on moved to R7, since it spends money. | A report of every change it would make, with how often it is right when it would act, measured on known links it never saw. Nothing is changed. |
| R7 | Reorganize links: the scorer's sure cases as batches to approve for reasons recorded, primary Epics chosen, issues placed and unsupported links taken off. | A sample is judged before each batch; an approved batch can be undone for 30 days; what is judged teaches the scorer. |
| R8 | Reorganize Stories: a delivery state for every open Story from its issues, OpenSpec changes, similar unlinked issues and documents; delivered Stories as a batch to close. | Every Story has a state with its evidence, followed in Planning health. |
| R9 | Clean-up: records that reasons speak against, proposed for closing on two reasons of different kinds or an exact one; a model's check of the unsupported Stories; Stories and Epics closed by batches to authorize. | Every proposal shows its reasons; nothing is closed without authorization; issues are closed on GitHub as not planned, with a comment. |
| R10 | Reorganize structure: how each Epic holds together, splits of Epics that hold more than 10% of the Stories, and User Stories proposed from groups of issues that fit none. | Each proposal can be edited, approved or set aside; what is set aside is not made again. |
| R11 | Continuous and everywhere: one table of every project with its next step, the trail of issues that arrived, a daily refresh with the drift since the day before, and that drift in the daily digest. | A new issue is read, scored and shown with its closest Story within minutes; a project with no plan says so. |
| R12 | Epic pages: one page for each Epic from its Stories, their issues and the closest documents, with no model asked, kept in versions with what changed; an overview by a model on request, each sentence with its records. | One cited page per Epic, refreshed when its sources change. |

R5 to R12 replace the earlier R5 to R7 and are described in [Reorganizing a project's plan automatically](planning-reorganization.md): a cascade from rules to ranking models to decision models, so that a language model and a person see only the cases cheaper evidence cannot settle.

### Planning health

**Project Home → Planning health**, also shown at the top of **Reviews → Related Work**, counts how the project's records hang together. Every number is the one the planning tables show: an issue is an orphan here exactly when its row says so. Lower is better for all of them.

| Number | What it counts |
| --- | --- |
| Open and closed issues with no User Story | Issues that are no bugs and have neither a saved parent nor an approved connection to a Story. |
| Bugs still linked to a User Story | Bugs that carry a link to a Story from before they were known as bugs. A bug has no Story, so the number should be zero. Bugs are counted on their own: the counts of issues on this page leave them out. |
| Issues under several User Stories | Issues with more than one parent. |
| User Stories with no issue | Stories no issue is linked to. |
| User Stories under several Epics | Stories with more than one Epic, with the average and the maximum. |
| User Stories in the largest Epic | The size of the largest Epic and its share of all Stories. |
| Epics with no User Story | Epics nothing is planned under. |
| Links with no recorded reason | Every link, until links record who made them and why (phase R1). |
| Reviews waiting | Automatic reviews that are queued, need input or failed, and evidence runs that were cut off. |

The project is measured once a day, and again when the page is opened after ten minutes or **Measure now** is pressed. The last measurement of each UTC day is kept (`planning_health`, migration 0070). The first one is the baseline: each tile says how its number moved since. `GET /api/dashboard/projects/{name}/planning/health?days=30` returns the current numbers, the baseline and the days between. No model is asked.

### Links as records, and the primary parent

Whether a User Story is under an Epic, or a technical Issue under a User Story, is still said by the link tables, and everything that reads or writes them works as before. What they cannot say is kept in `planning_links` (migration 0071), one record a link: who made it, the reason and the passages that justify it, and whether it is the child's primary parent. The records follow the link tables: a link without a record gets one when its row is next listed or the project is measured, and the record of a link that is taken away is kept with the day it went. A link made by any part of the platform is therefore recorded, and a link's history outlives the link.

Who made a link is established from what is stored, never guessed.

| Shown as | When |
| --- | --- |
| Stated in its text | The child's own text names the parent's GitHub number on a `Story:`, `Epic:` or `Parent:` line. The line is kept as evidence. |
| Chosen by you | The administrator set the record's parents in the console. |
| Matched by a review | A coverage review matched the issue with the Story. Its evidence is kept as the reason, with whether it covers the whole Story or part of it. |
| Proposed by a review, approved by you | The record was a proposal that named this parent, and the administrator approved it. |
| No reason recorded | Nothing stored explains the link. |

A record with parents has exactly one primary parent. An only parent is the primary one. Among several, one is suggested: first by how much is known of the link, then by how alike the two texts are, counting less the words that all the parents share. A suggestion is marked as one until the administrator chooses, with **Make this the primary Epic** or **User Story** under the parent's name. Every other parent is a related link.

In the User Stories and Issues tables each parent shows whether it is primary and who linked it; opening the line shows the reason, the passages and the date. `GET /api/dashboard/projects/{name}/planning/{kind}/{id}/links` returns a record's current and past links. `POST …/{id}/primary` with `parent_id` records the administrator's choice.

Planning health counts the links that say why, by who made them, and the records whose primary parent nobody chose.

### Open issues with no User Story

**Reviews → Related Work** lists the project's open issues that have no User Story and are no bug: the issues the Issues table marks as orphans. A closed issue is not listed, and neither is a bug: a bug has no User Story.

Each issue is shown with a shortlist of at most eight open User Stories, best first. A Story the issue's own text names on a `Story:` line comes first. The others are ranked by the words the two texts share, where a word counts for more the fewer Stories have it; each entry shows its score, the words it shares and its primary Epic, and one below 2.0 is marked a weak match. No model is asked for the shortlist, so it costs nothing and is the same every time it is computed. A closed or archived Story is never offered.

A run asks a model one question per issue: the issue, its shortlist, and four possible answers.

| Answer | Meaning | What approval does |
| --- | --- | --- |
| This User Story | Delivering the issue is part of delivering one Story of the shortlist. | Puts the issue under that Story. The link is recorded as proposed by a review and approved by you, with the review's reason and passages. |
| It is a bug | The issue reports that existing behaviour is wrong. | Confirms the issue as a bug, as the planning tables do: it leaves the plan for the Bugs tab, and the bug label is applied on GitHub. |
| None fits | It is planned work that no listed Story covers. The answer names the Story that is missing. | Nothing by itself. The answer opens a form with the missing Story's title and description, to word as wanted, and the project's Epics with those most like it first. **Make this User Story** creates it under the chosen Epic and puts the issue under it (`POST …/orphans/{issue}/draft`). |
| The texts do not say | No decision is possible from the two texts. | Nothing. |

The answer quotes one passage of the issue and one of the chosen Story. The platform checks that the quoted words are in those texts; an answer that fails the check is shown with a note and is left out when answers are approved together. An answer about a text that changed since, or a shortlist that changed since, is marked and left out as well.

Nothing is linked by an answer. The administrator approves it, alone or with every other checked answer at once, links the issue to another Story of the shortlist, says it is a bug, or sets the answer aside. An issue stays on the list until it has a Story, is a bug or is closed.

A run is started by the administrator with a model and a token limit, and shows beforehand how many issues it asks about and how many tokens that is likely to use, counting the texts at four characters a token. While a question is asked it holds its whole size against the project's review tokens, and gives back what it did not use when the provider reports. It asks only about issues that have no answer to the current question, unless told to ask again about all. It counts against the project's review tokens like every review, stops at its own limit, and can be stopped, resumed and ended.

- A call the provider refused, or that no account could make, used nothing: what it held returns to the project at once.
- A call that ended without saying what it used gives its tokens back as well, and is counted on the run as a call whose use is unknown. A run is never left waiting for a call that will not report.
- When no account can be asked the run stops and waits to be resumed. After three failures in a row it stops as well.

`GET /api/dashboard/projects/{name}/orphans` returns the list, the counts, what a run would hold and the last run. `POST …/orphans/runs` starts a run, `…/orphans/runs/{id}/control` stops, resumes or ends it, `…/orphans/{issue}/link`, `…/bug` and `…/dismiss` record the administrator's decision on one issue, and `…/orphans/approve` approves many answers at once. The runs and answers are kept in `choice_runs` and `choice_reviews` (migration 0073), which also hold the runs that choose primary Epics.

### Bugs stand apart from the plan

A bug reports that something built is wrong. It is not a piece of a User Story's work, so it belongs to no User Story and, through none, to no Epic. The platform keeps the two apart everywhere:

| Where | What happens with a bug |
|---|---|
| Planning tabs | **Bugs** is a tab of its own beside Epics, User Stories and Issues. The Issues tab lists no bug and does not count them. |
| Recognising one | The administrator's word comes first (the issue's type set to Bug or Enhancement). Without it: the GitHub issue type; without that: a bug label, or a title that calls the issue a bug (`[Bug] …`, `bug: …`, `bug(scope): …`), unless the issue carries an enhancement label. A word such as "fix" in a title is no claim. |
| Linking | Nothing links a bug to a User Story. What the platform finds by itself (a Story named in the text, a match of a review, a proposal's parents) is skipped for a bug; saving a User Story for a bug by hand is refused with the way out: set the type to Enhancement first. |
| Reviews and worklists | A bug is no orphan and is not asked about. It is left out of the pairs to judge and of every measurement on **Reviews → Reorganize**; a pair drawn before its issue was known as a bug is set aside and its place drawn again. |
| Health | Bugs are counted on their own. The page follows how many still carry a link to a User Story. |

A bug can still carry a link from before it was known as a bug. The Bugs tab lists those with the User Story and who linked it, and takes the links off on the administrator's word, one bug or all at once (`GET …/bugs/linked`, `POST …/bugs/unlink`). Unlinking changes nothing else: the issue keeps its type and nothing is written on GitHub. The link's record is closed, not deleted, so the history of the issue still shows it. An issue wrongly taken for a bug is set to Enhancement in the table and keeps its User Stories.

The planning listing takes `bugs=only` or `bugs=without`, and `planning/counts` returns `bugs` beside `issues`.

### Consolidating the plan: one primary Epic for each User Story

Measured on Falcone before anything was built: 704 of 737 User Stories are under several Epics, 3.6 on average, because Stories were generated with the instruction to link each to all relevant Epics. The Epic that looked like a catch-all, with 273 Stories linked to it, is the primary Epic of 53. Counting each Story once, under its primary Epic, the largest Epic holds 57 Stories and the median Epic 9. Almost no two Stories say the same thing: one pair of 737 has texts more alike than 0.7, on a scale where 1 is identical. So nothing is merged and no Epic is split; what the plan lacks is a primary Epic for each Story that somebody chose, and counts that use it.

**Reviews → Planning** lists the User Stories under several Epics whose primary Epic nobody chose. Each is shown with its own Epics, the one the texts suggest first, with a likeness score and the words only that Epic shares with the Story. Where the best Epic scores at least one and a half times the next, the texts are said to point to one Epic; otherwise they leave it open. The suggestion is not accepted in bulk: the texts are compared by their words, and a Story about installing the Helm chart is not matched to an Epic about installation.

Where a Story's own text names exactly one of its Epics, on an `Epic:` or `Parent:` line, that Epic is listed first and no model is asked about the Story: **Confirm the Epics the Stories name** makes each of them the primary Epic at one word of the administrator (`POST …/primary-epics/confirm-stated`).

For every other Story a run asks a model one question: the Story, the Epics it is linked to, and three answers.

| Answer | Meaning | What approval does |
| --- | --- | --- |
| This Epic | The Epic whose goal the Story delivers most directly. An Epic about the capability the Story adds is preferred over one about a quality many Stories share. | Makes it the Story's primary Epic, as the administrator's choice, and keeps the review's reason and passages on the link. |
| None of them | The Story belongs under none of its Epics. | Nothing. |
| The texts do not say | No decision is possible from the texts. | Nothing. |

The answer quotes the Story and the chosen Epic, and the quotes are checked as for issues. The page says for each answer whether it names the Epic the texts suggest, and can list only those that differ. A run can be limited to the Stories whose texts leave it open. The administrator approves an answer alone or with every other checked answer, or makes any of the Story's Epics the primary one. No link is added or removed: the other Epics stay linked as related.

The Epics table shows under each Epic's count of linked Stories how many it is the primary Epic of. Planning health follows the largest Epic counted that way, and how many primary parents were chosen.

**Reviews → Planning** also lists the open User Stories every issue of which is closed, with those issues and whether a review found one that covers the whole Story. Such a Story is delivered, or what is left of it has no issue yet. Closing it is done in the User Stories table, where its effect is shown first.

`GET /api/dashboard/projects/{name}/primary-epics` returns the list, the counts, what a run would use and the last run. `POST …/primary-epics/runs` starts a run (`unclear_only` limits it), `…/primary-epics/runs/{id}/control` stops, resumes or ends it, `…/primary-epics/{story}/choose` with `epic_id` and `…/dismiss` record a decision on one Story, and `…/primary-epics/approve` approves many answers at once. `GET …/finished-stories` lists the Stories whose issues are all closed.

Both kinds of run, for issues and for Stories, are one mechanism (`parent_choice`): a kind says which records wait, what each is shown with and what is asked; the run, the token accounting, the checks of an answer and the controls are shared.

### Documentation inventory

**Reviews → Discovery** opens with the project's documentation as its repository holds it. A reading takes the tree of the repository at one commit, keeps the documents, and compares them with the reading before by the identity git gives each file's content, so only what is new or changed is fetched. No model is asked, and the same commit always reads the same.

A document is a README anywhere, a `.md`, `.mdx`, `.rst` or `.adoc` file at the root of the repository, or a file of those kinds or `.txt` in a folder that holds documentation. The platform's list of such folders is `docs`, `doc`, `documentation`, `adr`, `design`, `openspec` and `docs-site`; a project can have its own list, and folders or files that are left out. A name without a slash is a folder of that name at any depth, a name with a slash a path from the root. Changing the list reads the repository again.

The first reading of a project is made at the commit Discovery read, where there is one, so the next reading says what changed since. Afterwards the branch is looked at every quarter of an hour and read again when it moved; **Read now** asks for a reading at once. A reading that fails says why, and the branch is left alone for six hours unless a reading is asked for.

For each document that changed, the page shows whether it was added, modified or removed, how many lines, under which headings, and on opening it the changed lines themselves. A document in a folder Discovery does not read is marked: Discovery's own reading covers `docs`, `doc`, `documentation`, `adr`, `design`, READMEs and the root, and condenses what it reads into the project's description.

What a document's path and text say about what it is is read by rule and kept with it, as the keys that later join documents, issues and Stories:

- the OpenSpec change it belongs to (`openspec/changes/<name>/…`, archived when under `archive/`), or the capability (`openspec/specs/<name>/…`);
- the issue number in a change's name (`add-1069-…`, `fix-976-…`);
- how many tasks of a change's `tasks.md` are done;
- the identifiers it names, such as `GAP-FAL-008`, and the issues it refers to.

The page lists the OpenSpec changes with their state (open, every task done, archived), their progress, and the issues that name the change in their title or that the change is named after. It counts the changes no issue names and those whose issues are all closed.

`GET /api/dashboard/projects/{name}/documents` returns the overview; `…/documents/changes` the documents that changed since the first reading; `…/documents/files` the documents; `…/documents/file?path=` one document with its text, headings, facts and every change the readings found; `…/documents/openspec` the changes. `POST …/documents/scan` asks for a reading and `…/documents/folders` sets the folders. Readings, documents and changes are kept in `doc_scans`, `doc_files` and `doc_changes`, the folders in `doc_folders` (migration 0074).

### Measuring how a parent is found

**Reviews → Reorganize** shows, for each project, how well each way of ranking the candidate parents does against the parents that are known to be right. A right parent is known from a link the record's own text states, from a choice the administrator made, or from a pair the administrator judged. For an issue the parent is its User Story; for a User Story under several Epics it is its primary Epic. The table gives, for each way, how often the right parent is ranked first and how often it is among the first 5 and the first 20. A line of the text that names the parent is hidden from the ranking.

**Pairs to judge** draws about 150 pairs per project, once, from six situations: an issue and the Story it is linked to where the words agree, where the words point elsewhere, the most likely Story of an issue that has none; the suggested primary Epic of a Story where the texts point to it and where they do not, and the Epic of a Story that its text is least like. No record is asked about twice and a pair whose answer is already known is not drawn. The page shows the two texts side by side and the question; it does not show why the pair was drawn, so that a judgment does not lean on the platform's guess. An issue and a Story are judged as part of it, not part of it, or cannot tell; a Story and an Epic as primary, related, not related, or cannot tell. A judgment can be changed or taken back, and changes no link.

The pairs of one Epic in five are held out: they are only tested on, never learned from.

**The ranking service.** Two small open models run in the cluster as their own workload (`deploy/base/ranker.yaml`, `engineering-ranker`): `BAAI/bge-base-en-v1.5` gives each record's text a vector, so that records that mean alike lie close, and `Xenova/ms-marco-MiniLM-L-12-v2` scores how well two texts fit. The models are part of the image, fetched once when it is built. The service has no database and no key and may open no connection at all, so no text leaves the cluster through it, and a fixed model gives the same answer for the same text.

The console keeps what the service said: a vector for every Epic, User Story and issue (`record_vectors`), and a score for each candidate parent of the records that are measured or judged (`pair_scores`, migration 0076). A text is sent once; it is sent again only when it or the model changed. A line that names a record's parent, and identifiers, are taken out of the text first. The measurement then has four rows: shared words; meaning; the two joined by rank; and the reranker on the first fifteen candidates of each. Neither a vector nor a score decides anything: they are signals that the next phase combines.

`GET /api/dashboard/projects/{name}/signals` returns the measurement and the state of the ranking service; `POST …/labels/draw` draws the pairs; `GET …/labels/next` returns the next pair with both texts; `POST …/labels/{id}` records a judgment. The pairs are kept in `gold_pairs` (migration 0075).

### The scorer, in shadow mode

**Reviews → Reorganize** also shows what the scorer would do. The scorer combines every signal measured above into one judgment, learned from the links known to be right in all projects, and says for each record whether one candidate parent is left that it cannot rule out (it would act) or several or none (it abstains). The method, the signals and the error rates are in [planning-reorganization.md](planning-reorganization.md#the-scorer-r6).

It is computed in the console's process by a keeper that looks every two minutes whether anything it reads has changed, and computes at most every five. It reads the plan while holding no lock and writes only `scorer_reports` and `scorer_decisions` (migration 0077). Nothing reads those tables to change the plan: there is no route that applies a decision.

`GET /api/dashboard/projects/{name}/scorer` returns what it learned from, the measurement, the weights, the counts of what it would do and the first count for the clean-up. `GET …/scorer/decisions?kind=&outcome=&page=` lists the records it speaks of, the surest first, each with the parent, the share of belief and the signals.

The ranking service scores the candidates of every record, not only of the measured ones, where the scorer's measurement shows that the reranker's signals help (`Signals.wanted_for_all`), about 400 pairs a pass.

### Changes to approve

**Reviews → Reorganize** opens with the changes the scorer is sure of, gathered into batches by kind: record why links hold, choose primary Epics, place issues under a User Story, take off links nothing supports. The rules (sample, error allowed, undo) are in [planning-reorganization.md](planning-reorganization.md#batches-to-approve-r7).

A batch is made when the administrator asks to review a kind (`POST …/reorganize/batches`), which changes nothing in the plan. Its lines are read with `GET …/reorganize/batches/{id}`; a line is judged with `POST …/reorganize/batches/{id}/lines/{line}`; `…/approve` applies the lines not judged wrong once the sample is judged, `…/withdraw` sets the batch aside and `…/undo` puts an approved batch back within 30 days. `GET …/reorganize/batches` returns what is ready by kind, the open batches and the decided ones. Batches and lines are kept in `reorg_batches` and `reorg_lines` (migration 0078).

Applying uses the existing paths: `PlanningRecords.link_stories` and `PlanningLinks.noted` to place an issue (origin `scorer`), `PlanningLinks.set_primary` for a primary Epic, `PlanningLinks.explain` for a reason, and `PlanningRecords.remove_story_link` to take one link off, which may leave the issue without a User Story and keeps the platform from making the link again. Bugs are never in a batch.

Planning health gains two numbers under "Reorganization": the changes the scorer is sure of, and the records it abstains on.

### Where each User Story stands

**Reviews → Planning** shows one state for every open User Story: delivered, work closed, in progress, dropped, work not linked, planned, or nothing supports it. The rules are in [planning-reorganization.md](planning-reorganization.md#where-each-user-story-stands-r8). It replaces the earlier list of open Stories whose issues are all closed, which is now the first two states.

`Delivery.refresh` works the states out after each computation of the scorer and keeps them in `story_delivery` (migration 0079) with their evidence. `GET /api/dashboard/projects/{name}/delivery` returns the count per state; `GET …/delivery/stories?state=&page=` lists the Stories of a state with their issues, OpenSpec changes, leads and documents. The ranking service gives each document of the inventory a vector (`record_vectors`, kind `doc`).

The batch kind `close` on **Reviews → Reorganize** closes the delivered Stories on approval, through `PlanningLifecycle.preview` and `confirm`, and only where closing touches no open issue. Planning health gains "Where the User Stories stand": delivered Stories still open, Stories whose work is not linked, and Stories that nothing supports.

### Clean-up

**Reviews → Reorganize** lists, under "Clean-up", the records that reasons speak against, by reason, with how many of them are proposed for closing. The rule (two reasons of different kinds, or an exact one) and the reasons are in [planning-reorganization.md](planning-reorganization.md#the-clean-up-as-built-r9).

`GET /api/dashboard/projects/{name}/cleanup` returns the reasons with their counts and what a model's check would ask and use; `GET …/cleanup/records?kind=&show=&page=` lists the records with their reasons and evidence. `POST …/cleanup/runs` starts the check of the User Stories that nothing supports and `…/cleanup/runs/{id}/control` stops, resumes or ends it; it is the kind `story_needed` of the choice-run engine (`parent_choice.py`), counted under the workflow `cleanup_check`.

What is proposed appears as the batch kinds `retire_stories`, `retire_epics` and `retire_issues`. Applying a line closes the record through `PlanningLifecycle`, only where that closes nothing else. For an issue the queued closure (`workspace_actions`, migration 0080) carries the reason `not_planned` and a comment; the publishing worker writes the comment once (`GitHubApp.comment_workspace_issue`, found again by its marker) and then closes the issue. A batch of issues cannot be undone from here. Planning health gains "Records proposed for closing".

### Structure

**Reviews → Reorganize** ends with "Structure": how each Epic holds together, the Epics proposed to be split, the User Stories that are missing, and the documents no Story covers. The rules are in [planning-reorganization.md](planning-reorganization.md#the-structure-of-the-plan-r10).

`GET /api/dashboard/projects/{name}/structure` returns the report and the proposals. `POST …/structure/stories/{proposal}` makes a proposed User Story with the title, description and Epic sent, and links its issues; `POST …/structure/splits/{proposal}` splits an Epic with the titles sent; `POST …/structure/{proposal}/dismiss` sets a proposal aside. Decisions are kept in `structure_decisions` (migration 0081). A Story is made through `PlanningRecords.save`, its issues linked through `link_stories` and recorded as found by the scorer and approved by the administrator.

### Epic pages

`epic_pages.compose` puts the page of every Epic of a project together in one pass over the plan, the Stories' states, the issues and the documents' vectors, and keeps the result until one of those tables changed. `EpicPages.refresh` keeps a new row of `epic_pages` (migration 0082) for each page whose digest differs from its last version, with the changes in sentences, and drops versions beyond the last thirty. It runs after the scorer's keeper worked the Stories' states out, and when a page or the list is read.

`GET /api/dashboard/projects/{name}/epic-pages` lists the Epics with their numbers, whether an overview is written and whether the records changed since, and what a run would ask. `GET …/epic-pages/{epic}` returns the page, its last ten versions and its overview; `GET …/epic-pages/{epic}/markdown` returns it as a Markdown document. `POST …/epic-pages/runs` starts a run of kind `epic_overview` in the choice-run engine (usage counted under `epic_page`), and `POST …/epic-pages/runs/{run_id}/control` stops, resumes or ends it. The answer is kept as the review of that Epic: the sentences with their sources as its evidence, `checked` when no sentence had to be dropped. The console shows the pages under Reviews → Epic pages.

### Across projects, and as records arrive

`GET /api/dashboard/rollout` returns one row for every project: whether it has a plan, what the ranking service has read, what the scorer is sure of and abstains on, the pairs judged, what moved since the day before, and the next steps in order. The Administrator Console shows it as "Planning across projects". `GET …/projects/{name}/rollout` is one project's row and `GET …/projects/{name}/arrivals?days=` the issues that arrived lately with where each stands; both are the first card of **Reviews → Reorganize**.

`keep_reorganization_fresh` works the scorer, the Stories' states and every planned project's health out again once a day after 02:00 UTC. `planning_drift.drift` compares a project's latest two measurements, and `digest_lines` gives the chat channel's daily digest a line for each project where something waits or moved. Planning health now also keeps `changes_ready`, `structure_proposals` and `issues_unplaced`.

### A review run that was cut off

A paid call whose provider never reported its usage leaves its run **uncertain**: its tokens stay reserved against the run and the project, and a later review of the same issue and Story is held with "An earlier decision for these sources has unconfirmed usage".

- When the provider refuses the request before doing any work, because it does not accept the key or rejects the request itself, nothing was used. The run ends as failed with the reason and holds nothing.
- When the provider was busy or did not answer, it may have done the work. **Reviews → Related Work** lists these runs with what each holds. **Release** ends the wait: the reserved tokens return to the project's allowance, the run ends as interrupted, and what the call used stays unknown and is not counted. A reply that still arrives afterwards is ignored.
- A run that cannot be continued three times in a row, for a reason that is not its own input, is ended as failed so that the runs of other projects are not kept waiting behind it.

The Discovery page asks for its status with `brief=true`, which leaves the texts of the proposals out; they are read in the planning tables.
