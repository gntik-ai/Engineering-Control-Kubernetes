# Functional Overview — Hermes and Temporal at Work

This is a companion to the [architecture course](../README.md). The course goes deep, one component at a time. This overview stays at a **higher level of abstraction** and answers the questions people ask first when they see the platform:

* What is the Hermes Agent framework, and how does it work alongside Temporal?
* How is the backlog built from GitHub issues?
* Who orchestrates the workflows, and how?
* How are workflows and agents defined? Who defines the agents? Where do they run?
* What are the skills and tools that agents and the platform use while work is running?

It was written for **architects who will present the platform** to audiences with very different backgrounds: a product manager, a new engineer, a security reviewer, another architect. Every chapter is therefore layered.

| Marker | Who it is for | What it contains |
|---|---|---|
| **In plain words** | Anyone | The idea without jargon, usually with an analogy. |
| **How it works** | Engineers | The mechanism, the names you will see in logs and dashboards, the diagrams. |
| **For architects** | Architects and reviewers | Design reasons, guarantees, trade-offs, known gaps, and the source file that implements the behaviour. |

You can present only the first layer of every chapter and still tell a complete, correct story. The other layers add precision; they never contradict the first.

## Chapters

| # | Chapter | The question it answers |
|---|---|---|
| 1 | [The big picture](01-the-big-picture.md) | What are the moving parts, and what does "Hermes" actually mean? |
| 2 | [The Hermes Agent framework alongside Temporal](02-hermes-and-temporal-together.md) | Agent framework versus workflow engine; the anatomy of Hermes Agent; its two installations; the integration contract; the three paths through the gateway; Hermes sub-agents versus Temporal steps; how the relationship evolved. |
| 3 | [From GitHub issues to the backlog](03-from-github-issues-to-the-backlog.md) | How open issues are read, classified, ordered, approved, and selected. |
| 4 | [Who orchestrates the workflows](04-who-orchestrates-the-workflows.md) | How a workflow is defined, who starts it, who runs it, and how it reaches the right machine. |
| 5 | [Agents, skills, and tools](05-agents-skills-and-tools.md) | What an agent is here, what it is made of, who defines it, where it runs, and which tools it may touch. |
| 6 | [One issue, end to end](06-one-issue-end-to-end.md) | The whole journey of one GitHub issue to a verified staging deployment, step by step. |
| 7 | [Teaching guide and FAQ](07-teaching-guide-and-faq.md) | Session plans for different audiences, common misconceptions, and answers to frequent questions. |

## Suggested ways to present it

| Time available | Audience | Use |
|---|---|---|
| 10 minutes | Executives, non-technical stakeholders | Chapter 1 ("In plain words" only) and the summary of Chapter 6. |
| 30 minutes | Mixed technical audience | Chapters 1, 2, and 6, with the diagrams. |
| 60–90 minutes | Engineers and architects | All chapters in order, using Chapter 7 for questions. |
| Workshop | People who will operate or extend the platform | This overview first, then lectures 3, 4, 5, and 7 of the course with the Temporal UI and the dashboard open. |

## Conventions

* **Names in `code font`** are the literal names used by the platform: workflow names, step names, Telegram commands, status codes, file paths. Where a path is given, it points at the module that implements the behaviour (for example `src/engineering_control/engineering/backlog.py`).
* **"The operator"** is the authorised human who runs the platform. Today that is one person, reached through Telegram, a command-line tool, and a web dashboard.
* Facts were taken from the source tree at the commit this document ships with and cross-checked against the repository's own inventories (`docs/inventory/*.md`, generated on 2026-09-27) and status ledger (`.go-status/`). When the code and an older document disagree, this overview follows the code and says so.
* Diagrams are Mermaid and render on GitHub and in most Markdown viewers.
