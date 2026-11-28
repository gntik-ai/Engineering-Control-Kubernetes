# Reviews in two pages: Organize and Coverage

Proposal, 27 November 2026, asked for the same day. The eight decisions at the end were taken that day, all as recommended. **Every phase is built and deployed: O0, OC and O1 to O6** (each has its "as built" section below). On the same day the administrator added that connected projects have to be taken into account as if they were part of the project: the plan was adjusted, with one more phase (OC) before O1. See "Connected projects", at the end.

## What is asked for

Reviews has too much on it to see what is happening or what a thing is for. It is to become three pages:

1. **Organize**, with two buttons. One checks and organizes Epics and User Stories; the other, User Stories and Issues. Each shows what it did and the ambiguous cases that need you. After a careful review, what has no counterpart gets one made: an Epic for Stories that have none, Stories for an Epic that has none, Issues for a Story that has none, a Story for Issues that have none.
2. **Coverage**: the documentation is checked against the Epics, User Stories and Issues, and where it is not covered in full, the missing records are made.
3. **Settings**, which stays.

Only Issues are reflected in GitHub. The work is done by several models, reusing what exists, and the aim is the most reliable and secure result.

## What Reviews is today

Seven views and twenty panels. Every one was built for a reason, and each has its own button, list and vocabulary. That is the problem: the engines are sound, and there are twenty doors to them.

| View | Panels | What each is, in a line |
| --- | --- | --- |
| Needs you | The list; what happened without you | Rows that point to the other views |
| Related Work | Issues with no User Story; Review evidence | A check that asks two models; verdicts to review |
| Planning | Primary Epics; where each Story stands; automatic planning reviews | A check; a state from evidence; a queue of reviews |
| Discovery | Documentation; the import of a repository | An inventory with history; a wizard in stages |
| Reorganize | Changes to approve; extra Epic links; clean-up; structure; arrivals; how well a parent is found; pairs to judge; what the scorer says; try a model; measure a judge | Batches, three more checks, and five panels of measurement |
| Epic pages | A page for each Epic | Written from the plan, each sentence checked |
| Settings | Models, makers, limits, numbers, what runs by itself | Stays |

## The proposal

### Two pages, one shape

Both pages work the same way, so that learning one is learning both.

1. **One sentence on top** says where things stand: "412 User Stories: 371 under one Epic, 29 in doubt, 12 with none."
2. **One button** starts a run. Before it starts it says what it will ask and what that will cost at most, and waits for your word.
3. **A run goes through fixed stages**, shown as a line of steps with a count under each: Read, Match, Verify, Create, Done. You can stop it; it stops by itself at the limits of Settings.
4. **Three lists of results**, and nothing else on the page:
   - **Needs you**: the ambiguous cases, the most doubtful first. Each shows the record, the candidates, what each model said and why, and buttons for the answers. This is the only list you have to work through.
   - **Done**: what was linked or corrected, each with its reason. It can be undone, one line or the whole run, for 30 days.
   - **Created**: the records the run made, each with what it was made from and why nothing existing fitted. Undoing one takes it away again.
