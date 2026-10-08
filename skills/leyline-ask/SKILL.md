---
name: leyline-ask
description: Answer a question about a mapped codebase from its Leyline map and its source, citing the code. Use when asked how something works, what happens when something runs, what a part of the code holds, where something is, who calls it, or what changing it would affect.
---

# Answer a question about the code with Leyline

Answer from the map and the source, never from memory. Every claim names the code it rests on.

It works with the Leyline MCP server or with the `leyline` command alone:

| MCP tool | Command |
| --- | --- |
| `map` | `leyline map <repo>` |
| `overview`, `search`, `expand`, `source` | `leyline overview`, `leyline search <text>`, `leyline expand <id>`, `leyline source <id>` |
| `neighbors` | `leyline neighbors <id> --direction in --kinds calls` |
| `impact` | `leyline impact <name or id>` |
| `context` | `leyline context <name, path or id> --tokens 2000` |
| `module_outline` | `leyline outline [module]` |
| `trace`, `flows`, `flow` | none; use the server |

If neither is there, say so and stop. If a tool says nothing is mapped, call `map` first.

## Route the question

| The question | Do this |
| --- | --- |
| How does X work? What happens when Y? | the leyline-explain-flow skill |
| What is in this part? Where do I start? | the leyline-explore-module skill |
| What would changing X affect? | the leyline-change-impact skill (`impact` alone for a quick answer) |
| Who calls X? Where is X? What does X use? | the steps below |

## Steps

1. **Find the code.** `search` with one distinctive word of the name, then `expand` the best hit to confirm it.
   If two things fit, say which you picked and why, or ask.
2. **Use the narrowest tool.** Who calls it: `neighbors` (direction in, kinds calls). Everything that can reach
   it: `impact`. What it calls and the fields it touches: `expand`. The code around it: `context`. How one
   function reaches another: `trace`.
3. **Read before you claim.** Call `source` on each thing you will say something about. The map says that A
   calls B, not why or under what condition.
4. **Answer** in a few sentences, then the evidence.

## Say how sure you are

- Cite each claim with its node id, or `path:line`.
- Say which links are exact (a compiler confirmed them), which come from syntax, and which are guesses
  (marked `guess` or `guessed`). A guessed link is a lead to check in the source, not a fact.
- Say what the map cannot see: calls by reflection or a name built at run time, a channel whose other end is
  built at run time, files in a language with no adapter, extractors that did not run (`overview` lists them).
- Say what you did not read.

## Rules

- No node, no claim. Never guess a node id: take it from `search`, `overview` or `expand`.
- Answer what was asked. Offer the next question; do not answer it unasked.
- Do not edit code or write views while answering a question.
