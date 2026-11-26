# Inkling: for writing code, never for reviews

Analysis and plan, 25 November 2026; decision and first trial, 26 November 2026.

## Decided (26 November 2026)

This replaces points 3 and 4 of the decisions of 25 November below, and the plan to ask Inkling first in the reviews.

1. **Inkling is for writing code only**, for example in the flow that resolves an issue. It is never asked in a review process: not as the model that answers, not as a first model, not as a judge, and not in a trial on review questions.
2. **Through OpenRouter**, as before. No model on our own nodes.
3. **What was built on 25 November stays, for other models.** The trial of a model on questions already answered, two models in a run, and the default first model are not tied to Inkling. They become parts of the judged reviews planned in [judged-reviews.md](judged-reviews.md). No trial of Inkling on review questions was run, and none will be.
4. **The rule itself is built** (26 November 2026, with J0 of the judged-reviews plan): Inkling is not offered where a review chooses its model and is refused there, and can be named only for the tasks of a flow that write a change.

## The trial on code (26 November 2026)

**What was tried.** Deliver issue, with Inkling 2 Small through OpenRouter in place of the subscription agent that writes the code: a flow made from the starter "Deliver issue, worked out by a model in a loop", in which the model chooses each step (search, read, have edits written, check) and writes the edits, and a subscription agent reviews the change as before. Tried in the evaluation lab on the one item Deliver issue has run on: #71 of falcone-workflow, "Create the contract-fixtures package and compatibility matrix schema", at the commit the past run worked on. A lab trial publishes nothing. The flow is `deliver-by-inkling-small`; it is a draft and in force for nothing.

**The past run**, by the subscription agents: finished in 11 minutes, 12 files, two rounds, pull request #350.

**Four attempts with Inkling 2 Small. None finished.**

| Attempt | What was set | How far it got | Why it ended | Cost |
| --- | --- | --- | --- | ---: |
| 1 | The starter as it is | One search, three reads | Its fourth answer was longer than the 8,000 tokens a model task allows unless told otherwise | $0.012 |
| 2 | 32,000 tokens an answer, the most the platform allows | One search, four reads, a plan for the whole change | It wrote the whole new package in one answer, which was longer than 32,000 tokens | $0.021 |
| 3 | The instruction to write in pieces of one or two files | Three files written and applied | It pasted a file's content into its choice of step, which the next step could not use; then it twice gave an answer without the field that says which step it chooses | $0.050 |
| 4 | The instruction to describe a piece, not paste it | The whole change written in six edits, declared done, reviewed, the reviewer's objection fixed | Writing a last small edit, it twice gave an answer without its summary field | $0.158 |

**What the fourth attempt shows.** The reviewer, a subscription agent, wrote: "The design is sound: the schema, loader and redaction code match the issue, and the pinned commit matches docs/sources.md. But both required commands fail on the committed tree", with about 13 type errors in a test file. Inkling had not run the tests: the platform's quick check does not know the test runner this package uses. It fixed the file when told. So the code it writes is to the point and close, and was not right the first time.

**What stopped it** was not the code. Two of the four attempts ended because its answer did not have the shape the platform asks for, twice in a row, and a run asks about 25 questions. Its answers are also long: 115,000 tokens written in the fourth attempt, most of it reasoning that cannot be switched off.

**What it cost.** $0.24 for all four attempts; $0.16 for the one that wrote the whole change, against a flat subscription for the past run. Cheap enough that cost is not the question.

**Conclusion.** Not ready to resolve issues by itself in this arrangement, on the evidence of one item. No flow that writes code was changed, and none is proposed to change yet.

**What would make the next trial fair** (not built):

| Phase | What | Done when |
| --- | --- | --- |
| C1 | **The arrangement, not the model.** An answer without the right shape is asked for again up to three times with what was wrong said to the model. The upstream provider behind OpenRouter can be named for a task, since some hold a model to the shape and some do not. The quick check knows `node --test`. The most an answer may be is raised for models that reason. | The fourth attempt, repeated, ends for a reason of the code and not of the shape. |
| C2 | **Inkling as an agent with tools.** Today the task that implements a change runs a subscription's command-line agent, which can run commands and read what they print; the gateway lets that agent be paid by an API key of its own vendor only. Letting the agent be pointed at OpenRouter would give Inkling the same tools the subscription agents have. | Deliver issue runs with Inkling as the implementer and a subscription agent as the reviewer. |
| C3 | **A trial on more than one item.** The lab can try a flow on at most eight past runs, and Deliver issue has two, of the same item. More delivered items are needed first. | The share of items finished, the rounds, and the cost, beside the subscription agents on the same items. Inkling becomes the implementer of a flow only where it finishes as many. |


