# Inkling: a cheaper tier for the reviews of the plan

Analysis and plan, 25 November 2026.

## Decided (25 November 2026)

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
| M1 | **Two models in a run.** A run names a first model and a second. What the first cannot settle goes to the second. The run's report shows what each settled and what was saved. | A run of the Epic-links check with Inkling first and your model second costs less than the same run with your model alone, and its batch passes its sample. |
| M2 | **The cheaper model first, by default,** for the checks where its trial agreed, the summaries of the code, the Epic overviews and bug triage. Never for writing code. | The share of questions the first model settled is on the Dashboard, and no batch's sample got worse. |

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

## Sources

- OpenRouter's public list of models, read 25 November 2026: prices, windows and what each model accepts.
- Thinking Machines Lab: [Introducing Inkling 2 Mini](https://thinkingmachines.ai/news/inkling-2-mini/), [Introducing Inkling 2 Small](https://thinkingmachines.ai/news/inkling-2-small/).
- Hugging Face: [thinkingmachines/Inkling-2-Mini](https://huggingface.co/thinkingmachines/Inkling-2-Mini).
- Ollama: [inkling-2-mini](https://ollama.com/library/inkling-2-mini).
- Artificial Analysis: [Inkling 2 Mini](https://artificialanalysis.ai/articles/inkling-2-mini).
- The platform's usage report, `GET /api/dashboard/stats?days=90`, read on live on 25 November 2026.
