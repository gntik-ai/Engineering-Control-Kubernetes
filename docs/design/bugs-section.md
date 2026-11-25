# A section for bugs

Proposal, 25 November 2026. Phase B1 is built (25 November 2026); B2 to B5 are not.

## What is asked for

A place in the platform where bugs are added and managed on their own: a bug is created as an issue with the `bug` label, and it is never linked to a User Story or an Epic.

This follows the decision of 24 November: a bug is no part of the plan. That decision took bugs out of the plan. This proposal gives them a home.

## What exists today

| Piece | State |
| --- | --- |
| Knowing that an issue is a bug | Built. By your word first, then by its GitHub issue type, then by a `bug` label or a title that calls it one. |
| A list of bugs | Built, as the Bugs tab of a project's page: open, closed and drafts, with close, edit and "bug or enhancement". |
| Reporting a bug from the console | Built, but slow. "New Bug" makes a proposal, the proposal waits for an automatic review, you approve it, the issue is created on GitHub, and the `bug` label is added afterwards by a second step. |
| Bugs of a project without a plan | Missing. The tab reads the plan's records, so a project that was never through Discovery shows no bugs, although its repository has them. |
| Severity, priority | Missing. An issue has no such field. |
| Triage: new, confirmed, duplicate, won't fix | Missing. A bug is open or closed, nothing more. |
| Finding duplicates | Missing for bugs. The ranking service reads no bug, because bugs were taken out of everything that looks for a parent. |
| Fixing | Only the general flow "Deliver issue". Nothing asks for a test that fails before the fix and passes after it. |
| Numbers | Planning health counts bugs. Nothing says how many are open by severity, how old they are, or how long a fix takes. |
| Reporting from the chat | Missing. The supervisor can propose an item; it cannot report a bug. |

On live today: Falcone has 271 bugs, 2 open and 269 closed, none linked to a User Story any more. Falcone-serverless has none. The four projects without a plan show none because of the gap above.

## Principles

1. **A bug stands apart from the plan.** No User Story, no Epic, in no stage of the reorganization. What a bug may point to is another bug (a duplicate), the pull request that fixed it, the change that caused it, and an area of the code.
2. **A bug lives on GitHub.** It is an issue with the label `bug`. The platform adds what GitHub does not have (severity, triage, duplicates, a flow that fixes it) and writes to GitHub only labels, comments and closures.
3. **Nothing leaves without your word.** Reporting a bug is your word for creating its issue. Closing, labelling and starting a fix are each a button you press. A model is asked only where you start it.
4. **Every project has it.** With or without Epics and User Stories.

## The section

### Where it is

A **Bugs** entry in each project's menu, beside Queue and Reviews, with its own page. It replaces the Bugs tab.

### Reporting a bug

One form, one step:

- Title.
- What happens, what was expected, how to reproduce it. Three short fields that become the issue's text under fixed headings, so that every bug reads the same and a flow can find the steps.
- Where it was seen: the version or commit, and the area of the code (optional).
- Severity.

While you type the title, the open bugs most like it are shown. If one is the same bug, you add your note to it and nothing new is created.

**Report** creates the issue on GitHub with the `bug` label and the severity label in the same step. There is no proposal, no review and no approval between: you wrote it, and it is not a planning record. A bug that someone opens directly on GitHub arrives by itself within a quarter of an hour and stands as New.

### The life of a bug

| State | Meaning | On GitHub |
| --- | --- | --- |
| New | Reported, nobody looked yet | Open, label `bug` |
| Confirmed | It is a bug and has a severity | Open, severity label |
| In progress | A fix is being worked on: a run is under way or a pull request is open | Open, the pull request refers to it |
| Fixed | The fix is merged | Closed as completed |
| Duplicate | The same as another bug, which is named | Closed as not planned, with a comment that names the other bug |
| Won't fix | Real, and not going to be fixed; the reason is kept | Closed as not planned, with the reason as a comment |
| Not a bug | It is work or a question, not a fault | The `bug` label is taken off |

"Not a bug" has two exits: the issue becomes ordinary work, and then waits for a User Story like any other issue; or it is closed. The opposite path exists already: an issue in the plan can be marked as a bug, and it then leaves the plan.

### What a bug carries

- **Severity**: critical, high, medium or low. Set by you; never guessed silently.
- **Area**: a short name such as `console`, `api` or `billing`, chosen from the areas the project already uses. It is what lets bugs be counted by where they are, without an Epic.
- **Seen in**: a version or a commit.
- **Duplicate of**: another bug.
- **Fixed by**: the pull request that closed it.
- **Caused by**: the pull request or commit that introduced it, when known. This is what makes a bug a regression.

### Working the list

Filters by state, severity, area and age; search; the oldest and the most severe first. Several bugs can be confirmed, given a severity or closed at once. The page opens with the numbers: open by severity, new and not yet looked at, the age of the oldest open bug.

### Fixing a bug

A flow of its own, **Fix a bug**, built from the tasks the engine already has:

1. Read the bug and find the files it is about.
2. Write a test that reproduces it and see it fail. A bug that cannot be reproduced stops here and says so on the bug: that is an answer too.
3. Fix it. The new test passes and the rest of the suite still does.
4. Open a pull request that says it fixes the issue, wait for GitHub's checks, and merge on your word, as "Deliver issue" does.

