# A section for bugs

Proposal, 25 November 2026. Phases B1 and B2 are built (25 November 2026), B3 and B4 on 27 November 2026; B5 is not.

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
| B3 | **Fixing** (built, 27 November 2026). The flow "Fix a bug" with a test that fails before and passes after. A section of the engine for bugs. Start from a row, or queue by severity. "Fixed by" and the state follow the pull request. | A bug goes from Confirmed to Fixed through a merged pull request that contains its test, started with one button. A bug that cannot be reproduced says so. |
| B4 | **Seen and told** (built, 27 November 2026). Numbers: open by severity and area, age, time from report to fix, reopened bugs, regressions. Dashboard tiles, the table of all projects, digest lines, the alert for a critical bug, reporting from the chat. | The numbers match the list. A critical bug reaches the chat within minutes. |
| B5 | **Help with triage, on your word.** A run in which a model suggests severity, area and a likely duplicate for the bugs that are New, and says what is missing from a report. You accept or change each suggestion. Optional: a failing check on the default branch becomes a draft bug. | Suggestions are shown with their reasons and nothing is set without you. |

B1 and B2 change no flow and spend no model tokens.

### B1 as built

- **The page.** Bugs is an entry of every project's menu, beside Queue. It lists the project's bugs from its issues and not from its plan: open, closed or all; the newest, the oldest or the last changed first; search by title or number. The Bugs tab of the project's page is gone, and a link to it leads to the section.
- **Reporting.** One form: the title, what happens, what was expected, how to reproduce it, where it was seen. Each part that is filled in becomes a heading of the issue's text. **Report** creates the issue with the `bug` label in the same request to GitHub, and the list shows it with its number at once. Where GitHub cannot be reached the bug is kept, shown as being created, and created by the worker that already retries what GitHub did not take. A bug with the same title as an open one is refused; looking for bugs that are merely alike is B2.
- **Changing.** Title and text are changed on GitHub and kept here as GitHub answered. If the text was changed on GitHub since it was read here, nothing is overwritten: it is read again and the change is made once more.
- **Closing and opening again.** Closed as fixed or as not going to be fixed, with an optional comment left on the issue; GitHub is told "completed" or "not planned". It goes through the same confirmed closure as every other issue the platform closes. A closed bug is opened again from its row.
- **Not a bug.** The issue becomes ordinary work by your word, and its `bug` label is taken off. The opposite, "It is a bug", stays where it was: on an issue of the plan.
- **Not in B1.** States beyond open and closed, severity, area, duplicates and bulk changes (B2). The numbers on the Dashboard (B4).

### B2 as built

- **States.** New, confirmed, fixed, duplicate, not going to be fixed. A state is worked out from the bug's issue and from what was decided here, and is not kept beside them, so it cannot differ from GitHub: open with no severity is new, open with a severity is confirmed, closed as completed is fixed, closed with a duplicate named is a duplicate, closed as not planned is not going to be fixed. A bug closed before the reason was kept shows as closed. "In progress" comes with B3, when a fix can be started from a bug.
- **Severity.** Critical, high, medium or low, given when the bug is reported or in its triage. Giving one confirms the bug. It is put on the issue as the label `severity: high`, with the other severity labels taken off, and the label is made in the repository where it is missing. GitHub is written first: where it does not take the label, nothing is changed here. A severity label that somebody set on GitHub is read as the bug's severity; the bug stays new until you confirm it.
- **Area and where it was seen.** Kept here. The area is offered from those the project's bugs already have, and an `area: …` or `component: …` label on the issue is read as its area. The area is not written to GitHub.
- **Alike.** While a bug is being written, the open bugs that have at least half of its title's words are shown, the ones sharing the rarer words first. The ranking service reads every bug as its own kind, compared only with other bugs: an open bug is shown with up to three open bugs that lie as close to it in meaning as 0.9, as possible duplicates. No language model is asked, and likeness closes nothing.
- **Duplicates.** A bug is closed as the duplicate of another, named by its number. GitHub gets the comment "Duplicate of #n", with your note under it, and the issue is closed as not planned. Opened again, it is no duplicate any more.
- **Several at once.** Bugs are selected in the list and given a severity together, or closed together as fixed or as not going to be fixed. Each is done on its own: one that GitHub does not take is told, and the others are done.
- **The list.** Narrowed by state, severity and area, and ordered by severity as well as by age. The page opens with the open bugs by severity and the count of new ones nobody looked at.

B3 spends what a delivery run spends. B5 is the only phase that asks a model about bugs in bulk.

### B3 as built

**The flow "Fix a bug".** One of the platform's own flows, version 0 of `fix-bug`, built from the tasks the engine has and one new one.