## Decided (25 November 2026), in part replaced

1. **Inkling is used through OpenRouter.** Both models of the second generation are offered there through the account the platform already has.
2. **No model is run on our own nodes.** The section on that below is kept as the record of what was looked at; nothing of it is built or planned.
3. **Inkling is not a default for writing code.** Nothing found shows it writing code as well as the Claude and GPT models that write it here: the maker's own table puts Inkling 2 Mini at 48% and Inkling 2 Small at 72% on DeepSWE, below a model that costs less than a fifth of Inkling 2 Small, and claims no parity with the largest models. Every flow that writes code keeps the model it has. Inkling stays in the list of models you can choose for a step, and the platform never chooses it for one.
4. **It is measured before it is trusted with anything.** The first phase is built: a trial of a model on questions a check already has an answer to.

## What is asked for

Consider Inkling, above all for code generation: what it costs on OpenRouter, how much it would save, where else it helps. Look at running it on our own node, which will have about 16 GB of graphics memory, and prefer that if it is feasible. Add the work to the plan.

## What Inkling is

A family of open-weight models from Thinking Machines Lab, under the Apache 2.0 licence. All four are on OpenRouter. Prices are per million tokens, read on 25 November 2026.

| Model | Size (active of total parameters) | Read | Written | Cached read | Context | Holds an answer to a shape | Offered on the platform today |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| Inkling 2 Mini | 3.4 B of 26.4 B | $0.075 | $0.30 | $0.015 | 1 M | yes | yes, through the OpenRouter account |
| Inkling 2 Small | 17 B of 594 B | $0.30 | $1.00 | $0.03 | 1 M | yes | yes, through the OpenRouter account |
| Inkling Small | 12 B of 276 B | $0.45 | $1.20 | $0.10 | 524 k | no | no |
| Inkling | 41 B of 975 B | $1.00 | $4.05 | $0.17 | 1 M | no | no |

The two older models do not hold an answer to a shape, which every task of the platform asks for, so the platform does not offer them. The two of the second generation are the ones to consider.

What the maker reports for coding, not measured by us:

| Benchmark | Inkling 2 Mini | Inkling 2 Small | Compared with, in the maker's table |
| --- | ---: | ---: | --- |
| DeepSWE v1.1 | 48.2% | 72.1% | Qwen3.8-27B 42.2%, DeepSeek V4.1 Flash 77.0% |
| SWE-bench Pro v2, hard | 77.9% | 86.3% | |
| Vibe Code Bench v1.1 | 77.9% | 81.6% | |
| Artificial Analysis index (independent) | 20 | 31 | |

Inkling 2 Mini is ahead of the open model of its size it is compared with, and well behind the large ones; Inkling 2 Small is behind DeepSeek V4.1 Flash, which costs less than a fifth of it. One report says Inkling 2 Small writes more tokens for a task than its predecessor did: a price per token is not a cost per task, and only a measurement on our own work says what a task costs.

## What the platform spends today

From the platform's own usage report, 90 days to 25 November 2026.

| | Tokens | Recorded cost |
| --- | ---: | ---: |
| All | 85.2 M | €154.41 |
| Anthropic key | 41.8 M | €154.02 |
| OpenAI key | 31.4 M | €0.03, with 31.1 M tokens that have no price recorded |
| OpenAI, not attributed to an account | 11.4 M | no price recorded |
| OpenRouter | 0.4 M | €0.03 |
| Claude and ChatGPT subscriptions | 0.3 M | flat fee |

€154 is a floor: half of the tokens carry no price.

By what the tokens were for:

| Work | Tokens | Share |
| --- | ---: | ---: |
| Reviews of the plan: Discovery, coverage, the worklists, the checks | 83.9 M | 98.5% |
| Evidence and accuracy studies | 0.9 M | 1.1% |
| Code: Audit code (10 runs, 4 of them metered), Code clean (1 run) | 0.2 M | 0.2% |
| Deliver issue (2 runs, 57 minutes of sandbox) | not metered: it ran on a subscription | |

**Code generation costs next to nothing today.** Three kinds of flow that touch code ran thirteen times in all, on the subscriptions. The money goes to reading and judging the plan, at €3.69 per million tokens on the Anthropic key. That is where a cheaper model saves first. Code generation becomes a cost when delivery and the bug-fix flow run in numbers and on an API key.

## What Inkling would save

### On code generation

No delivery run was metered, so this is per million tokens read and 200,000 written, the usual proportion of an agent that reads much and writes little.

