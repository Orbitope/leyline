# Change: `context` can carry the bodies of the focus, its callers and the types it takes

## Why
`context` gives an agent about to edit code the declaration lines around it, never a body, so the agent then calls
`source` once for each function it needs to read. What an edit needs is narrow: the whole body of the code being
changed, the code around the places that call it (they break when its behavior changes) and the shape of the types it
takes, with none of the unrelated methods that share a file with them. The map knows which those are.

## What Changes
- `context` takes `bodies`. With it the budget is at least 600 tokens (a smaller one is raised, and the answer says so).
  The answer holds the declaration outline as before, cut to a third of the budget, and then "Bodies", in this order:
  1. the whole body of each focus function, at most three (the highest ranked); other focus functions, and the
     functions of a file or type given as the focus, are listed as left out;
  2. for each type the focus takes as a parameter or returns, its declaration line and its field lines, at most 12, and
     "and N more fields". They come before the callers because they are short and the focus cannot be read without
     them, and a function with a hundred callers would otherwise use the room up first;
  3. for each direct caller, non-test callers first and nearest in the rank first, the lines around the call site: at
     most 40 lines of its body, centered on the call (the whole body when shorter), and 16 for a caller that is a file's
     top level. A caller found by a guess says so, and a caller of the method the focus overrides says "through" it.
  A body is shown once: one that sits inside a body already shown (the caller of a nested function is the function that
  contains it) is skipped. A function defined twice (an if and an else, a getter and a setter) shows each definition.
  A body past 80 lines is cut and says at which.
- Packing is decided from the lengths the map holds, before any file is read, and stops after ten bodies refused for
  size. What did not fit is listed under "Bodies left out", ten at most and "and N more", each with its length.
- A file that is a link leaving the repository is not read, as `source` does not read it. A body whose lines no longer
  hash to what the map saw, because the file was edited since, is not shown and is named as changed since the map.
- The text holds the outline and the bodies once. The answer's `bodies` is a list of what each is (id, name, role, path,
  lines, whether whole, cut or left out, and why) without its text. The tool's answer may be 44,000 characters, and
  `bodies` is never cut.
- `leyline context --bodies` and the tool's `bodies` argument say the same. Without it nothing changes.

## Impact
The graph keeps two more values per node, its last line and its content hash. The reader `context` already uses for
declarations gains the guard `source` has against a link leaving the repository. A call with `bodies` reads only the
files of the bodies it shows, and the types and callers it examines. The default answer is what it was.
`docs/mcp-tools.md` lists every tool's arguments and sizes, and a test checks the arguments.
