# Judged reviews: a second model checks every answer, and you see only what is in doubt

Plan, 26 November 2026. Decided the same day; J0, most of J1 and most of J2 are built, J3 to J6 are not.

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
| J0 | **Rules and measurement** (built, 26 November 2026). Inkling is refused in every review and offered only on tasks that write code. A model's family is its maker. Each candidate judge is asked about pairs whose truth is known: links the records state, your decisions, the pairs you judged, and pairs known to be wrong. | For each judge and each pairing with an answering model: how often it accepts a wrong answer, how often it refuses a right one, and the cost of a verdict. About $1 to $3. |
| J1 | **The judge in the checks.** For issues with no User Story, primary Epics, clean-up and Epic links: answer, checks, judge, sent back twice at most. Each answer keeps the judge's verdict, its objection and the rounds. The run reports what each step settled and cost. | A run ends with every answer agreed or in doubt, and its cost for each record settled is shown beside the same run without a judge. |
| J2 | **Settings in Reviews.** Everything in the table above in one view; a limit in dollars; makers allowed; the numbers that are in the code today. Runs take their model and limits from it. | No model, token limit or switch of Reviews is set anywhere else. |
| J3 | **Only what is in doubt.** The scorer joined to the two worklists. Both open on "Needs you", ordered by doubt. What is agreed is applied, or waits as one batch, as you decide below. The sample, the stop and the digest. | On Falcone, the Stories shown for a primary Epic are those in doubt and the sample, not 424. |
| J4 | **By itself.** New and changed records are answered and judged as they arrive, within Settings, in the night's batch where it can wait. | Nothing in Related Work or Planning needs a run started by hand. |
| J5 | **The judge everywhere else.** Review evidence (its second opinion becomes this judge), the proposals of planning reviews and of Discovery, connections, execution order, structure, and Epic overviews, where the judge checks each sentence against the records it names. | Every workflow that asks a model has its answers judged, and shows what is in doubt first. |
| J6 | **One list.** "Needs you" across all of Reviews, with each workflow's view behind it for detail; panels that say the same thing twice are merged. | Reviews opens on one list of decisions, and it is short. |

J0 to J3 are what the request names. What was built on 25 November goes into them: the trial of a model on questions already answered is how J0 measures an answering model, and "a cheaper model first" becomes the answering model of J1.

Order with the rest of the roadmap: J0 to J3, then B3 of the Bugs section, then J4 to J6. C1 to C3 for Inkling on code ([inkling-models.md](inkling-models.md)) whenever you want them.

## Decided (26 November 2026)

| # | Question | Decided |
| --- | --- | --- |
| 1 | May what both models agree on be applied without you? | **Yes**, for what adds or corrects and can be undone. What removes, closes or writes to GitHub stays a batch you approve. This replaces "approval always" of 23 November for those changes, from the phase that builds it (J3). |
| 2 | The error rate accepted in what is applied without you | **5%.** |
| 3 | To which makers may a project's texts be sent? | **Google, and through OpenRouter the makers of the decision models**, beside Anthropic and OpenAI. The Chinese makers only at your word. |
| 4 | May J0 spend about $1 to $3 on the measurement? | **Yes.** |
| 5 | A stronger model for what stays in doubt, before it reaches you? | **Yes**, with its own cap. |
| 6 | A limit in dollars a month for each project | **$3** to begin with, to be set in Settings. Until Settings is built (J2) it is the sum all of a project's measurements of judges may cost together. |

## J0 as built

**The rule for models that write code only.** A model whose name begins with `thinkingmachines/inkling` is in no list a review chooses its model from, and a run, a first model, a planning model or a review flow's setting that names it is refused. In a flow it can be named for the two tasks that write a change, "Ask a model: write the change" and "Ask a model: choose the next step", and for no task that judges, reports or summarises.

**A model's family is its maker.** Through OpenRouter a model was of the family "OpenRouter", so what Claude wrote there could be reviewed by Claude. Now `anthropic/…` there is of Claude's family and `openai/…` of ChatGPT's, and every other model of its maker's. The rule that a change is not reviewed by the family that wrote it uses this.

**Measure a judge**, at the foot of a project's Reorganize page.

