# Agents Take-Home Assignment

## Build an Access Investigation Agent

- **Timebox:** Four hours
- **Review:** 60 minutes
- **Stack:** Your choice

## Your task

Build a small LLM-based agent that helps a security team investigate employee
access.

The company has roughly 2,000 people and uses several connected systems for HR,
identity, Google Workspace, GitHub, and device management. Access can come from
direct grants, group membership, nested groups, and synchronization between
systems. Account removal and synchronization do not always work as expected,
and some records are incomplete.

Your agent should use tools you design to query the supplied database, gather
the right context, and explain its conclusions with supporting record or event
IDs.

You decide which investigation workflows to support, what interface to provide,
and what you can deliver well in four hours. We intentionally do not provide
investigation questions. Scoping the problem is part of the exercise.

## What you receive

- `input_data/access_snapshot.sqlite`: a synthetic SQLite database
- `input_data/SCHEMA.md`: a guide to the tables and their meaning
- `token.txt`: an OpenRouter API key with a spending limit

The database contains both a snapshot of current access and six months of audit
activity. It answers individual queries quickly, but it is too large to send to
a model in full. Deciding what the model sees, how it retrieves more
information, and how it supports its conclusions is central to the exercise.

We do not provide starter code, an agent framework, a command-line interface,
or a model-client wrapper.

## Definition of done

- The project runs from a clean checkout with documented setup steps.
- The supplied database remains read-only.
- The agent demonstrates at least two investigations that you chose.
- Conclusions include the record or event IDs that support them.
- The submission explains your architecture, how you tested the agent, its
  known limitations, and what you would build next.
- The supplied API key is read from configuration or an environment variable
  and is not included in your submission.

Incomplete but well-reasoned work is acceptable. Do not spend more than four
hours on the implementation, and do not spend time building a polished user
interface. A small command-line or programmatic interface is enough.

Your agent may recommend actions, but it must not change the supplied data or
execute remediation.

## What to submit

Send either a GitHub repository we can access or a zip file containing:

- Runnable source code and setup instructions
- A short explanation of the scope and architecture you chose
- At least two example investigations and the outputs your agent produced
- A description of how you checked whether the agent was working
- Known limitations and what you would build next

You may use coding agents and other development tools. If you use a coding
agent, include the complete session transcript or transcripts with your
submission.

## Using OpenRouter

The supplied key gives you access to current models through OpenRouter. You may
choose any available model and use the key with your own code or a coding agent.
The quickstart is available at
[openrouter.ai/docs/quickstart](https://openrouter.ai/docs/quickstart).

If you exhaust the supplied limit, let us know. Do not commit or submit the key.

## How we will evaluate the work

In priority order, we care about:

1. **Agent and tool design.** How the model decides what to query, what each tool
   does, and how the pieces support a dependable investigation.
2. **Context management.** How you give the model enough information to reason
   accurately without sending it the entire database or losing important
   evidence.
3. **Evidence and uncertainty.** How clearly you separate facts from inference,
   show the records behind a conclusion, and identify gaps in the data.
4. **Scoping and judgment.** How you turn a broad problem into a coherent,
   useful product within the timebox.
5. **Implementation quality.** Whether the code is understandable, runnable,
   and appropriately tested for the time available.

Model choice and cost matter, but a more expensive model is not inherently
better. We also do not reward work beyond the four-hour timebox.

## The review

We will spend 60 minutes together using and discussing your submission. Be
prepared to explain your choices, walk through how the model and tools work
together, discuss how the implementation can fail, and adapt it to an
investigation you did not prepare in advance.

The review is collaborative, not a presentation exercise.
