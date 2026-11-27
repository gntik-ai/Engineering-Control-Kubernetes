# Reviews in two pages: Organize and Coverage

Proposal, 27 November 2026, asked for the same day. The eight decisions at the end were taken that day, all as recommended. **O0 is built and deployed** ("O0 as built", below); O1 to O6 are not. On the same day the administrator added that connected projects have to be taken into account as if they were part of the project: the plan was adjusted, with one more phase (OC) before O1. See "Connected projects", at the end.

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

Only one connection is recorded. The repositories name each other more than that. Counted in the local copies of five repositories (falcone-serverless has no local copy), as documents that name the other repository:

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

Which of these are connections is the administrator's to say. The platform will propose them with this evidence and record none by itself.

### The adjustment

**A run works on a scope: the project and the projects connected to it.** Every record and every part of a document keeps the project it belongs to, and every line of a list names it.

| Stage | With connected projects |
| --- | --- |
| Read | The records, links and documents of the whole scope. The documents of a connected project are read as documentation of the project |
| Match | Candidates come from the whole scope. A parent may be a record of a connected project: the link crosses projects, as the links of a connection do today |
| Verify | The models see each candidate with its project. An approved link of a connection is a stated link; a pending one is a candidate like any other |
| "Has none" | Said only when nothing in the whole scope fits |
| Create | A twin is searched in the whole scope, so that nothing is created that a connected project holds. **A run creates only in its own project.** What belongs in a connected project is left to that project's run and shown in "Needs you" as such |
| Coverage | A part of the project's documentation is covered by a record of any project of the scope. The parts of connected documentation that ask something of this project are counted beside it, each covered or not |
| Done, undo | A link across projects is undone like any other |

Further rules:

- **Direct connections only.** A project connected to a connected project is not in the scope, unless the administrator says so. Otherwise falcone would bring in every project.
- **Makers.** A question that holds text of two projects goes only to makers both projects allow.
- **Money.** A run is charged to the project it was started in. A larger scope costs more, and the estimate shown before the run says so.
- **The connection's own analysis is taken into the run.** Its question (which records of two projects belong together) is the question of Match and Verify over a scope. Its 500 approved findings stay as links. Its 614 pending ones go through Verify and end in "Done" or "Needs you". Its own run is not resumed, and its view goes in O5 with the others.
- **A change in a connected project** (a new record, a changed document) puts the records it touches up for checking again, in O6.

### Phases, adjusted

One phase is added before O1, because the first button already has to draw its candidates from the scope.

| Phase | What | Done when |
| --- | --- | --- |
| O0 | Built | |
| **OC** | **Connected projects as one scope.** A run and its lines carry the scope and the project of every record. The count of O0 says what each connected project brings. Candidates are drawn across the scope by the ranking service, and the links of connections are read as links. Connections are proposed from evidence, for the administrator to confirm, in Settings. The makers rule | For a Falcone Issue the candidates include Stories of falcone-serverless; the count shows each project with its scope; and no question goes to a maker one of its projects does not allow |
| O1 | As before, over the scope. The pending Issue links of the connection (334) go through it | As before, with every line naming its project |
| O2 | As before, over the scope | As before |
| O3 | As before. Twins are searched in the scope; a run creates only in its own project | As before, and nothing is created that a connected project holds |
| O4 | As before. Connected documentation is read as documentation; the second number | As before, and the parts of connected documents that ask something of the project are counted |
| O5 | As before. The view of a connection and its own analysis are removed; connections are kept in Settings | As before |
| O6 | As before. A change in a connected project is taken up | As before, across the scope |

### Decisions needed

| # | Question | Recommended |
| --- | --- | --- |
| 9 | Which projects are connected? The table above lists what the repositories say | Confirm the first six pairs; leave the weak ones |
| 10 | Direct connections only, or also the connections of connected projects? | **Direct only** |
| 11 | A run creates records only in its own project; what belongs in a connected one is shown and left to that project's run. Agreed? | **Yes** |
| 12 | Text of two projects goes only to makers both allow. Agreed? | **Yes** |
| 13 | The connection's own analysis is not resumed: its pending findings go through Organize, and its view is removed in O5. Agreed? | **Yes** |