| Model | Cost | Against Opus 5.5 | Against Sonnet 5.5 |
| --- | ---: | ---: | ---: |
| Claude Opus 5.5 | $8.00 | | |
| Claude Sonnet 5.5 | $4.00 | −50% | |
| Inkling | $1.81 | −77% | −55% |
| Inkling 2 Small | $0.50 | −94% | −88% |
| Claude Haiku 5.5 | $0.20 | −98% | −95% |
| Inkling 2 Mini | $0.135 | −98% | −97% |
| Inkling 2 Mini on our node | electricity | | |

Two things this table says plainly. Inkling 2 Small would cut the cost of code written by Opus or Sonnet by about nine tenths, if it does the work as well, which the maker's own numbers do not claim. And Claude Haiku 5.5, already on the platform, costs less than Inkling 2 Small and about what Inkling 2 Mini costs: on price alone Inkling is not a new tier. What it adds is in the next two sections.

### On the reviews of the plan

The 41.8 M tokens of the Anthropic key, at list price, if nine in ten are read and one written:

| Model | Cost | Against the €154 recorded |
| --- | ---: | --- |
| Inkling 2 Small | about $15 | about a tenth |
| Claude Haiku 5.5 | about $6 | about a twenty-fifth |
| Inkling 2 Mini | about $4 | about a fortieth |
| Inkling 2 Mini on our node | electricity | |

### What it adds beyond price

- **Open weights.** It can run on our node, with no price per token, no allowance to run out of, and nothing of a project's text leaving the cluster.
- **A third family.** The platform does not let a change be judged by the family that wrote it. Today there are two families that write code well, Anthropic's and OpenAI's. Inkling is a third, which makes "written by one, reviewed by another" cheaper to arrange.
- **It reads images.** The UI and UX audits look at screenshots.

## Running it on our node (looked at, and set aside)

Set aside on 25 November 2026. What follows is what was found, kept so that the question need not be looked into again.

**What the maker says.** Inkling 2 Mini is one checkpoint of about 20 GB. It is supported on an NVIDIA DGX Spark and on Apple computers with 32 GB or more; for other graphics cards 32 GB or more is recommended. The builds published for Ollama are 18 to 20 GB. It runs under vLLM, SGLang, Ollama and llama.cpp. The three larger models are 276 B to 975 B parameters and are out of the question for one node.

**What that means for 16 GB.** It does not fit whole, and it is below what the maker recommends. Two ways remain, and neither is proven:

1. The 18 GB four-bit build, with what does not fit on the card kept in the node's memory. The model uses 3.4 B of its 26.4 B parameters for each token, which is the kind of model that suffers least from being split this way. How fast it then is, nobody has published.
2. A smaller build that fits whole. Such builds are listed, but I could not confirm their sizes, and a smaller build answers worse by an amount that has to be measured.

Either way the window is not the million tokens of the hosted model: on 16 GB it is some tens of thousands. That suits the short questions the platform asks about the plan, which are 2,000 to 4,000 tokens each. It does not suit a delivery run, which reads far more.

**So: feasible to try, not shown to work.** I could not try it. The cluster does not hand a card to any workload today (the ranking service runs on the processors), and this session has no graphics card. The same model through OpenRouter costs $0.075 and $0.30 a million tokens.

### Three nodes with 16 GB each (added 25 November 2026)

The cluster has three nodes, each with a 16 GB card. That is not one card of 48 GB: a model runs in one process on one machine, and memory on different machines adds up only if the model itself is cut in pieces that talk over the network.

