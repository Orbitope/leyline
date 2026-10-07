---
name: leyline-explain-flow
description: Answer "how does X work" or "what happens when Y" about a mapped codebase by finding where the behavior starts and walking it step by step on a Leyline map, reading the code at each step. Use when asked to find, trace, follow or explain a flow, a request, a feature or a code path from a description, especially in a large repository.
---

# Explain a flow with Leyline

The person describes a behavior in words. Leyline finds where it could start and walks the calls and channels from
there; you decide which start fits, read the code, and write the answer. Leyline holds no language model: every step
it gives is a link on the map, and every sentence you write must rest on one of those steps or on code you read.

It works with the Leyline MCP server or with the `leyline` command alone:

| MCP tool | Command |
| --- | --- |
| `find_flows(description)` | `leyline find-flows "<description>"` |
| `explain_path(start, to?, through?, max_steps?)` | `leyline explain-path <start> [--to X] [--through Y] [--steps N]` |
| `diagram(ids)` | `leyline diagram <id or name> ...` |
| `source(node_id)` | `leyline source <id>` |
| `save_tour(title, stops)` | none: the server only |
| `map` | `leyline map <repo>` |

Add `--json` to a command for the same fields the tool returns. If neither the server nor the command is there, say so
and stop. If a tool says nothing is mapped, run `map` first.

## Steps

1. **Restate the question** in one sentence, in the code's terms if you know them ("what happens when a writer saves
   a dialogue: from the editor's Save to the file on disk").
2. **Call `find_flows`** with the question in plain words. Each candidate has its kind (route handler, UI event
   handler, program entry, message handler, test, function), `at` (file:line), `why` it matched, and the flows that
   start there or reach it.
3. **Pick the start.** When `ambiguous` is true, or the first few candidates are different things (a UI handler, a
   route, a test), show the person two to four of them, one line each, and ask which they mean; or pick one and say
   why in a sentence. A UI or command handler is usually the best start for "what happens when"; a route handler when
   the question is about the server; a test whose name states the behavior when nothing else fits. If no candidate
   fits, call `find_flows` again with the words the code would use, or use `search`.
4. **Call `explain_path`** from that start. With nothing else it follows the main flow; give `to` when the question
   names an end ("until it is written to disk") and `through` when it names a stop on the way. Raise `max_steps`, or
   walk again from a later step, when `left_out` hides the part that matters.
5. **Read the code** (`source`) at every step you will say something about: the start, each step that crosses a
   channel (`crosses`), each `later_elsewhere`, and each step whose name does not say what it does. The excerpt in a
   step (`call`, `declaration`) is one line; it is not enough to say what a function does.
6. **Write the answer** as a short numbered walk, one step a line, each naming the code and where it is:

   > 1. The Save button's handler `DialogueCanvas.save` (DialogueCanvas.tsx:230) calls `saveEntity` in the store.
   > 2. `performSave` (store.ts:867) sends `PUT /api/entities/:type/:id` to the host, a separate process.
   > 3. The host's handler for that route (server.ts:361) checks the entity's shape, then `saveOne` writes the file.
   > 4. It then calls `scheduleValidation` (validation.ts:161); validation runs later, off the request.

   Say where the walk crosses into another process, program or service, and where data is written for something
   that reads it later. Skip steps that add nothing (a helper that formats a string); keep the ones a person would
   need to change the behavior.
7. **Include the diagram**: the `mermaid` text of the walk, in a ```` ```mermaid ```` block. Use `diagram` for a
   different set of functions.
8. **Say what the map could not see**: the `not_seen` notes that apply (calls through callbacks or frameworks, an
   interface whose implementation is picked at run time, links guessed by name, messages sent over a program's pipes,
   a walk cut at its depth limit), in one or two sentences.
9. **Offer to save it as a tour** so it can be reopened from the map page. If the person agrees, call `save_tour`
   with one stop per step you wrote, each `{"title", "kind": "node", "ref": <step id>, "narrative": <your sentence>}`.

## Rules

- Every claim cites a node: its name and file:line, from a step or from code you read.
- Never invent a step the map or the code does not show. If the walk stops where the behavior clearly goes on, say
  where it stops and why (`not_seen`), and read the code to find the next step yourself; say that you found it by
  reading, not on the map.
- A step marked `guessed` is a link the map found by name: check it in the code before relying on it.
- Keep the answer short: a walk of 4 to 10 steps, the diagram, and the blind spots. Offer more depth, do not pad.
