# Project workspaces

The Project is the workspace for a single repository. The Superadmin signs in to the global default Project. An administrator can use the existing global tools through **Administrator Console**.

| Phase | Deliverable | Acceptance criteria |
| --- | --- | --- |
| 0 | Baseline and migration rehearsal | Inventory the live Argo CD revision, schema, existing projects, issue states and completion dates. Rehearse both a physical database upgrade and a fresh install. Trace stored token usage, model prices and the source of settled charges separately. |
| 1 | Project identity and relationships | Persist name, description, repository identity and Architecture. Select Falcone on the existing installation, or the first project created on an empty installation. Support one changeable global default. Keep project-owned Epics, User Stories and technical Issues with same-project relationships. |
| 2 | Authentication and navigation | Every fresh sign-in, including a visit from a bookmark, opens the current default. Authenticated deep links still work. The gntik-ui project switcher and top-right Administrator Console link are usable at desktop and mobile widths. Existing global sections remain accessible. |
| 3 | Project tabs and planning | Home shows the name, description and repository URL. Epics and User Stories have full CRUD. Every Story lists all its Epics, and every workspace Issue belongs to at least one Story. Reject changes that would orphan an active record. Architecture is an empty fifth tab. |
| 4 | Repository analysis and Description review | Start from a new name and repository URL or analyze an already-connected project in place. Save the source commit, documentation inventory, code context and all pages of open and closed issues, excluding pull requests. Resume interrupted scans. Generate an editable Description. Approval unlocks Epic analysis. |
| 5 | Epic and Story review | Analyze existing GitHub planning issues, import the valid ones and generate missing coverage. When no Epic exists, generate Epics individually. Approve Epics before generating Stories. Review every Story's Epic links. Commit each accepted local identity before closing an open planning issue. Already-closed planning issues stay closed. |
| 6 | Technical Issues and GitHub approval | Map open and closed technical issues to one or more approved Stories, retain the original issue number, state and closure date, and generate local drafts for uncovered requirements. Allow the Superadmin to edit mappings. Only an explicit approval may publish a new issue. Store the approval and reconcile interrupted GitHub writes by identity. Pending work must be excluded from manual starts, queues, triggers and Supervisor execution. |
| 7 | Project Home statistics | Provide two daily charts for the last 7 or 30 UTC calendar days. Count resolved technical Issues using their actual closure dates. Plot measured tokens and finalized EUR charges together with separate units. Show missing billing coverage explicitly and distinguish it from a confirmed zero. |
| 8 | Acceptance and release | Exercise an empty project and a project with mixed open and closed records. Test cross-project isolation, changed default, interrupted imports, approval retries, conflicting deletions and legacy automation. Build via GitHub Actions and verify the Argo CD revision, migration hook and application readiness. |

## Evidence needed before enabling external effects

The normal GitHub scanner uses read permissions. Publishing a technical issue and closing an approved planning issue each use a short-lived Issues write token scoped to that repository. On 18 November 2026 the installed app created, read back and closed Falcone issue 1649 as `engineering-control[bot]`. The acceptance check used a repository-scoped Issues write token. [GitHub record](https://github.com/gntik-ai/falcone/issues/1649)

Model prices per million tokens live in the provider account records. The usage service multiplies those rates by reported tokens and also accepts client-reported cost. Neither source establishes a finalized EUR charge for a particular project and day. Verify a settled entry from the billing feed with its project attribution, date, currency, amount and source identifier before marking spend available. Replayed entries must preserve totals.

## Preservation and recovery

Keep the existing project identifiers, repository identity, historical issue states and completion dates. Importing a technical issue may attach it to Stories without reopening or reclosing it. Closing an imported Epic or Story is a planning action and must be excluded from resolved-Issue counts. Keep a stable mapping from repository and issue number to the local record.

Persist each review stage and its approved inputs. A restart must resume at the last committed stage. A timeout after GitHub receives a create request requires reconciliation before another request can create an issue. Read-only scanning and local review remain usable when a write grant or a billing source is unavailable.