| Way | Fits? | What it costs | Known? |
| --- | --- | --- | --- |
| **A copy on each node**, of a build that fits whole in 16 GB (about 13 GB, leaving room for the texts) | Yes, if such a build of Inkling 2 Mini answers well enough | Nothing in speed. Three questions at a time, and a node can fail. | The quality of the smaller build has to be measured |
| **A copy on each node**, the 18 GB build with the rest in the node's memory | Yes | Slower by an amount nobody has published | To be measured |
| **One copy cut across two nodes** (llama.cpp's RPC server, or vLLM over several machines) | Yes: 20 GB over two cards of 16 | Every token crosses the network between the nodes. Inkling is a looped model, which may cross it several times for one token. One node failing stops it. | Whether these servers cut this model at all is not confirmed |

The first two are preferred, in that order. The platform's work is thousands of short questions that do not depend on each other (1,567 links in Falcone), and three copies answer three at a time: that is what three nodes are good for. Cutting one copy across nodes is the last thing to try, and only if both others fail the gate.

One use of the cards is separate from Inkling and stays open: the ranking models run on processors today, at 3 to 8 texts a second, and would be much faster on a card, with models that fit in a few GB. It is not planned.

Confirmed on 25 November 2026: the cards are NVIDIA, 16 GB of graphics memory each. Every server named here runs on NVIDIA cards, so nothing above changes.

What Argo CD shows of the nodes, read the same day:

| Node | Processors | Memory | Note |
| --- | ---: | ---: | --- |
| `kind` | 12 | 32.5 GB | All eight pods of the platform run here |
| `github-actions` | 12 | 32.4 GB | |
| `mus-srv-0` | 4 | 8.1 GB | Too little memory to hold part of the model beside the card: only a build that fits whole in 16 GB can run here, and its server must load it without first holding it in memory |

No application in Argo CD installs anything of NVIDIA's, and no workload asks for a card. Whether the drivers are on the nodes I cannot see from here.

**What it would have needed.**

- On each node, on the machine itself: NVIDIA's driver and NVIDIA's container toolkit, then a restart of k3s, which finds the toolkit when it starts and adds the `nvidia` runtime by itself. This is outside the cluster and is yours.
- In the cluster: the runtime class `nvidia` and NVIDIA's device plugin, which makes each node offer `nvidia.com/gpu: 1`. That is set up for the whole cluster, outside the three applications I manage, so it is yours to install or to allow me as a fourth application.
- About 30 GB of disk for the weights, and one download of them from Hugging Face or Ollama's registry.
- A choice: the card holds the language model or the ranking models, not both. The ranking service works on the processors today, so the card would go to the language model.

## Where it is useful

In the order of what it would save, with what guards the answer in each case.

| Use | Why it fits | Guard that exists |
| --- | --- | --- |
| The checks of the plan: Stories nothing supports, extra Epic links, issues without a Story, primary Epics | Most of the spending. Short questions with a fixed shape. | Quoted passages are checked against the texts; "unclear" is an answer; a sample is judged before a batch is approved |
| Summaries of the code | Much text read once, little written | The summary names the files it read |
| Epic overviews | One per Epic, rewritten when records change | A sentence that names no record is dropped |
| Bug triage (B5): severity, area, likely duplicate, what a report lacks | Short, frequent, and nothing is set without you | You accept or change each suggestion |
| Bug or enhancement, and which section an item goes to | One word answers | Your confirmation |
| First pass of Audit code and Code clean | Finds candidates; a stronger model confirms each | The confirmation step |
| The UI and UX audits | It reads screenshots | Findings are proposals |
| Code generation: Deliver issue, Fix a bug | Not as a default (decided 25 November 2026). You can choose it for a step; the platform does not. | |

It is not for: the supervisor's decisions about what to run, and the review that decides whether a change is merged. Those stay with the strongest model.

## How a task would choose its model

For the tasks that ask a model a question with a fixed shape, and only once a trial shows the cheaper model answers them as the answers on record do:

1. **First: Inkling 2 Mini or Inkling 2 Small, through OpenRouter**, whichever the trial shows is enough for that check.
2. **Second: the model you choose for the run**, asked only about what the first could not settle: it said "unclear", its quoted passages were not in the texts, or its answer did not have the shape.
3. Every run reports how many questions each settled, and what the run cost against what it would have cost with the second model alone.

This is the same cascade the reorganization uses, one step longer: rules, ranking models, a small language model, a large one, you. It does not apply to writing code.

## Phases

| Phase | What | Done when |
| --- | --- | --- |
| M0 | **Try a model on questions already answered** (built, 25 November 2026). A trial asks another model the questions a check has an answer to, keeps what it says apart, and reports agreement and cost. | For each model tried: how often it says the same as the answer on record, how often it says "unclear", how often its quotes are in the texts, and what a question costs. |
| M1 | **Two models in a run** (built, 25 November 2026). A run of a check may name a first model. What it settles is kept; the rest is asked of the run's own model. The run reports what each did and what it cost. | A run of the Epic-links check with Inkling first and your model second costs less than the same run with your model alone, and its batch passes its sample. Not yet shown on live: no such run has been started. |
| M2 | **The cheaper model first, by default** (built for the four checks, 25 November 2026). A check asks first the model whose trial on that check passed a gate, unless you choose otherwise. Never for writing code. | A check with a passed trial preselects that model; one without preselects none. Not built: the same for the summaries of the code, the Epic overviews and bug triage, which have no trial to go by, and a Dashboard tile. |

## M0 as built

**Where.** At the foot of a project's Reorganize page: "Try another model on questions already answered".

**What a trial asks.** The questions of one check, exactly as the check asks them: whether anything supports a User Story, the Epics a User Story is also linked to, the primary Epic of a User Story, the User Story of an issue. Only about records whose answer on record is still about the texts as they stand. A sample of 100, or all of them at your word. The sample is picked by a digest of each record's id, so it is spread over the project and is the same for every model: two models are tried on the same records.

**What it keeps.** A line per question: what the model said, and beside it the answer on record, who gave it, and what you did with it. Nothing but the trial's report reads these lines. No worklist shows them, no batch is made from them, no count changes, and the answer on record stays. This is tested: a batch prepared after a trial holds what the check found, not what the trial said.

**What it reports**, for each trial:

- how many questions it answered, and how many it gave no usable answer to;
- how often it said the same as the answer on record, and for a check that judges several links in one question, the share of links;
- how often it said it could not tell, beside how often the answer on record does;
- how often every passage it quoted is in the texts, beside the same for the answers on record;
- the tokens and the cost of a question, beside what a question of that check cost with the model on record, from the project's metered calls;
- where you approved or set aside answers on record, how the model did against those;
- each question where it said otherwise, with both answers and their reasons.

**What it does not say.** The answer on record is another model's, not the truth. A trial measures agreement. Where the two differ, the lines show both reasons, and which is right is yours to judge.

**What it costs.** A trial counts against the project's review tokens like any run, stops at the limit you give it, and can be stopped, resumed and ended.

## M1 as built

**Where.** In the form that starts a run of a check, beside the model: "Ask a cheaper model first". On the four checks that judge: the User Story of an issue, the primary Epic, whether anything supports a User Story, and the Epics a User Story is also linked to. Not on the run that writes Epic overviews, and not on a trial.

**What the first model's answer must be to be kept.** Clear: it says something other than that it cannot tell. Not given with low confidence. And every passage it quotes is in the texts. For the Epic-links check, which judges several links in one question: it judged every link. Anything else, and an answer that cannot be read at all, sends the question on to the run's own model, which is asked as if nobody had been asked before.

**What stays the same.** Every answer waits for you as before, whichever model gave it, and says which model gave it. Batches, samples and undo are unchanged. Both models count against the run's token limit and the project's review tokens; a question that goes on is paid twice, which is why the limit suggested for a run with a first model is higher.

**What the run reports.** How many questions the first model was asked and how many it settled by itself; what the run cost; and what it would have cost had the run's own model answered everything, worked out from what that model's calls in the same run cost.

## M2 as built

**The gate.** A trial now also reports how many of the model's answers a run would have kept from it as a first model, and how many of those say the same as the answer on record. A model passes for a check when its latest trial on that check, in that project, kept at least 30 answers and at least 90% of them were as on record.

**The default.** A check whose trial a model passed asks that model first unless you choose otherwise: the form opens with it chosen and says why, with the numbers. Among several that passed, the one whose questions cost least. A model's latest trial counts, so a later trial that does worse takes the default away. A check with no passed trial asks no model first, and says so.

**What it does not cover.** The summaries of the code and the Epic overviews are prose, and nothing here measures a cheaper model's prose; bug triage (B5) is not built. They keep the model you choose. Nothing that writes code has a first model at all. The share of questions a first model settled is on each run and on the trials table, not on the Dashboard.

**On live, 25 November 2026.** Deployed. Falcone has answers on record to try a model on: 129 User Stories for "whether anything supports a User Story" (by Claude Fable 5 and Claude Opus 5.5), 351 for "the Epics a User Story is also linked to" and 51 for "the primary Epic" (both by Claude Fable 5). A sample of 100 is likely to use about 315,000 tokens for the first and 353,000 for the second. Falcone-serverless and musematic have no answers on record yet. No trial has run: Falcone has used 2.98 of its 3.00 million review tokens, and a trial counts against them, so it waits for the limit to be raised.

## Sources

- OpenRouter's public list of models, read 25 November 2026: prices, windows and what each model accepts.
- Thinking Machines Lab: [Introducing Inkling 2 Mini](https://thinkingmachines.ai/news/inkling-2-mini/), [Introducing Inkling 2 Small](https://thinkingmachines.ai/news/inkling-2-small/).
- Hugging Face: [thinkingmachines/Inkling-2-Mini](https://huggingface.co/thinkingmachines/Inkling-2-Mini).
- Ollama: [inkling-2-mini](https://ollama.com/library/inkling-2-mini).
- Artificial Analysis: [Inkling 2 Mini](https://artificialanalysis.ai/articles/inkling-2-mini).
- The platform's usage report, `GET /api/dashboard/stats?days=90`, read on live on 25 November 2026.