| Step | Task | What it does | Where it goes on |
| --- | --- | --- | --- |
| prepare, install, tests | Clone, Install, Run the tests | As in Deliver issue: the checkout, the installation, and what already fails before anything is changed | reproduce |
| reproduce | Agent: change the code | An agent is told to fix nothing and to write one test, in a file of its own, that fails for the reason the bug describes. It is told to run it and see it fail, and to answer that it cannot where it cannot | shown; an agent that cannot ends the run, with its reason |
| shown | **Run the bug's test** (new) | The tests the run wrote are run, and nothing else. No agent is asked | fails: fix. Passes, or no test: the flow ends, the bug is not shown |
| fix | Agent: change the code | The same subscription is told to fix the bug so that the test passes, to leave the test as it is, and is given what the test said | fixed |
| fixed | Run the bug's test | The same files have to pass now | passes: review. Fails: back to fix, which is a round |
| review | Review the change | The other subscription reads the whole change, test and fix | pass: verify. Changes: back to fix |
| verify, scan, bundle | Run the tests, Scan for secrets, Collect the change | As for any change | approve |
| approve, publish | Wait for the administrator, Open a pull request | The run waits for your word. The pull request says "Resolves #n" | the flow ends |

Three rounds at most, as in Deliver issue. The two instructions are settings of the flow: a later version, made in the designer, can word them otherwise.

**The task "Run the bug's test"** (`check.reproduction`, among the deterministic checks). It runs in the run's workbench, without the network and without an agent.

- **Which tests.** The first time: the test files the run added or changed, and none that only cover what was changed. They are kept with the run, and the same files are run after the fix.
- **How.** Each by the repository's own runner: pytest, vitest, jest, node --test, go test. The flow can name commands for it instead, where a repository needs a build first.
- **Where none can be run alone**, as in a Rust project: the project's own test commands are run, every one, and compared command by command with before anything was changed. A command that failed before as well says nothing of the bug.
- **Before the fix** a failing test is what shows the bug, and what it said is given to the agent that fixes. A test that passes shows nothing: the step is told as "not reproduced". **After the fix** the tests have to pass.
- It does not stand in for Run the tests: a flow that has only this cannot publish.

**A section of the engine.** Bugs is the seventh core section of every project, with the labels `bug`, `defect` and `regression`; Issues keeps `fix`. For the six projects of the registry its flow is Fix a bug. When the section is added to a project that was there before, those three labels leave whichever section had them, since a label sends work to one section; and the description of Issues is replaced where it was still the platform's own words. A section named Bugs that you made yourself would be left alone, and none is added beside it.

**From the Bugs page.**

- **Fix**, on the row of a confirmed bug. The bug goes to the section Bugs, by your word, and the section's flow starts for it; the page opens the run. A new bug is given a severity first. One fix of a bug runs at a time.
- **Queue them, the most severe first**, where more than one confirmed bug waits. It makes a queue of the confirmed bugs that have no fix under way and are in no queue: critical first, and of one severity the oldest first. The queue is a draft: nothing starts until you start it on the Queue page, where it runs within the budgets like any queue.
- **In progress** is a state now: a run is under way, or a pull request a run opened is open. The row says which, with a link to the run and to the pull request. A fix that waits for your decision says so.
- **Fixed by.** The pull request resolves the bug's issue. When it is merged, GitHub closes the issue as completed; the next reading of the repository says so here, and the bug is fixed, by that pull request.
- **Not reproduced.** Where no test could show the bug the row says so, with what the run said: the check's sentence, or the agent's reason where it could not write the test at all. The bug is confirmed again, and a fix can be started anew.
- Nothing of this is kept beside the run and the issue: where fixing stands is read from the bug's latest run and from the pull request the run noted on the item.

**Different from the proposal.**

- **The platform does not merge.** The proposal said "merge on your word, as Deliver issue does". Deliver issue, as it is in force, ends at the pull request, and so does Fix a bug: you merge on GitHub, and the bug follows. Waiting for GitHub's checks and merging are two tasks the engine has; they can be added to a version of the flow in the designer.
- **Bugs that carry the label move.** The proposal said issues already routed stay where they are until you move them. A label decides the section at every reading of the repository, so an open bug that carries `bug`, `defect` or `regression` and that nobody routed by hand goes from Issues to Bugs at the next reading. Closed bugs, and bugs you routed yourself, stay where they are. A bug that is one by its title or its GitHub type alone is not routed by a label; Fix puts it in Bugs.
- **Nothing is written to GitHub about a bug that was not reproduced.** It is said on the bug's row here.

**Limits.**

- A pull request that is closed on GitHub without being merged leaves the bug in progress here until a new fix is started or the bug is closed: the platform does not read the state of pull requests.
- A test added to an existing test file is run with the whole file. The agent is told to write a file of its own.
- Not exercised on live: no fix has been started there. The flow is covered by a run in tests up to the administrator's decision, with stand-ins for the sandboxes and the workbench.

