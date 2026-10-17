# Change: Links from Unity and Godot assets to the code they run

## Why
In a game project the link that matters most is often not in the code: a Unity prefab names a script by a GUID and an
event on it names a method by a string, and a Godot scene names a script by a path and a signal connection names a
method by a string. Rename that method, or the class, and nothing in the code says the scene breaks. The map shows no
caller, so `impact` says nothing reaches it, and a plan for the rename lists no one to edit.

## What Changes
- Two new adapters read the text assets of the engines as files on the map, each with its own language: Godot
  `godot-scene` (`.tscn`, `.tres`) and Unity `unity-yaml` (`.unity`, `.prefab`, `.asset`). They are registered whatever
  `LEYLINE_GENERIC` says, and read a file line by line, not as YAML, in a stream: no list of its lines is built.
  They take what an asset names: the script a node or component runs, the scene or prefab it includes, and the methods
  its signal connections and persistent event calls name.
- A Unity script, scene, prefab or asset is found by its GUID, from the `.meta` file beside it; a Godot script by its
  `res://` path, which is relative to the folder of the `project.godot` above it, not to the repository. Godot 4's
  `uid://` is read through the same files. A Unity event on a component of a prefab instance is followed to the prefab.
- Each asset becomes a file node with no functions, in the module of the nearest folder above it that already is one:
  a folder of assets alone never becomes a module. A pass after calls and endpoints are resolved links it: an `imports`
  edge (exact) to the script, scene or prefab it names, and a `communicates` edge on the channel `asset` (heuristic,
  because it is found by name) to a method of that script, from one edge to a method however many connections name it.
  A method counts as the script's when its class or a base class on the map has it, in any file of a partial class.
- `impact` of a type, a method or a field of a script reaches the assets that import its file, through a new reverse
  step from the contents of a script to the assets that run it, so renaming a class or a serialized field names the
  prefab. `impact` of a method also reaches the assets that wire it by name.
- What cannot be tied to code is counted by reason and the first few named, in the extractor's coverage, which `overview`
  lists: a script or prefab outside the repository (a GUID no `.meta` file claims), a GUID that two `.meta` files claim, a method the
  script lacks, a script whose language is not read (GDScript without its grammar), and calls on built-in components.
  Nothing is linked for those. Nothing is executed, and no engine is needed.
- A changed or added `.meta` file counts as a change to `plan`, `check` and `map`, so the links are made again. The pass
  runs over every asset's references on each map, which are small and cached, and an incremental map equals a full one.
- Assets up to 64 MB are read (`LEYLINE_MAX_ASSET_MB`; code keeps 5), and the page leaves the text of assets out.

## Impact
Files with these extensions, which no adapter read before, now go through the file listing: binary ones and ones over
the limit are left out and named. A repository with none maps as before, and reads no `.meta`. The Godot links need the
GDScript grammar (`pip install 'leyline-code[languages]'`); the Unity ones do not. `docs/indexing.md` says what is read. The test of the stream measures memory with the standard library's `tracemalloc`.
