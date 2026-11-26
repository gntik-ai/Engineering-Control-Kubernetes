# Judged reviews: a second model checks every answer, and you see only what is in doubt

Plan, 26 November 2026. Decided the same day; J0 to J6 are built.

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
| J6 | **One list** (built, 26 November 2026). "Needs you" across all of Reviews, with each workflow's view behind it for detail; panels that say the same thing twice are merged. | Reviews opens on one list of decisions, and it is short. |

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

**J1 completed (26 November 2026).** The two parts first left open are built.

- **A judge for the Epics a User Story is also linked to.** An answer of that check judges several links at once, so the two are compared link by link: the judge's word on each link is kept, and the two agree when they say the same of every one. A link is proposed for taking off only where the judge, asked unseen, calls it unrelated too; a link the two differ on is shown as in doubt with what the judge said, and is not proposed.
- **A third model for what stays in doubt** (decision 5). Settings can name one, which can be a stronger model: it is asked the same question, unseen, only about answers the two did not agree on and answers that could not tell. It decides nothing. The answer stays in doubt and says whether the third went with the answer, with the judge, or with neither, so that what reaches you comes with a third reading. It has its own sum in dollars a month, $1 unless changed, and is not asked once that is used. It replaces "a comparison where the scorer disagrees" of the first plan: the comparison measured no better than a second answer, and a third reading needs no new question.

**The third model, measured (26 November 2026).** On Falcone's 60 issues whose User Story is known, with GPT-5.6 Luna answering and Claude Haiku 5.5 as the judge, a third model was asked about what the two left in doubt.

| Third model | Agreed at once | Right | In doubt | Third went with the answer | Right | With the judge | Right | With neither | Cost of the third, for each issue asked |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Gemini 3.8 Flash | 48 | 42 | 8 | 3 | 1 | 4 | 1 | 1 | $0.011 |
| Claude Sonnet 5.5 | 50 | 42 | 6 | 3 | 2 | 3 | 0 | 0 | $0.013 |

- **The side a third model takes is right 4 times in 13.** That is worse than choosing between the two by chance, with either model, the stronger one included. What two cheap models do not agree on is hard for a third as well: often neither of the two has the right Story, and the third still goes with one of them.
- **It costs more than the rest.** About a cent for each issue it is asked about, against a fifth of a cent for the answer and the judge together.
- **So no third model is named by default, and none is recommended.** It stays in Settings, off, for a project where it may measure otherwise; the measurement can be run again with any model. What stays in doubt goes to the administrator as it is, which is what J3 is about.

The numbers are small: 14 issues in doubt over two runs. They are enough to say a third model does not settle doubt, and not enough to rank third models.

**What the measurements have cost** is now counted with the judge and the third model: $1.58 of the $3 set aside.

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

## J2 completed (26 November 2026)

What the first part left open is built.

- **A run of a check is started without a form.** The five worklists (issues with no User Story, primary Epics, the clean-up check, extra Epic links, Epic overviews) show what a run will ask and one button. The model that answers, the judge and the tokens the run stops at come from Settings. Where a check can have a judge and Settings names one of another maker, the run has it; where there is no judge and the switch is on, the model whose trial on the check agreed is asked first. With no model chosen to answer, nothing starts and the worklist says where to choose. A trial of another model still chooses its model where it is started: choosing it is the trial.
- **The Planning page has no model and no switch.** Both are in Settings only; the Planning page says which model is set and whether the reviews run. Discovery shows the model of planning reviews and Discovery and where it is set, instead of a list to choose from. Review evidence, on a project's page, asks the model that answers and stops at the tokens Settings gives a run of it; in the Flow Designer, where a flow is tried, the choice stays.
- **The makers hold for every review.** A call of a planning review, Discovery, the coverage check, a connection's analysis, an execution plan, an Epic description or Review evidence is refused before it is made when its model is of a maker the project's texts are not sent to, whatever the review was set to before. The lists of models these reviews offer hold only models of allowed makers. A run of a check that is resumed after the makers changed is held and says why.
- **The numbers can be changed**, for each project: the tokens of a run and of a run of Review evidence, the candidates shown for an issue, the margin that makes an Epic clear, the error accepted where a batch adds and where it removes (which the scorer works to as well), the size of a batch's sample, the days a batch can be undone, the gate of a trial, the records a trial and a measurement ask about, and what all measurements may cost together. Each has the platform's value until it is changed, and a least and a most it can be set to.
- **What it cost** is in Settings: for each workflow and model, the calls, tokens and dollars since the limit began to count. And for each check, what a run would ask, how many records have no answer, what one question cost before with the same models, and what asking about the rest is likely to cost. Where a model never answered that check, the cost is said to be not known.
- **The messages of a run on hold** point to Reviews, Settings.

