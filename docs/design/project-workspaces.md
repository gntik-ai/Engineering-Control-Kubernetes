# Project workspaces

The Project is the workspace for a single repository. The Superadmin signs in to the global default Project. An administrator can use the existing global tools through **Administrator Console**.

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


## Project connections

Open Settings → Connections in the project sidebar. Dashboard opens the five project tabs. The shell uses the gntik-ui sidebar rail, collapsible Settings group and mobile drawer. A project can depend on several projects and act as a dependency for others. Each connection appears in both projects. Disconnect archives the relationship and retains its decision history.

A connection checks repository-qualified GitHub references in the retained issue history, including closed issues. It shows the original reference while planning is incomplete, then resolves it to an approved local record. The Issues table supports manual links to the other project or to a specific record, a connected-project filter and links back from the target.

When both sides have approved planning, select a model from Accounts for a comparison. The worker reviews records from each project against relevant planning records from its peer. It saves progress and decisions, and checks for new evidence. Analysis usage is attributed to the project that established the connection. Administrators can accept a link or a local edit, dismiss a suggestion, or send missing work to the owning project's planning table. That table handles approval and GitHub publication even after its repository import has completed.