The bug then closes on GitHub by the merge, and its state follows. A fix is started from the bug's row, or the open bugs are queued in order of severity. Bugs get their own section in the engine, so that this flow is theirs and "Issues" keeps the tasks.

### Seen elsewhere

- The Dashboard and the table of all projects show the open bugs by severity.
- The daily digest says what arrived and what was fixed; a new critical bug is told at once in the chat.
- "Report a bug: …" in the chat creates one, after showing you what it understood.

## Phases

Each phase is deployed and usable on its own.

| Phase | What | Done when |
| --- | --- | --- |
| B1 | **The section and reporting.** The Bugs page for every project, read from the repository's issues and not from the plan. Reporting in one step, with the label set at creation. Edit, close with a reason, reopen. "Not a bug" and "It is a bug". The Bugs tab goes. | A bug reported in the console is an issue with the `bug` label on GitHub within a minute. A project without a plan lists its bugs. Nothing about a bug mentions a User Story. |
| B2 | **Triage.** States, severity, area, seen in. Similar bugs while reporting and for bugs that arrive, found by the ranking service with no language model. Closing as duplicate or won't fix writes the comment on GitHub. Severity mirrored as a label. Several bugs at once. | Every open bug has a state and, once confirmed, a severity. A duplicate is found before it is created. GitHub shows the same severity as the platform. |
| B3 | **Fixing.** The flow "Fix a bug" with a test that fails before and passes after. A section of the engine for bugs. Start from a row, or queue by severity. "Fixed by" and the state follow the pull request. | A bug goes from Confirmed to Fixed through a merged pull request that contains its test, started with one button. A bug that cannot be reproduced says so. |
| B4 | **Seen and told.** Numbers: open by severity and area, age, time from report to fix, reopened bugs, regressions. Dashboard tiles, the table of all projects, digest lines, the alert for a critical bug, reporting from the chat. | The numbers match the list. A critical bug reaches the chat within minutes. |
| B5 | **Help with triage, on your word.** A run in which a model suggests severity, area and a likely duplicate for the bugs that are New, and says what is missing from a report. You accept or change each suggestion. Optional: a failing check on the default branch becomes a draft bug. | Suggestions are shown with their reasons and nothing is set without you. |

B1 and B2 change no flow and spend no model tokens.

### B1 as built

- **The page.** Bugs is an entry of every project's menu, beside Queue. It lists the project's bugs from its issues and not from its plan: open, closed or all; the newest, the oldest or the last changed first; search by title or number. The Bugs tab of the project's page is gone, and a link to it leads to the section.
- **Reporting.** One form: the title, what happens, what was expected, how to reproduce it, where it was seen. Each part that is filled in becomes a heading of the issue's text. **Report** creates the issue with the `bug` label in the same request to GitHub, and the list shows it with its number at once. Where GitHub cannot be reached the bug is kept, shown as being created, and created by the worker that already retries what GitHub did not take. A bug with the same title as an open one is refused; looking for bugs that are merely alike is B2.
- **Changing.** Title and text are changed on GitHub and kept here as GitHub answered. If the text was changed on GitHub since it was read here, nothing is overwritten: it is read again and the change is made once more.
- **Closing and opening again.** Closed as fixed or as not going to be fixed, with an optional comment left on the issue; GitHub is told "completed" or "not planned". It goes through the same confirmed closure as every other issue the platform closes. A closed bug is opened again from its row.
- **Not a bug.** The issue becomes ordinary work by your word, and its `bug` label is taken off. The opposite, "It is a bug", stays where it was: on an issue of the plan.
- **Not in B1.** States beyond open and closed, severity, area, duplicates and bulk changes (B2). The numbers on the Dashboard (B4).
 B3 spends what a delivery run spends. B5 is the only phase that asks a model about bugs in bulk.

## What changes underneath

- **Records.** One new table for what a bug carries beyond its issue (state, severity, area, seen in, duplicate of, fixed by, caused by, who decided and when). The issue itself stays where it is.
- **GitHub.** Creating an issue with its labels in one call; adding and removing a label by name; the labels `severity: critical` to `severity: low` created where missing. No new permission: the platform already writes issues, comments, labels and closures.
- **Ranking service.** Bugs are read as their own kind, compared only with other bugs. They stay out of everything that looks for a User Story.
- **Engine.** A seventh core section, Bugs, with the labels `bug`, `defect` and `regression`; the section Issues keeps `fix` and tasks. One new flow.
- **Unchanged.** The bug rule: no code path gives a bug a User Story or an Epic, and the reorganization keeps leaving bugs out.

## Decisions needed

| # | Question | Recommended |
| --- | --- | --- |
| 1 | Does **Report** create the GitHub issue at once, or keep a draft until you publish it? | At once. You wrote it; a draft adds a step and nothing else. |
| 2 | Is severity also written to GitHub as a label? | Yes, as `severity: high`. The triage state is not: open or closed with its reason says enough there. |
| 3 | Do bugs get their own section in the engine, apart from Issues? | Yes, in B3. It is what gives them their own flow and queue. Issues already routed stay where they are until you move them. |
| 4 | May a fix ever start by itself? | Not at first. Later it can be allowed for one severity, off by default. |
| 5 | Severity scale | Four levels: critical, high, medium, low. |

## Not proposed

- A link of any kind between a bug and a User Story or an Epic.
- Assignees and due dates: there is one administrator, and the queue says what is next.
- A public form for people outside to report bugs: they report on GitHub, and the bug arrives from there.
