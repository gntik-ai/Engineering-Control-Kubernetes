# Judged reviews: a second model checks every answer, and you see only what is in doubt

Plan, 26 November 2026. Nothing in it is built.

## What is asked for

The Reviews section is thorough, scattered, and asks for too much checking by hand. Wanted:

1. One model gives a first answer; a different model, the judge, checks it. An answer the judge finds wrong goes back and is given again.
2. Every token limit, setting and cost control of Reviews in one place: Settings, inside Reviews.
3. Related Work, "Open issues with no User Story": the system places issues by itself and shows you the uncertain ones.
4. Planning, "User Stories under several Epics": you are shown the ambiguous Stories, not the whole list.
5. A judge in every Reviews workflow, and models beyond Anthropic's and OpenAI's that suit judging.
6. Least token cost for the same or better result.

## What exists today

### What you do by hand

| Where | Workflow | By hand, today |
| --- | --- | --- |
| Related Work | Open issues with no User Story | Start each run, choose its model and its token limit. Then approve, link otherwise, mark as bug or set aside each answer; or tick answers and approve them together. |
| Related Work | Review evidence | Start each run and set its cap. Open each verdict, choose a scope, save. No bulk action. |
| Planning | User Stories under several Epics | Start each run. Approve or choose an Epic for each Story; or approve ticked answers together; confirm the Epics that Stories name. |
| Planning | Automatic planning reviews | They run by themselves; each proposal waits in its record. Model, pause and token budget are set here. |
| Discovery | Repository analysis, project coverage | Start each step; approve each proposal in its table. |
| Reorganize | Changes to approve | Prepare a batch, judge its sample line by line, strike lines, approve. |
| Reorganize | Epics a Story is also linked to; Clean-up | Start each run; what they find becomes a batch. |
| Reorganize | Structure; Pairs to judge | One decision for each proposal; one judgment for each pair. |
| Epic pages | Overviews | Start the run. |

On Falcone today: 424 User Stories wait for a primary Epic, of which the texts point clearly to one Epic for 249 and not for 175. 42 open issues have no User Story; 25 have an answer waiting for you. 1,158 links to further Epics are to be judged.

### Where the settings are

In at least six places:

- **Planning:** the project's review token budget, the planning model, the pause of automatic reviews.
- **Every run's form:** its model, its first model, "Stop at this many tokens".
- **Review evidence:** the pilot token cap.
- **The Flow Designer and Flow assignments:** the decision model, the second model, the writing model, the ranking model and their limits, for each review flow.
- **Global settings, Accounts:** which models can be asked, and each account's pause.
- **In the code, not editable:** about forty numbers, among them the error rates the scorer works to (5% for adding, 2% for removing), the gate of a trial (30 answers, 90%), the size of a sample, the days a batch can be undone, the margin that makes an Epic "clear" (1.5), and the room an answer is given.

There is no limit in money anywhere: every limit counts tokens, and a token of one model costs up to a hundred times a token of another.

### What already resembles a judge

- **A second opinion on an issue and a Story** (Review evidence): a decision model answers; when it says unrelated, or contradicts itself or you, a second decision model of another vendor is asked. Agreement is an advisory result, disagreement goes to you. It never changes a link.
- **Two models in a run** (25 November): a cheaper model first, the run's own model for what the first cannot settle. The second is asked only when the first is unsure; it does not check an answer the first was sure of.
- **The scorer:** acts only where one candidate is left within a measured error rate.
- **A sample before a batch:** you judge a few lines; too many wrong withdraws the batch.
- **Quoted passages:** an answer whose quotes are not in the texts cannot be approved in bulk.

None of these is a judge of every answer, and none sends an answer back.

### What it costs today

The run over issues with no User Story that is under way on Falcone has cost $2.81 for 45 answers: **6 cents an answer**, with Claude Fable 5. A decision model's verdict on one issue and one Story, as the platform has recorded it, cost €0.00025: **a 250th of that**. That difference is what this plan uses.

## The idea

Each question goes down these steps and stops at the first that settles it. The cheapest steps come first.