- *The pairs.* An issue and a User Story; a User Story and an Epic. A pair is right when the records state it or you made or judged it. It is wrong when you judged it so. It is presumed wrong when the record has a known parent and this is another one, the one most like it by its words: the hard case a judge has to refuse. The page says how many pairs of each sort there are and where each is known from.
- *The question.* A decision model is asked through its own interface, with the rubric the platform already has for an issue and a Story and a new one for a Story and an Epic. Its answer is a yes when it gives the link a probability of a half or more and a scope that says so; a probability and a scope that contradict each other are no verdict. A general model is asked for yes, no or cannot tell, with a reason in one sentence.
- *The sample.* Up to 60 right and 60 wrong pairs, the same for every judge.
- *The report.* How many wrong pairs it let through, and how many of those were presumed wrong; how many right pairs it refused; how often it could not tell; of what it said yes to, how much was right; tokens and cost of a verdict; and each error with its reason.
- *The limit.* In dollars: half a dollar for a measurement unless you say otherwise, $3 for all of a project's measurements together. A measurement holds and uses none of the project's review tokens, so it runs whatever the reviews have left. A model whose account does not say what it costs cannot be measured.
- *Which models.* The decision models, and the general models of Anthropic, OpenAI and Google. No model of a maker on the list kept for your word, and no model that writes code only. Both lists are in the code until Settings holds them.

A measurement changes nothing in the plan.

## What J0 measured (26 November 2026)

On Falcone, an issue and a User Story: 60 right pairs and 19 wrong ones, the same for every judge. All 60 right pairs and 18 of the 19 wrong ones are your own links and judgments; one is presumed. The wrong pairs are hard ones: you were shown them because they looked plausible.

| Judge | Maker | Wrong pairs let through | Right pairs refused | Could not tell | Cost of a verdict |
| --- | --- | ---: | ---: | ---: | ---: |
| Gemini Decision 1 | Google | 14 of 19 | 3 of 60 | 1 | $0.00011 |
| Jev 1.14 | TypeSafe | 13 of 19 | 1 of 60 | 4 | $0.00006 |
| D1.2 Ultra | Liquid | 13 of 19 | 6 of 60 | 1 | $0.00004 |
| GPT-6 Luna, decisions | OpenAI | 14 of 19 | 3 of 60 | 5 | $0.00012 |
| Decisions Fast | Parallel | 10 of 19 | 2 of 60 | 7 | $0.0010 |
| Claude Haiku 5.5 | Anthropic | 13 of 19 | 3 of 60 | 5 | $0.00028 |
| Gemini 3.1 Flash Lite | Google | 15 of 19 | 6 of 60 | 0 | $0.00038 |
| Gemini 3.8 Flash | Google | 8 of 19 | 7 of 60 | 2 | $0.0026 |
| Claude Sonnet 5.5, as a reference | Anthropic | 3 of 5 | 0 of 17 | 0 | $0.0045 |

The reference was stopped at 22 of 79 pairs when OpenRouter asked for a pause. All of it cost $0.46.

**A stricter threshold does not rescue it.** A decision model gives a probability. Asked to be 95% sure, the five decision models let 2 or 3 of the 19 wrong pairs through and no longer pass 22 to 28 of the 60 right ones.

**What this says.**

- No judge asked "is this pair right?" comes near the 5% you accept. The cheapest let through two wrong pairs in three; the best, one in two and a half.
- It is the question more than the models. A strong model lets them through as well. Asked whether an issue is work towards a Story, a model finds a contribution where you said "not this Story": the issue belongs to a neighbouring Story, and that is something the judge is not shown.
- The cost is as hoped: a hundredth of a cent a verdict for the decision models.
- 19 wrong pairs is few. Two of 19 could be anything from 1% to 33% over many. The direction is clear all the same.

**What changes in the plan.** The judge of J1 is not asked about one pair alone. Two ways are to be measured next, on the issues whose User Story is known, before either is built into the checks:

1. **A second answer.** Another family's model answers the same question from the same candidates, without seeing the first answer. They agree when they choose the same parent. Two models that do not see each other rarely choose the same wrong one.
2. **A comparison.** The judge is shown the chosen parent beside the next best candidates and says which fits best, or none. That shows it the neighbouring Story it was missing here.

What is measured for each: how often they agree, and when they agree, how often it is the parent that is known. Only a way that reaches 5% is given the right to apply without you; until one does, everything waits for you as today.

## What the second measurement found (26 November 2026)

On Falcone, the 60 issues whose User Story you set and whose own text does not name it. The link was hidden and the check's own question asked, with the eight candidates the check would show. For 50 of the 60, the right Story was among those eight; for 10 it was not.

**A model answering alone**

