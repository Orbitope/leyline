"""Learnings match a later finding that says the same thing in other words: synonyms from code review, code names
written another way, and the code both claims are about left out of the comparison. A different worry about the same
code must still not match."""

import pytest

from leyline import learnings, spec, store
from test_learnings import CLAIM, _id, _second_change, decided   # noqa: F401  (the fixture and helpers)
from test_pr import branch   # noqa: F401

# (a learning's claim, the code both are about, a later claim, whether it repeats the learning). The thresholds were
# tuned on these; the old word overlap matched 9 of the 12 rephrasings and 4 of the 15 different worries.
C2 = "validate() throws when a dialogue node has a null showIf, which kills the host"
C3 = "saveOne deletes the lock file before the rename, so a second writer can lose its entry"
C4 = "registerLoreRoutes does not check the path parameter, so a request can read files outside lore/"
C5 = "parse_args returns None when the --tokens option is missing instead of the default"
C6 = "Engine.child copies the name, so a child of a loud engine is loud too"
PAIRS = [
    (CLAIM, {"count"}, "The caller count was not updated for load's new encoding argument", True),
    (CLAIM, {"count"}, "count passes one argument to load and reads the file with no encoding", True),
    (CLAIM, {"count"}, "count invokes load with a single parameter, so the file is read without an encoding", True),
    (CLAIM, {"count"}, "The call site in count still omits the encoding parameter when it loads the file", True),
    (CLAIM, {"count"}, "app.use.count fetches the file through load() with no charset argument", True),
    (CLAIM, {"count"}, "count returns the wrong length for an empty file", False),
    (CLAIM, {"count"}, "count reads the whole file into memory on every call", False),
    (CLAIM, {"count"}, "count does not close the file after reading it", False),
    (CLAIM, {"count"}, "count reads the file twice when it is called with a path argument", False),
    (CLAIM, {"count"}, "count swallows the error when the file is missing", False),
    (C2, {"validate"}, "validate raises an exception if node.showIf is undefined, crashing the host process", True),
    (C2, {"validate"}, "A dialogue node whose showIf is None makes validate throw and the host crash", True),
    (C2, {"validate"}, "validate is slow on large projects because it rebuilds the reference index each time", False),
    (C2, {"validate"}, "validate does not report a missing showIf target node", False),
    (C2, {"validate"}, "validate throws on a dialogue with no nodes array", False),
    (C3, {"saveOne"}, "saveOne removes the lock before renaming the temp file, so a concurrent writer may lose its entry", True),
    (C3, {"saveOne"}, "projectStorage.saveOne drops its lock file ahead of the rename and a second writer loses an entity", True),
    (C3, {"saveOne"}, "saveOne holds the lock for the whole serialization, which is slow on large registries", False),
    (C3, {"saveOne"}, "saveOne writes the entry without sorting its keys", False),
    (C4, {"registerLoreRoutes"}, "The route handler in registerLoreRoutes never validates the path argument, so a request"
                                 " can load files outside the lore folder", True),
    (C4, {"registerLoreRoutes"}, "registerLoreRoutes answers 500 instead of 404 for a missing lore file", False),
    (C4, {"registerLoreRoutes"}, "registerLoreRoutes reads every lore file on each GET /api/lore-files request", False),
    (C5, {"parse_args"}, "parseArgs gives back undefined, not the default, when no --tokens setting is passed", True),
    (C5, {"parse_args"}, "parse_args accepts a negative --tokens value", False),
    (C5, {"parse_args"}, "parse_args returns the wrong default for --json", False),
    (C6, {"child", "Engine"}, "Engine.child duplicates the name, so the child of a loud Engine is loud as well", True),
    (C6, {"child", "Engine"}, "child creates a new Engine on every call instead of caching it", False),
]


@pytest.mark.parametrize("learned, names, claim, same", PAIRS, ids=[p[2][:50] for p in PAIRS])
def test_rephrasings_match_and_other_worries_do_not(learned, names, claim, same):
    sim = learnings.similarity(learned, claim, names)
    assert (sim >= learnings.SAME_CODE) == same, f"{sim:.2f}"


def test_the_synonym_table_is_short_and_each_word_means_one_thing():
    groups = [g.split() for g in learnings.SYNONYMS.strip().split("\n")]
    assert 30 <= len(groups) <= 50
    home = {}
    for k, g in enumerate(groups):
        for w in g:
            assert home.setdefault(learnings._stem(w), k) == k, f"{w!r} is in two groups, which would make them one"
    assert learnings.words("delete the parameter") == learnings.words("remove the argument") == {"remov", "argument"}
    assert learnings.words("returns null") == learnings.words("returns None") == learnings.words("returns undefined")
    assert learnings.words("every call site") == learnings.words("every caller") == set()   # generic: says nothing


def test_code_names_are_compared_as_names():
    # a qualified name is its short name; camelCase, snake_case and acronyms split; a name both claims are about drops
    assert learnings.words("app.use.count reads") == learnings.words("count reads") == {"count", "read"}
    assert learnings.words("`Engine::start()` stops") == {"start", "stop"}
    assert learnings.words("core.py is long") == {"cor", "long"}
    assert learnings.words("parse_args and parseArgs") == {"pars", "argument"}
    assert learnings.words("HTTPServer") == {"http", "server"}
    assert learnings.words("parse_args returns None", {"parseArgs"}) == {"return", "null"}
    assert learnings.short_names(["python:app.use.count", "repo:csharp:P::N.T.Run(int)", "typescript:a.b.<module>"]) \
        == {"count", "Run"}


def test_a_rephrased_finding_is_marked_and_another_worry_is_not(decided):
    root, lid = decided
    _second_change(root, "later")
    from test_pr import run
    assert run("pr", "feature")[0] == 0
    con = store.connect(root / ".leyline/leyline.db")
    count = _id(con, "count")
    same = spec.add_finding(con, "pr-later", "logic", "medium",
                            "The call site in count still omits the encoding parameter when it loads the file", [count])
    assert same["learned"]["id"] == lid
    for other in ("count does not close the file after reading it",
                  "count reads the file twice when it is called with a path argument"):
        assert "learned" not in spec.add_finding(con, "pr-later", "logic", "low", other, [count])
