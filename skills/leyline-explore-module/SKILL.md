---
name: leyline-explore-module
description: Explore one large module of a mapped codebase with Leyline, name its parts from their code, and give the person a one-screen guide to it. Use when asked what a module or folder holds, where to start reading in it, how its parts connect, or which parts are risky to change.
---

# Explore a large module with Leyline

On a large repository one module can hold hundreds of files. Leyline splits it into at most twelve parts
(folders, or groups of files that work together where a folder is flat). You read each part, name it, and
give the person a guide they can read in one screen.

It works with the Leyline MCP server or with the `leyline` command alone:

| MCP tool | Command |
| --- | --- |
| `module_outline` | `leyline outline [module or part id] --depth 2` (`--json` for the ids) |
| `name_part` | `leyline name-part <part id> "<name>" --summary "<one line>" --evidence <node ids>` |
| `source`, `context` | `leyline source <id>`, `leyline context <name or path> --tokens 2000` |
| `search`, `expand` | `leyline search <text>`, `leyline expand <id>` |
| `save_tour` | none; use the server |

If neither is there, say so and stop. If a tool says nothing is mapped, call `map` first.

## Steps

1. **Pick the module.** Take the one the person names. If they name none, call `module_outline` with no
   module and take the largest. A part id from an earlier outline works too.
2. **Get the outline.** `module_outline(module)`. It gives each part's size, entry points, what it uses and
   what uses it, channels, busiest functions, key types and risks. Note the parts that already have a name:
   do not rename one the person named (`named_by`), and read again any marked `stale`.
3. **Read each part.** For each part without a name, read the code that tells you what it is for: its
   busiest functions and key types (`source` on their ids), its entry points, and for a folder its main
   file. `context` on a part's key function shows the code around it. Read at least two items per part.
4. **Name each part.** `name_part(part_id, name, summary, evidence)`: a short name a person would use
   ("Validation rules", not "validation folder"), a one-line summary of what it does, and as evidence the
   node ids you read. Skip a `@more` part: it only holds the rest of the level.
5. **Go one level down where it helps.** A part with many files and no clear purpose: call
   `module_outline` with its id, and repeat steps 3 and 4 for its parts. Stop at two levels unless asked.
6. **Write the guide.** Call `module_outline(module)` again so the names show, then give the person one
   screen:
   - one line on what the module does;
   - the parts, each with its name and summary, largest first;
   - how they connect: which parts call which, and the channels to other modules (routes, files, events);
   - where to start reading for two or three common tasks, each a part and a function or type in it;
   - which parts are risky to change: busy (on the most flows), many dependents, or few tests (from
     `risky`), and why.
7. **Offer to save it as a tour** (the leyline-tour skill, `save_tour`), one stop per part, in reading order.

## Rules

- Names come from reading code, never from folder names alone. Cite the nodes you read for each part.
- One screen per level. Put detail behind a drill-down, not in the guide.
- Say what the outline cannot see: a part reached only through a framework, a callback or a channel the map
  did not follow can look unused.
- Do not edit code while exploring.
