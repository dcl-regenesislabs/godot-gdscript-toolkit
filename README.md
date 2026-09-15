# GDScript Toolkit
[![](https://github.com/Scony/godot-gdscript-toolkit/workflows/Tests/badge.svg)](https://github.com/Scony/godot-gdscript-toolkit/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Buy me a coffe](https://img.shields.io/badge/Buy%20me%20a%20coffe-FF5E5B?logo=ko-fi&logoColor=white)](https://ko-fi.com/pawel_lampe)

This project provides a set of tools for daily work with `GDScript`. At the moment it provides:

- A parser that produces a parse tree for debugging and educational purposes.
- A linter that performs a static analysis according to some predefined configuration.
- A formatter that formats the code according to some predefined rules.
- A code metrics calculator which calculates cyclomatic complexity of functions and classes.

## Installation

To install this project you need `python3` and `pip`.
Regardless of the target version, installation is done by `pip3` command and for stable releases, it downloads the package from PyPI.

### Godot 4

```
pip3 install "gdtoolkit==4.*"
# or
pipx install "gdtoolkit==4.*"
```

### Godot 3

```
pip3 install "gdtoolkit==3.*"
# or
pipx install "gdtoolkit==3.*"
```

### `master` (latest)

Latest version (potentially unstable) can be installed directly from git:
```
pip3 install git+https://github.com/Scony/godot-gdscript-toolkit.git
# or
pipx install git+https://github.com/Scony/godot-gdscript-toolkit.git
```

## Linting with gdlint [(more)](https://github.com/Scony/godot-gdscript-toolkit/wiki/3.-Linter)

To run a linter you need to execute `gdlint` command like:

```
$ gdlint misc/MarkovianPCG.gd
```

Which outputs messages like:

```
misc/MarkovianPCG.gd:96: Error: Function argument name "aOrigin" is not valid (function-argument-name)
misc/MarkovianPCG.gd:96: Error: Function argument name "aPos" is not valid (function-argument-name)
```

### Decentraland fork: object-lifetime checks

This fork adds three checks for code that runs on Godot's **release** export
templates, where a method call, `is`/`as` or `for` on a freed object is a
SIGSEGV instead of the logged error the debug template gives you. `await` is
where references go stale; the engine protects `self` (a coroutine whose
instance was freed is never resumed) but nothing else.

- `node-reference-across-await` — a method call on a member, parameter or
  local that may hold a node, after the nearest preceding `await` (or anywhere
  in a loop body that awaits, or inside a lambda), with no re-resolution
  (assignment) or `is_instance_valid` / `NodeGuard.is_alive` in between. The
  object whose coroutine or signal is awaited is alive on resume. `self`,
  autoloads, engine singletons, `@onready` members and nodes the class parents
  itself are exempt.
- `node-argument-across-await` — same, for a node-typed name passed as an
  argument after an await (the callee will dereference it).
- `node-null-comparison` — `name == null` / `not name` / bare `if name:` on a
  node-typed or scene-owned name: a freed instance is not null. Noisy on
  lazily-created children; meant for audits and usually disabled in `.gdlintrc`.

The fix the first two ask for is structural: make the async work a method of
the node it needs, re-resolve the node from its owner after the await, or
cancel the work when the owner frees the node. Validity checks are for
lifetimes the code does not own.

A name "may hold a node" when its declared type is not a built-in, an engine
class that does not inherit `Node`, or matched by the `lifetime-safe-types`
regex (put the project's own RefCounted / Resource / GDExtension classes
there); an untyped name is tracked unless it matches `lifetime-safe-names`.
`gdtoolkit/linter/tools/generate_safe_types.gd` prints both lists from a
running engine.

```yaml
# .gdlintrc
lifetime-safe-types: '^(PlaceholderManager|SocialItemData|Dcl\w+)$'
disable:
  - node-null-comparison
```

## Formatting with gdformat [(more)](https://github.com/Scony/godot-gdscript-toolkit/wiki/4.-Formatter)

**Formatting may lead to data loss, so it's highly recommended to use it along with Version Control System (VCS) e.g. `git`**

To run a formatter you need to execute `gdformat` on the file you want to format. So, given a `test.gd` file:

```
class X:
	var x=[1,2,{'a':1}]
	var y=[1,2,3,]     # trailing comma
	func foo(a:int,b,c=[1,2,3]):
		if a in c and \
		   b > 100:
			print('foo')
func bar():
	print('bar')
```

when you execute `gdformat test.gd` command, the `test.gd` file will be reformatted as follows:

```
class X:
	var x = [1, 2, {'a': 1}]
	var y = [
		1,
		2,
		3,
	]  # trailing comma

	func foo(a: int, b, c = [1, 2, 3]):
		if a in c and b > 100:
			print('foo')


func bar():
	print('bar')
```

## Parsing with gdparse [(more)](https://github.com/Scony/godot-gdscript-toolkit/wiki/2.-Parser)

To run a parser you need to execute the `gdparse` command like:

```
gdparse tests/valid-gd-scripts/recursive_tool.gd -p
```

The parser outputs a tree that represents your code's structure:

```
start
  class_def
    X
    class_body
      tool_stmt
      signal_stmt	sss
  class_def
    Y
    class_body
      tool_stmt
      signal_stmt	sss
  tool_stmt
```

## Calculating cyclomatic complexity with gdradon

To run cyclomatic complexity calculator you need to execute the `gdradon` command like:

```
gdradon cc tests/formatter/input-output-pairs/simple-function-statements.in.gd tests/gd2py/input-output-pairs/
```

The command outputs calculated metrics just like [Radon cc command](https://radon.readthedocs.io/en/latest/commandline.html#the-cc-command) does for Python code:
```
tests/formatter/input-output-pairs/simple-function-statements.in.gd
    C 1:0 X - A (2)
    F 2:1 foo - A (1)
tests/gd2py/input-output-pairs/class-level-statements.in.gd
    F 22:0 foo - A (1)
    F 24:0 bar - A (1)
    C 18:0 C - A (1)
tests/gd2py/input-output-pairs/func-level-statements.in.gd
    F 1:0 foo - B (8)
```

## Using gdtoolkit's GitHub action

In order to setup a simple action with gdtoolkit's static checks, the base action from this repo can be used:

```
name: Static checks

on:
  push:
    branches: [ "main" ]
  pull_request:
    branches: [ "main" ]

jobs:
  static-checks:
    name: 'Static checks'
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4
    - uses: Scony/godot-gdscript-toolkit@master
    - run: gdformat --check source/
    - run: gdlint source/
```

See the discussion in https://github.com/Scony/godot-gdscript-toolkit/issues/239 for more details.

## Using gdtookit in pre-commit

To add gdtookit as a pre-commit hook check the latest GitHub version (eg `4.2.2`) and add the followingto your `pre-commit-config.yaml` with the latest version.

```Yaml
repos:
  # GDScript Toolkit
  - repo: https://github.com/Scony/godot-gdscript-toolkit
    rev: 4.2.2
    hooks:
      - id: gdlint
        name: gdlint
        description: "gdlint - linter for GDScript"
        entry: gdlint
        language: python
        language_version: python3
        require_serial: true
        types: [gdscript]
      - id: gdformat
        name: gdformat
        description: "gdformat - formatter for GDScript"
        entry: gdformat
        language: python
        language_version: python3
        require_serial: true
        types: [gdscript]
```

## Development [(more)](https://github.com/Scony/godot-gdscript-toolkit/wiki/5.-Development)

Everyone is free to fix bugs or introduce new features. For that, however, please refer to existing issue or create one before starting implementation.
