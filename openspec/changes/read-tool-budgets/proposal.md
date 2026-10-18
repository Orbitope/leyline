# Change: The read tools that do not size themselves take a token budget

## Why
On a 47,000-line repository `overview` answers in 57 KB, `explain_path` 33 KB, `module_outline` 31 KB and
`expand` 22 KB before the server's cut. Fifteen such calls in one session cost over 100,000 tokens. Only `context`
lets the agent say how much it can afford, and it sizes its own text with that budget. The explain-flow skill also
says `--json` works on each command it names, but `leyline source` and `leyline map` have none.

## What Changes
- A `budget_tokens` argument (600 to 6,000; a token is four characters) on `overview`, `expand`, `impact`,
  `module_outline`, `explain_path`, `find_flows` and `neighbors`, and on no other tool: `context` keeps its own.
  The tool's answer is cut to the smaller of the budget and the tool's limit, by `_fit`, and when the cut came
  from a budget `more` names `budget_tokens` beside the tool's own knob.
- The budget is applied after the answer is computed; it saves tokens, not time. `explain_path` and
  `module_outline` also lower their own `max_steps` or depth to fit, so a small budget walks less.
- `leyline tour --json` prints the tour as `tours.get` returns it, whole; the `tour` tool's cut applies over MCP only.
- The leyline-explain-flow skill names `--json` only on the commands that have it.

## Impact
`_tool` takes a flag saying which tools are budgeted and reads `budget_tokens` only for them. Defaults do not
change, so present callers see the same answers; a budget can only make an answer shorter than the default.
