# Reorganizing a project's plan automatically

Status: proposal, 23 November 2026. Nothing in it is built except where a phase says so.

## The problem, in Falcone's numbers

Planning health of Falcone, measured on the live plan (63 Epics, 737 User Stories, 826 issues):

| Misalignment | Count | What it means |
| --- | --- | --- |
| User Stories with no issue | 543 | Delivered without an issue, planned and not broken down, or supported by nothing. The number does not say which. |
| User Stories under several Epics | 704 (3.6 Epics each, 10 at most) | Stories were generated with "link each to all relevant Epics". |
| Stories linked to the largest Epic | 273 (37%) | An effect of the same multiple links: it is the primary Epic of 53. |
| Links with no recorded reason | 2,887 of 3,005 (first written here as 887, a misreading) | Nobody can check them or trust them. |
| Open issues with no Story | 70 | Work that is under way outside the plan. |
| Closed issues with no Story | 209 | Evidence of delivery that no Story can use. |
| Open Stories whose issues are all closed | 89 | Done, or missing an issue. |

Phases R2 and R3 can resolve two of these lines, the 70 open orphans and the 704 primary Epics, by asking a large language model one question per record and waiting for approval. That is about 774 questions and 1.08 million tokens, every answer waits for a person, and the other five lines stay as they are. Asking a language model about everything would be roughly 2,400 questions and 4 to 5 million tokens for Falcone alone, repeated for each project.

## The idea

Most of these decisions are not hard. An issue whose title contains the name of an OpenSpec change belongs to the Story drawn from that change. A Story and an Epic that describe the same capability are recognised by a small ranking model in milliseconds. A language model is needed only where cheap evidence disagrees or is thin, and a person only where the language model is unsure as well, or where something is created or destroyed.

So the process is a cascade. Each stage costs more than the one before and sees fewer cases:

| Stage | What it does | Kind of model | Cost per case |
| --- | --- | --- | --- |
| 0. Keys | Reads identifiers and structure: explicit `Story:`/`Epic:` lines, issue and pull-request numbers, OpenSpec change names in issue titles and branch names, requirement codes, labels, file paths. | None: rules | Nothing |
| 1. Recall | Finds the 20 to 30 candidate parents of a record among hundreds. Word matching (what R2 uses now) joined with meaning matching, by reciprocal rank. | Text embedding model (bi-encoder) | Negligible: each text is embedded once and kept |
| 2. Rerank | Reads the record and each candidate together and scores how well they fit. | Cross-encoder reranker | Milliseconds on a CPU |
| 3. Score | Combines every signal into one probability that the link is right, and decides whether the evidence is enough to act. | Calibrated classifier with a rule for abstaining | Nothing |
| 4. Decide | Only for cases stage 3 abstains on: a constrained question with a closed set of answers. | Native decision model (the platform's existing Decisions task), then a second one of another family when the first is negative or unsure | A fraction of a chat model's call |
| 5. Write | Only where text has to be written: the name of a new Epic, the wording of a missing Story. | Large language model, in batch | Tens of calls per project |
| 6. Person | What is left, and anything that creates or destroys structure, presented as one plan. | You | Minutes |

The first target was that at most one case in five goes beyond stage 3, and at most one in twenty reaches a person. The first measurements, below, put the share that goes beyond stage 3 nearer two or three in five.

## The models, and why each

**Rules (stage 0).** The most reliable evidence is what the records say themselves. Falcone-serverless shows it: 176 of its links are stated in the text, and 41 Stories name their Epic. Falcone's issue titles carry OpenSpec change names (`enhancement(add-tenant-fair-job-admission-queue): …`), and its repository has `openspec/changes/<name>/` with the proposal that the Story was drawn from. Joining on such keys is exact, free and explains itself. This needs the documentation inventory (R4), which is why that phase stays first.

**Embedding model (stage 1).** Turns each Story, Epic, issue and document section into a vector once; similar meaning gives nearby vectors. It finds the candidates that share no word with the record ("installation" and "install the Helm chart", which R3's word comparison missed). About 5,000 texts for Falcone. Used for recall only, never to decide.

**Cross-encoder reranker (stage 2).** A small model that reads two texts together and returns one relevance score. It is far more accurate than comparing vectors and far cheaper than a language model, and it is the right tool for "which of these 20 is the parent". About 30,000 pairs for Falcone covers every existing link, every orphan against its candidates, and every Story without issues against issues and document sections.

**Calibrated scorer with abstention (stage 3).** A logistic regression over the signals: key match, word score, vector similarity, reranker score, the margin between the best and the second candidate, shared labels, whether the Story's other issues agree, how close in time. It is trained on links whose truth is known and calibrated so that "0.97" means 97 in 100 are right. Thresholds are set with split conformal prediction: for each record it yields the set of parents that cannot be ruled out at the chosen error rate. One parent in the set: act. Several or none: abstain and go to stage 4. This is what makes automation defensible: the error rate is chosen beforehand and checked on held-out cases, not hoped for.

**Clustering and assignment (stage 3, for structure).** Density clustering of vectors finds near-duplicate Stories and the natural groups inside an Epic that is too broad. Choosing primary Epics is solved for all Stories at once as an assignment with a soft limit on an Epic's size, so that no Epic becomes a catch-all because it shares words with everything.

**Decision models (stage 4).** The platform already has a Decisions task with OpenAI and OpenRouter (phases 22 to 27 of the project-workspaces notes): binary, choice and ordered-score questions, a rubric, receipts. It fits "is this issue part of this Story: yes, no, cannot tell" exactly and returns a score that stage 3 can use. Two models of different families that agree are accepted; a disagreement goes to a person.

**Language models (stage 5).** Kept for writing. Run through the batch interface and the prompt cache the platform already has.

### Where the ranking models run

Two options for stages 1 and 2:

- **In the cluster, with open models** (chosen). A small service with an ONNX runtime on CPU and an open embedding model and reranker (candidates to benchmark in the first phase: `multilingual-e5-small` or `bge-small` for vectors, `bge-reranker-base` or a MiniLM cross-encoder for reranking). No price per call, no text leaves the cluster, and a fixed model gives the same score for the same input every time. Cost: one more workload of 1 to 2 GB of memory, and a reading of Falcone takes minutes instead of seconds.
- **At a provider.** Embedding and rerank endpoints of an account the platform already has, where the provider offers them. Faster to build, costs cents per project, and the scores can change when the provider changes the model.

The service is written so that either can be used, per project.

## What makes it reliable

1. **Known truth to measure against.** Three sources: links stated in the records' own text (176 on Falcone-serverless); every decision you have made; and one labelled set of about 150 pairs that you judge once, in 30 to 40 minutes, drawn to include easy, hard and wrong cases. Whole Epics are held out for testing, as the existing evaluation notes require.
2. **Error rates chosen beforehand.** Per kind of change (see the table below), with thresholds derived from the held-out set.
3. **Shadow mode first.** The cascade runs over the whole project and reports what it would do, with its measured precision, before it changes anything.
4. **Every change explains itself.** A link made or kept by the cascade records the signals, the score and the two passages that match best. That is also how the 2,887 links without a reason get one.
5. **Everything approved in bulk can be undone.** Changes are applied in batches; a batch can be reverted for 30 days. Link history is already kept.
6. **A sample before the batch.** A batch opens with a random sample of its lines to confirm or reject. If the rejected share exceeds the chosen error rate, the batch is withdrawn and the scorer recalibrated before it is offered again.
7. **The few questions you get are the useful ones.** Cases are sent to a person in order of how many similar cases the answer settles, and each answer recalibrates the scorer.

### Nothing is applied without approval

Decided on 23 November 2026: approval always. The cascade proposes and never applies. What it changes is how much there is to read, and in what order:

| What the scorer finds | How it reaches you |
| --- | --- |
| Changes that add or correct, above the measured threshold: a reason for an existing link, a primary Epic, an issue under a Story, a bug | One batch per kind, to approve with one word. The batch states its measured precision, and a sample of it is shown first. |
| The same, where the scorer abstained and a decision model answered | A second batch, marked as decided by a model, with each answer's reason and passages. |
| Where the models disagree or cannot tell | A short list, one decision each, in the order in which an answer settles the most similar cases. |
| Removing a link the evidence contradicts; closing a Story whose work is done | Their own batch, with stricter thresholds and two independent signals for each line. |
| Removing an Epic, a User Story or an issue; merging two Stories or two Epics | The clean-up plan, grouped by reason. |
| Creating an Epic or a Story; splitting an Epic | The structure plan, already worded. |

An approved batch can be undone as a whole for 30 days. A line you strike from a batch is a label for the scorer.

## What happens to each misalignment

**Links without a reason (2,887: 2,674 between Stories and Epics, the rest between issues and Stories).** Every existing link is scored. Those the evidence supports get their reason and passages recorded. Those it contradicts become candidates for removal. Expected to end near zero without a language model.

**Stories under several Epics (704), and the broad Epic (273).** The reranker scores each Story against its Epics; the assignment chooses one primary Epic per Story; related links that score below the contradiction threshold are removed. Target: every Story has a chosen primary Epic, the average falls from 3.6 Epics per Story to under 2, and no Epic is the primary Epic of more than 10% of Stories.

**Issues without a Story (70 open, 209 closed).** The same cascade. Closed issues are included now, because they are the evidence of delivery that the Stories without issues are missing.

**Records that are no longer relevant.** Not in Planning health today, because nothing looks for them. See "Proposing what to remove" below; Planning health gains "records proposed for removal" and "removed with your authorization".

**Stories without an issue (543).** Each gets a delivery state from evidence, which replaces the single alarming number:

| State | Evidence | Action |
| --- | --- | --- |
| Delivered | Closed issues or merged pull requests that match; a section of `openspec/specs/` or of the published documentation that describes it; an archived OpenSpec change | Link the evidence; close the Story |
| In progress | Open issues, or an open OpenSpec change | Link them |
| Planned | A document section that asks for it, and no work yet | Keep; it is what the next issues are written from |
| Unsupported | Nothing in issues, documents or code | Proposed for removal in the clean-up plan |
| Duplicate | Clusters with another Story | Proposed for a merge in the clean-up plan |

Planning health then follows "Stories that nothing supports" and "delivered Stories still open", which are numbers that should be zero.

**Open Stories whose issues are all closed (89).** Offered as a batch to close when a review found full coverage; otherwise checked by the decision model first.

**Missing Stories and Epics.** Orphans that fit no Story are clustered; each cluster and each document section that no Story covers yields one proposed Story, worded by a language model and placed under an Epic by the same cascade. This is the earlier phase "Epics and Stories grounded in documents and issues".

## Bugs are not part of the plan

A bug is no piece of a User Story's work: it has no User Story and no Epic (decided on 24 November 2026). Every stage of the cascade leaves bugs out: no candidate parents are looked for, no pair is drawn or scored, and no batch proposes a link for one. What the cascade does with bugs is two things only: it proposes that an issue is a bug when the texts say so (the orphan review's "it is a bug" answer, approved like every other), and it lists bugs that still carry a link so the link is taken off on approval. How a bug is recognised and handled is described in [project-workspaces.md](project-workspaces.md#bugs-stand-apart-from-the-plan).

## Proposing what to remove

A backlog fills with records that no longer matter: an issue for a dependency that was replaced, a Story drawn from a document section that was deleted, an Epic whose Stories all moved elsewhere. The process proposes their removal, for Epics, User Stories and individual issues alike. It never removes anything by itself.

The platform can already close and delete all three kinds, with a preview of what the change affects and a confirmation; deleting archives the record and, for a published issue, closes it on GitHub. What is missing is finding the candidates and saying why. That is what this adds.

**Reasons a record is proposed for removal.** Each is checked by rule or by the ranking models; none needs a language model to be found.

| Kind | Reason | Evidence that is shown |
| --- | --- | --- |
| Issue | Duplicate of another issue | The other issue, their likeness score, the passages that match |
| Issue | Superseded: a closed issue or merged pull request delivered the same thing; a newer issue replaces it (a later version bump, a rewritten request) | The record that replaced it |
| Issue | Its subject is gone: it names files, components, an OpenSpec change or a document section that no longer exist | What it names, and the reading of the repository where it disappeared |
| Issue | Stale: open, no activity and no linked work for longer than the project's limit, and no Story that still needs it | Dates, and what was looked for |
| Issue | Its User Story was closed as delivered or removed, and nothing else needs the issue | The Story and its state |
| User Story | Nothing supports it: no issue, no document section, no code that matches | What was searched and the best matches that were not good enough |
| User Story | Its source is gone: the document section it was drawn from was deleted or rewritten (from the documentation inventory's changes) | The section before and after |
| User Story | Duplicate of, or contained in, another Story | The other Story and the matching passages; proposed as a merge that keeps the links of both |
| User Story | Every issue under it was closed as not planned | Those issues |
| Epic | No Story has it as primary Epic after the reorganization | Where its Stories went |
| Epic | Overlaps another Epic: their Stories cluster together | The other Epic; proposed as a merge |
| Epic | Every Story is closed or removed, and no document asks for more | Its Stories and the documents searched |

**Why this needs more care than adding.** Showing that a record is irrelevant means showing an absence, and an absence is easy to get wrong. So:

- A removal is proposed only on two independent reasons, or on one reason that is exact (a stated duplicate, a subject that no longer exists).
- Before it is proposed, a decision model is asked the opposite question, with the current documents as context: is there any reason this is still needed? An answer of yes or cannot tell withdraws the proposal, or shows it as doubtful.
- A record that is doubtful is proposed for closing, not for deletion.

**How it reaches you.** One clean-up plan per project, grouped by reason, each line with its evidence and with what the removal would affect: the Stories of an Epic, the issues of a Story, the links that would be re-pointed. You authorize the whole plan, a group, or single lines, and can strike any line. Nothing in it is applied before that.

**How it is applied.** Through the existing close and delete actions, so that the impact preview and the confirmation record are the same as for a removal you make by hand. Epics and Stories are archived, not erased, and can be restored. A published issue is closed on GitHub as not planned, with a comment that gives the reason and the record that replaced it; reopening it restores it here. Children are never lost: a Story of a removed Epic is first given its new primary Epic by the cascade, and an issue of a removed Story is placed again or listed in the same plan.

**What it learns.** A line you strike is a label: that reason, for that kind of record, was wrong here. The thresholds for proposing follow, so the next plan is shorter and better.

## Cost

For Falcone, with the ranking models in the cluster:

| | Now (R2 and R3, language model for everything they cover) | Cascade |
| --- | --- | --- |
| Records decided | 774 | About 2,400, and 3,000 links explained |
| Paid model calls | 774 chat calls | About 400 to 500 decision calls, and tens of chat calls |
| Tokens | About 1.08 million | About 0.6 million, on the cheapest model that passes the labelled set, in batch |
| Decisions that wait for you | 774 | About 100, and two plans to authorize: clean-up and structure |

The figures on the right are targets to be confirmed in shadow mode, not measurements. The platform reads current prices and shows an estimate before any paid run, as it does now.

Other projects cost less than Falcone: vectors and thresholds are computed per project, but the scorer starts from what was learned on the others.

## First measurements (23 November 2026)

Measured before the ranking service was built, on Falcone-serverless, where 134 issues state their User Story in their own text. The line that names the Story was hidden; each way had to find it among 66 Stories.

| Way of ranking the candidates | Right Story first | Among the first 5 | Among the first 20 |
| --- | --- | --- | --- |
| Shared words, as the worklists use now | 74% | 94% | 99% |
| Meaning alone (`bge-small-en-v1.5`) | 49% | 77% | 90% |
| Meaning alone (`bge-base-en-v1.5`) | 58% | 85% | 96% |
| Words and meaning joined by rank | 70% | 93% | 100% |
| A small reranker on the joined list (`ms-marco-MiniLM-L-12`) | 49 to 57% | 92% | 99% |
| A larger reranker (`bge-reranker-v2-m3`, 8-bit), on 50 of the issues | 56% | — | — |
| **A learned combination of words, meaning and the small reranker**, cross-validated | **82%** | — | — |

What this says, and what it changes in the plan:

- **Word matching is already good at recall.** The right Story is among the first 20 for 99% and among the first 5 for 94%. The first target of R5, 95% among the first 20, is met without a model.
- **No open ranking model beats words by itself.** A generic reranker judges whether two texts are about the same thing, not whether an issue implements a Story, and is worse than words when used alone.
- **Combined, the signals help.** A logistic regression over all of them puts the right Story first for 82% instead of 74%. The combination is what R6 builds; a single model replacing words is not the way.
- **Confidence is what makes approval cheap.** Of the cases the combination is most sure of, the top half is right 96 times in 100 and the top 70% is right 91 times in 100. Those are the batches to approve; the rest goes to a decision model.
- **The target of one case in five reaching a paid model was too optimistic.** On this evidence it is nearer two or three in five at the precision asked for. That still halves the paid calls and, more to the point, orders what is left by how sure each answer is.
- **These projects differ.** Falcone-serverless's issues were written from its Stories and share their wording. Falcone's Stories were generated from a description. The pairs judged on **Reviews → Reorganize** will say how much of this carries over.
- **Speed.** On four slow processors: 3 to 8 texts a second for vectors, 10 pairs a second for the small reranker, 1.3 for the larger one. A reading of Falcone is minutes for vectors and an hour for 30,000 pairs with the small reranker. The larger reranker needs the graphics card to be practical.

An instruction-following reranker or a small language model on the graphics card, asked the real question (does this issue implement part of this Story?), is the candidate that could replace the paid decision model for most cases. It is to be measured on the labelled set once that exists, not assumed.

## The scorer (R6)

**What it learns from.** Every link whose truth is known: stated in a record's own text, chosen by the administrator, or judged as a pair. A pair judged "no" is a known wrong link. All projects are learned from together, because the signals are relative (a place in a ranking, a share of the best score) and one project may have little truth of its own. Bugs are left out.

**Signals.** For each candidate parent of a record: shared words as a share of the best candidate's, and its place by words; closeness of meaning, the distance from the closest candidate, and its place by meaning; the words the two titles share; how close the record lies to the parent's other children; whether it stands among the first by both words and meaning (for an issue) or the share of all Stories the Epic holds (for a Story's Epic); and, where the reranker has scored every candidate, its score, the distance from its best, and whether it is its first choice.

**The model.** A logistic regression with a penalty on large weights, fitted by Newton's method in the console's own process: no service, no cost, and the weights can be read on the page. Each candidate gets a share of belief that it is the parent; what is left of one is the belief that none of them is.

**Knowing when it does not know.** From the known links, each scored by a model that never saw it or any record of its Epic (five folds, by Epic), the scorer takes how far a right parent's share fell short of certainty. The bound at an error of 5% is the distance within which 95% of right parents lie (split conformal prediction). For a new record every candidate within the bound is kept. Several or none kept: it abstains. One kept: it would act, but only where it is sure enough. How sure is measured too: on the known links, the lowest share of belief down to which a single candidate left was wrong at most 5% of the time, over at least ten such cases. Below that, or where ten cases were never reached, it abstains as well. Candidates are the first 30 by words and the first 30 by meaning: with 15 of each, one right parent in twenty was no candidate at all, and what is no candidate can never be chosen. An existing link is called unsupported only when its parent falls outside the stricter bound of 2%.

| Error | Used for |
| --- | --- |
| 5% | Placing an issue under a Story; choosing a primary Epic; recording an existing link as supported |
| 2% | Calling an existing link unsupported |

**What it reports** (Reviews → Reorganize, "What the scorer says, and how well"; until J6 "What the scorer would do"): per kind of link, how many known links it learned from here and elsewhere; measured on this project's own links when it has at least 20, otherwise on all projects together and marked as not yet shown to hold here; how often the right parent is first, is kept, how often it would act and how often it is right when it does; the signals it leans on; and of every record what it would do, the surest first. It also counts the cases that would go on to a decision model, with what they would cost at the price of a question so far.

**The reranker pays its way or is not asked.** Scoring every candidate of every record takes hours on a large project. The scorer is measured with and without the reranker's signals; only where they are measured to help does the ranking service go on to score every record. Until then it scores the measured ones only.

**Where the clean-up will look.** A first count, by reason, of records that one reason speaks against: an issue or a Story that says nearly the same as another, a Story every issue of which was closed as not planned, an Epic no Story has as its primary. One reason never proposes a removal (see above); the count only shows the size of what R9 will examine.

**Shadow mode.** The scorer writes only its own two tables (`scorer_reports`, `scorer_decisions`, migration 0077). No link is made, removed or proposed from them. R7 turns what it would do into batches to approve.

## Batches to approve (R7)

What the scorer would do is turned into batches, one per kind of change. A batch is the unit of approval: nothing in it is applied before your word.

| Batch | What it gathers | Extra condition | Error allowed |
| --- | --- | --- | --- |
| Record why links hold | Existing links with no reason, whose User Story the scorer singles out among all candidates | The link says nothing yet about why | 5% |
| Choose primary Epics | User Stories under several Epics for which the scorer leaves one Epic | You have not chosen a primary Epic yourself | 5% |
| Place issues under a User Story | Issues with no User Story for which the scorer leaves one | The issue still has none | 5% |
| Take off links nothing supports | Existing links the scorer rules out at the stricter bound | The User Story also stands below the fifth place both by shared words and by meaning (the second, independent signal); never a link the text states or you chose | 2% |

**A sample first.** A batch opens with a sample drawn without regard to how sure the scorer is: the square root of its size, at least five lines and at most twenty, or all of them in a small batch. Each sampled line is judged right or wrong. More wrong ones than the error allows for that sample (with these sizes: one) and the batch is withdrawn; the wrong line is not offered again, the scorer learns from it, and a new batch can be made from what is left. Any other line can be struck without withdrawing the batch.

**Approval.** Every line not judged wrong is applied through the same paths the worklists use: the link, the primary Epic, the link's record with who made it. A link the scorer placed is recorded as found by the scorer and approved by you, with the reason in words (what it shares, where it stands, among how many candidates). A line the plan has moved away from since the batch was made is skipped and says why.

**Undo.** Each applied line keeps how the link stood before. An approved batch can be undone as a whole for 30 days: reasons are taken out again, the earlier primary Epic is restored, a placed issue is taken off its Story, a link that was taken off is put back with its record. A link that changed in the meantime is left as it is now.

**A link taken off stays off.** The platform does not make the same link again by itself (the issue remembers it); undoing the batch lifts that.

**What is learned.** A judged line is known truth for the scorer: right lines of a batch that adds, and wrong lines of the batch that takes links off, are right links; the others are wrong links. Lines applied without being judged are not learned from, so that the scorer does not teach itself.

**What the decision stage is for now.** The worklists of R2 and R3 (Reviews → Related Work, Reviews → Planning) remain the place where a model is asked about records the scorer abstains on; their runs start on your word and within the budget, and every answer you approve becomes known truth that the scorer learns from.

## Where each User Story stands (R8)

One state per open Story, worked out again whenever the scorer is. Only what is linked decides the first four; what merely looks alike is a lead and decides nothing. Bugs are not a Story's work and are not counted.

| State | Rule | What follows |
| --- | --- | --- |
| Delivered | It has issues, none open, at least one closed as done; and a second signal shows the whole Story was covered: a review found an issue that covers it in full, or the OpenSpec change of one of its issues is archived or has every task done | Offered in the batch "Close delivered User Stories" |
| Work closed | The same, without the second signal | Shown for you to close, or to add the issue that is missing |
| In progress | An issue of it is open | Nothing |
| Dropped | Every issue of it was closed as not planned | Shown: either still wanted, or to close |
| Work not linked | No issue is linked, but for at least one issue with no Story the scorer gives this Story a share of belief of 20% or more | Leads to link, through the worklist or a batch once the scorer is sure |
| Planned | No issue, linked or alike; a document of the repository lies as close to the Story as an issue typically lies to the Story it is known to belong to (the median over the known links; 0.7 where fewer than ten are known) | Kept: it is what the next issues are written from |
| Nothing supports it | None of the above | One reason for the clean-up, which needs a second |

**Documents.** Each file of the documentation inventory gets a vector from the ranking service, from its title and the beginning of its text. This is per file, not per section: enough to say "a document describes this", not which paragraph.

**Closing delivered Stories.** A batch like the others (sample, strike, approve, undo), at the stricter error of 2% because closing removes a Story from view. It follows from a rule, not from the scorer, so nothing was measured beforehand and the sample is the check. Applying uses the existing close with its preview: a Story is closed only if that touches no open issue, so nothing is written on GitHub. Undoing reopens the Stories.

**Shown where.** Reviews → Planning, "Where each User Story stands": the count per state, and each Story with its issues, OpenSpec changes, leads and documents.

## The clean-up as built (R9)

**Reasons, by kind.** Two reasons count as two only when they are of different kinds.

| Kind | Reason | For |
| --- | --- | --- |
| Absence | No issue and no document supports it (its delivery state) | User Story |
| Model | A model, shown the Story with the issues and documents closest to it, found nothing that has to do with it | User Story |
| Alike | It says nearly the same as another record (vectors at 0.95 or closer). Of two Stories the one with more work stays; of two open issues the older | User Story, issue |
| State | Every issue under it was closed as not planned; every Story under the Epic is closed; every Story the issue is under is closed; the issue it says the same as was closed as done | all three |
| Age | Open, under no Story, untouched for more than 180 days | Issue |
| Structure | No Story has the Epic as its primary one | Epic |
| Stated (exact) | The issue's own text says it duplicates another; the Epic holds no Story | Issue, Epic |

**Proposed or only listed.** A record with an exact reason, or with reasons of two kinds, is proposed for closing. With one reason it is listed and nothing more. Closing is never deleting: a closed Story or Epic can be reopened.

**The model's check.** A run the administrator starts, with a model and a token limit, over the Stories that nothing supports. One question per Story: the Story, the four issues with no Story and the three documents that lie closest to it, and three answers. "None has to do with it" is the second reason. "This one is its work" names the piece and withdraws the Story from the proposals, as does "cannot tell". An answer whose quoted passage is not in the Story's text gives no reason. The run uses the same engine, budget and controls as the worklists. A model that thinks before it answers spends its thinking against the limit of its answer, so the limit of these short answers carries room for it (8,000 tokens beside the answer): the first run on Falcone, with a model that always thinks, had every answer cut at 400 tokens and stopped after three. A run that stopped after failures in a row asks what failed again when it is resumed.

**Batches.** "Close User Stories nothing stands behind" and "Close Epics nothing stands behind" on Reviews → Reorganize, at the error of 2%, with the sample, the strike and the undo of every batch. A line is applied only if the reasons still hold and closing touches nothing but the record itself.

**Issues.** Closed by the batch "Close issues nothing stands behind" (decided on 24 November 2026: the system closes them, not a person by hand). The same sample and strike as every batch. On approval each issue is closed on GitHub as not planned, after one comment that gives the reasons in the words of the approved line, names the issue that replaced it, and says to reopen the issue if it is still needed. The comment carries a marker and is not written twice. This is the one batch that cannot be undone from here: an issue closed by mistake is reopened on GitHub, and the next import brings it back as open.

**Not built.** Reasons that need what the project does not have yet: a Story's source document being gone (Stories do not record their source), an issue's subject no longer existing in the repository, an Epic overlapping another (R10). No code is read: the project has no code summaries.

## The structure of the plan (R10)

**How each Epic holds together.** A Story is counted once, under its primary Epic. For each Epic the report gives its primary Stories and their share of all Stories, the Stories linked to it at all, how close its primary Stories lie to one another, and "closer elsewhere": the Stories linked to it although every other Epic they are under lies closer. That last number is what the breadth of an Epic by links is made of; removing such links waits for judged pairs that say "related" or "no relation", which is what a model of relatedness is learned from.

**Splits.** An Epic that is the primary Epic of more than 10% of the Stories, and of at least 20, is too broad. Its primary Stories are grouped by meaning into two to four groups of at least five, where that divides them better than leaving them together. The largest group stays; each other group would become an Epic, named provisionally from the words that set its Stories apart. On approval, with the titles the administrator gives, each new Epic is made and becomes the primary Epic of its Stories; the Stories stay linked to the Epic they came from.

**Missing User Stories.** Issues that belong to no Story, and for which the scorer gives no existing Story a share of belief of 20% or more, look like nobody's work. Among them, groups are formed around the issue with the most neighbours: every issue that lies as close to it as two issues under the same Story typically lie (issues are more alike among themselves than an issue is like a Story, so the measure is taken from issues). A group of more than twelve is not one Story: it is looked at again more strictly until its parts are small enough. A group of three to twelve with at least one open issue is proposed as a User Story: called after its oldest open issue without the labels in front of the title, described by the list of its issues, and placed under the Epic closest to it. The words and the Epic are the administrator's to change. Making the Story links its issues to it. A group of closed issues only is work that was done with no Story: it is counted, and nothing is planned from it.

**Decisions are kept.** A proposal is known by a digest of what it is made of. One that was approved or set aside is not made again while it stays the same.

**Documents.** The documents that lie close to no open Story are counted and the first are listed. Nothing is proposed from them yet: a file may be no plan at all, and wording a Story from one needs a language model.

## Continuous and everywhere (R11)

**What runs by itself, and how soon.**

| Step | By | How soon |
| --- | --- | --- |
| Issues read from GitHub | the backlog sync | every 15 minutes |
| Documents read when the branch moved | the documentation inventory | checked every 15 minutes |
| A vector for a new or changed record | the ranking service | within 2 minutes of the record |
| The scorer, then the Stories' states | their keeper | looked at every 2 minutes, worked out at most every 5 |
| Clean-up and structure | on reading | at once |
| Everything again, whatever changed, and the day's health | the daily refresh | once a day, after 02:00 UTC |

So an issue opened on GitHub is, within about twenty minutes and without anyone asking, either flagged as a bug, shown with its closest User Story in the worklist, or in a batch to approve. Nothing is linked by that: approval always.

**The trail.** The next steps in order and what moved since the day before are in Reviews → Needs you (J6 of [judged-reviews.md](judged-reviews.md)). Reviews → Reorganize has "Issues that arrived lately": the issues that arrived in the last two weeks, each with where it stands: a bug, under a Story (and who linked it), in a batch, waiting with its closest Story, or not yet read or scored. An issue arrived on the day it was opened on GitHub, not on the day the platform first read it (a project's whole history is read on one day), and only issues of the plan are counted: one that is itself an Epic or a User Story is not.

**Drift.** Planning health is measured every day. The latest measurement is compared with the one before, for the numbers that should not grow: changes waiting, records proposed for closing, proposals for the structure, open issues with no Story, issues that fit no Story, delivered Stories still open, Stories with unlinked work, Stories with nothing behind them, bugs still linked, records the scorer abstains on. The chat's daily digest gets one line for each project where something waits or moved.

**Every project.** The Administrator Console lists all projects: whether each has a plan, what the ranking service has read, what the scorer is sure of, the pairs judged, and the next steps. Thresholds are each project's own once it has twenty known links of a kind; until then the measurement of all projects together stands in and the row says to judge the pairs.

**A project with no plan.** Musematic, llmwiki, gntik-ui and falcone-workflow have no Epics and no User Stories. The cascade organizes a plan; it does not write one. Their documents are read and have vectors, so that Discovery, when it is run for them, starts from the inventory; until then there is nothing to reorganize.

## Epic pages (R12)

An Epic's own text says what was meant when it was written. What became of it is spread over its User Stories, their issues and the repository's documents. Reviews → Epic pages brings that together, one page for each Epic, and everything on a page is a record that can be opened.

**What a page holds.** The Epic's text. Where it stands: how many of the User Stories whose primary Epic it is are in progress, delivered, closed, planned or supported by nothing (the states of R8), and how many issues under them are open, done and closed as not planned. The Stories themselves in that order, each with its issues, the issues that only look like its work, the document it is described in and the OpenSpec change that shows it done. The Stories that are also linked to it but belong to another Epic first, with that Epic named. The six documents of the repository closest to the Epic and its open Stories taken together, each with how it begins; a document one of its Stories rests on is said to describe that Story, and one that is merely the closest there is says so. What needs attention: delivered Stories still open, Stories with nothing behind them, work that only looks like a Story's, a Story with no primary Epic chosen, an Epic without a description or without Stories. Bugs are on no page.

**No model is asked for a page.** It is put together from the records, and put together again whenever one of them changed: after the scorer and the Stories' states were worked out (R11), in the daily refresh, and when it is read. A page that differs from its last version is kept as a new one, with what changed said in sentences: a Story that came or went, a Story that moved from one state to another, an issue that was linked, closed or opened again, a document that came close or no longer is. The last thirty versions are kept, so the page is also the Epic's history. How alike two texts are is left out of the comparison, so that a new reading by the ranking service alone makes no new version.

**An overview written by a model, when asked for.** A run the administrator starts, with a model and a token limit, in the same engine as the other runs. One question for each open Epic that has Stories: the Epic's text, up to forty of its Stories with where each stands, its issue counts and three issue titles, and up to five documents with how each begins. The model writes four to seven sentences and must give with each the records it rests on. A sentence that names no record it was shown is dropped; an overview with none left is no answer and is asked again by the next run. The page shows each sentence with its records as links, says which model wrote it and when, and says so when the records have changed since. An overview changes nothing in the plan: the Epic's own description stays as its author wrote it.

**To take elsewhere.** A page can be downloaded as a Markdown document, with the issues linked to GitHub and the overview, if there is one, marked as written by a model.

## The Epics a Story is also linked to (R13)

The plan's own target for "Stories under several Epics" was not reached by R1 to R12: on 25 November 2026 the 595 open Stories of Falcone stood under 3.6 Epics each, with 1,567 links to an Epic that is not the primary one, and the 66 of Falcone-serverless under 4.8 each, with 248 such links. The scorer chooses a primary Epic among a Story's Epics; it never speaks against the others. Whether an Epic is *related* to a Story is not something it can learn from the judged pairs: only twenty of them ask that question. So it is asked of a model, as the third stage of the cascade.

**Which links are judged.** A link of an open Story to an Epic that is not its primary one, that the Story's own text does not state, and that the administrator did not make by hand. A link that came with a proposal approved as a whole is judged: on Falcone and Falcone-serverless every link of a Story to an Epic came that way, marked as confirmed and with no reason, and those are the links nothing explains. The primary Epic is shown to the model for comparison and never judged, so no Story is left without an Epic.

**The question.** One for each Story: its text, its primary Epic, and up to twelve of the other Epics. Of each of those the model answers *part* (delivering the Story is part of reaching that Epic's goal), *unrelated* (the Story would be delivered the same, and matter the same, if the Epic did not exist) or *unclear*. It is told that an Epic about a quality many Stories share has a Story as part only when that quality is what the Story is about, that shared words or a shared component are no relation, and to answer unclear when in doubt. It quotes one passage of the Story and one of each Epic it judges. An answer about an Epic it was not asked to judge is dropped; an Epic it leaves out stands as unclear. The Stories with the most links to judge are asked first, so that a run that stops at its limit has asked where the most can be taken off.

**What follows.** Nothing, by an answer alone. A link found unrelated, whose quoted passages are in the Story's and the Epic's texts, becomes a line of the batch "Take off Epic links a model found unrelated" under Changes to approve: a sample is judged first, any line can be struck, one wrong line in the sample withdraws the batch, approval takes the links off, and the batch can be undone for thirty days, which puts each link back as it stood. A line judged wrong is not proposed again. A line judged right teaches the scorer that this Epic is not that Story's primary one. An answer no longer stands once the Story's text, an Epic's text or the Story's links changed: nothing is proposed from it and the next run asks again.

**Followed.** Planning health gains the links beyond the primary Epic and how many of them are not judged; the second is watched for drift and is a next step of the project.

**The estimate of a run.** The first runs used about 1.8 times what the page had estimated (four characters a token and a short answer): the texts are dense, the shape of the answer is counted, and a model that thinks writes more than its answer. The estimate of every run of this engine is now two and a half characters a token and an answer with its thinking.

## Revised plan

R0 to R3 are done and deployed. Their worklists stay: they become the place where the cases the cascade abstains on are decided, and where its batches are approved.

| Phase | What | Done when |
| --- | --- | --- |
| R4 | Documentation inventory (server side built, console page pending). Extended: reads the state of OpenSpec changes (proposed, in progress, archived), extracts identifiers from documents, issues and Stories, and keeps which sections were deleted or rewritten, as evidence for removals. | Every project's documents are listed with what changed since the last reading; identifiers join issues, changes and Stories where they exist. |
| R5 | Signals. The measurement of each way of ranking against known truth, and the pairs to judge (built). Ranking service with an embedding model and a reranker; vectors kept per record and refreshed when its text changes. | The true parent is among the top 20 candidates for at least 95% (met by words alone: 99%). Every way of ranking is shown beside word matching on the same known links. The earlier target of 85% first by a reranker alone is dropped: see the first measurements. |
| R6 | Scorer in shadow mode (built, 24 November 2026). One learned judgment from all signals; a bound per error rate that says which candidates cannot be ruled out; a report per project of what it would do with every record, and a first count of where the clean-up will look. The reranker is asked about every record only where it is measured to help. Not in R6 after all, and moved to R7 with the batches: asking a decision model about the cases the scorer abstains on, which spends money and so starts on your word; and the trial of a local model on the graphics card, which waits until a workload in the cluster is shown to reach the card. | The scorer is measured on known links it never saw, per project where there are enough. Where it would act it is right at least as often as the chosen error allows; how often it abstains is reported, not assumed. Nothing is changed. |
| R7 | Reorganize links (first part built, 24 November 2026). The scorer's sure cases reach you as batches by kind: reasons recorded, primary Epics chosen, issues placed, unsupported links taken off. A sample is judged before a batch can be approved, any line can be struck, and an approved batch can be undone for 30 days. What you judge teaches the scorer. Planning health follows what the scorer is sure of and what it abstains on. Still to build in R7: using the scorer's order and candidates in the worklists' runs, so that a model is asked only about what the scorer abstains on, with a shorter list. | Each kind of change can be approved in one word after its sample, and undone. On Falcone the batches are empty until the scorer has enough known links there; on Falcone-serverless they hold what the scorer is sure of. |
| R8 | Reorganize Stories (built, 24 November 2026). Every open Story has a delivery state from its issues, the OpenSpec changes of those issues, issues with no Story that look like its work, and the repository's documents. Delivered Stories are a batch to close. Code summaries are not read yet: no state depends on them so far. | Every Story has a state with its evidence; "Stories that nothing supports", "delivered Stories still open" and "Stories whose work is not linked" are followed in Planning health. |
| R9 | Clean-up (built, 24 November 2026). Records that reasons speak against are found among User Stories, Epics and open issues; a record is proposed for closing on two reasons of different kinds or an exact one. A run that asks a model about the Stories nothing supports gives the second reason, or withdraws the Story. Proposed Stories and Epics are batches to authorize, with sample and undo. Proposed issues are closed on GitHub by their own batch, as not planned and with a comment that gives the reasons. | Every proposal shows its reasons and evidence; nothing is closed without authorization; a closed Story or Epic can be reopened by undoing the batch; an issue is reopened on GitHub. |
| R10 | Reorganize structure (built, 24 November 2026). A report of how each Epic holds together; a split proposed for an Epic that is the primary Epic of more than 10% of the Stories; User Stories proposed from groups of issues that fit no Story. Each proposal is worded provisionally, edited and approved or set aside by the administrator. Not built: Stories proposed from documents no Story covers (they are counted and listed), and wording by a language model. | Each proposal can be edited, approved or set aside; what is set aside is not made again; an Epic above 10% has a split to approve. |
| R11 | Continuous and everywhere (built, 24 November 2026). The pieces already ran by themselves; this adds what makes that visible and dependable: one table of every project with its next step, a trail of the issues that arrived with where the cascade put each, a daily refresh of everything with the drift since the day before, and that drift in the chat's daily digest. A project that was never through Discovery has no plan to reorganize: its row says so and points to Discovery. | A new issue is read, scored and shown with its closest Story within minutes, without anyone asking; each project is measured on its own links once it has them, and has its own row with what waits. |
| R12 | Epic pages (built, 25 November 2026). One page for each Epic, put together from its Stories with where they stand, their issues and the closest documents, with no model asked; kept in versions with what changed; refreshed when a record changes. On request a model writes an overview in which every sentence names its records. Not built: a page for a component of the code, and sending the pages to a wiki outside the platform. | Each Epic has a page that cites its Stories, issues and documents. |
| R13 | The Epics a Story is also linked to (built, 25 November 2026). Added after the plan was through, because its target for Stories under several Epics was not reached. A model says of each link beyond the primary Epic whether the Story is part of that Epic, unrelated or unclear, with quoted passages; links found unrelated are a batch to approve, with sample and undo. Not built: learning relatedness in the scorer from the lines you judge, so that it proposes such removals itself. | The average number of Epics per Story falls as batches are approved; no Story loses its primary Epic. |

### After it: a cheaper model for the reviews (25 November 2026)

Asked for on 25 November. The analysis and the prices are in [inkling-models.md](inkling-models.md). In 90 days, 98.5% of the tokens went to the reviews of this plan and next to nothing to code, so the saving is here. Decided the same day: Inkling through OpenRouter, no model on our own nodes, and never as a default for writing code.

| Phase | What | Done when |
| --- | --- | --- |
| M0 | Try a model on questions already answered (built, 25 November 2026). A trial asks another model the questions a check has an answer to, on a sample that is the same for every model, keeps what it says apart from the plan, and reports agreement with the answer on record, the share of "unclear", the share of quotes found in the texts, and the cost of a question. | Each model tried has those numbers for each check it was tried on. |
| M1 | Two models in a run (built, 25 November 2026). A run of a check may name a first model; an answer of it is kept when it is clear and its quoted passages are in the texts, and everything else is asked of the run's own model. The run reports what each did and what it cost against the run's own model alone. | A run of the Epic-links check with two models costs less than the same run with the second alone, and its batch passes its sample. Not yet shown on live. |
| M2 | The cheaper model first, by default (built for the four checks, 25 November 2026). A check asks first the model whose latest trial on it kept at least 30 answers by itself with 90% of them as on record. Never for writing code. Not built: the same for summaries, Epic overviews and bug triage, and a Dashboard tile. | A check with a passed trial preselects that model; one without preselects none. |

### After it: judged reviews (26 November 2026)

Asked for on 26 November: a judge in every Reviews workflow, one Settings view, and only the uncertain cases shown. The inventory, the models, the costs and the decisions it needs are in [judged-reviews.md](judged-reviews.md). Decided the same day: Inkling is for writing code only and is never asked in a review ([inkling-models.md](inkling-models.md)); M0 to M2 above stay as built, for other models, and go into J0 and J1. J0 to J6 are built.

| Phase | What | Done when |
| --- | --- | --- |
| J0 | Rules and measurement (built, 26 November 2026). Inkling refused in every review. A model's family is its maker. Each candidate judge is asked about pairs whose truth is known. | For each judge, and each pairing with an answering model: how often it accepts a wrong answer, how often it refuses a right one, and the cost of a verdict. Measured the same day on Falcone: no judge asked about one pair alone lets through fewer than 8 of 19 wrong pairs, so the judge of J1 will be a second answer or a comparison, to be measured first. |
| J1 | The judge in the checks (built, 26 November 2026). Candidates by meaning joined with those by words, twelve. A run can name a judge of another maker that answers unseen; what differs goes back once; each answer ends agreed or in doubt. Worklists show what is in doubt first. What is agreed goes into the batches that place issues and choose primary Epics, with sample and undo. For issues with no User Story, primary Epics, the clean-up and extra Epic links, where the two are compared link by link. A third model, named in Settings with its own sum in dollars, is asked about what stays in doubt and says with which of the two it sides; it decides nothing. | Measured on the issues whose Story is known: two cheap models that agree are right about 85 times in 100, not 95, so nothing is applied without approval; the batch's sample is the measurement that could change that. |
| J2 | Settings in Reviews (built, 26 November 2026). A limit in dollars a month for each project, $3 from the deployment on, beside the token limit, which moved here; the model that answers and the judge, which a run of a check takes without a form; whether an answer goes back; the makers a project's texts may be sent to, which hold for every review; the model and the switch of the automatic reviews, here only; the numbers the reviews work to, which can be changed for each project; and what each workflow and model cost, with what a run of each check is likely to cost. | A project's reviews stop asking when its dollar limit is reached; no review asks a model of a maker the project does not allow; no model, token limit or switch of Reviews is set on another page of Reviews. |
| J3 | Only what is in doubt (built, 26 November 2026). Each answer is settled, needs the administrator, or changed. Settled: two models chose alike at once and the texts alone rank the same candidate first, which measured right 32 times in 35 against 10 in 15 where the texts point elsewhere. Both worklists open on what needs the administrator, the most doubtful first. What is settled goes on with what the scorer is sure of: as one batch to approve, or applied without the administrator where the settings ask for it and the project's record of judged samples allows it, with one line in twenty to judge afterwards, a wrong line put back, and a stop when the record no longer holds. The daily digest says what needs the administrator. | No project applies anything yet: each proposes, and none has a record. On Falcone the Stories shown for a primary Epic are those that need the administrator, not all 424, once a run with a judge has answered them. |
| J4 | By itself (built, 26 November 2026). For each of the five checks a project's settings say whether it runs only when started, by itself as records arrive, or by itself each night. A run that starts by itself takes its models and limits from Settings like any other, is not started while one is under way or on hold, and does not ask again within a day about a record no model could answer. The daily digest says what ran and what it cost. The night run asks at the usual price: handing it to the providers as a batch at half price is left out, because a first pass costs about a dollar and a day's records a few cents. | Where a project turns it on, nothing in Related Work or Planning needs a run started by hand. No project has turned it on: it is off unless chosen, because it spends money. |
| J5 | The judge everywhere else (built, 26 November 2026). One step asks the project's judge the same question unseen, wherever it is of another maker than the model that answered. Epic overviews: each sentence is checked against the records it names. Coverage and the parents a planning review names: applied only where both agree. Discovery, connections and proposals: each says whether the judge proposed the same. Execution order: a type the judge reads otherwise is ambiguous. Review evidence: the second opinion is asked about every verdict. The structure of the plan asks no model. | Every review that asks a model can have its answers judged, and its list shows what is in doubt first; in the planning tables a proposal is marked, not moved up. Not judged where judge and model are of one maker, as Falcone's planning model and judge are. |
| J6 | One list of what needs you across Reviews, with each workflow's view behind it; panels that say the same twice merged (built, 26 November 2026). | Reviews opens on one short list of decisions. |

Decided on 26 November 2026: what both models agree on is applied without approval where it adds or corrects and can be undone, within an error rate of 5%; what removes, closes or writes to GitHub stays a batch to approve. "Approval always" (decision of 23 November, below) holds until J3 builds that, and after it for those batches.

### Queued: Reviews in two pages (27 November 2026)

Asked for on 27 November: Reviews has too much on it. It is to become Organize (two buttons: Epics and User Stories; User Stories and Issues), Coverage (the documentation against the plan) and Settings, with missing records created after a careful review and only Issues on GitHub. The proposal, what happens to each of today's panels, and the decisions it needs are in [organize-and-coverage.md](organize-and-coverage.md). None of it is built; it starts once the decisions are taken.

| Phase | What | Done when |
| --- | --- | --- |
| O0 | **Built, 27 November 2026.** Ground: one run type with stages and three lists; the numbers a first run would meet; only Issues are written to GitHub. See "O0 as built" in `organize-and-coverage.md`. | The numbers are known for each project, and no new Epic or Story reaches GitHub. |
| OC | **Built, 27 November 2026** (see "OC as built" in `organize-and-coverage.md`). Added 27 November 2026: connections. Adding or changing a connection prepares its plan (the requirements to update and how they will be verified, with the most it can cost); at the administrator's word the changes for Epics, User Stories and Issues of both projects are proposed and each reviewed by a second model, into the three lists. Runs of one project read, match and search for twins over the project and the projects connected to it. See "Connected projects" in `organize-and-coverage.md`. | A connection added on the page shows its plan without a model being asked; carrying it out fills the three lists; a Falcone Issue is offered Stories of falcone-serverless. |
| O1 | **Built, 27 November 2026** (see "O1 as built" in `organize-and-coverage.md`). Organize, User Stories and Issues: linking, behind one button. | Every open Issue that is no bug is under a Story or in "Needs you". |
| O2 | **Built, 27 November 2026** (see "O2 as built" in `organize-and-coverage.md`). Organize, Epics and User Stories: linking, behind the other button. | Every open Story has one Epic or is in "Needs you". |
| O3 | **Built, 27 November 2026** (see "O3 as built" in `organize-and-coverage.md`). Creating what has no counterpart, after the careful review; the list "Created", with undo. | No open Story without an Epic and no open Issue without a Story, except what is in "Needs you". |
| O4 | **Built, 27 November 2026** (see "O4 as built" in `organize-and-coverage.md`). Coverage: the documentation in parts, matched and verified, one number, missing records created. | One number for each project; a part nothing covered has a record or is in "Needs you". |
| O5 | **Built, 27 November 2026** (see "O5 as built" in `organize-and-coverage.md`). Two pages: the old views removed, what survives moved, old addresses redirected. | Reviews has three entries: Organize, Coverage, Settings. |
| O6 | By itself, and told: organized as records and documents arrive; Dashboard, digest and chat; each stage measured again when it changes. | A new Issue is under a Story or in "Needs you" without a button. |

The eight decisions the plan needed were taken on 27 November 2026, all as recommended: Issues a run creates are kept here and published together on one word; new Epics and User Stories are made without asking and can be undone for 30 days; the Epics and Stories that are GitHub issues are closed there in one approved batch, in O5; a Story with no Issue whose work is done is proposed for closing; an Epic with no Story and nothing that says what it should hold is the administrator's to close or explain; Coverage counts the parts of the documentation about the product; a run that would pass the dollar limit stops and shows its estimate; the six old views are removed in O5.

## Decisions (23 November 2026)

1. **Approval always.** Nothing is applied without the administrator's word; the cascade prepares batches and plans.
2. **The ranking models run in the cluster, with open models.** The node has 32 GB of memory and a graphics card with 6 GB, which is enough for an embedding model and a reranker of the base size on the card, and for larger ones on the CPU.
3. **A labelled set of about 150 pairs** will be judged once by the administrator.

## Decisions (24 November 2026)

1. **A bug is no part of the plan.** It has no User Story and no Epic, is listed on its own tab and is left out of every stage of the cascade. A link a bug still carries is taken off on approval.
2. **The system closes issues.** A proposed issue is closed on GitHub by a batch the administrator approves, as not planned and with a comment; not by hand.