| Step | Who | Cost | Settles |
| --- | --- | --- | --- |
| 0 | Rules: what the records state themselves | none | Links named in the text |
| 1 | The scorer, from the ranking models in the cluster | none | What is clear within the error rate |
| 2 | **The answering model**: a cheap model that answers with quoted passages | a twentieth to a fifth of a cent, by the model | Proposes; settles nothing alone |
| 3 | Checks without a model: the answer has its shape, its quotes are in the texts | none | Sends back what fails |
| 4 | **The judge**: a model of another family, asked only "is this answer right?" | a thirtieth of a cent | Agreed, or sent back |
| 5 | **Sent back**: the answering model answers again, told what the judge objected to and that the candidate was refused. Twice at most. | as step 2 | Agreed on a later round, or in doubt |
| 6 | **A stronger model**, for what is still in doubt (optional, capped) | as today, for few | Agreed, or in doubt |
| 7 | **You** | your time | What is in doubt |

**What the judge is asked.** Not the whole question again. The answering model read eight candidate Stories and chose one; the judge reads the issue, that one Story and the quoted passages, and says yes, no or cannot tell. That is two short texts in place of nine, and it is the question decision models are built for. For "none of them fits" the judge is asked about the best candidates of the scorer; if it says no to each, "none" stands.

**Another family, always.** The judge is never of the family that answered. Through OpenRouter a model's family is its maker, not OpenRouter: that distinction is missing today and is part of the first phase.

**What "in doubt" is.** The judge said no or cannot tell after the last round; or the judge and the scorer contradict each other; or the answer could not be given its shape; or the record changed while it was asked about. Everything else is agreed.

**What you see.** A list of what is in doubt, the most ambiguous first, each with the answer, the judge's objection and the passages of both. Behind it, what was agreed, to look at when you want to.

### What is applied without you

This is the one thing in the plan that changes a standing decision. On 23 November 2026 you decided "approval always": nothing is applied without your word. What you ask for now, that the system handles what is clear by itself, needs that changed for some kinds of change. Proposed, for you to decide:

| Kind of change | Proposed |
| --- | --- |
| Adds or corrects, and can be undone: an issue put under a User Story, a primary Epic chosen, a reason recorded for a link | **Applied when agreed**, once the two models have passed the measurement of the first phase on this project. Undone as a batch for 30 days. |
| Removes, closes, or writes to GitHub: a link taken off, an issue closed, a User Story or Epic retired, a bug label, a new Story or Epic | **Stays a batch you approve.** It arrives already judged, so its sample is the only thing to read. |

Three guards on what is applied without you:

- **A sample.** One in twenty of what was applied is put before you to confirm. Your answers measure the real error rate.
- **A stop.** When that rate passes the one you accept, applying stops by itself and everything goes back to waiting for you.
- **A digest.** Each day: what was applied, what waits for you, what it cost.

## Models for judging

Asked for: models beyond Anthropic and OpenAI. Prices are per million tokens, read and written, from OpenRouter's list of 25 November 2026.

**Decision models.** They answer yes, no or a score about what they are given, with a probability, and do not write text. The platform already asks them, through OpenRouter's Decisions interface and OpenAI's.

| Model | Maker | On the platform | Note |
| --- | --- | --- | --- |
| Gemini Decision 1 | Google | The second opinion in Review evidence today | 30 verdicts cost €0.01 |
| Jev 1.14 | TypeSafe | Asked in the earlier studies | The most used decision model on OpenRouter |
| D1.2 Ultra | Liquid | Asked in the earlier studies | |
| Decisions Fast | Parallel | Asked in the earlier studies | |
| Mercury Decide, Solar Decide, Decider v1 27B | Inception, Upstage, Perplexity | Not yet | Listed by OpenRouter; not looked at closely |

**General models that hold an answer to a shape**, for the answering model, or as a judge where a reason in words is wanted:

| Model | Maker | Read | Written | In a batch |
| --- | --- | ---: | ---: | --- |
| Hy4 Flash | Tencent | $0.035 | $0.14 | |
| DeepSeek V4.1 Flash | DeepSeek | $0.051 | $0.203 | $0.05 and $0.15 |
| Nemotron 3.5 Lightning | NVIDIA | $0.07 | $0.20 | |
| MiniMax M3.1 Flash | MiniMax | $0.075 | $0.30 | |
| GLM 5.3 Flash | Z.ai | $0.12 | $0.38 | $0.023 and $0.068 |
| Mistral Small | Mistral | $0.15 | $0.60 | |
| Qwen 3.8 Flash | Alibaba | $0.15 | $0.47 | |
| Gemini 3.1 Flash Lite | Google | $0.25 | $1.50 | |
| Mistral Large 4 | Mistral | $0.70 | $2.10 | |
| Gemini 3.8 Flash | Google | $0.825 | $4.125 | |
| Grok 4.3 | xAI | $1.25 | $2.50 | $1.00 and $2.00 |

