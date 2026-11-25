# Inkling: a cheaper tier, and a model on our own node

Analysis and plan, 25 November 2026. Nothing in it is built yet, and no model was asked anything for it.

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

## Running it on our node

**What the maker says.** Inkling 2 Mini is one checkpoint of about 20 GB. It is supported on an NVIDIA DGX Spark and on Apple computers with 32 GB or more; for other graphics cards 32 GB or more is recommended. The builds published for Ollama are 18 to 20 GB. It runs under vLLM, SGLang, Ollama and llama.cpp. The three larger models are 276 B to 975 B parameters and are out of the question for one node.

**What that means for 16 GB.** It does not fit whole, and it is below what the maker recommends. Two ways remain, and neither is proven:

1. The 18 GB four-bit build, with what does not fit on the card kept in the node's memory. The model uses 3.4 B of its 26.4 B parameters for each token, which is the kind of model that suffers least from being split this way. How fast it then is, nobody has published.
2. A smaller build that fits whole. Such builds are listed, but I could not confirm their sizes, and a smaller build answers worse by an amount that has to be measured.

Either way the window is not the million tokens of the hosted model: on 16 GB it is some tens of thousands. That suits the short questions the platform asks about the plan, which are 2,000 to 4,000 tokens each. It does not suit a delivery run, which reads far more.

**So: feasible to try, not shown to work.** I could not try it. The node has a 6 GB card today, the cluster does not hand the card to any workload (the ranking service runs on the processors), and this session has no graphics card. The plan below makes the local model the preferred first tier on one condition: that it passes a measured gate on the 16 GB card. If it does not, the same model through OpenRouter takes its place at $0.075 and $0.30, and nothing else in the plan changes.

**What it needs from you.**

- The 16 GB card in the node.
- The cluster handing the card to a workload: NVIDIA's device plugin and runtime class. That is set up for the whole cluster, outside the three applications I manage, so it is yours to install or to allow.
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
| Code generation: Deliver issue, Fix a bug | Inkling 2 Small through OpenRouter, not the local model | Tests, a review by another family, your agreement before a pull request |

It is not for: the supervisor's decisions about what to run, and the review that decides whether a change is merged. Those stay with the strongest model.

## How a task would choose its model

One rule for every task that asks a model a question with a fixed shape:

1. **First tier: Inkling 2 Mini on our node**, where it passed the gate. No cost per question.
2. **Second tier: the model you choose for the run**, asked only about what the first tier could not settle: it said "unclear", its quoted passages were not in the texts, or its answer did not have the shape.
3. Every run reports how many questions each tier settled, and what the run cost against what it would have cost with the second tier alone.

This is the same cascade the reorganization uses, one step longer: rules, ranking models, a small language model, a large one, you.

## Phases

| Phase | What | Done when |
| --- | --- | --- |
| M0 | **Measure on what we have.** Inkling 2 Mini and Inkling 2 Small, through OpenRouter, asked the questions the platform already has answers to: the 223 Stories the check answered, the pairs you judged, the accuracy study's held-out decisions. No new answer is kept or acted on. | For each model: how often it agrees with the answer on record, how often it says "unclear", how often its quotes are in the texts, and what a question costs. A few dollars of OpenRouter credit. |
| M1 | **Tiers in a run.** A run names a first model and a second. What the first cannot settle goes to the second. The run's report shows what each settled and what was saved. | A run of the Epic-links check with Inkling 2 Mini first and your model second costs less than the same run with your model alone, and its batch passes its sample. |
| M2 | **A model on the node.** A ninth provider, Local: a model server in the cluster with the card, the weights on a volume, no key, no price, one question at a time. The Accounts page shows it with its health and its window. | A question asked of the local model is answered from inside the cluster and metered at no cost. |
| M3 | **The gate on 16 GB.** The two ways above, measured on the card: memory, tokens a second, and agreement against M0's numbers for the same model through OpenRouter. | Passed when it writes at least 15 tokens a second and agrees within 3 points of the hosted model. If it fails, M4 uses the hosted Inkling 2 Mini and says so. |
| M4 | **First tier by default** for the checks of the plan, the summaries, the overviews and bug triage. | The share of questions settled at no cost is on the Dashboard, and no batch's sample got worse. |
| M5 | **Code generation.** The Lab replays past delivery runs with Inkling 2 Small writing and another family reviewing, beside what was done. Then per flow: which step each model takes. | Per flow: the share of runs whose tests pass and whose review agrees, and the cost of a finished task, for each arrangement. A flow changes its default only where the cheaper arrangement finishes as many tasks. |

M0 and M1 need nothing but your word for the OpenRouter credit, and can start now. M2 to M4 wait for the card and for the cluster to hand it out. M5 follows B3 of the Bugs section, which builds the flow it measures.

## Decisions needed

| # | Question | Recommended |
| --- | --- | --- |
| 1 | May M0 spend a few dollars of OpenRouter credit to measure? | Yes. Without it every number above about quality is the maker's. |
| 2 | Who sets up the card for the cluster, and when is the 16 GB card in? | You install NVIDIA's device plugin, or allow me a fourth application for it. |
| 3 | Order of work | M0 and M1 first, then B3, then M5; M2 to M4 when the card is there. |
| 4 | The gate of M3 | 15 tokens a second and within 3 points. Tell me if you would accept slower. |

## Sources

- OpenRouter's public list of models, read 25 November 2026: prices, windows and what each model accepts.
- Thinking Machines Lab: [Introducing Inkling 2 Mini](https://thinkingmachines.ai/news/inkling-2-mini/), [Introducing Inkling 2 Small](https://thinkingmachines.ai/news/inkling-2-small/).
- Hugging Face: [thinkingmachines/Inkling-2-Mini](https://huggingface.co/thinkingmachines/Inkling-2-Mini).
- Ollama: [inkling-2-mini](https://ollama.com/library/inkling-2-mini).
- Artificial Analysis: [Inkling 2 Mini](https://artificialanalysis.ai/articles/inkling-2-mini).
- The platform's usage report, `GET /api/dashboard/stats?days=90`, read on live on 25 November 2026.