| Model | Maker | Chose a Story | Right | Wrong | Wrong with the right one shown | Cost of an answer |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| GPT-5.6 Luna | OpenAI | 53 | 43 | 10 | 5 | $0.00074 |
| Gemini 3.1 Flash Lite | Google | 57 | 46 | 11 | 4 | $0.00091 |
| Claude Haiku 5.5 | Anthropic | 51 | 40 | 11 | 6 | $0.00073 |

Each is wrong about one time in five when it chooses.

**Two or three models, each answering alone**

| Models | Chose the same Story | Right | Wrong | Wrong with the right one shown | Cost of both |
| --- | ---: | ---: | ---: | ---: | ---: |
| Luna and Haiku | 44 | 38 | 6 | 3 | $0.0015 |
| Luna and Gemini Flash Lite | 48 | 41 | 7 | 2 | $0.0016 |
| Haiku and Gemini Flash Lite | 47 | 40 | 7 | 3 | $0.0016 |
| All three | 43 | 38 | 5 | 2 | $0.0024 |

**A comparison**, over Luna's 53 answers, 43 of them right

| Model comparing | Confirmed | Right | Wrong | Wrong answers it did not confirm | Cost of a comparison |
| --- | ---: | ---: | ---: | ---: | ---: |
| Claude Haiku 5.5 | 47 | 39 | 8 | 2 of 10 | $0.00065 |
| Gemini 3.8 Flash | 44 | 40 | 4 | 6 of 10 | $0.0047 |

Both measurements together have cost $0.89 of the $3.

**What this says.**

- **Agreement alone is not enough.** Two cheap models that agree are wrong about one time in seven; three, one in nine.
- **Most of what is left is not the models' error.** Of the 5 wrong answers all three agreed on, 3 are issues whose right Story was not among the candidates: shown eight Stories without the right one, three models pick the same best of the rest. With the right Story shown, three models that agree are wrong 2 times in 40, which is the 5% you accept. Two models, 2 or 3 in 41 to 44.
- **The candidates are the first thing to repair, and it costs nothing.** They are chosen by shared words alone. The ranking service in the cluster, which also finds Stories that mean the same in other words, is not used for them yet.
- **A comparison is worth its price only with a stronger model.** Haiku confirms nearly everything. Gemini 3.8 Flash caught 6 of the 10 wrong answers, at six times the cost of an answer.
- **None of it is proven to 5% yet.** These are 60 issues; a difference of two answers moves every figure here by several points.

**The judge of J1, from this.**

1. Candidates from the ranking service joined with the words, twelve in place of eight, so that the right Story is nearly always shown.
2. An answer from a cheap model, and a second answer from a cheap model of another maker that does not see the first. Agreed when both choose the same Story, clearly, with their passages in the texts.
3. Where they agree and the scorer's own first candidate is another Story, a comparison by a stronger model of a third maker.
4. Where they disagree, the answer goes back once with the other's choice among the candidates; still no agreement, and it is in doubt and yours.

This is measured again on the same 60 issues before anything is applied without you. Until a measurement shows 5%, what is agreed arrives as one batch with its sample, as batches do today.

## What better candidates changed (26 November 2026)

The candidates of the check are now the Stories closest in meaning joined with those that share the most words, twelve in place of eight. On the same 60 issues the right Story is among them for 55, up from 50.

It did not lower the error. Asked again with the new candidates, GPT-5.6 Luna chose wrongly 11 times, Gemini 3.1 Flash Lite 14 and Claude Haiku 5.5 10, as before. Two that agree are wrong 7 or 8 times in 45 to 49; all three, 5 times in 42. What changed is where the errors lie: with the right Story shown, the models now agree on another one in 3 to 6 cases. The five issues gained are hard ones, and showing a model the right Story is not the same as its choosing it.

So the reading above, that most of what is left comes from the candidates, was wrong, and is kept here as it was written. The candidates were worth repairing all the same: the right Story is now nearly always shown, to the models and to you.

**Where this leaves the 5%.** Two cheap models agreeing are right about 85 times in 100 on these issues, and nothing measured so far reaches 95. One caution about the yardstick: the "right" Story is the one you set, and many of those links were approved from an earlier model's answer, so some of what counts as wrong here may be a second Story that also fits. That can be settled only by you reading the cases, which the batch's sample asks of you anyway.

## J1 as built

**A judge in a run.** The form that starts the check of issues with no User Story, of primary Epics, or of the clean-up has "Judge: a second answer, unseen". The judge has to be of another maker than the model that answers; a run has a judge or a cheaper model first, not both.