5. **Details on request.** How a decision was reached (signals, the scorer's share, the two answers) opens from a line. It is not on the page otherwise.

The words are the same everywhere: a record is *settled*, *in doubt* or *has none*. Today's words (orphan, primary, extra link, unsupported, abstains, shadow) leave the console.

### Organize

**Epics and User Stories.** After a run every open User Story has exactly one Epic it belongs to, and every open Epic has at least one User Story.

| Case | What the run does |
| --- | --- |
| A Story under one Epic, and the review agrees | Nothing. Counted as settled |
| A Story under several Epics | Chooses the one it belongs to. The others stay as "also related" only where both models find them related; the rest are taken off |
| A Story under an Epic that the review finds wrong | Moves it, where the review is sure; else in doubt |
| A Story with no Epic | Looks for the Epic it belongs to. Found and sure: linked. Not sure: in doubt |
| Stories that fit no Epic, after the review | Grouped by what they are about, and an Epic is made for each group, not one for each Story |
| An Epic with no Story | Stories are written for it from its own text, the documentation and the code that names it. Where nothing says what it should hold, it is in doubt: you close it or say what it is for |
| An Epic that holds unrelated groups, or a quarter of all Stories | Proposed to be split, in doubt: splitting changes the shape of the plan |

**User Stories and Issues.** After a run every open Issue that is no bug belongs to a User Story, and every open User Story has Issues or is shown to be done.

| Case | What the run does |
| --- | --- |
| An Issue under a Story, and the review agrees | Nothing |
| An Issue with no Story | Looks for its Story. Sure: linked. Not sure: in doubt |
| Issues that fit no Story, after the review | Grouped, and a Story is made for each group, under the Epic it fits. If no Epic fits either, the Story waits for the other button |
| A Story with no Issue whose work is done already | No Issue is made. It is proposed to be closed, in doubt, with the evidence |
| A Story with no Issue and nothing done | Issues are written for it, each with what is to be done and how it is known to be done |
| A bug | Left out, as decided on 24 November: a bug has no Story and no Epic |
| An Issue closed on GitHub | Left as it is. Only open work is organized; closed work is evidence |

**What "a careful review" is.** A record is not called parentless, and nothing is created, on one opinion. All of these have to hold:

1. No link is stated in its own text, and you chose none.
2. The ranking service finds no candidate close enough, by meaning and by the reranker.
3. The scorer, which is measured on this project's known links, cannot single one out.
4. Two models of different makers, neither seeing the other's answer, both say that none of the candidates fits.
5. For a record that is created: it is checked against everything that exists for a twin, and a near twin puts it in doubt instead.

Where the stages disagree, the case is in doubt and comes to you. That is the design: disagreement is the signal.

### Coverage

**What is covered.** The documentation is read as the repository holds it (the inventory with history exists). It is cut into parts at its headings. Each part is one of:

- **About the product**: it says what the product does or has to do. This is what has to be covered.
- **Not about the product**: licence, change log, how to contribute, a table of contents. Set by rule where the path says so, by a model otherwise, and yours to correct.

A part is **covered** when at least one Epic, User Story or Issue does what it says, shown by a passage of each that is found in the texts. Coverage is the share of the parts about the product that are covered.

**The page.** The sentence on top is the number: "Documentation covered: 86% (412 of 478 parts)". The button checks it. The three lists are the same: what was matched, what is in doubt, and what was created for the parts nothing covered, at the right level (an Epic for a whole capability nothing holds, a Story under an existing Epic, an Issue under an existing Story).

**Added by this proposal: the other direction.** Records that no document mentions are listed as "not in the documentation". Nothing is created for them, because the platform does not write your documentation; it is a list of what the documents leave out.

**When the documentation changes**, only the changed parts are checked again. A part that was removed puts the records made for it in doubt; it deletes nothing.

### Models: who does what

The stages exist. They are put in one fixed order, each doing what it was measured to be good at.

| Stage | What | Exists as | Asks a paid model |
| --- | --- | --- | --- |
| Stated links | A text that names its parent, a choice you made | Links with reasons | No |
| Candidates | The closest records by meaning and by shared rare words | Ranking service in the cluster: embeddings | No |
| Order | The candidates read pair by pair | Ranking service: reranker | No |
| How sure | One learned judgment from all signals, with a bound: which candidates cannot be ruled out. It abstains where it cannot | Scorer, measured on each project's known links | No |
| Decision | A model answers; a second, of another maker, answers without seeing the first | The check engine with its judge | Yes |
| Agreement | Settled only where both chose the same and the texts alone rank it first | Measured on Falcone: 32 of 35 right | No |
| Writing | A stronger model writes a new Epic, Story or Issue from named sources | Epic overviews, the import's generation | Yes |
| Checking what was written | A model of another maker checks each sentence against the sources it names | The overview judge | Yes |
| Twins | A new record against all that exist | Ranking service | No |
| You | What is in doubt | — | — |

Not used: a third model for what two disagree on. It was measured on 26 November and its side was right 4 times of 13.

Four of the ten stages ask a paid model. The cheap stages run on everything; a model is asked only about what they leave open. That is what keeps a run affordable and is why the order is fixed.

### Reliability and security

- **Nothing is deleted.** A run links, moves, creates and proposes. Closing and taking away are always in doubt, for you.
- **Everything a run did can be undone**, by line or as a whole, for 30 days.
- **A sample is put before you** from what was applied, as today. If more than the 5% you accepted is wrong, applying stops for that kind of change and everything goes to "Needs you" until the record is good again.
- **The texts of your projects are data, never instructions.** A model gets them marked as material, answers in a fixed shape, has no tools, and every passage it quotes has to be found in the text or its answer does not count. This holds today and stays.
- **Texts go only to the makers you allowed.** Inkling is never asked in a review.
- **Limits.** Dollars a month and tokens a run, from Settings. A run says what it will cost before it starts, and stops at the limit with what it has.
- **Measured before trusted.** Each stage is measured on links whose truth is known, per project, and the page for it moves to Settings. A change to a stage that makes the measurement worse is not put in force.

### Only Issues are on GitHub

Epics and User Stories live on the platform. New ones are never written to GitHub. Issues are: created there, labelled, closed.

Two things follow that need your word (below): how the Issues a run creates reach GitHub, and what happens to the Epics and User Stories that are GitHub issues today.

### Where everything goes

| Today | Becomes |
| --- | --- |
| Issues with no User Story; Review evidence | Organize, User Stories and Issues |
| Primary Epics; extra Epic links; structure; clean-up of Stories | Organize, Epics and User Stories |
| Where each Story stands | A stage of User Stories and Issues: whether a Story's work is done |
| Automatic planning reviews | A new record is organized by the same run, when it arrives |
| Changes to approve | The lists "Done" and "Created", with undo |
| Needs you | The "Needs you" list of each page; a count beside the menu entry |
| Documentation; the import of a repository | Coverage. A project's first run of Coverage is what the import was |
| Epic pages | Opened from the Epic in the planning tables |
| How well a parent is found; pairs to judge; the scorer; try a model; measure a judge | Settings, one closed section "How well it works" |
| Arrivals; what happened without you | The digest and the Dashboard |
| Planning health | The Dashboard, as today |

## Phases

Each phase is deployed and usable on its own. The old views stay until O5, so that nothing is lost before its replacement is shown to work.

| Phase | What | Done when |
| --- | --- | --- |
| O0 | **Ground.** One run type with the stages and the three lists, as records. Every existing check is listed with the stage it becomes. Counted on live: Epics and Stories that are GitHub issues, records with no counterpart, parts of the documentation. New Epics and Stories stop being written to GitHub | The numbers a first run would meet are known for each project, and no new Epic or Story reaches GitHub |
| O1 | **Organize, User Stories and Issues: linking.** The page with its second button. The run reads, matches and verifies through the fixed stages and fills "Needs you" and "Done". Nothing is created yet | On Falcone every open Issue that is no bug is under a Story or in "Needs you", from one button, and a line can be undone |
| O2 | **Organize, Epics and User Stories: linking.** The first button: the one Epic of each Story, related Epics kept or taken off, a wrong Epic moved | Every open Story has one Epic or is in "Needs you" |
| O3 | **Creating.** The careful review. Grouping what has no counterpart, writing the record, checking each sentence, the search for twins. Epics and Stories are made here; Issues as you decide below. The list "Created", with undo | After both buttons no open Story is without an Epic and no open Issue without a Story, except what is in "Needs you"; and each created record names what it was made from |
| O4 | **Coverage.** Parts of the documentation, about the product or not, matched and verified, the number, and the missing records made by the same creator. The list of records no document mentions | The page shows one number for each project, and a part nothing covered has a record or is in "Needs you" |
| O5 | **Two pages.** The old views are removed; what survives is moved as in the table above; old addresses lead to the new pages. What you decide below about the Epics and Stories on GitHub is carried out | Reviews has three entries: Organize, Coverage, Settings |
| O6 | **By itself, and told.** New and changed records and documents are organized as they arrive, within Settings. The Dashboard, the digest and the chat say what was done and what needs you. Each stage is measured again whenever it changes | A new Issue is under a Story, or in "Needs you", without a button; and a worse measurement stops a change |

O1 and O2 are mostly existing engines behind a new page. O3 and O4 are where the new work is.

## Decisions (27 November 2026)

All eight were answered by the administrator on 27 November 2026, each as recommended. The table keeps the question and what was decided.

| # | Question | Decided |
| --- | --- | --- |
| 1 | The Issues a run creates: written to GitHub at once, or kept here and published together on one word from you? | **Kept here, published together.** On 26 November you decided that what writes to GitHub stays a batch you approve. One button publishes them all; none is public before you saw it |
| 2 | New Epics and User Stories are made and linked without asking, and can be undone for 30 days. Confirm? | **Yes.** It is your decision of 26 November applied to creation: it adds, and it can be undone. With the 5% you accepted, watched by the sample |
| 3 | The Epics and User Stories that are GitHub issues today: left open there, or closed there with a comment, in one batch you approve? | **Closed in one approved batch**, in O5, after their number is known in O0. Their records stay here. Left open, GitHub would show a plan that nothing updates |
| 4 | A User Story with no Issue whose work is done: proposed to be closed, for you, and no Issue made? | **Yes.** An Issue for finished work would be wrong work |
| 5 | An Epic with no Story and nothing that says what it should hold: yours to close or explain, and no Story invented? | **Yes** |
| 6 | Coverage counts the parts of the documentation that are about the product, and you can correct which are. Agreed as the measure? | **Yes** |
| 7 | Writing records needs a stronger model than choosing among candidates. The limit is $3 a month for each project. May a run that would pass it stop and show its estimate, for you to raise the limit or not? | **Yes.** A first full run over Falcone will cost more than $3; how much is shown before it starts |
| 8 | May I remove the six old views in O5, with the measurements moved into Settings and the Epic pages opened from the planning tables? | **Yes**, once O1 to O4 are shown to do what they did |

## O0 as built (27 November 2026)

O0 is the ground. It has no page and no button: those come with O1. It asks no model and changes nothing of the plan.

### One kind of run

A run is a record (`organize_runs`, migration 0095) with what it organizes (`stories_issues`, `epics_stories` or `coverage`), its state and the stage it is in. Its results are lines (`organize_lines`) in one of three lists.

| | |
| --- | --- |
| States | proposed (it says what it would ask and cost, and waits for the word), running, held (it reached the dollars it may cost), stopped, complete, failed |
| Stages | Read, Match, Verify, Create, Done. A run never goes back to a stage it left. Each stage keeps how many records it took in and how it left them: settled, in doubt, none |
| Lists | Needs you (what still waits, the most doubtful first), Done, Created |
| A line | The record it is about, the candidates, what each stage said of it, the reason, how doubtful it is, what was done and how things stood before, and for a created record what it was made from |
| Limits | One run of a kind at a time in a project. At its dollar limit a run is held with what it has, and goes on at the administrator's word |

A line keeps how things stood before it, so that it can be put back. The undoing itself arrives with the first operation, in O1: O0 has nothing to undo.

Read: `GET /api/dashboard/projects/{name}/organize/{kind}` (the run under way or the last one, with its stages and how many lines each list holds) and `.../organize/runs/{run}/{list}` (one list, in pages).

### Every check, and what it becomes

Kept in the code (`BECOMES` in `dashboard/organize.py`), and a test holds that no check of the Needs-you list and no kind of batch is missing from it.

| Today | Run | Stage |
| --- | --- | --- |
| Which User Story an Issue belongs to (`issue_story`) | User Stories and Issues | Verify |
| Whether a User Story with no Issue and nothing done is still wanted (`story_needed`) | User Stories and Issues | Verify |
| What the code says of an Issue's Story (review evidence) | User Stories and Issues | Verify |
| Where each User Story stands (delivery) | User Stories and Issues | Read |
| Batches: reasons of links, Issues placed | User Stories and Issues | Done |
| Batches: links nothing supports, delivered Stories to close, Stories and Issues nothing stands behind | User Stories and Issues | Verify, then Needs you |
| The one Epic of a User Story under several (`story_epic`) | Epics and User Stories | Verify |
| Whether a Story's other Epics are related (`epic_links`) | Epics and User Stories | Verify |
| Batches: primary Epics chosen, unrelated Epic links taken off | Epics and User Stories | Done |
| Batch: Epics nothing stands behind | Epics and User Stories | Verify, then Needs you |
| Structure: Stories an Epic lacks, Epics to split | Epics and User Stories | Create; splits in Needs you |
| Epic overviews: the writer and the check of each sentence | Epics and User Stories | Create |
| Links stated in a text, and those you chose | every run | Read |
| Ranking service: meaning, rare words, pair by pair | every run | Match |
| Scorer | every run | Match |
| The judge: a second model of another maker | every run | Verify |
| Automatic planning reviews | every run | Read, when a record arrives (O6) |
| Documentation inventory | Coverage | Read |
| The import of a repository | Coverage | Create |

One correction to the proposal: the clean-up check (is a Story that nothing supports still wanted?) goes with User Stories and Issues, not with Epics and User Stories. It is what decides, for a Story with no Issue, between writing Issues and proposing to close it.

### Only Issues are written to GitHub

Reading the code showed that the platform never did write an Epic or a User Story to GitHub: the one place that creates an issue there is the publication of an approved Issue. Two things now hold it:

- An approved record that calls itself an Epic or a User Story (by its title or the head of its text) is refused at publication and says why. Nothing is sent.
- A test fails if an issue is ever created on GitHub from another place in the code.

Epics and Stories that were issues on GitHub when a project was imported are another matter (decision 3). Their numbers are below.

### The documentation in parts

`dashboard/doc_parts.py` cuts each document at its headings. A part is what stands under one heading, down to the next heading of any level. Fewer than 20 words under a heading say nothing by themselves and go with the part before (or, at the top of a document, the part after). The same text always cuts the same.

What a part is about is said by rule where the path or the heading says it:

- **About the product:** an OpenSpec capability; the proposal, design and requirements of an OpenSpec change that is not archived.
- **Not about the product:** a licence, change log, contributing guide and the like, by file name; a table of contents and the like, by heading; the task list of a change; an archived OpenSpec change, whose decisions are in the capabilities.
- **Open:** everything else. A model says which in O4, and the administrator can correct it (decision 6).

### What a first run would meet, read on live

Read on 27 November 2026 from `GET /api/dashboard/projects/{name}/organize/ground`, which counts from the records in one to two seconds. Only open records are counted; bugs are left out.

**Epics and User Stories**

| Project | Epics | Stories | Under one Epic | Under several | With no Epic | Epics with no Story | Largest Epic holds |
| --- | --- | --- | --- | --- | --- | --- | --- |
| falcone | 63 | 580 | 73 | 507, the one Epic chosen for all | 0 | 0 | 97 Stories (17%) |
| falcone-serverless | 16 | 66 | 1 | 65, none chosen (one is suggested for each) | 0 | 0 | 45 Stories (68%) |
| falcone-workflow, gntik-ui, llmwiki, musematic | 0 | 0 | | | | | |

"Chosen" is a primary Epic set by the administrator or by a batch that was approved or applied; "suggested" is one set only by how alike the texts are.

**User Stories and Issues**

| Project | Issues in the plan | Under a Story | Under several | With no Story | Stories with no Issue at all | Open issues never taken into the plan |
| --- | --- | --- | --- | --- | --- | --- |
| falcone | 265 | 265 (103 of them under a Story of a connected project) | 44 | 0 | 389 of 580 | 0 |
| falcone-serverless | 204 | 203 | 78 | 1 | 0 of 66 | 0 |
| falcone-workflow | 0 | | | | | 279 |
| gntik-ui | 0 | | | | | 431 |
| llmwiki | 0 | | | | | 2 |
| musematic | 0 | | | | | 40 |

Of Falcone's 389 Stories with no Issue: for 151 similar work exists, for 123 only documents speak of it, and 115 are supported by nothing. A further 46 Stories have closed Issues only. Falcone has 7 open bugs, left out.

**Epics and Stories that are issues on GitHub**

| Project | Open on GitHub | Closed there | Note |
| --- | --- | --- | --- |
| falcone | 0 | 23 Epics | 182 closures were made on approval earlier |
| falcone-serverless | 0 | 16 Epics, 58 Stories | All 74 were closed by the platform on approval |
| falcone-workflow | 12 Epics, 47 Stories | 0 | None of them is a record here |
| gntik-ui, llmwiki, musematic | 0 | 0 | |

**Documentation**

| Project | Documents | Parts | Words | About the product, by rule | Not about it, by rule | Open |
| --- | --- | --- | --- | --- | --- | --- |
| falcone | 432 | 2,855 | 424,700 | 1,300 | 238 | 1,317 |
| falcone-serverless | 56 | 398 | 87,930 | 0 | 1 | 397 |
| falcone-workflow | 28 | 362 | 58,576 | 0 | 0 | 362 |
| gntik-ui | 323 (1 too large to keep) | 1,916 | 125,544 | 1,462 | 340 | 114 |
| llmwiki | 11 | 461 | 33,424 | 0 | 0 | 461 |
| musematic | 958 | 8,064 | 727,554 | 2,571 | 957 | 4,536 |

### What the numbers change

1. **On Falcone and falcone-serverless there is almost nothing left to link.** No Story is without an Epic and one Issue is without a Story, because the checks built before did that work. O1 and O2 are still what was asked for (one button, three lists, undo), and they verify what is linked: 122 Issues under several Stories, and the Stories of Falcone whose other Epic links are in doubt.
2. **The work is in creating.** 389 Stories of Falcone have no Issue. Each gets Issues written or is proposed for closing (decisions 4 and 5 apply). This is the part that will pass the 3 dollars a month, as decision 7 foresaw.
3. **falcone-serverless has one Epic that holds 68% of its Stories**, and no Story of several Epics has its one Epic chosen. O2 starts there.
4. **Four projects have no plan here at all**, and 752 open issues between them. A run of Organize would have to create a Story for nearly every Issue. For them Coverage should run first: it makes Epics and Stories from the documentation, and Organize then places the Issues under them. The order is offered by the page; it is not forced.
5. **falcone-workflow has 59 issues on GitHub that call themselves Epics and Stories and no record here.** The batch of decision 3 closes on GitHub only what is a record here, so these are taken in first.
6. **Coverage is large.** 14,056 parts and 1.46 million words over six projects, 8,064 of the parts in musematic. Half of the parts are decided by rule without a model. What a run costs is said by the run before it starts.

## Connected projects (added 27 November 2026)

Asked by the administrator after O0: projects are connected, and a review, a reorganization or a discovery has to take the connected projects into account and read their features as if they were part of the project's own documentation.

### What was checked

What takes a connected project into account today:

- **Connections are records.** One exists on live: falcone depends on falcone-serverless. The other four projects have none.
- **A connection has its own analysis**, which reads the records of the two projects and proposes links between them. On live it has been paused since 20 November (the OpenAI account ran out of credit) at 353 of 1,919 records. It holds 500 approved findings, 614 pending and 88 outdated; 334 of the pending ones are links of Issues.
- **The planning tables** count an Issue as placed when it is linked to a User Story of a connected project. That is why 103 open Issues of Falcone have no Story in Falcone and are no orphans.
- **The code evidence** of a planning review can read the connected repository.

What does not:

| Where | What it reads | What goes wrong |
| --- | --- | --- |
| Candidates for a parent: ranking service, scorer, every check | The project's own records | A Story or Epic of a connected project is never offered, however well it fits |
| Where each Story stands; clean-up; structure | The project's own Issues and documents | A Story is called "supported by nothing" when its work is tracked in the connected project. Falcone has 389 Stories with no Issue of its own |
| Documentation inventory; the import of a repository | The project's own repository | A feature documented in the connected repository is unknown to Discovery |
| Epic pages | The project's own plan | An Epic that spans two projects is described from half of it |
| O0: the count and the parts | The project's own records and documents | The numbers say nothing of what the connected projects bring |
| The plan O1 to O6 as it was written | One project at a time | Organize would write Issues, and Coverage would create records, for what a connected project already holds |

So the point stands, and the plan needed adjusting.

### Which projects are connected

Connections are made by the administrator on the Connections page of a project, which exists: the two projects, which depends on which, and what the connection is for. The platform records none by itself and proposes none. Only one is recorded on live. For reference, the repositories name each other as follows (counted in the local copies of five repositories; falcone-serverless has no local copy):

| Pair | Evidence | Recorded |
| --- | --- | --- |
| falcone and falcone-serverless | The connection, with 500 approved findings | Yes |
| falcone and falcone-workflow | 9 documents of falcone, 12 of falcone-workflow | No |
| falcone-workflow and falcone-serverless | 18 documents of falcone-workflow | No |
| falcone and gntik-ui | falcone depends on `@gntik-ai/ui` in a manifest; 24 documents | No |
| musematic and gntik-ui | 232 documents of musematic, 13 of gntik-ui and a manifest | No |
| falcone and llmwiki | 11 documents of falcone, 6 of llmwiki | No |
| falcone and musematic | 8 documents of falcone | No, and weak |
| falcone-workflow and llmwiki; falcone-workflow and musematic | 5 documents each | No, and weak |

### When a connection is added (the administrator, 27 November 2026)

Stated by the administrator: the page already offers the connections service; what the system must do, every time a connection is added, is prepare a plan for updating and verifying the requirements, and then propose and review the changes to the entities.

**What the service does today.** Adding a connection queues its analysis at once. A model is asked about the records six at a time: each record of one project with the four records of each kind of the other that share the most words with it. It suggests links, edits and missing records, a judge is asked the same, and every suggestion waits for the administrator. There is no plan and no cost said beforehand; the documentation of the projects is not read; the candidates are by shared words, not by the ranking service; and nothing is staged, so the administrator meets 614 pending findings in one list.

**What it becomes.** A connection is worked through by the same kind of run as everything else (O0), in three steps.

1. **The plan, prepared at once when the connection is added or changed.** No model is asked for it, so it costs nothing and is there within moments. It has two halves:
   - *Requirements to update.* What of each project the connection touches, counted and listed: the Epics, User Stories, Issues and parts of the documentation of one project that concern the other. Found by what is stated (a record or document that names the other repository, its package or one of its issues) and by closeness (the ranking service, across the two projects). Sorted by what is expected of each: a link across the projects, a text to compare with the other project's, something one project's documentation asks of the other that nothing there covers, and twins (the same thing recorded in both).
   - *How it will be verified.* For each group: which stages check it and in what order, on how many records, by which models of which makers, what a quoted passage has to match, what is applied and what is only shown, and the most the run can cost. Also what the run will not do: it closes and deletes nothing.

   The plan is shown on the connection's page. Nothing is asked of a model before the administrator's word.
2. **Changes are proposed for the entities.** The run goes through Match, Verify and Create over the two projects and proposes, for Epics, User Stories and Issues of both: links across the projects, corrected texts, and new records for what is missing. A new record is proposed in the project that has to do the work.
3. **The changes are reviewed.** Each proposal is answered a second time by a model of another maker that does not see the first answer, and every passage it quotes has to be found in the text. The result is the three lists: "Needs you" for what is in doubt, "Done" for what was applied, "Created" for new records, each line with its project.

What is applied without asking follows the decisions that stand: a link both models agree on (26 November), a new Epic or User Story (decision 2); a new Issue is kept here until published (decision 1). A changed text of an existing record is always shown first, with the text before and after.

**When either project changes later** (a new record, a changed document), the plan is brought up to date for what changed and says so; in O6 it is then carried out within Settings.

**The connection on live.** Its 500 approved findings stay as links. Its 614 pending findings are taken into its first plan as proposals still to be reviewed. Its old analysis is not resumed.

### Runs of Organize and Coverage over connected projects

The administrator's first point stands beside this: a review, a reorganization or a discovery of one project reads the connected projects as part of it. **A run works on a scope: the project and the projects connected to it.** Every record and every part of a document keeps the project it belongs to, and every line of a list names it.

| Stage | With connected projects |
| --- | --- |
| Read | The records, links and documents of the whole scope. The documents of a connected project are read as documentation of the project |
| Match | Candidates come from the whole scope. A parent may be a record of a connected project: the link crosses projects, as the links of a connection do today |
| Verify | The models see each candidate with its project. An approved link of a connection is a stated link |
| "Has none" | Said only when nothing in the whole scope fits |
| Create | A twin is searched in the whole scope, so that nothing is created that a connected project holds. A run of one project creates only in that project; what belongs in a connected one is the business of the connection's own run |
| Coverage | A part of the project's documentation is covered by a record of any project of the scope. The parts of connected documentation that ask something of this project are counted beside it, each covered or not |

Rules taken as recommended unless the administrator says otherwise:

- **Direct connections only.** A project connected to a connected project is not in the scope. Otherwise falcone would bring in every project.
- **Makers.** A question that holds text of two projects goes only to makers both projects allow.
- **Money.** A run is charged to the project it was started in; a connection's run to the project the connection was made in. The plan says the most it can cost.

### Phases, adjusted

One phase is added before O1, because the first button already has to draw its candidates from the scope, and because a connection added tomorrow should get its plan.

| Phase | What | Done when |
| --- | --- | --- |
| O0 | Built | |
| **OC** | **Connections.** (1) Adding or changing a connection prepares its plan: the requirements to update and how they will be verified, with the most it can cost, on the connection's page. (2) At the administrator's word the run proposes the changes for Epics, User Stories and Issues of both projects and reviews each with a second model, into the three lists, with undo. (3) Runs and their lines carry the scope and the project of every record; candidates are drawn across the scope by the ranking service; the makers rule. The old analysis is replaced, its findings kept | A connection added on the page shows its plan without a model being asked; carrying it out fills the three lists for the two projects; and for a Falcone Issue the candidates include Stories of falcone-serverless |
| O1 | As before, over the scope, every line naming its project | As before |
| O2 | As before, over the scope | As before |
| O3 | As before. Twins are searched in the scope; a run of one project creates only in it | As before, and nothing is created that a connected project holds |
| O4 | As before. Connected documentation is read as documentation; the second number | As before, and the parts of connected documents that ask something of the project are counted |
| O5 | As before. The Connections page stays, in the shape of the other two | As before |
| O6 | As before. A change in a connected project brings the connection's plan up to date and is carried out within Settings | As before, across the scope |

### What is still the administrator's to say

Nothing blocks OC. Three points are taken as written above unless the administrator says otherwise: the plan waits for the administrator's word before a model is asked (today the analysis starts by itself); a changed text of an existing record is always shown before it is applied; and only direct connections are in a scope.

## OC as built (27 November 2026)

Started at the administrator's word ("go next OC"). The Connections page is the service; what changed is what happens when a connection is added or changed.

### What happens now

1. **Adding or changing a connection prepares its plan. No model is asked.** The worker reads the records of the two projects, records the references one states to the other as findings that wait, and writes the plan as a run in the state `proposed` (the run type of O0, kind `connection`). The connection's state is then "A plan waits for your word".
2. **The plan has the two halves that were asked for.**
   - *What is to be updated*, as a table: issues one project names in the other's repository; records whose text names the other project; records that lie close by meaning to a record of the other; the same thing recorded in both; suggestions of an earlier analysis that still wait; parts of the documentation that name the other project; and the records nothing ties to the other project, which no model is asked about. Each row says how many, in which project, what is expected, and who checks it.
   - *How it will be verified*, as six steps: stated links, candidates, the answer, the review by a second model, what is applied, and the limits. Then what it can cost at most, per model.
3. **At the administrator's word ("Carry out the plan") the run goes.** Stated references are recorded. Each touched record is put before the answering model, six to a question, with three records of each kind of the other project, drawn by shared words and by meaning joined. A second model of another maker is asked the same without seeing the first answer.
4. **The results are three lists over the connection**, each line naming the project of its records:
   - **Needs you:** what only one model said, what no second model was asked about, every changed text and every missing record, the most doubtful first. A changed text is shown as it stands beside what is proposed.
   - **Done:** links the text states, links both models made, and what the administrator accepted. Each can be undone for 30 days: a link comes off, a text is put back as it stood.
   - **Created:** records that were missing and were sent, at the administrator's word, to the project that has to make them, as proposals in its planning tables. Undoing withdraws the proposal.
   A whole run can be undone.
5. **A change in either project** brings a new plan, which names only what changed since it was asked about: the changed record, and the records it is offered to. Nothing is asked for it by itself.

### Rules that hold

- **A record nothing ties to the other project is not put before a model.** What "close" is, is not a fixed number: how close two texts lie depends on the model that read them. Once a connection has 30 approved links, the threshold is set so that 5 in 100 of them would be missed, and the plan shows the measurement. Before that a default is used, and without the ranking service every record is asked about, as before.
- **Text of two projects goes only to a maker both allow.** A model one project does not allow blocks the plan and says why; a judge one project does not allow is not asked, and then every suggestion waits.
- **Money.** The run is charged to the project the connection was made in. A plan estimated above what is left of the month's limit is refused with the estimate, unless the word is to go as far as the limit allows. At the limit the run is held with what it has.
- **A connection the administrator stopped is never woken by the worker.** Its plan is worked out when its page asks for it, kept only in memory, and nothing is written. Carrying it out is the word to go on.
- **Nothing is closed and nothing is deleted.**

### What a run of one project got from this

- A run carries its scope: the project and the projects directly connected to it (`organize.scope`), and a line can name the project of its record.
- The count of O0 says what each connected project brings: its records, and the links across that are approved or wait.
- `GET /api/dashboard/projects/{name}/organize/candidates/{record}` gives the parents a record could be put under, from the whole scope, each with its project. The buttons of O1 and O2 are what will use it.

### Read on live

The one connection on live (falcone depends on falcone-serverless) is stopped, so its plan was worked out on request: 7 to 10 seconds the first time, a third of a second after. No model was asked and nothing was written.

| | |
| --- | --- |
| Records read | 1,628 of falcone, 293 of falcone-serverless; all read by the ranking service |
| Links the texts state | 130, all in falcone-serverless naming issues of falcone |
| Records that name the other project | 308 (177 in falcone, 131 in falcone-serverless) |
| Records close by meaning | 919 (757 in falcone, 162 in falcone-serverless) |
| The same thing recorded in both | 12 pairs |
| Earlier suggestions that still wait | 484 |
| Parts of the documentation that name the other project | 159 (156 in falcone-serverless) |
| Records nothing ties to the other project | 694, all in falcone: not asked about |
| To put before the models | 1,227 records in 205 questions, about 3.9 million tokens read |
| What it can cost | The judge (claude-haiku-5-5): about $0.61. The answering model on record (gpt-6.2-sol): not priced by its account, and that account has no credit |
| The threshold | 0.71, learned from 423 approved links. Linked pairs lie between 0.71 and 0.86; a record's nearest in the other project between 0.65 and 0.87 |
| Measured on the 423 approved links | All 423 have a source the plan would put before the models; 354 (84%) would be offered the record they are linked to |
| Can it be carried out now | No: falcone's reviews have cost $6.11 of the $6.00 allowed this month. The page says so in place of the button |

With the fixed threshold the first version used, 1,914 of the 1,921 records counted as touched and the plan saved nothing. Learned from the approved links, 694 records are left out and the tokens fall from 6.0 to 3.9 million.

Candidates across the scope, on 12 Falcone issues that are linked to a Story of falcone-serverless: all 12 are offered Stories of falcone-serverless, and 11 are offered the Story they are linked to, among ten candidates.

### What is not in OC, and why

- **A missing record is never made without the administrator.** The plan of the phase said a new Epic or User Story both models agree on would be made without asking (decision 2). That decision rests on the careful review of O3 (the search for twins, each sentence checked), which does not exist yet. Until it does, every missing record waits under "Needs you".
- **The documentation is listed, not checked.** The plan counts and names the parts that name the other project. Checking them against the records is Coverage (O4).
- **The runs of one project do not read the scope yet.** The scope, the count and the candidates are there; the buttons that use them are O1 and O2.
- **Nothing is carried out by itself.** A change brings a new plan; carrying it out within Settings is O6.
- **Not exercised on live:** carrying a plan out, answering and undoing. They need the administrator's word and a model; they are covered by tests and were looked at in a local console.

## O1 as built (27 November 2026)

Started at the administrator's word ("go next O1"). Reviews has a new first entry, **Organize**, with the button for User Stories and Issues. The second button, Epics and User Stories, is O2: the page says so and points to where that work is done until then.

### The page

1. **One sentence on top:** "265 open Issues: 265 under a User Story, 0 in doubt, 0 with none", and how many bugs are left out.
2. **What the button will do, said before it starts:** how many Issues will be asked about, by which two models, what that is likely to cost (from what the same question cost before), which connected projects' Stories are offered, and whether what is settled is applied or only put before the administrator, with the reason.
3. **One button, "Check and organize",** behind a confirmation that repeats the above. Where it cannot start, the page says why in its place.
4. **The stages of the run:** Read, Match, Verify, Create, Done, each with its count. Create is shown and stays at zero: nothing is created in this phase.
5. **Three lists:** Needs you, Done, Created.

### The run

It puts the engines that exist in one order and adds none.

| Stage | What | Engine |
| --- | --- | --- |
| Read | The open Issues that are no bug, and which have no User Story | The worklist of the check `issue_story` |
| Match | Each Issue with none is offered the nearest User Stories, by shared words and by meaning | The shortlist of the check, now over the scope |
| Verify | A model answers; a second, of another maker, answers without seeing the first | The check's run, started with the project's models. A check that is asking already is taken into the run |
| Done | What both chose, with the texts ranking it first, is applied where the project's record allows | The batch "Place issues under a User Story", applied without the administrator, its sample judged afterwards |
| Needs you | Every other answer, the most doubtful first; and links that nothing supports | The standings of the check; the scorer's "unsupported" |

The run follows the check's asking in the background and gathers what was answered when it is over. It can be stopped, resumed, or ended keeping what was answered.

### Needs you, and what the administrator does there

A case shows the Issue, what each model answered and why, the passages it quoted, and the Stories the Issue was offered, each with its Epic, its project where that is another one, and who chose it. What both models chose is selected already.

| The case | The answers |
| --- | --- |
| An Issue with no User Story | Put it under the chosen Story; it is a bug; leave it without a Story |
| A link nothing supports | Take the link off; keep it |

What stays out of "Needs you": an Issue that was left by the administrator, and one nobody was asked about yet (the sentence on top counts both under "with none").

### Done, and undo

A line under Done is an Issue put under a Story, by the record or at the administrator's word, or a link taken off. Each can be undone for 30 days, and so can everything a run applied. Undone, the case waits under "Needs you" again, and:

- a link the record applied counts as **judged wrong** on that record. It is not proposed again, and it is part of what decides whether applying without the administrator goes on;
- a link to a Story of a connected project is taken off the connection.

### Connected projects

- The Stories an Issue is offered are the project's own **and those of the projects connected to it**, ranked together. The models are told which project a Story belongs to.
- An Issue put under a Story of a connected project is a link of their connection. It counts as placed, as it did before.
- This holds for the old worklist under Related Work as well, since it is the same engine.

### When applying happens by itself

Unchanged, and now said on the page: a project applies what is settled once the administrator judged enough sampled lines of its batches and the share judged wrong is within what the settings accept (40 lines and 5% unless changed). Until then, what is settled is a case under "Needs you" with "Both models and the texts agree" and one click.

### Read on live

| Project | Open Issues | Under a Story | With none | Links nothing supports | What the button would do |
| --- | --- | --- | --- | --- | --- |
| falcone | 265 | 265 | 0 | 0 | Nothing to check; it says so. 7 bugs left out |
| falcone-serverless | 204 | 203 | 1 | 11 | No model is set for this project's reviews, so the one Issue is not asked about; the 11 links are put before the administrator |
| falcone-workflow, gntik-ui, llmwiki, musematic | 0 | | | | Nothing: they have no Issue in the plan yet |

On Falcone the answering model is gpt-5.6-luna and the judge claude-haiku-5-5; a question has cost $0.0017 with the two. Neither project applies by itself yet: no sampled line was judged.

So the goal of the phase holds on Falcone today: every open Issue that is no bug is under a User Story.

### What is not in O1, and why

- **Nothing is created.** An Issue that fits no Story waits under "Needs you" (both models say none fits). Making its Story is O3.
- **A User Story with no Issue is not looked at.** Falcone has 389. Writing their Issues or proposing to close them is O3.
- **The old views are still there.** Related Work shows the same worklist; both read the same records. They go in O5.
- **Not exercised on live:** starting a run, answering and undoing. Falcone has nothing to check, and falcone-serverless would put 11 cases before the administrator, which is theirs to start. Covered by five tests of the run and looked at in a local console.

## O2 as built (27 November 2026)

Started at the administrator's word ("go next O2"). The Organize page now has both buttons: **Epics and User Stories** first, as the plan orders them, then User Stories and Issues. The card that said the button was not built is gone.

### The page

The same shape as the first button, so that one is learned for both.

1. **One sentence on top:** "580 open User Stories: 579 with their one Epic, 1 in doubt, 0 under several Epics and not checked yet, 0 with none", and how many open Epics there are.
2. **What the button will do, said before it starts:** how many Stories get their Epic by rule, how many are asked about and by which two models, what that is likely to cost, whether what is settled is applied or only put before the administrator, how many links to other Epics will be put before the administrator, and how many Stories the texts call in question.
3. **One button, "Check and organize",** behind a confirmation that repeats the above.
4. **The stages of the run** and **the three lists**, as for the first button.

### The run

It puts the two checks that exist in one order, adds one rule and one comparison, and asks no new kind of question.

| Step | What | Engine |
| --- | --- | --- |
| By rule | A Story whose own text names one of its Epics gets that Epic as its one Epic. No model is asked. Listed under Done, and can be undone | The stated link of the check `story_epic` |
| The one Epic | For a Story under several Epics, a model says which it belongs to; a second, of another maker, says so without seeing the first | The check `story_epic`, started with the project's models |
| Done | What both chose, with the texts ranking it first, is made the Story's one Epic where the project's record allows | The batch "Choose primary Epics", applied without the administrator, its sample judged afterwards |
| The other Epics | For a Story that has its one Epic, the two models say of each other Epic it is under whether the Story has to do with it | The check `epic_links`, asked only about Stories whose one Epic is chosen |
| The only Epic | A Story under one Epic is compared with every Epic of the project by shared words and by meaning. Where both place the same other Epic first and neither places its own among the first five (the first third, and at least two, in a project with few Epics), it is called in question. No model is asked | The ranking service, as the connection's plan uses it |
| With none | A Story under no open Epic is offered the nearest Epics of the project and of the projects connected to it | The candidates of the scope, from OC |

The run follows each check's asking in the background, gathers what was answered when it is over, and goes on to the next. It can be stopped, resumed, or ended keeping what was answered. A check that is asking already is taken into the run.

**Why the other Epics are asked only once the one Epic is chosen.** The question is about the Epics *beside* the Story's own. Asked before that one is known, the answer would go stale the moment the administrator chose another, and the money would be spent twice. So in a project that does not yet apply by itself, the first run settles the one Epic, and the next run, after the administrator answered, asks about the others. Where the Epic is given by rule or applied by the record, both happen in one run.

### Needs you, and what the administrator does there

| The case | The answers |
| --- | --- |
| A Story under several Epics: the models differ, could not tell, or agree where the project does not apply by itself | Make the chosen Epic its one Epic; leave it as it is |
| A link to another Epic that both models call unrelated | Take the link off; keep it |
| A link to another Epic in doubt: the models differ, could not tell, only one answered, or it rests on a passage that is not in the Epic's text | Take the link off; keep it |
| A Story under one Epic that the texts place elsewhere | Move it under the chosen Epic; keep it where it is |
| A Story under no open Epic | Put it under the chosen Epic; leave it under none |

**A link is never taken off without the administrator.** The proposal said that links the models do not both find related "are taken off". The decision of 26 November stands above it: what removes stays for the administrator. So every such link is a case. A link both models call related stays and is no case.

**One word for many.** Read on live, Falcone's first run would put 421 links before the administrator. Two buttons above the list answer them together: one takes off every link both models call unrelated, one keeps every link in doubt. Each link taken off is its own line under Done and can be put back; kept links are not put before the administrator again, and neither is a link whose taking off they judged wrong in a batch before.

### Done, and undo

A line under Done is a Story given its one Epic (by rule, by the record or at the administrator's word), a Story moved, or a link taken off. Each can be undone for 30 days, and so can everything a run applied. Undone, the links stand as they did and the case waits under "Needs you" again. What the record applied counts as judged wrong on that record, as for the first button.

### Connected projects

- **The one Epic of a Story is an Epic of its own project.** A Story stands under at least one Epic of its project by the records' own rule, and what it has to do with the Epics of a connected project is a link of the connection, which the connection's plan proposes (OC). The run does not offer another project's Epic as the home of a Story that has one.
- **A Story with no Epic is offered the Epics of the whole scope,** each of a connected project with its project. Choosing one makes a link of the connection; the Story then counts as having its Epic, and the sentence on top says how many stand so. Undone, the link is taken off the connection.

### Read on live

| Project | Open Stories | With their one Epic | Under several, not checked | With none | What the button would do |
| --- | --- | --- | --- | --- | --- |
| falcone | 580 | 579 | 0 | 0 | Ask no model and cost nothing. Put 421 links to other Epics before the administrator, all in doubt and none that both models call unrelated, with one word to keep them; and 1 Story whose only Epic the texts place elsewhere |
| falcone-serverless | 66 | 1 | 65 | 0 | Give 41 Stories their Epic by rule, at once and for nothing. The other 24, and what the other Epics of those 41 are to them, are not asked about: no model is set for this project's reviews |
| falcone-workflow, gntik-ui, llmwiki, musematic | 0 | | | | Nothing: they have no Story in the plan yet, and the page says so |

Of Falcone's 1,027 links to other Epics, the models found 739 related, 31 unrelated and could not tell about 257; none of the 31 is called unrelated by both models with its passage in the text, which is why nothing is offered for taking off together. No Epic of either project is without an open Story. The page answers in 0.3 to 0.7 seconds.

So the goal of the phase holds on Falcone but for one Story: every open Story has its one Epic, and that one is called in question by the texts and will wait under "Needs you" after the first run.

### What is not in O2, and why

- **Nothing is created.** Stories that fit no Epic, and an Epic with no Story, are O3. Both projects have none of either today.
- **No model is asked about a Story's only Epic.** The comparison by shared words and by meaning calls it in question, and the administrator decides: moving a Story takes a link off, which is theirs anyway. A model's answer would not change who decides. One Story on Falcone is concerned.
- **An Epic that holds a quarter of all Stories or unrelated groups is not proposed for splitting.** It is in the proposal's table for this pair; it changes the shape of the plan and belongs with creating, in O3.
- **The old views are still there** (Planning, Reorganize). They read the same records. They go in O5.
- **Not exercised on live:** starting a run, answering, the two one-word answers, and undoing. They are the administrator's to start. Covered by four tests of the run and looked at in a local console.

## O3 as built (27 November 2026)

Started at the administrator's word ("go next O3"). Both buttons of Organize now go on after the linking with a stage that creates. The stage "Create" of a run, which stood at zero, counts what was made and what was put before the administrator; the list "Created" is filled.

### What each button creates

**User Stories and Issues**, in this order:

| What the run meets | What it does |
| --- | --- |
| A User Story whose Issues are all closed as done, with the proof the records hold | Puts it before the administrator to be closed. No Issue is made for finished work (decision 4). One word closes all of them |
| Issues that two models found to fit no User Story | Groups those that lie close together and has the User Story written that they are the work of, under the Epic it belongs to. Made, the Issues are put under it |
| A User Story no Issue is linked to | Has its Issues written (one to three, each with what is to be done and how it is known to be done); or finds its work done by closed Issues it is shown; or finds that nothing says what would have to be done. The last two are the administrator's to close or keep |

**Epics and User Stories:**

| What the run meets | What it does |
| --- | --- |
| An Epic with no open User Story | Has its Stories written from its own text, the Stories that were under it and are closed, and the documents nearest to it; or finds its work done; or finds that nothing says what it should hold, and then it is the administrator's to close or explain (decision 5). No Story is invented |
| An Epic that is the one Epic of too many Stories | Proposes the split the Structure view proposed: its Stories in groups by meaning, each group beyond the largest an Epic of its own. Nothing is split without the administrator, who can give the new Epics their titles |

### The careful review

A record is made without the administrator only when all of this holds:

1. **One model wrote it, and named for every sentence the records it rests on.** It is shown those records and nothing else: the User Story or Epic, the documents nearest to it, the Issues that may be its work. A sentence that names no record it was shown is left out, and then nothing of that answer is made without the administrator.
2. **A second model, of another maker, found every sentence supported.** It is not asked to write again. It is shown the same records and what was written, sentence by sentence, and says of each: supported, not supported, or it cannot tell.
3. **Nothing like it exists** in the project or the projects connected to it: no record of that kind with the same title, and none as close as 0.9 by meaning. The new record is read by the ranking service before it is made. Where that fails, nothing is made on words alone.
4. **For a new User Story: an Epic it belongs under was named, and the second model agreed.**
5. **Making records is in force:** of the records made so far, no more were taken away again than the settings accept (5% unless changed).

Everything else is a case under "Needs you", with what was written, each sentence with the records it names and the check's word on it, and the records like it where there are any. The administrator makes it as written, or leaves it.

These are three new checks of the engine that asks the other reviews (`new_story`, `new_issues`, `new_stories`), so a run of them is started, limited in tokens and dollars, stopped, resumed and paid for like the others, and its cost is counted under "Organize: records that were written".

### Who writes

Settings has one more model: **the model that writes new records**. Where none is named, the model that answers writes. What it writes is checked by the judge, or else by the model that answers, whichever is of another maker than the writer. Where neither is, every answer waits for the administrator.

The proposal said writing needs a stronger model than choosing (decision 7). That is the administrator's to set: on Falcone the writer is gpt-5.6-luna today, because it is the model that answers and no other was named.

### Created, and undo

Every record made is a line under "Created": what it is, under what, its text, the records it was made from, and whether a run made it or the administrator. It can be taken away again for 30 days. Taken away:

- an Issue or a User Story is removed again, and Issues that were put under a new Story are taken off it;
- the record counts as wrongly made. Where more than the accepted share was, nothing more is made without the administrator, and what is written and checked waits for one word;
- it is not written again.

The proposal said a sample of what was applied is put before the administrator. Here the whole list is the sample: every made record is listed with its sources, and taking one away is the judgment.

A record that was made does not get the automatic planning review a new record otherwise gets: two models have read it, and that review would ask a third time and propose records under it.

### Issues reach GitHub at one word

The Issues a run makes are kept here (decision 1). The page says how many wait, and one button publishes them all: each is then created on GitHub by the worker that writes every approved Issue. An Issue that was published is not taken away from here: on GitHub it is closed.

### What a run would cost, said first

Before it starts, the page says how many records it would write for, who writes and who checks, how many tokens that is and what it is likely to cost at the accounts' prices, without a model being asked for the estimate. Where the cost would pass the month's limit it says so, and that the run stops there with what it has (decision 7). It also says where one run stops at its tokens and the next goes on.

### One word for many

| | |
| --- | --- |
| The User Stories whose work is done | Close them |
| What was written and only waits for a word (no second model, or making is not in force) | Make them |
| The records nothing says anything about | Keep them open. Closing is one by one |
| The Issues that were made | Publish them to GitHub |

### Two things read on live, and corrected

The administrator used both buttons after O2. Reading what was done showed two faults of O2, corrected with this phase:

- **421 links in doubt were taken off one by one,** in nine minutes, because only keeping them was offered together. One word now takes them off, as one keeps them; each is a line that can be undone.
- **Taking a link off made the check ask again about the Story's other links:** 216 Stories on Falcone, estimated at $0.33. What a model said of a link was said of that link by itself. An answer now stands when links it judged went away and nothing else changed. On live the Epics card says again that no model is asked.

### Read on live

| Project | What the stage would do |
| --- | --- |
| falcone | 389 User Stories have no Issue: 123 that a document describes, 151 that an Issue may be the work of, 115 with nothing behind them. For each its Issues would be written, or it would be found done, or found to have nothing behind it. gpt-5.6-luna writes, claude-haiku-5-5 checks. Estimated at $0.71 and 2.1 million tokens; one run stops at 1 million, so it takes two or three. The month's limit is $10, of which $6.11 is spent. No Story is delivered and still open; no Issue fits no Story; no Epic is without a Story; no split is proposed |
| falcone-serverless | Nothing to create: every Story has an Issue and every Epic a Story. A run of the Epics button is held: the project's review tokens are used up |
| falcone-workflow, gntik-ui, llmwiki, musematic | Nothing: they have no plan yet |

The page answers in 0.3 to 1.3 seconds.

### What is not in O3, and why

- **No Epic is written for Stories that fit none.** The records do not let a User Story stand without an Epic: it is made under one, and its last Epic cannot be taken off. So there is no group of Stories without an Epic to write one for. A Story that was written and for which no Epic fits waits for the administrator, who chooses the Epic it is made under. A new Epic comes from a split, or from the administrator's hand.
- **The code is not read for an Epic with no Story.** The proposal named the Epic's own text, the documentation and the code that names it. The Stories are written from the first two and the closed Stories; the code is not indexed by Epic.
- **No split is proposed on live.** An Epic is proposed to be split when it is the one Epic of more than a tenth of the Stories, and of at least twenty. Counted by the one Epic of each Story, the largest holds 32 of 580 on Falcone and 11 of 66 on falcone-serverless. The Epic of falcone-serverless that 68% of its Stories were linked to (read in O0) is the one Epic of 11. The rule is the Structure view's own, unchanged.
- **The old views are still there.** They go in O5.
- **Not exercised on live:** writing, making, publishing and undoing. They are the administrator's to start, and the first asks 389 questions of two models. Covered by four tests of the stage and looked at in a local console. The search for twins by meaning asks the ranking service from the console's process for the first time; where it cannot, the cases say so and nothing is made.

## O4 as built (27 November 2026)

Coverage is one page under Reviews, beside Organize: how much of a project's documentation its plan covers, checked behind one button. `dashboard/coverage.py` is the run, `coverage_parts.py` keeps the parts, `coverage_check.py` holds the two checks. The run is the same kind as a run of Organize (`organize_runs`, `organize_lines`), with the same stages and the same three lists, and it creates through the same creator (`organize_create.py`).

### The parts, kept

Until O4 the parts were cut on the fly (O0). A run now keeps them (`doc_parts`, migration 0098): the document, the heading and the headings above it, the text, what a rule says it is about, what the administrator says it is about, where it stands (not checked, covered, covered by nothing, in doubt, not about the product) and the records that cover it, each with how that is known.

A part's key is made from the document's path and the part's own text. A part that did not change keeps everything that was found of it when the document around it changes; a part whose text changed is a new part and is checked; a part that is no longer there is marked as gone and kept, because records may have been made for it.

### The number

"Documentation covered: 60% (3 of 5 parts that are about the product)". The parts that count are those about the product: by rule, by what the two models said, or by the administrator's word. A part of which nobody said yet does not count until a run or the administrator says what it is about. Under the number the page says how many parts are covered by nothing, in doubt and not checked, how many are not about the product, and how many are still open.

A part is covered in one of four ways, and the page says which:

| How | What it means |
| --- | --- |
| The change is named after it | The part belongs to an OpenSpec change that is not archived and whose name carries the number of an Issue of the plan. No model is asked: the change says itself what it is for |
| Found by both models | Two models of different makers, each without seeing the other's answer, named the same record, and the passages they quoted are in the texts |
| Made for this part | A run wrote a record for it and made it after the careful review, or the administrator made it |
| Said by you | The administrator chose the record |

### The run

1. **Read.** The documents as the last reading left them are cut into parts and the kept parts are brought in step, for the project and the projects connected to it. Then the ranking service reads the parts it has not read (160 at a time, between the worker's passes), so that each part can be compared with the records by meaning. Where there is no ranking service, or the records were read with another model, parts and records are compared by the words they share. No model is asked in this stage.
2. **Match and Verify** (the check `doc_cover`). Every part that is about the product, or of which nobody said yet, and that nothing settles without a model, is shown the eight records nearest to it: Epics, User Stories and Issues of the project and of the connected projects, closed ones too, never bugs and never Issues closed as not planned. The model answers one of four: a record covers it (it names up to three, the best first, and quotes a passage of the part and of the record), nothing covers it, it is not about the product, or it cannot tell. The second model, of another maker, answers the same question without seeing the first answer. They agree when they say the same and, for "covered", name a record in common.
3. **Create** (the check `doc_record`). The parts found covered by nothing are put in groups that lie close together by meaning (at most six; a part the ranking service did not read is a group of its own). For each group the writer is shown the parts, the five open Epics and the six open User Stories nearest to them, and writes one to three records at the lowest level that fits: an Issue under a User Story that is there, a User Story under an Epic that is there, or an Epic where no Epic holds the capability. Every sentence names the part it rests on. The second model checks every sentence, and the search for a record like it runs over the project and the connected projects, exactly as in O3.

What follows from the answers:

| Answer | What happens |
| --- | --- |
| Both say covered, a record in common, the passages are in the texts | The part is covered. Listed under Done with what was quoted. Nothing in the plan changes, so there is nothing to undo |
| Both say nothing covers it | The part goes to the stage that creates |
| Both say it is not about the product | The part stops counting. The administrator can say otherwise in the list of parts |
| They differ, one cannot tell, only one answered, or a quoted passage is not in the texts | Needs you: the part's text, what each model said, and the records it was shown. The administrator says which record covers it, that nothing does, or that it is not about the product |
| Written, every sentence supported, nothing like it exists, and it is an Issue or a User Story | Made, and listed under Created with the part it was made from. The part is covered |
| An Epic was written | Needs you, always. An Epic shapes the plan: it is made at the administrator's word, also when everything else about it passed |
| Written, and a sentence is not supported, or a record like it exists, or the check could not tell, or no second model is set | Needs you, with every sentence and its verdict |

An Issue that is made is kept here and is not on GitHub (decision 1). The page offers the one word that publishes the Issues that were made; it is the same word as on Organize, and counts the same Issues.

### Answers that stand

An answer was paid for and is not thrown away lightly. A part is asked about again only when its own text changed, or:

- it was covered, and a record that covers it changed;
- nothing covered it, and it would be shown a record today that it was not shown then. A record that Coverage itself made for another part does not count: parts that ask for the same thing are written for together.

A part found not to be about the product is not asked about again. What the administrator decided about what was written for a group of parts stands while the parts are the same: a record that was taken away is not written again because an Epic was added nearby.

### When the documentation changes

The page says when the documentation was read again, and changed, since the last run; the button is then offered although every kept part has its answer. The run brings the parts in step and asks only about the parts that are new. A record that was made for parts that are all gone is put before the administrator ("Its part of the documentation is gone"), who keeps it or takes it away. Nothing is deleted by a run.

### Connected projects

A part of a connected project's documentation that names this project (by its name, its repository or what it is shown as, standing by itself) is checked against this project's plan as well: what the neighbour's documents ask of this project should be in this project's plan. Those parts are counted beside the number, not in it, and nothing is written for them: the documentation is the neighbour's. The records a part is compared with are those of the whole scope, so a part of this project's documentation can be covered by a record of a connected project, and the page says whose it is.

### Correcting what a part is about

"The parts of the documentation" lists the parts by where they stand, with what each is about and who said so (a rule, the models, or the administrator) and what covers it. One word says that a part is about the product or is not, and one takes that word back. A part the models set aside and the administrator calls about the product counts as covered by nothing: the models saw the records nearest to it and named none.

### Not in the documentation

The other direction, as proposed: the open Epics, User Stories and Issues of the project that no part is covered by and that no part lies close to by meaning (0.7 and more counts as close). Nothing is created for them.

### What a run would cost, said first

Before the button the page says how many parts would be asked about, by which two models, the estimate in dollars and tokens, the month's limit and what is spent, the tokens one run stops at, what the project's review tokens still allow where the estimate is more than that, and how many parts the ranking service has yet to read. The writing for what the run itself finds covered by nothing is not in the estimate, and the page says so: how many such parts there are is known only when the models have answered. The run holds at the month's limit like every other run.

A run with no model set still reads: the parts are kept and what the changes state themselves is counted. A project with no Epic, User Story or Issue of its own is read in the same way and no model is asked about it: there is nothing to compare its documentation with, and Discovery makes the first plan. Without that rule a run on such a project would have asked about every part and then written a plan from single parts, which is not what Coverage is for.

### Read on live

No run was started. Before a first run the page says what the rules alone say of the parts, and what a run would do:

| Project | Documents | Parts | About the product by rule | Not | Nobody said yet | What a run would do |
| --- | --- | --- | --- | --- | --- | --- |
| falcone | 432 | 2,855 | 1,300 | 238 | 1,317 | Ask about 2,617 parts, gpt-5.6-luna and claude-haiku-5-5. Estimated at $2.43 and 8.9 million tokens. The month's limit is $10, of which $6.11 is spent. One run stops at 1 million tokens; the project's review tokens allow 2.28 million more |
| falcone-serverless | 56 | 398 | 0 | 1 | 397 | Ask about 397 parts, the same two models. Estimated at $0.37 and 1.35 million tokens. The limit is $3, of which $0.06 is spent. Its review tokens allow 15,820 more: the run would be held almost at once, until that limit is raised |
| falcone-workflow | 28 | 362 | 0 | 0 | 362 | Read the parts and ask nothing: the project has no plan yet |
| gntik-ui | 322 | 1,916 | 1,462 | 340 | 114 | The same |
| llmwiki | 11 | 461 | 0 | 0 | 461 | The same |
| musematic | 958 | 8,064 | 2,571 | 957 | 4,536 | The same. Before the rule for projects with no plan it would have asked about 7,107 parts |

The ranking service has read none of the parts yet; the first run has it read them. The parts of a connected project's documentation that name a project are counted once a run has kept the neighbour's parts; until then that number is nought. The page answers in 0.3 to 1.7 seconds the first time and in under 0.3 after.

### What is not in O4, and why

- **No run was started on live by the platform's builder.** Starting one spends money and is the administrator's; they started two a day later (see "The first runs of Coverage on live", under O6). What a run does is covered by three tests (a run from the first reading to the answers and the undo; connected documentation and documents that change; a run without models and a project without a plan) and was looked at in a local console.
- **The first run on Falcone takes several starts as the settings stand.** The estimate is 8.9 million tokens and one run stops at 1 million, so it would take nine starts, and the project's review tokens allow 2.28 million. Both numbers are the administrator's, under Settings; the page names both before the button. Each start goes on where the last one stopped: answers are kept.
- **Nothing is written for a part of a connected project's documentation.** It is counted and shown; the record, if one is wanted, is the administrator's to ask for in the project the document belongs to.
- **An Epic is never made by a run.** Decision 2 lets Epics and User Stories be made without asking. For Coverage an Epic waits all the same: one part of a document is thin ground for a whole capability, and a wrong Epic costs more to put right than a wrong Issue. One word makes all that wait.
- **Coverage's cases are not in the shared "Needs you" view.** Neither are those of Organize. The old views go in O5, and the list across both pages is part of that.
- **Parts are not checked by themselves when a document changes.** The page says that the documentation was read again and offers the button. Running it without a button is O6.

## O5 as built (27 November 2026)

Reviews has three entries: **Organize**, **Coverage** and **Settings**. The six views beside them are gone from the menu and from the console. Nothing that waited in them was dropped: each panel that still held something was moved, and the address of every old view leads to where its panel is now.

### Where each view went

| The view, and what it held | Where it is now |
| --- | --- |
| **Needs you**: one list of what waits across Reviews, and what happened without you | The project's Dashboard, as its first card. Beside each of Organize and Coverage in the menu stands how many cases wait there |
| **Related Work**: Issues with no User Story | Organize, User Stories and Issues: the button and its three lists. The old worklist is removed |
| **Related Work**: Review evidence (what the code says of an Issue's User Story) | Organize, "Beside the two buttons", closed until opened: *What the code says* |
| **Planning**: primary Epics | Organize, Epics and User Stories. The old worklist is removed |
| **Planning**: where each User Story stands | The stage of User Stories and Issues that reads it: delivered Stories are put before the administrator to close, Stories with no Issue get Issues written. The table of every Story's state is removed; the state is still in the page of each Epic |
| **Planning**: automatic planning reviews | Organize, "Beside the two buttons": *Records reviewed as they arrive*. Until O6 does it by a run |
| **Reorganize**: changes to approve | Organize, "Beside the two buttons": *Changes to approve*, open when something waits in it |
| **Reorganize**: extra Epic links, clean-up, structure | Organize, Epics and User Stories (links, splits, Epics with no Story) and User Stories and Issues (Stories with no Issue). The three panels are removed |
| **Reorganize**: arrivals | The project's Dashboard |
| **Reorganize**: how well a parent is found, the pairs to judge, the scorer in shadow, trials of models, measurements of judges | Settings, one closed section at the end: *How well it works* |
| **Discovery**: the import of a repository | Coverage: *The first plan, from the repository* |
| **Discovery**: the documentation, its readings and what changed | Coverage: *The documents, and what changed in them* |
| **Epic pages** | The planning tables, Epics: a closed section lists the page of each Epic and writes the overviews; the page of one Epic opens there |

A panel is still named, by the server and in old links, by the view it was in and its own name (`reorganize#changes`, `related-work#review-evidence`). One table in the console (`FORMER` in `router.tsx`) says where each is now, and both an old address typed into the browser and a row of "what waits" go through it. The server keeps answering the old addresses with the console, so a bookmark still works.

### Batches, beside the runs

A batch was the way to apply many sure changes at one word. Five of the nine kinds are now what a run of Organize carries out itself, case by case, with its own undo: placing an Issue under a User Story, choosing the one Epic of a Story, taking off a link nothing supports, taking off an Epic link two models found unrelated, and closing a delivered User Story. For those no new batch is offered, and what is ready for them is not counted as waiting: the next run of the button takes it. A batch of such a kind that was already opened is still decided as a batch, and a batch that was approved can still be undone.

Four kinds are still offered as batches, because no run does them: recording why links hold, and closing User Stories, Epics and Issues that nothing stands behind by rule.

### The count beside the menu entry

`GET /api/dashboard/projects/{name}/reviews/waiting`. For Organize: the open lines of "Needs you" of both buttons, the answers of their checks that no run has gathered into a list yet, and what waits in the three panels beside them (changes, verdicts of the code, reviews that ask a question). For Coverage: its open lines. The lines are counted as they are; the rest as it was worked out at most five minutes ago.

The list of what waits (`needs_you.py`) names the new pages, has a row for the cases of each run, and no longer counts twice what a run put in its list: an answer in doubt is a row of its own only while no run of Organize gathered it. A proposal for the structure is something to start (the button of Epics and User Stories makes cases of it), not a second count.

### Epics and User Stories on GitHub (decision 3)

Decision 3 was to close the Epics and User Stories that are issues on GitHub in one approved batch, their records staying here. Counted in O0 and again now:

| Project | Open on GitHub | What O5 does |
| --- | --- | --- |
| falcone | 0 (23 closed) | Nothing is left to close |
| falcone-serverless | 0 (74 closed) | Nothing is left to close |
| falcone-workflow | 12 Epics, 47 User Stories | None of them is a record here. They are not closed |
| gntik-ui, llmwiki, musematic | 0 | Nothing |

So there is no batch to approve: where a record exists, its issue is closed already, and the platform closed it when the record was approved. The 59 of falcone-workflow have no record here, and closing them now would leave the plan nowhere: "their records stay here" would not be true. They become records, and are closed on GitHub, one by one as the first plan of falcone-workflow is approved under Coverage. That is the path that closed the 97 of the other two projects, and it refuses to close an issue that has no approved, living record here.

The Coverage page says this with the numbers, above the first plan. **This departs from the letter of the decision**, and a batch that takes the 59 in as they stand and closes them, with no model asked, can be built if that is what is wanted.

### Read on live

Nothing was pressed. After the deploy:

| Project | Beside Organize | Of which | Beside Coverage |
| --- | --- | --- | --- |
| falcone | nothing | | nothing |
| falcone-serverless | 110 | 24 cases of Epics and User Stories, 2 of User Stories and Issues, 9 changes to approve, 4 verdicts of the code, 71 reviews of single records that ask a question | nothing |
| falcone-workflow, gntik-ui, llmwiki, musematic | nothing | Their one next step: "make its first plan under Coverage" | nothing |

Before the deploy falcone-serverless showed 36 changes waiting for approval, none of them in an opened batch: 23 primary Epics to choose, 3 Epic links to take off, 1 link nothing supports, 8 reasons of links to record and 1 Issue nothing stands behind. The first 27 are what the two buttons carry out, and are no longer offered as batches; the last 9 still are. Its run of Epics and User Stories is held as before (the project's review tokens are used up), and takes the 26 that are its own when it goes on.

On falcone the list of what waits holds no decision: 256 reviews of single records are queued and stopped by the administrator's switch, 2 Epic overviews are not written, and three rows of answers two models differed on, which decide nothing.

Every address of a view that is gone was opened and led on: `needs-you` to Organize, `related-work#review-evidence` and `reorganize#changes` to their panels of Organize, `planning#primary-epics` to Epics and User Stories, `reorganize#pairs` to "How well it works" under Settings, `discovery` to the first plan under Coverage, `epics` to the Epics table. The count beside the menu answers in 0.2 seconds.

### What is not in O5, and why

- **The engines behind the removed panels are kept, with their addresses on the server.** Organize runs on them (the checks for Issues with no Story, the one Epic, the Epic links, the batches, the delivery states, the structure), and their tests drive them through those addresses. What was removed is every view of them in the console.
- **Three panels are moved as they were, not rebuilt as runs:** changes to approve, what the code says, and the reviews of single records. They sit beside the two buttons, closed. Making them part of a run is O6 for the reviews of single records; the other two are not planned.
- **The check "is a User Story that nothing supports still wanted?" has no button of its own any more.** Organize answers the question differently (it writes the Issues, finds the work done, or finds nothing that says what to do, and puts the last two before the administrator). As a check it still runs where Settings say it runs by itself, and what it finds reaches the batches that close Stories nothing stands behind.
- **The table of every User Story's state is gone.** The numbers by state are in the plan of User Stories and Issues, and each Story's state is on the page of its Epic.
- **"Discovery" is still what the reading of a repository is called** in Settings and in the costs by workflow. It is a workflow, no longer a view.

## O6 as built (28 November 2026)

The last phase. A run starts by itself where the settings say so, what it did is told, and the record that decides what is applied without the administrator starts anew when the models behind it change. `dashboard/organize_auto.py` is the new part; the rest is in the settings, the list of what waits, the digest and the chat.

### What starts by itself

Under Settings, "What starts by itself" lists four runs, each with three choices (only when you start it; by itself as records and documents arrive; by itself each night):

| Run | By itself it does |
| --- | --- |
| Organize: Epics and User Stories | What its button does |
| Organize: User Stories and Issues | What its button does. A new Issue is under a User Story, or in "Needs you", without a button |
| Coverage | What its button does. Its first start reads the documentation into parts; later it starts when the documentation was read again and changed, or parts wait |
| Connections: carrying out a plan that changed | The plan of a connection is brought up to date by the platform whenever a record of either project changes (OC). With this setting the plan the project owns is also carried out, where nothing stands against it |

All four are off until the administrator says otherwise: asking a model costs money. A run that starts by itself is the same run, with the same models, limits and lists. Four rules keep it from spending what was not meant:

- **As records arrive** means within half an hour of something for the run to do, and never within half an hour of its last start by itself. **Each night** means once, between 1 and 5 UTC.
- **A run estimated to pass the month's limit is not started by itself.** It waits for the administrator's word, as decision 7 says, and the page says why.
- **A run that left as much to do as it found is not started again for a day**, unless what waits changes. What it could not do, it cannot do half an hour later either. What a run left is noted as it ends.
- **A project that is paused starts nothing.**

Each page says under its card whether its run starts by itself and what a start would wait for now (`GET /api/dashboard/projects/{name}/reviews/by-itself`).

### What it takes the place of

- **The checks a run asks** (Issues with no User Story, the one Epic of a Story, extra Epic links) could be set to run by themselves since J4. Such a setting is left exactly as it is: the check goes on starting as it was set. It does not let the run start, because a run does more than the check did (it also writes what is missing), and that is allowed for the run by its own setting. Once the run is set to start by itself, it asks the check and gathers what it answers, and the check is no longer started on its own as well. Under Settings such a check is listed only while it is set, with the run that asks it.
- **The review each record got as it arrived.** Where a run of Organize starts by itself, new records are organized by the run and not reviewed one by one as well: those reviews are not asked, and are no longer counted as waiting. Reviews that asked a question before can still be answered in their panel.

### Told

- **The daily digest** has a part "Organize and Coverage": for each project what the runs that started by themselves did since the last digest (how many, what they applied, created and put before the administrator, and what they cost), and how many cases wait under each page.
- **The project's Dashboard**, under "What happened without you", says the same for the last day.
- **The chat's inbox** (`/inbox`, and what the supervisor reads when asked what waits) lists the cases that wait under Organize and under Coverage of each project. They stop nothing, so they come last and are not counted as pressing.

### Measured again when it changes

What a run may apply without the administrator was already decided by a record, kind by kind: the sampled lines the administrator judged and how many of them wrong, and for created records how many were taken away again. Both records were for the project, whatever models had answered.

Now each belongs to a stage, and a stage is its models:

| Stage | Its models | Its record |
| --- | --- | --- |
| Decision | The model that answers and the judge | Sampled lines judged, and how many wrong |
| Writing | The model that writes, the model that answers and the judge (the last two write and check where no writer is set) | Records made, and how many taken away again |

When the administrator changes a model of a stage, what was judged or made with the models before says nothing of the new ones: **the record starts anew from that moment**, and until it holds again nothing of that stage is applied without the administrator. A stage whose new models do worse than the settings accept stops applying, as before. And the change can be taken back: **one word puts the models back as they were before the last change, and the record they had counts again** (`POST .../review-settings/put-back`). Settings shows both records, since when each counts, and the models before.

Setting models for the first time restarts nothing: there is no record that could be stale.

The stages that ask no model measure themselves already and needed nothing: the scorer is fitted again from the known links whenever they change and sets its own bound so that its sure cases stay within the error the settings accept, and the ranking service's measurement ("How well it works") is worked out from the same links.

### Read on live

Deployed on 28 November 2026 at 01:14 UTC, after two runs of Coverage the administrator had started on falcone and falcone-serverless had ended: nothing is deployed while a run asks. Nothing was pressed.

| | Read |
| --- | --- |
| What starts by itself | Nothing new, on any of the six projects: the four runs are off everywhere |
| Checks set before | On falcone three checks are set to run each night (Issues with no User Story, primary Epics, extra Epic links) and one as records arrive (User Stories nothing supports). They stand exactly as set, and each is listed with the run that asks it. No other project had anything set |
| The record of each stage | falcone: 15 sampled lines judged, none wrong; 12 records made, none taken away. falcone-serverless: no line judged yet; 1 record made. Neither stage was ever changed, so nothing can be put back |
| Told | The inbox lists 86 cases under Coverage of falcone, and 61 under Organize and 41 under Coverage of falcone-serverless. No run started by itself in the last day, and the Dashboard says so |
| Reviews of single records | Stopped by the administrator's switch on every project, as before; none is taken over by a run, because no run of Organize starts by itself |

The page that says whether a run starts by itself answers in 0.2 to 0.3 seconds.

### The first runs of Coverage on live (the administrator's, 27 to 28 November)

O4 said that no run had been started on live. Two were, by the administrator, while O6 was being built. Read afterwards, without pressing anything:

| | falcone | falcone-serverless |
| --- | --- | --- |
| Parts read | 2,855 | 398 |
| Covered without a model (an OpenSpec change named after an Issue) | 481 | 0 |
| Asked about before the run stopped at its tokens | 165 | 141 |
| Of those: covered, by nothing, in doubt | 43, 59, 56 | 59, 8, 36 |
| Groups of uncovered parts written for | 39 | 6 |
| Records made | 12 (9 Issues, 3 User Stories) | 1 Issue |
| Written and put before the administrator | 30 (15 with a sentence not supported, 15 the check could not tell) | 5 |
| Documentation covered now | 41.5% (540 of 1,300) | 58.3% (60 of 103) |
| Cases that wait | 86 | 41 |
| Parts not asked about yet | 2,127 | 259 |
| Cost, and time | $0.31, 77 minutes | $0.23, 61 minutes |

What this showed:

- **One run stops at a million tokens long before the documentation is through.** A question costs about 6,000 tokens for both models together, so a run asks about some 150 parts. For the rest of falcone the page now estimates $3.01 and 14 million tokens (it said 8.9 million before the parts were read: the real questions are longer than the guess), which is fourteen starts as the settings stand. The number of tokens a run may use is the administrator's, under Settings, up to five million.
- **The careful review holds back most of what is written.** Of 45 groups written for, 13 passed and were made; 35 wait, about half because the second model found a sentence the parts do not support. A stronger model to write would change that more than anything else.
- **Where the two models differ, it is mostly the first saying "covered".** On falcone-serverless 21 of the 34 differences are the answering model naming a record and the judge finding none, or finding the part not about the product.
- **Two runs do not ask at once.** The writing for falcone-serverless waited 25 minutes in the queue behind the asking for falcone. That is the check engine's rule, not Coverage's.

### What is not in O6, and why

- **Nothing was set to start by itself on live.** Every run is off until the administrator chooses otherwise, and choosing is theirs. The path is covered by four tests: a new Issue organized without a button and told; records not reviewed one by one where a run organizes them; the record of a stage starting anew and being put back; a connection's plan carried out by itself.
- **A changed model is not tried on old questions by itself.** Such a trial exists ("How well it works", try a model on questions already answered) and costs money, so it stays at the administrator's word. What O6 does without spending is start the record anew, hold back what would be applied, and offer the way back.
- **A run by itself does not publish.** Issues it makes are kept here until the administrator publishes them, and an Epic it writes waits for their word, exactly as with the button.
