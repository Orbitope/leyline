"""Language adapters. Each exposes NAME, VERSION, LANGUAGE, EXTENSIONS and parse()."""

from . import csharp, python

ADAPTERS = [csharp, python]
BY_EXTENSION = {ext: a for a in ADAPTERS for ext in a.EXTENSIONS}