Added 25 November 2026: [inkling-models.md](inkling-models.md) plans a cheaper model for the reviews. B5's suggestions are one use for it. The flow B3 builds writes code, and keeps the models that write code here: Inkling is not a default for it.

### B4 as built

**The numbers** (Bugs page, "In numbers"; `bug_numbers.py`, `GET /api/dashboard/projects/{name}/bugs/numbers`). Counted from the same records and in the same way as the list, so a number and the list it leads to cannot differ. No model is asked and nothing is kept: they are counted when the page asks.

| Number | What it counts |
| --- | --- |
| Open bugs | With how many are new, confirmed and in progress |
| By area and severity | A table of the open bugs: one row an area, one column a severity, and a row for those with no area yet. Twelve areas are named, the fullest first |
| Age | Of the oldest open bug, and the age half of the open bugs are over |
| Arrived and fixed | In the last 30 days. Arrived is the day it was opened on GitHub; fixed is the day its issue was closed as completed |
| From the report to the fix | The middle of the bugs fixed in the last 90 days, and the longest |
| Regressions | Bugs that a change broke: what caused it is named, or the issue carries the label `regression`. Open, and in all |
| Came back | Bugs closed and opened again, whether here or on GitHub. Open, and in all |

A number that counts open bugs has "Show them": the list below is narrowed to exactly what it counts. The list can now be narrowed to the bugs with no area, to the regressions, and to those that came back.

**What caused a bug** is a new thing a bug carries, set in its triage: the pull request or commit that introduced it, when known (`bug_records.caused_by`, migration 0094). Naming one makes the bug a regression. It is kept here and not written to GitHub. The proposal listed it; B2 had left it out.

**Came back** is kept from 27 November 2026: when a closed bug is opened again, the item says so and counts how often. What was reopened before that day is not known.

**Seen elsewhere.**

- **A project's dashboard** has a card "Bugs": the open ones by severity, the new ones, those in progress, the age of the oldest, what arrived and was fixed in 30 days, and how long a fix took.
- **The table of all projects** on the administrator's dashboard says each project's open bugs in a line, by severity, with a link to its Bugs page. A project without a plan has the line too.
- **The daily digest** has a line for each project in which a bug arrived or was fixed since the day before, or a critical one is open: how many arrived, how many were fixed, and what is open by severity.
- **`/bugs`** in the chat answers with the open bugs of every project, by severity. The platform answers it itself; no model is asked.

**A critical bug is told in the chat**, once: an open bug that a label on GitHub calls critical (`severity: critical`) and that nobody gave a severity here. One you called critical yourself, here or when you reported it, is no news to you and is not told. The chat looks once a minute; issues are read from GitHub every few minutes, so it is told within minutes of being opened or labelled there. The message links to the issue and to the project's Bugs page.

**Reporting from the chat.** "Report a bug in falcone: the export leaves out the last row" goes to the supervisor like anything written there. It has a new tool, `report_bug`, with the parts a report has: title, what happens, what was expected, how to reproduce, where it was seen, severity. It is told to pass on what you said and to make nothing up. The bug is never created at once, whatever the supervisor's settings say: you are shown what was understood, with the open bugs whose titles look alike, and accept or reject it with a button, in the chat or in the console's inbox. Accepted, the bug is kept as you would have reported it on the Bugs page and its issue is created on GitHub, with the `bug` label and its severity, by the worker that writes to GitHub, within half a minute. A bug with the title of an open one is refused.

**Limits.**

- Understanding what you wrote is the supervisor's, which is a model on one of your subscriptions: it spends a turn of it, as any message to the supervisor does. Everything else in B4 asks no model.
- The alert does not tell a bug that became critical by your own word, and knows no severity GitHub does not carry as a label.
- "From the report to the fix" counts bugs whose issue was closed as completed, whoever closed it and whether or not a pull request did.
- Not exercised on live: no critical bug arrived, and no bug was reported from the chat, since that asks the supervisor and creates an issue.

## What changes underneath

- **Records.** One new table for what a bug carries beyond its issue (state, severity, area, seen in, duplicate of, fixed by, caused by, who decided and when). The issue itself stays where it is.
- **GitHub.** Creating an issue with its labels in one call; adding and removing a label by name; the labels `severity: critical` to `severity: low` created where missing. No new permission: the platform already writes issues, comments, labels and closures.
- **Ranking service.** Bugs are read as their own kind, compared only with other bugs. They stay out of everything that looks for a User Story.
- **Engine.** A seventh core section, Bugs, with the labels `bug`, `defect` and `regression`; the section Issues keeps `fix` and tasks. One new flow, and one new task for it.
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
