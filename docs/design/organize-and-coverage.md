# Reviews in two pages: Organize and Coverage

Proposal, 27 November 2026. Asked for the same day. Nothing of it is built. It needs the decisions at the end before the first phase starts.

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

## Decisions needed

| # | Question | Recommended |
| --- | --- | --- |
| 1 | The Issues a run creates: written to GitHub at once, or kept here and published together on one word from you? | **Kept here, published together.** On 26 November you decided that what writes to GitHub stays a batch you approve. One button publishes them all; none is public before you saw it |
| 2 | New Epics and User Stories are made and linked without asking, and can be undone for 30 days. Confirm? | **Yes.** It is your decision of 26 November applied to creation: it adds, and it can be undone. With the 5% you accepted, watched by the sample |
| 3 | The Epics and User Stories that are GitHub issues today: left open there, or closed there with a comment, in one batch you approve? | **Closed in one approved batch**, in O5, after their number is known in O0. Their records stay here. Left open, GitHub would show a plan that nothing updates |
| 4 | A User Story with no Issue whose work is done: proposed to be closed, for you, and no Issue made? | **Yes.** An Issue for finished work would be wrong work |
| 5 | An Epic with no Story and nothing that says what it should hold: yours to close or explain, and no Story invented? | **Yes** |
| 6 | Coverage counts the parts of the documentation that are about the product, and you can correct which are. Agreed as the measure? | **Yes** |
| 7 | Writing records needs a stronger model than choosing among candidates. The limit is $3 a month for each project. May a run that would pass it stop and show its estimate, for you to raise the limit or not? | **Yes.** A first full run over Falcone will cost more than $3; how much is shown before it starts |
| 8 | May I remove the six old views in O5, with the measurements moved into Settings and the Epic pages opened from the planning tables? | **Yes**, once O1 to O4 are shown to do what they did |
