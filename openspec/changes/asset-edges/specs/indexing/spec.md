## ADDED Requirements

### Requirement: Godot scenes and resources are linked to their scripts and methods
#### Scenario: A Godot scene is linked to the script it runs
- **WHEN** a `.tscn` file has a node whose script is a `res://` path to a GDScript file in the repository
- **THEN** the scene has an `imports` edge to that file

#### Scenario: A Godot project in a subfolder is resolved from its own root
- **WHEN** the repository holds a Godot project in a folder, with its `project.godot`, and a scene there names `res://player.gd`
- **THEN** the scene is linked to the `player.gd` of that folder

#### Scenario: A Godot signal connection is linked to its method
- **WHEN** a scene connects a signal to a method by name on a node whose script has that method
- **THEN** the scene has a `communicates` edge on the channel `asset` to the method, and `impact` of the method lists the scene

#### Scenario: A Godot scene that includes another scene is linked to it
- **WHEN** a scene has a PackedScene resource that is another `.tscn` in the repository
- **THEN** it has an `imports` edge to that scene

#### Scenario: A Godot script missing its grammar is said so
- **WHEN** the GDScript grammar is not installed and a scene names a `.gd` file
- **THEN** no edge is made, and the coverage counts it under "script in a language that is not read", not as outside the repository

### Requirement: Unity prefabs and scenes are linked to their scripts and methods
#### Scenario: A Unity prefab is linked to the script a component runs
- **WHEN** a `.prefab` has a component whose script GUID is the one in the `.meta` file of a C# script in the repository
- **THEN** the prefab has an `imports` edge to that script's file

#### Scenario: A Unity event is linked to the method it calls
- **WHEN** a persistent event call names a method and its target component runs a script that has that method
- **THEN** the prefab has a `communicates` edge on the channel `asset` to the method

#### Scenario: A method of a base class counts as a method of the script
- **WHEN** an event names a method that the component's script class inherits from a base class on the map
- **THEN** the edge goes to the base class's method

#### Scenario: A Unity scene that includes a prefab is linked to it
- **WHEN** a `.unity` file includes a prefab by its GUID, found in the `.meta` file of that prefab
- **THEN** it has an `imports` edge to that prefab

### Requirement: Renaming code a script holds names the assets that run it
#### Scenario: Impact of a script class lists the prefab that runs it
- **WHEN** `impact` is asked of the class a prefab's component runs, or of a method or field of it
- **THEN** the prefab is among what reaches it

### Requirement: What cannot be tied to code is said, not guessed
#### Scenario: A reference to a script outside the repository is counted
- **WHEN** an asset names a GUID with no script in the repository, or a method its script does not have
- **THEN** no edge is made, and the coverage counts each reason apart and names the first few

#### Scenario: A GUID that two files claim is not linked
- **WHEN** two `.meta` files hold the same GUID
- **THEN** nothing is linked to it and the coverage says so

#### Scenario: A call on a built-in component is not counted as missing
- **WHEN** an event calls a method of a built-in engine component
- **THEN** it is counted as a built-in call, not as a method its script lacks

### Requirement: A new kind of file does not cost the map more than it holds
#### Scenario: A folder of assets alone is not a module
- **WHEN** a folder holds only prefabs and scenes
- **THEN** they belong to the module of the nearest folder above that is one, and no module is made for the folder

#### Scenario: The page leaves the text of assets out
- **WHEN** the map page is written for a repository with assets
- **THEN** it holds their names and links and not their text

#### Scenario: A large asset is read in a stream
- **WHEN** a scene of 20 MB is mapped
- **THEN** it is read and linked, and no list of its lines is made

#### Scenario: A binary asset is left out and named
- **WHEN** a repository holds a Unity asset in binary serialization
- **THEN** it is left out of the map and named with its reason, as any unreadable file is

#### Scenario: A changed meta file links the assets again
- **WHEN** a `.meta` file is added or changed and `plan` or `check` maps the code again
- **THEN** the assets are linked again, and the map equals a full map of the same code

#### Scenario: An unchanged asset is not read again
- **WHEN** one code file is edited and the map is made again
- **THEN** no unchanged asset is opened and parsed again

#### Scenario: Both engines map in one repository
- **WHEN** a repository holds a Godot project and a Unity project
- **THEN** each file is linked under its own language and the map is made without error, also with `LEYLINE_GENERIC` set

#### Scenario: A repository without assets maps as before
- **WHEN** a repository has no files of these kinds
- **THEN** nothing about them appears in its map, no `.meta` is read, and the map is what it was
