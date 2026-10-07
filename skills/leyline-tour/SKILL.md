---
name: leyline-tour
description: Write a guided tour of a codebase on a Leyline map, or walk someone through an existing tour. Use when asked to onboard someone, explain how a feature works end to end, or say what to read before changing a part of the code.
---

# Write a tour with Leyline

A tour is an ordered list of stops on the map. Each stop points at one thing (a module, a type, a
function, a flow, a pattern, a saved view) and says what to notice there. Use this when the user
wants to learn a codebase or a slice of it, or wants something to hand to another person. It needs
the Leyline MCP server connected. If the `tours` tool is missing, say so and stop.

Leyline already writes an orientation tour on every index. Read it first (`tours`, then `tour`):
often the user wants that, or a narrower tour that starts where it ends. To explain one flow in the
conversation, with nothing saved, use the leyline-explain-flow skill; for what one module holds, leyline-explore-module.

## Steps

1. **Settle who it is for and what they will do next.** "New to the repo" and "about to change the
   scoring code" need different tours. If the request does not say, ask one question.
2. **Find the spine.** A tour follows something: the path of one request, the life of one piece of
   data, or the dependency order of a few modules. Use `overview`, `flows` with `through`, `trace`
   and `patterns` to find it. Pick one spine. Do not mix two.
3. **Choose 5 to 9 stops.** For each, call `expand` and read `source`. Only point at code you read.
   Prefer a function to its type and a type to its module: the narrower the stop, the more the
   reader sees. Use a `flow` stop when order matters and a `pattern` stop when a structure does.
4. **Write each narrative** in two to four sentences: what this is, why the tour stops here, and
   what to carry to the next stop. Say what you inferred and what the graph states. Name the thing
   the reader should look at in the source panel (a field, a branch, a call).
5. **Order them the way you would explain it aloud.** Start where the reader already has footing,
   usually an entry point or a test. End with what to do next or where the edges of the tour are.
6. **Call `save_tour`** with a title that says what the tour covers, the audience, and the stops as
   `{"title", "kind", "ref", "narrative"}`. Check `missing` in the result: a stop whose id does not
   exist was dropped.
7. **Tell the user** the tour is under the Tour tab after a refresh, and give the stop titles in
   order so they can redirect you before reading it.

## Rules

- A tour is a reading order, not a summary. If a stop has nothing to look at, cut it.
- Do not restate the orientation tour. Link to where it left off.
- State blind spots: if the path crosses a process, an event or a file, say the link is not in the
  type system and name both sides.
- Before saving, check every sentence against the source you read. A wrong sentence in a tour does
  more harm than a missing stop, because the reader trusts the order.
- Keep sentences plain. No stop needs more than four.