For comparison, the models asked today: Claude Opus 5.5 at $4 and $20, Claude Fable 5.2 at $8 and $40, Claude Haiku 5.5 at $0.10 and $0.50.

**What is known about how well they judge: little.** No leaderboard found ranks judges by agreement with people and by cost together, and the careful sources say the same thing: a judge that does well on a public benchmark can do badly on your own records, so it has to be measured on them. The platform's earlier studies asked four of the decision models above, but their answers were never scored against your judgments, so they say what a verdict costs and not how often it is right. The first phase measures that. Until then every row above is a candidate.

**Proposed to measure first:** as judge, Gemini Decision 1, Jev 1.14 and D1.2 Ultra; as answering model, DeepSeek V4.1 Flash, Gemini 3.1 Flash Lite and Claude Haiku 5.5. Three makers on each side, so that a judge of another family exists for each.

**Not among them:** Inkling. Decided on 26 November 2026: it writes code and is never asked in a review.

**To decide with this:** these models are other companies', several of them in China. A question carries the text of an issue and of a User Story. Settings will hold the list of makers a project's texts may be sent to; nothing is asked of a maker that is not on it.

## Keeping the cost down

- **Ask a model last.** Rules and the scorer cost nothing and come first. Joining the scorer to the two worklists is still to build.
- **The judge reads two texts, not nine.**
- **Decision models where the answer is a verdict.** They are paid for what they read only.
- **In a batch, at night,** whatever does not need an answer at once: about half the price at the providers that offer it. The platform has batches already.
- **The part of a question that repeats, first,** so that the provider's cache serves it. Built already for flows; the checks do not use it yet.
- **No room for thinking where none is wanted.** Every check gives 8,000 tokens of room to every model today, because one model needed it.
- **A strong model only for what is in doubt,** with its own cap.
- **A limit in money,** beside the one in tokens.

**What that should cost**, to be measured in the first phase: an answer and a verdict together about a tenth of a cent, against 6 cents today for an issue with no User Story. If one question in seven stays in doubt, which is an assumption until it is measured, and a strong model is asked about those, about one cent on average. The questions about primary Epics are half as long and cost about 3 cents each today: for Falcone's 424, about $2 in place of about $12, and about $0.50 without the stronger model.

## Settings, in Reviews

One new view, Reviews, Settings, per project with defaults for all projects. What moves there leaves the place it is in now.

| Group | What | Today |
| --- | --- | --- |
| Spending | Limit in tokens; limit in dollars a month (new); what is used, held and left; one switch that pauses all review spending | Planning; the pause is in two places |
| Models by role | The answering model, the judge, the stronger model for doubt, the writing model; the decision model and second model of Review evidence; the planning model; an exception for one workflow where wanted | Each run's form, Planning, the Flow Designer |
| Makers allowed | To whom a project's texts may be sent (new) | Nowhere |
| How far it goes alone | For each workflow: off, proposes, or applies what is agreed (new) | Nowhere: everything proposes |
| The judge | Rounds an answer may be sent back (2); the error rate you accept for what is applied (5%); the share put before you as a sample (1 in 20) | Nowhere |
| What is clear | The scorer's error rates, the margin that makes an Epic clear, the gate of a trial, the size of a batch's sample, the days a batch can be undone | Numbers in the code |
| What runs by itself | The readings and checks of R11, each with a switch; whether questions wait for the night's batch | No switch but the pause |
| What it cost | For each workflow and model: tokens, dollars, and dollars for each record settled; what the records that wait would cost | Global settings, Stats, for all projects together |

A run no longer asks for a model or a token limit: it takes them from Settings and stops at what Settings allows.

## The two places named

**Related Work: open issues with no User Story.** When an issue arrives or changes, it goes down the steps by itself, within the limits of Settings. The view opens on "Needs you": the issues in doubt, most ambiguous first, with both models' reasons. "Placed by the system" lists the rest, with undo. No run to start.