- The answering model answers as before.
- The judge is asked the same question with the same candidates, and is not shown the answer.
- Where both say the same, the answer is **agreed**.
- Where they differ, the answer goes back once: the answering model is told which candidate another reader chose, and to decide by the texts alone. If it now says what the judge said, agreed. If not, **in doubt**.
- An answer that says it cannot tell is in doubt without a judge being asked.
- For the clean-up, two answers agree when both say supported or both unsupported, whichever piece each read.

**What you see.** Each answer says whether a second model answered the same, or that it is in doubt and what the judge said. Both worklists list what is in doubt first, the primary Epics in the order of how close the two best Epics lie, and can show only what is in doubt or only what was agreed. The run says how many answers the judge was asked for, how many the two agreed on, and what the run cost.

**What is agreed is one batch.** Answers the two agreed on, that name a parent and whose passages are in the texts, go into the batches "Place issues under a User Story" and "Choose primary Epics" under Changes to approve, beside what the scorer is sure of. A batch opens with its sample; more wrong in the sample than 5% allows withdraws it; an approved batch can be undone for thirty days. Nothing is applied without you: that waits for a measurement that shows 5%, and the sample you judge is that measurement.

**Not built of J1:** the comparison by a stronger model where the scorer disagrees, and the judge for the Epics a User Story is also linked to, whose answers judge several links at once.

## What sending an answer back does (26 November 2026)

The whole of it was measured on the same 60 issues: an answer, a judge that answers unseen, and the answer sent back once where they differ.

| Answers | Judge | Agreed at once | Right | Agreed in the end, after going back | Right | In doubt | Cost of an issue |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| GPT-5.6 Luna | Claude Haiku 5.5 | 45 | 38 | 52 | 42 | 5 | $0.0018 |
| Gemini 3.1 Flash Lite | GPT-5.6 Luna | 49 | 41 | 55 | 44 | 5 | $0.0015 |

Sending an answer back makes the two agree more often and not more rightly. Of the 7 answers the first pair agreed on only after one was told what the other chose, 4 were right; of the 6 of the second pair, 3. A disagreement between two readers is the best sign there is that an issue is hard, and the round back talks one of them out of it.

So, differently from what was asked for:

- **An answer does not go back by default.** Reviews, Settings has the choice: in doubt at once, back once, back twice.
- **What was agreed only after going back is not put into a batch.** It is shown as agreed, with the round it took, and waits for you like any answer.

## J2 as built

**Reviews, Settings**, for each project.

- **What the reviews may cost.** A limit in US dollars a month, beside the limit in tokens, which has moved here from Planning. The limit counts what the project's reviews have cost since the month began, or since the limit was first put in force for the project, whichever is later: for the projects there are, that is the moment this was deployed, so that nothing spent before is held against them. When it is reached no review of the project asks a model: a run is held and says so, a new one does not start, and the automatic reviews wait. Measurements of judges have their own sum.
- **Which models are asked.** The model that answers and the judge: a run's form opens with them. What happens when the two differ. The model of the automatic planning reviews.
- **To whom the project's texts may be sent.** Each maker an account offers, ticked or not. A review asks only models of ticked makers; a run that names another is refused. By default Anthropic, OpenAI, Google and the makers of the decision models; the makers kept for your word are never ticked by default.
- **What runs by itself.** The switch of the automatic planning reviews.
- **Numbers that are fixed**, each with what it is for.

**The limit decided, $3 a month.** Every project has it from the deployment on.

**Not in J2 yet:** a run still shows its model and its token limit when it starts, filled in from Settings, and they can be changed there. The model and the switch of the automatic reviews are also still on the Planning page. The makers are enforced for the checks and the measurements; the planning reviews, Discovery and Review evidence still take any model an account offers. The fixed numbers are shown and cannot be changed. What each workflow cost is still under Global settings, Stats.

**Not in J0:** the whole list of makers is not yet applied to the model lists of the reviews themselves, only to what a measurement may ask; it comes with Settings (J2), where it can be seen and changed.

## Sources

- The console and the code of the platform, read on 26 November 2026; Falcone's numbers from the live console the same day.
- OpenRouter's public list of models, read 25 November 2026, for prices.
- OpenRouter on decision models: [the Decisions interface](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request.md), [their use](https://openrouter.ai/skills/openrouter-decisions), [which are used most](https://openrouter.ai/rankings/decisions?view=day).
- On judges: [JudgeBench](https://benchlm.ai/benchmarks/judgebench), and a [roundup of 2026 studies of their reliability](https://dev.to/bean_bean/llm-as-judge-reliability-in-2026-what-8-june-studies-actually-show-eca). Both are secondary; neither ranks the models named here.
