"""Language adapters. Each exposes NAME, VERSION, LANGUAGE, EXTENSIONS and parse()."""

from . import csharp, python, typescript

ADAPTERS = [csharp, python, typescript]
BY_EXTENSION = {ext: a for a in ADAPTERS for ext in a.EXTENSIONS}
BY_LANGUAGE = {a.LANGUAGE: a for a in ADAPTERS}