**Planning: User Stories under several Epics.** Of Falcone's 424, the 249 whose texts point to one Epic get that Epic once the judge agrees; the 175 others are answered and judged. The view opens on those still in doubt, ordered by how close the two best Epics are. You are not shown a Story the two models and the scorer agree on unless it is in the sample.

## Phases

| Phase | What | Done when |
| --- | --- | --- |
| J0 | **Rules and measurement.** Inkling is refused in every review and offered only on tasks that write code. A model's family is its maker. Each candidate judge is asked about pairs whose truth is known: links the records state, your decisions, the pairs you judged, and pairs known to be wrong. | For each judge and each pairing with an answering model: how often it accepts a wrong answer, how often it refuses a right one, and the cost of a verdict. About $1 to $3. |
| J1 | **The judge in the checks.** For issues with no User Story, primary Epics, clean-up and Epic links: answer, checks, judge, sent back twice at most. Each answer keeps the judge's verdict, its objection and the rounds. The run reports what each step settled and cost. | A run ends with every answer agreed or in doubt, and its cost for each record settled is shown beside the same run without a judge. |
| J2 | **Settings in Reviews.** Everything in the table above in one view; a limit in dollars; makers allowed; the numbers that are in the code today. Runs take their model and limits from it. | No model, token limit or switch of Reviews is set anywhere else. |
| J3 | **Only what is in doubt.** The scorer joined to the two worklists. Both open on "Needs you", ordered by doubt. What is agreed is applied, or waits as one batch, as you decide below. The sample, the stop and the digest. | On Falcone, the Stories shown for a primary Epic are those in doubt and the sample, not 424. |
| J4 | **By itself.** New and changed records are answered and judged as they arrive, within Settings, in the night's batch where it can wait. | Nothing in Related Work or Planning needs a run started by hand. |
| J5 | **The judge everywhere else.** Review evidence (its second opinion becomes this judge), the proposals of planning reviews and of Discovery, connections, execution order, structure, and Epic overviews, where the judge checks each sentence against the records it names. | Every workflow that asks a model has its answers judged, and shows what is in doubt first. |
| J6 | **One list.** "Needs you" across all of Reviews, with each workflow's view behind it for detail; panels that say the same thing twice are merged. | Reviews opens on one list of decisions, and it is short. |

J0 to J3 are what the request names. What was built on 25 November goes into them: the trial of a model on questions already answered is how J0 measures an answering model, and "a cheaper model first" becomes the answering model of J1.

Order with the rest of the roadmap: J0 to J3, then B3 of the Bugs section, then J4 to J6. C1 to C3 for Inkling on code ([inkling-models.md](inkling-models.md)) whenever you want them.

## Decisions needed

| # | Question | Recommended |
| --- | --- | --- |
| 1 | May what both models agree on be applied without you? This changes "approval always" of 23 November. | Yes for what adds or corrects and can be undone. No for what removes, closes or writes to GitHub. |
| 2 | The error rate you accept in what is applied without you | 5%, the rate the scorer works to today. |
| 3 | To which makers may your projects' texts be sent? | Google, and through OpenRouter the makers of the decision models. The Chinese makers only at your word. |
| 4 | May J0 spend about $1 to $3 on the measurement? | Yes. Nothing above about quality is known without it. |
| 5 | A stronger model for what stays in doubt, before it reaches you? | Yes, with its own cap in Settings. |
| 6 | A limit in dollars a month for each project | Yes; I will propose a figure from what J0 measures. |

## Sources

- The console and the code of the platform, read on 26 November 2026; Falcone's numbers from the live console the same day.
- OpenRouter's public list of models, read 25 November 2026, for prices.
- OpenRouter on decision models: [the Decisions interface](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request.md), [their use](https://openrouter.ai/skills/openrouter-decisions), [which are used most](https://openrouter.ai/rankings/decisions?view=day).
- On judges: [JudgeBench](https://benchlm.ai/benchmarks/judgebench), and a [roundup of 2026 studies of their reliability](https://dev.to/bean_bean/llm-as-judge-reliability-in-2026-what-8-june-studies-actually-show-eca). Both are secondary; neither ranks the models named here.