**Left where it belongs.** How far each workflow goes alone (off, proposes, applies what is agreed) comes with J3, which builds applying. A switch for each reading that runs by itself, and whether questions wait for the night's batch, come with J4, which makes the checks run by themselves: today the readings ask no model and cost nothing, and the one thing that asks by itself, the automatic planning reviews, has its switch in Settings. The analysis of a connection between two projects keeps its choice of model on the Connections page, outside Reviews; the makers hold for it.

**The makers since J2:** the list of makers, which J0 applied only to what a measurement may ask, holds for every review and can be seen and changed in Settings.

## Sources

- The console and the code of the platform, read on 26 November 2026; Falcone's numbers from the live console the same day.
- OpenRouter's public list of models, read 25 November 2026, for prices.
- OpenRouter on decision models: [the Decisions interface](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request.md), [their use](https://openrouter.ai/skills/openrouter-decisions), [which are used most](https://openrouter.ai/rankings/decisions?view=day).
- On judges: [JudgeBench](https://benchlm.ai/benchmarks/judgebench), and a [roundup of 2026 studies of their reliability](https://dev.to/bean_bean/llm-as-judge-reliability-in-2026-what-8-june-studies-actually-show-eca). Both are secondary; neither ranks the models named here.

## What joining the texts measured (26 November 2026)

The candidates of an issue are listed by how well the texts alone fit: shared words and closeness in meaning, joined. That ranking costs nothing and no model made it. On the 50 answers GPT-5.6 Luna and Claude Haiku 5.5 agreed on at once, of Falcone's 60 issues with a known User Story:

| What the two agreed on | Answers | Right |
| --- | ---: | ---: |
| The Story the texts alone rank first | 35 | 32 |
| Another Story than the texts rank first | 15 | 10 |
| The Story closest in meaning | 24 | 21 |

- **Where the texts say the same, the two are right 9 times in 10; where the texts point elsewhere, 2 times in 3.** So the texts are the third reader a third model was not: independent, and free.
- **It is still not the 5% you accept.** Three wrong in 35 is too few to tell 91% from 95%, and some of the "wrong" may be a second Story that fits as well as the one on record. Nothing is applied on this measurement. What decides is the project's own record: the sampled lines you judge.

## J3 as built

**Where an answer stands.** Each answer of the two worklists is one of:

- **Settled**: two models chose alike at once, every passage quoted is in the texts, and the texts alone rank the same candidate first. It is not shown one by one. It goes on in a batch.
- **Needs you**, the most doubtful first: the two chose differently; the answer could not tell; they agreed only after one was told what the other chose; they agree and the texts point elsewhere; they agree and a quoted passage is not in the texts; they agree on an answer that is yours to act on, such as "none fits" or "a bug"; and an answer no second model was asked about.
- **Changed**: the texts changed since it was answered. The next run asks again.

For primary Epics the rule about the texts is taken over from the issues: it was not measured there.

**The worklists open on what needs you.** Issues with no User Story, on Related Work, and primary Epics, on Planning, list what needs you and say why for each, the primary Epics in the order of how close the two best Epics lie. A line above says how many are settled and where they went, and how many were not asked about yet. On Related Work the worklist now comes before Planning health.

**What is settled goes on together.** In Reviews, Settings, for placing issues under a User Story and for choosing primary Epics, one of:

- **Waits as one batch for your approval** (as it was): a sample to judge, then one word.
- **Applied without you, where the record allows**: decision 1.
- **Not gathered**: each answer waits in its worklist.

**The record, the sample and the stop.** Applying without you is in force for a kind of change only while all of this holds:

- The settings ask for it.
- You judged at least 40 sampled lines of that kind's batches in this project.
- Of the latest hundred you judged, no more than 5% were wrong.
- No more than 20 lines applied before still wait to be judged.

Every ten minutes what is settled is then applied as a batch that can be undone for thirty days like any other. One in twenty of its lines is put before you to judge afterwards, under Changes to approve. A line you judge wrong is put back at once and counts in the record; when the record no longer holds, applying stops by itself, the kind proposes batches again, and Settings says why. The four numbers can be changed in Settings.

**No project applies anything today.** Every project proposes, and none has a record yet.

**The digest.** The daily digest in the chat says for each project how many answers need you and how many lines applied without you wait to be judged; Planning health keeps both numbers day by day.

**Not exercised on live:** a run with a judge on Falcone's worklists, a batch of settled answers, and applying on a record. The first two wait for a run you start; the third for 40 lines you judge.

## J4 as built

**A check can run by itself.** In Reviews, Settings, for each of the five checks (issues with no User Story, primary Epics, the clean-up check, extra Epic links, Epic overviews), one of:

- **Only when you start it** (as it was, and the default).
- **By itself, as records arrive**: within half an hour of a record that has no answer, or whose texts changed since it was answered.
- **By itself, each night**: once, between 1 and 5 UTC, over what waits by then.

**It is a run like any other.** It takes the model that answers, the judge and the tokens it stops at from Settings, stops at the project's limit in dollars and at its token budget, appears in its worklist with "It started by itself", and can be stopped and ended there. The worklist says whether the check runs by itself and what it waits for; a run can still be started by hand.

**What keeps it from spending without need.**

- Nothing runs by itself unless Settings says so, check by check: asking a model costs money.
- No run starts while one of the same check is under way, stopped or on hold. A run that stopped after three failed calls in a row therefore waits for you.
- A record a model gave no usable answer about is not asked about again by itself for a day.
- Without a model chosen to answer, or at the limit in dollars, nothing starts, and Settings says which.
- A project that is paused or archived is left alone.

**With J3, the path is whole.** A new issue is read and ranked without a model, answered and judged within half an hour, and then either goes on in a batch, or is applied where the record allows, or is listed under "Needs you" with its reason.

**The digest** says each day, for each project, how many runs started by themselves, how many answers they gave and what they cost.

**Two things of the plan left out, and why.**

- **The night run is not handed to the providers as a batch at half price.** The run at night asks as a run by day does. Handing the questions in as a provider batch would halve what is already small: Falcone's first pass over everything is about a dollar, and a day's new records a few cents. It would need the answer, the judge and what goes back to wait for each other across hours, which the run that asks one question at a time does not do. It can be built if a project's volume makes it worth it.
- **The readings that ask no model have no switch.** The backlog, the documents, the meaning of records, the scorer and where each Story stands cost nothing, and the candidates, the ranking by the texts and the batches rest on them. Stopping one would only make the checks worse. Pausing a project stops its work as a whole.

**Not exercised on live:** every project has every check on "Only when you start it", so nothing has started by itself yet.

## J5 as built

**One step for all of them.** Wherever a review asks a model, the project's judge can be asked the same question without seeing the answer. It is asked when Settings names a judge, the judge can be asked, its maker is one the project's texts may be sent to, and it is of another maker than the model that answered. A judge that gives no usable answer leaves the first answer as it would be without one. What the judge used is counted under the review's own name with "judge" beside it, so Settings shows what judging each review costs.

**What agreeing means differs, review by review.**

| Review | What the judge is asked | Where the two differ |
| --- | --- | --- |
| Epic overviews | Not to write it again: to check each sentence against the records the sentence names | The sentence is marked in doubt with the judge's reason, the Epic's row says "In doubt: needs you", and nothing goes back or to a third model |
| Coverage of User Stories by issues | The same matching | A match becomes a link without your approval, so only a match the judge makes too is kept, and it counts as covering the whole Story only where both say so. An issue left with no Story waits in its worklist |
| Automatic planning reviews | The same review | Where the judge names other parents, nothing is linked: the review asks you, saying what each named. A Story or issue the review proposes says whether the judge proposed one like it |
| Discovery | The same review step | An issue that is there says whether the judge gave it the same parents; a record proposed anew, whether the judge proposed one whose title shares half its words |
| Connections between projects | The same analysis | Each suggestion says whether the judge made it too; those it did not make are listed first |
| Execution order | The same classification | An Issue the judge reads as the other type, bug or enhancement, is marked ambiguous, which you confirm before the plan can start |
| Review evidence | Its second opinion, which the flow names | Where the project has a judge, the second opinion is asked about every verdict, not only one that says unrelated. A contribution the first model finds counts as advisory only when the second finds the same; else it needs your review |
| Check of a new Epic | The same check | Where one finds the description ready and the other would ask a question, the dialog says so |

**What needs you comes first** in the list of Epics (by its mark), the connection's suggestions, the planning reviews and the candidates of a Discovery stage. In the planning tables a proposal says that it is in doubt, and is not moved up.

**Where the judge is not asked.**

- **Where it is of the maker that answered.** On Falcone the planning model is Claude Sonnet 5.5 and the judge Claude Haiku 5.5: the planning reviews, Discovery, the execution order and the check of a new Epic are not judged there until one of the two is of another maker. Settings says so.
- **The structure of the plan** asks no model: its proposals come from the ranking service.
- **The description of the repository** in Discovery is a text you approve as a whole.
- **The supervisor's advice** is outside Reviews.

**What it costs.** One more call for each call of the review, to the judge's model. With a judge that is cheaper than the model that answers, less than twice.

**Not measured.** That two models agreeing are right more often was measured for issues and their User Stories. For proposals, suggestions, classifications and sentences it is assumed, not measured: there the judge marks and orders, and what was applied without approval before (coverage links, the parents a planning review names) is now applied only where both agree.

**Not exercised on live.** No review has run with a judge on live. The judge's step and each review's rule for agreeing are covered by tests; the judge of Epic overviews is tested from the run to the page.

## J6 as built

**Reviews opens on "Needs you".** It is the first view of Reviews, per project. One list says everything in Reviews that waits for you, in rows: how many, what, why, and a link to the panel where it is decided. Nothing is decided in the list, and no number is counted in a way of its own: each is the one its own view shows, or, for what was left in doubt, counts the records its view marks as in doubt (`needs_you.py`, `GET /api/dashboard/projects/{name}/needs-you`).

The rows come in four groups, in this order.

| Group | What is in it | Where it leads |
| --- | --- | --- |
| **Yours to decide** | Changes that wait for approval, one row for all kinds, each kind with its number and how far the sample of its open batch is judged | Reorganize, Changes to approve |
| | Lines applied without you that wait to be judged | Reorganize, Changes to approve |
| | For issues with no User Story and for primary Epics: the answers that need you, which are the ones the worklist opens on, with how many stand for which reason | The check's own panel |
| | Planning reviews that ask a question | Planning, the reviews |
| | Proposals that wait in the planning tables, by kind, and how many of them the judge did not make | The planning tables |
| | Verdicts of Review evidence that wait for your review | Related Work |
| | Proposals for the structure: Epics to split, User Stories that are missing | Reorganize, Structure |
| | Bugs that still carry a link to a User Story | Bugs |
| | The pairs to judge, while the scorer is not measured on the project's own links | Reorganize |
| **On hold** | A run of a check that is on hold or was stopped, with where it stopped and why; a run that failed with records left to ask | The check's own panel |
| | A check set to run by itself that has no model to ask; the reviews at their limit in dollars, where no run says so already | Settings |
| | A kind of change you set to be applied without you that its own record stopped | Reorganize |
| | Planning reviews that failed; planning reviews that are queued while the reviews are stopped, the project is paused or no model is chosen | Planning |
| | Runs of Review evidence that were cut off and keep their tokens reserved | Related Work |
| | The ranking service has not read the project's records | Reorganize |
| **Not asked yet** | For each check that does not run by itself in the project: the records no model was asked about | The check's own panel |
| **In doubt, and left as it is** | For the clean-up, the extra Epic links and the Epic overviews: the records the two models did not agree on. Nothing was proposed for them and nothing changes: a Story is not proposed for closing, a link stays, a sentence is marked on the Epic's page. They do not wait for you, and are no next step on the dashboard | The check's own panel |

A check that is set to run by itself is not listed under "Not asked yet": it starts without you. A project that was never through Discovery has one row, which says so.

**A row leads to its panel, not to the top of a view.** The link carries the panel's name, and the view scrolls to it. The panels above load after the page and push it down, so it is brought back into view for a few seconds, and left alone as soon as you scroll.

**Below the list**: what the checks did by themselves in the last day (runs, answers, dollars), and what moved between the last two measurements of the plan.

**What was said twice, and is now said once.**

| Before | Now |
| --- | --- |
| The next steps of a project were worked out a second way for "Where this project stands" on Reorganize and for the table of all projects on the dashboard, from the day's measurement | They are the rows of the list. The dashboard shows the first three of each project and links to the rest. The list of a project is used again there for five minutes; the project's own view works it out anew |
| "Where this project stands" on Reorganize: next steps, what moved, and the issues that arrived | The first two are in "Needs you". The card is "Issues that arrived lately" and says only that |
| Planning health was shown on the project's dashboard and again on Related Work | On the dashboard only |
| Planning health counted "Reviews waiting", "Changes that wait for your approval" and "Records the scorer abstains on" | Those tiles are gone: the list says them. The numbers are still measured each day, for the digest and for what moved |
| "What the scorer would do" said it was in shadow mode and proposed nothing, and counted again where the clean-up will look | "What the scorer says, and how well": what it says goes into the batches; the clean-up is said by the Clean-up card alone |

**Left as it was, on purpose.**

- **The views behind the list keep their panels.** Reorganize still has ten cards. Each says something the others do not; they are long, not repeated. The two trials of models are about choosing a model and would fit Settings better; they were not moved, because that is a move and not a merge.
- **Outside Reviews**: suggestions of a connection between projects the judge did not make (Project settings, Connections), and Issues marked ambiguous in an execution plan (Queue). They are decided where they are and are not in this list.
- **The daily digest** still says what waits from the day's measurement, in its own words.

**How long it takes.** The list reads every check's records and candidates, as each view does for itself. It is worked out when the view is opened and every two minutes while it is open.

**Read on live (26 November 2026).** Falcone's list has six rows: 37 changes to approve (13 primary Epics, 24 Epic links to take off), 18 answers about a primary Epic that need you, 256 planning reviews queued while the reviews are stopped, 38 Epic overviews not written or out of date, and, left in doubt, 5 Stories of the clean-up and 180 Stories with extra Epic links. Its numbers are the ones the worklist and the batches show. Working it out took 3.7 seconds there, 1.2 for falcone-serverless (ten rows), and 0.2 for a project with no plan. The first reading put the answers left in doubt under "Yours to decide", which said that 180 answers about extra Epic links needed you: they do not, a link the models differ on stays. They were given their own group the same day.
