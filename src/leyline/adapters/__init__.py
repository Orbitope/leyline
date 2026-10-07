"""Language adapters. Each exposes NAME, VERSION, LANGUAGE, EXTENSIONS and parse().

Every language with an installed tree-sitter grammar is read by the generic adapter. C#, Python and
TypeScript also have hand-written adapters that see more (receiver types, overloads, field access);
those are used for their extensions unless LEYLINE_GENERIC=1 asks for the generic one everywhere.
"""

import os

from . import csharp, generic, python, typescript

SPECIFIC = [csharp, python, typescript]
_force = os.environ.get("LEYLINE_GENERIC") == "1"
GENERIC = generic.adapters(skip=() if _force else tuple(a.LANGUAGE for a in SPECIFIC))
ADAPTERS = ([] if _force else SPECIFIC) + GENERIC
BY_EXTENSION = {ext: a for a in ADAPTERS for ext in a.EXTENSIONS}
BY_LANGUAGE = {}
for _a in ADAPTERS:
    BY_LANGUAGE.setdefault(_a.LANGUAGE, _a)
