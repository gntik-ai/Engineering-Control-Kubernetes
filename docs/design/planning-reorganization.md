# Reorganizing a project's plan automatically

Status: proposal, 23 November 2026. Nothing in it is built except where a phase says so.

## The problem, in Falcone's numbers

Planning health of Falcone, measured on the live plan (63 Epics, 737 User Stories, 826 issues):

| Misalignment | Count | What it means |
| --- | --- | --- |
| User Stories with no issue | 543 | Delivered without an issue, planned and not broken down, or supported by nothing. The number does not say which. |
| User Stories under several Epics | 704 (3.6 Epics each, 10 at most) | Stories were generated with "link each to all relevant Epics". |
| Stories linked to the largest Epic | 273 (37%) | An effect of the same multiple links: it is the primary Epic of 53. |
| Links with no recorded reason | 887 of about 3,000 | Nobody can check them or trust them. |
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

The target is that at most one case in five goes beyond stage 3, and at most one in twenty reaches a person.

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

- **In the cluster, with open models** (recommended). A small service with an ONNX runtime on CPU and an open embedding model and reranker (candidates to benchmark in the first phase: `multilingual-e5-small` or `bge-small` for vectors, `bge-reranker-base` or a MiniLM cross-encoder for reranking). No price per call, no text leaves the cluster, and a fixed model gives the same score for the same input every time. Cost: one more workload of 1 to 2 GB of memory, and a reading of Falcone takes minutes instead of seconds.
- **At a provider.** Embedding and rerank endpoints of an account the platform already has, where the provider offers them. Faster to build, costs cents per project, and the scores can change when the provider changes the model.

The service is written so that either can be used, per project.

## What makes it reliable

1. **Known truth to measure against.** Three sources: links stated in the records' own text (176 on Falcone-serverless); every decision you have made; and one labelled set of about 150 pairs that you judge once, in 30 to 40 minutes, drawn to include easy, hard and wrong cases. Whole Epics are held out for testing, as the existing evaluation notes require.
2. **Error rates chosen beforehand.** Per kind of change (see the table below), with thresholds derived from the held-out set.
3. **Shadow mode first.** The cascade runs over the whole project and reports what it would do, with its measured precision, before it changes anything.
4. **Every change explains itself.** A link made or kept by the cascade records the signals, the score and the two passages that match best. That is also how the 887 links without a reason get one.
5. **Everything automatic can be undone.** Changes are applied in batches; a batch can be reverted for 30 days. Link history is already kept.
6. **Audit by sample.** One automatic change in twenty is shown to you to confirm or reject. If the rejected share exceeds the chosen error rate, that kind of change stops being automatic until the scorer is recalibrated.
7. **The few questions you get are the useful ones.** Cases are sent to a person in order of how many similar cases the answer settles, and each answer recalibrates the scorer.

### What is automatic and what is not

This changes the rule agreed in R2, "approval always". Proposed policy, settable per project:

| Change | Error rate allowed | Default |
| --- | --- | --- |
| Record the reason and passages of an existing link | — | Automatic |
| Choose a Story's primary Epic; put an issue under a Story; mark an issue as a bug | 3 in 100 | Automatic above the threshold, with undo and audit |
| Remove a link the evidence contradicts; close a Story whose work is done | 1 in 100, and two independent signals | Automatic above the threshold, with undo and audit |
| Archive a Story that nothing supports; merge two Stories | 1 in 100 | One batch for you to approve |
| Create an Epic or a Story; split an Epic | — | One plan for you to approve, already worded |

## What happens to each misalignment

**Links without a reason (887).** Every existing link is scored. Those the evidence supports get their reason and passages recorded. Those it contradicts become candidates for removal. Expected to end near zero without a language model.

**Stories under several Epics (704), and the broad Epic (273).** The reranker scores each Story against its Epics; the assignment chooses one primary Epic per Story; related links that score below the contradiction threshold are removed. Target: every Story has a chosen primary Epic, the average falls from 3.6 Epics per Story to under 2, and no Epic is the primary Epic of more than 10% of Stories.

**Issues without a Story (70 open, 209 closed).** The same cascade. Closed issues are included now, because they are the evidence of delivery that the Stories without issues are missing.

**Stories without an issue (543).** Each gets a delivery state from evidence, which replaces the single alarming number:

| State | Evidence | Action |
| --- | --- | --- |
| Delivered | Closed issues or merged pull requests that match; a section of `openspec/specs/` or of the published documentation that describes it; an archived OpenSpec change | Link the evidence; close the Story |
| In progress | Open issues, or an open OpenSpec change | Link them |
| Planned | A document section that asks for it, and no work yet | Keep; it is what the next issues are written from |
| Unsupported | Nothing in issues, documents or code | Batch to archive, for approval |
| Duplicate | Clusters with another Story | Batch to merge, for approval |

Planning health then follows "Stories that nothing supports" and "delivered Stories still open", which are numbers that should be zero.

**Open Stories whose issues are all closed (89).** Closed automatically when a review found full coverage; otherwise checked by the decision model.

**Missing Stories and Epics.** Orphans that fit no Story are clustered; each cluster and each document section that no Story covers yields one proposed Story, worded by a language model and placed under an Epic by the same cascade. This is the earlier phase "Epics and Stories grounded in documents and issues".

## Cost

For Falcone, with the ranking models in the cluster:

| | Now (R2 and R3, language model for everything they cover) | Cascade |
| --- | --- | --- |
| Records decided | 774 | About 2,400, and 3,000 links explained |
| Paid model calls | 774 chat calls | About 400 to 500 decision calls, and tens of chat calls |
| Tokens | About 1.08 million | About 0.6 million, on the cheapest model that passes the labelled set, in batch |
| Decisions that wait for you | 774 | About 100, and three batches |

The figures on the right are targets to be confirmed in shadow mode, not measurements. The platform reads current prices and shows an estimate before any paid run, as it does now.

Other projects cost less than Falcone: vectors and thresholds are computed per project, but the scorer starts from what was learned on the others.

## Revised plan

R0 to R3 are done and deployed. Their worklists stay: they become the place where the cases the cascade abstains on are decided, and where its batches are approved.

| Phase | What | Done when |
| --- | --- | --- |
| R4 | Documentation inventory (server side built, console page pending). Extended: reads the state of OpenSpec changes (proposed, in progress, archived) and extracts identifiers from documents, issues and Stories. | Every project's documents are listed with what changed since the last reading; identifiers join issues, changes and Stories where they exist. |
| R5 | Signals. Ranking service with an embedding model and a reranker; vectors kept per record and refreshed when its text changes; joined recall. The labelled set and the evaluation harness. | On held-out links, the true parent is among the top 20 candidates for at least 95% and is ranked first by the reranker for at least 85%. Measured against R2's word matching. |
| R6 | Scorer in shadow mode. Calibrated classifier, abstention thresholds, escalation to the decision models. A report per project of every change it would make, with measured precision per kind of change. | Precision on the held-out set meets the error rates above; the share of abstentions is under one in five. Nothing is changed yet. |
| R7 | Reorganize links. Applies the policy: reasons recorded, primary Epics chosen, unsupported links removed, orphans placed, bugs marked. Batches with undo, audit sample, automatic stop. Planning health redefined. | Falcone: links without a reason under 50; every Story has a chosen primary Epic; open orphans under 10; audited precision within the allowed error rate. |
| R8 | Reorganize Stories. Delivery state for every Story from issues, OpenSpec, documents and code summaries. Delivered Stories closed; unsupported and duplicate Stories offered as batches. | Every Story has a state with its evidence; "Stories that nothing supports" and "delivered Stories still open" are followed in Planning health. |
| R9 | Reorganize structure. Epic cohesion; proposals to split a broad Epic and to merge overlapping ones; missing Stories from orphan clusters and from document sections; all as one plan to approve. Replaces the earlier R5. | The plan can be approved or edited as a whole; after it, no Epic is the primary Epic of more than 10% of Stories. |
| R10 | Continuous and everywhere. A new issue or a changed document goes through the cascade when it arrives; a nightly check for drift; rollout to the other projects, including those never through Discovery. Replaces the earlier R7. | A new issue is placed, or queued with its shortlist, within minutes; each project has its own thresholds and its health follows the same numbers. |
| R11 | Epic pages written by the platform from the organized plan. The earlier R6, now last because it reads better from a plan that is in order. | Each Epic has a page that cites its Stories, issues and documents. |

## Decisions needed before R5

1. **Automation.** Whether changes above the measured threshold may be applied without approval of each one, as in the policy table.
2. **Where the ranking models run.** In the cluster with open models, or at a provider.
3. **The labelled set.** Whether you will judge about 150 pairs once. Without it the thresholds rest on stated links and on agreement between models, which is weaker.
