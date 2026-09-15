"""Object-lifetime checks for GDScript running on Godot's release templates.

On a release export the GDScript VM does not validate the receiver of a method
call, an `is`/`as` test or a `for` iteration: if the object was freed, that is
a use-after-free and the process dies with SIGSEGV (the debug template turns
the same access into a logged error). Property get/set is validated in every
build, so only the call-like shapes are dangerous.

`await` is where references go stale: the function hands control back to the
engine, and whatever it was about to touch may be gone when it resumes. The
engine protects `self` - a coroutine whose instance was freed is never
resumed, and a lambda that captures `self` is skipped once `self` is gone -
but nothing else: arguments, members, captured locals.

The rules therefore detect a *non-self node reference carried across an
await*. The fix is structural, in this order: make the async work a method of
the node it needs (so the engine cancels it with the node); re-resolve the node
from its owner after the await instead of carrying it; cancel the work when
the owner frees the node (a generation counter). `is_instance_valid()` /
`NodeGuard.is_alive()` clear the rule too, but belong only where the code does
not own the lifetime at all (a remote player that can leave at any time).

Rules:

* `node-reference-across-await` - after the nearest preceding `await` in a
  function (or anywhere in a loop body that awaits, or anywhere in a lambda),
  a method call / `is` / `as` on a member, parameter or local that may hold a
  node, unless the name was re-resolved (assigned) or validated in between.
  Flow-insensitive, in source order; awaits inside one `if`/`match` branch do
  not count for a sibling branch. The object whose coroutine or signal is
  awaited is alive on resume.
* `node-argument-across-await` - same, for a node-typed name passed as an
  argument after an await (the callee will dereference it).
* `node-null-comparison` - `name == null`, `name != null`, `not name` or a
  bare `if name:` on a name that holds a node: a freed instance is not null.

A name "may hold a node" when its declared type is not a built-in, an engine
class that does not inherit Node, or matched by the `lifetime-safe-types`
regex; an untyped name is tracked unless it matches `lifetime-safe-names`.
`self`, uppercase names (autoloads, classes), engine singletons, `@onready`
members, and nodes the class parents itself (`add_child`) and never frees from
another function are exempt from the await rules.
"""

import re
from types import MappingProxyType
from typing import Dict, List, Optional, Set, Tuple

from lark import Token, Tree

from ..common.ast import AbstractSyntaxTree, Class, Function
from ..common.utils import get_column, get_line

from .engine_safe_types import ENGINE_SAFE_TYPES
from .problem import Problem

AWAIT_RULE = "node-reference-across-await"
ARG_RULE = "node-argument-across-await"
NULL_RULE = "node-null-comparison"

GUARD_FUNCTIONS = {"is_instance_valid"}
GUARD_METHODS = {("NodeGuard", "is_alive")}


Position = Tuple[int, int]


# pylint: disable-next=too-few-public-methods
class _TrackedName:
    """One member / parameter / local visible inside a function body."""

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments
    def __init__(
        self,
        name: str,
        declared_at: Optional[Position],
        node_like: bool,
        scene_owned: bool,
        typed_node: bool,
    ):
        self.name = name
        self.declared_at = declared_at
        # May hold a node: declared type not known-safe, or untyped with a
        # name that does not look like plain data.
        self.node_like = node_like
        # Looked up from the scene (@onready / $ / % / get_node): lives and
        # dies with self, so an await cannot leave it dangling.
        self.scene_owned = scene_owned
        # Declared with a type that is not known-safe (or scene-owned): the
        # null-comparison rule only fires on these, untyped names are too noisy.
        self.typed_node = typed_node
        # Position of the last guard or (re)assignment seen in source order.
        self.cleared_at = None  # type: Optional[Position]


class _Scope:
    """Names visible in one function (or lambda) body."""

    def __init__(self, parent: Optional["_Scope"] = None):
        self.parent = parent
        self.names = {}  # type: Dict[str, _TrackedName]

    def declare(self, tracked: _TrackedName) -> None:
        self.names[tracked.name] = tracked

    def lookup(self, name: str) -> Optional[_TrackedName]:
        scope = self  # type: Optional[_Scope]
        while scope is not None:
            if name in scope.names:
                return scope.names[name]
            scope = scope.parent
        return None


# pylint: disable-next=too-many-locals
def lint(parse_tree: Tree, config: MappingProxyType) -> List[Problem]:
    disable = config["disable"]
    run_await = AWAIT_RULE not in disable
    run_arg = ARG_RULE not in disable
    run_null = NULL_RULE not in disable
    if not run_await and not run_arg and not run_null:
        return []
    ast = AbstractSyntaxTree(parse_tree)
    safe_types = _compile_safe_types(config["lifetime-safe-types"], ast)
    safe_names = re.compile(config["lifetime-safe-names"])
    problems = []  # type: List[Problem]
    for a_class in ast.all_classes:
        members = _collect_members(a_class, safe_types, safe_names)
        # The AST helper skips `static func`; its node wraps a plain func_def.
        functions = list(a_class.functions) + [
            Function(s.lark_node.children[0])
            for s in a_class.statements
            if s.kind == "static_func_def"
        ]
        own_children = _names_added_as_children(a_class)
        for function in functions:
            checker = _FunctionChecker(
                function,
                members,
                own_children,
                safe_types,
                safe_names,
                (run_await, run_arg, run_null),
            )
            problems += checker.run()
    return problems


# --- declarations ---------------------------------------------------------


def _compile_safe_types(pattern: str, ast: AbstractSyntaxTree) -> "re.Pattern":
    """The configured regex, extended with the file's own inner classes that
    do not extend a node (`class Response:` / `class Foo extends RefCounted`):
    those are data holders the project never lists in its config."""
    inner = [
        c.name
        for c in ast.all_classes
        if c.name is not None
        and c.lark_node.data == "class_def"
        and _extends_non_node(c, default=True)
    ]
    if not inner:
        return re.compile(pattern)
    return re.compile(
        "(?:{})|^(?:{})$".format(pattern, "|".join(map(re.escape, inner)))
    )


# pylint: disable-next=too-many-locals
def _collect_members(
    a_class: Class, safe_types: "re.Pattern", safe_names: "re.Pattern"
) -> Dict[str, _TrackedName]:
    members = {}  # type: Dict[str, _TrackedName]
    own_children = _names_added_as_children(a_class)
    freed_members = _names_freed(a_class)
    singletons = _names_assigned_engine_singletons(a_class)
    for statement in a_class.statements:
        if statement.kind not in ("class_var_stmt", "static_class_var_stmt"):
            continue
        var_node = statement.lark_node.children[0]
        if var_node.data == "class_var_stmt":  # static var: one level deeper
            var_node = var_node.children[0]
        name, type_hint, initializer = _split_var_node(var_node)
        if name is None:
            continue
        onready = any(a.name == "onready" for a in statement.annotations)
        # A member the class builds in its declaration and never frees is
        # owned by the instance: nothing else holds it, so it cannot be gone
        # while `self` is alive (and a Node self never resumes after death).
        self_built = (
            _constructed_type(initializer) is not None
            and name not in freed_members
            and not _extends_non_node(a_class)
        )
        scene_owned = (
            onready
            or _is_node_lookup(initializer)
            or name in own_children
            or self_built
        )
        node_like = scene_owned or _is_node_like(
            name, type_hint, safe_types, safe_names
        )
        if name in singletons or (
            type_hint is None and _is_safe_initializer(initializer, safe_types)
        ):
            node_like = False
        typed_node = scene_owned or _is_typed_node(type_hint, safe_types)
        members[name] = _TrackedName(name, None, node_like, scene_owned, typed_node)
    return members


def _names_added_as_children(a_class: Class) -> Set[str]:
    """Names passed to `add_child(name)` / `x.add_child(name)` anywhere in the
    class. When the class is itself a node, a node it parents (directly or
    under one of its children) is freed with it, like an @onready one; the
    engine never resumes a coroutine whose `self` is gone, so such a name
    cannot dangle across an await. Scripts extending a non-Node base
    (RefCounted, Resource) get no such exemption, and neither does a name the
    class frees from a function other than the one that parented it (a list
    that rebuilds its rows gives them a shorter lifetime than `self`); the
    create / add_child / await / queue_free shape inside one function is fine.
    """
    if _extends_non_node(a_class):
        return set()
    added = set()  # type: Set[str]
    freed_elsewhere = set()  # type: Set[str]
    for function in a_class.lark_node.find_data("func_def"):
        added_here, freed_here = _children_added_and_freed(function)
        added |= added_here
        freed_elsewhere |= freed_here - added_here
    return added - freed_elsewhere


def _names_assigned_engine_singletons(a_class: Class) -> Set[str]:
    """Members assigned `Engine.get_singleton(...)` anywhere in the class:
    platform plugins that live for the whole process."""
    names = set()  # type: Set[str]
    for node in a_class.lark_node.find_data("assnmnt_expr"):
        target = _single_name(node.children[0])
        if target is None or len(node.children) != 3:
            continue
        value = node.children[2]
        if isinstance(value, Tree) and value.data == "getattr_call":
            if _attr_names(value.children[0]) == ["Engine", "get_singleton"]:
                names.add(target)
    return names


def _names_freed(a_class: Class) -> Set[str]:
    """Names on which the class calls queue_free() / free() anywhere."""
    freed = set()  # type: Set[str]
    for function in a_class.lark_node.find_data("func_def"):
        freed |= _children_added_and_freed(function)[1]
    return freed


def _children_added_and_freed(function: Tree) -> Tuple[Set[str], Set[str]]:
    added = set()  # type: Set[str]
    freed = set()  # type: Set[str]
    for node in function.iter_subtrees():
        if node.data == "standalone_call":
            callee = node.children[0]
            is_add_child = isinstance(callee, Token) and callee.value == "add_child"
        elif node.data == "getattr_call":
            attrs = _attr_names(node.children[0])
            method = attrs[-1] if attrs else None
            if method in ("queue_free", "free"):
                receiver, direct = _receiver_name(node.children[0])
                if direct and receiver is not None:
                    freed.add(receiver)
            is_add_child = method == "add_child" or attrs[-2:] == [
                "add_child",
                "call_deferred",
            ]
        else:
            continue
        if is_add_child and len(node.children) > 1:
            child = _single_name(node.children[1])
            if child is not None:
                added.add(child)
    return added, freed


def _extends_non_node(a_class: Class, default: bool = False) -> bool:
    """True when the class's `extends` names a built-in / engine class that
    is not a Node. Unknown (project) bases are assumed to be nodes; a class
    with no `extends` at all is RefCounted, reported as `default` (a script
    file without one is usually a scene script, an inner class a data holder)."""
    has_extends = False
    for statement in a_class.statements:
        if statement.kind not in ("extends_stmt", "classname_extends_stmt"):
            continue
        tokens = [
            c.value
            for c in statement.lark_node.children
            if isinstance(c, Token) and c.type == "NAME"
        ]
        if statement.kind == "classname_extends_stmt":
            tokens = tokens[1:]
        has_extends = True
        if tokens and tokens[0] in ENGINE_SAFE_TYPES:
            return True
    return default and not has_extends


def _split_var_node(var_node: Tree):
    """NAME, TYPE_HINT (or None) and initializer expr (or None) of a var node.

    An untyped `var x = Foo.new()` / `var x := Foo.new()` counts as typed Foo.
    """
    name = None
    type_hint = None
    initializer = None
    for child in var_node.children:
        if isinstance(child, Token):
            if child.type == "NAME" and name is None:
                name = child.value
            elif child.type == "TYPE_HINT":
                type_hint = child.value
        elif isinstance(child, Tree) and child.data == "expr":
            initializer = child
    if type_hint is None:
        type_hint = _constructed_type(initializer)
    return name, type_hint, initializer


def _constructed_type(expr: Optional[Tree]) -> Optional[str]:
    """`Foo.new(...)` -> "Foo"; anything else -> None."""
    if expr is None or len(expr.children) != 1:
        return None
    value = expr.children[0]
    if not isinstance(value, Tree) or value.data != "getattr_call":
        return None
    getattr_node = value.children[0]
    parts = [
        c.value
        for c in getattr_node.children
        if isinstance(c, Token) and c.type == "NAME"
    ]
    if len(parts) == 2 and parts[1] == "new" and parts[0][0].isupper():
        return parts[0]
    return None


def _is_node_lookup(expr: Optional[Tree]) -> bool:
    """`$Path`, `%Unique` or get_node*(...) anywhere in an initializer."""
    if expr is None:
        return False
    for node in expr.iter_subtrees():
        if node.data in ("get_node", "unique_node_path"):
            return True
        if node.data == "standalone_call":
            callee = node.children[0]
            if isinstance(callee, Token) and callee.value.startswith("get_node"):
                return True
        if node.data == "getattr_call":
            method = _last_attr(node.children[0])
            if method is not None and method.startswith("get_node"):
                return True
    return False


def _is_node_like(
    name: str,
    type_hint: Optional[str],
    safe_types: "re.Pattern",
    safe_names: "re.Pattern",
) -> bool:
    if type_hint is not None:
        return _is_typed_node(type_hint, safe_types)
    return safe_names.match(name) is None


def _is_typed_node(type_hint: Optional[str], safe_types: "re.Pattern") -> bool:
    """A declared type that is neither built-in, engine non-Node, nor listed
    as safe by the project. `Array[Foo]` is an Array; `Outer.Inner` (enums,
    inner classes) is treated as safe."""
    if type_hint is None:
        return False
    base = type_hint.split("[", 1)[0].strip()
    if "." in base:
        return False
    if base in ENGINE_SAFE_TYPES:
        return False
    return safe_types.match(base) is None


# pylint: disable-next=too-many-return-statements
def _is_safe_initializer(expr: Optional[Tree], safe_types: "re.Pattern") -> bool:
    """Literal, collection or `SafeType.new()` initializers cannot hold a node."""
    if expr is None:
        return False
    if len(expr.children) != 1:
        return False
    value = expr.children[0]
    if isinstance(value, Token):
        return value.type != "NAME" or value.value in ("null", "true", "false")
    if value.data in ("string", "rstring", "array", "dict", "string_name", "node_path"):
        return True
    if value.data == "getattr_call":
        getattr_node = value.children[0]
        parts = [
            c.value
            for c in getattr_node.children
            if isinstance(c, Token) and c.type == "NAME"
        ]
        # Engine singletons (platform plugins) live for the whole process.
        if parts == ["Engine", "get_singleton"]:
            return True
        if len(parts) == 2 and parts[1] == "new":
            return _is_safe_class_name(parts[0], safe_types)
    return False


def _is_safe_class_name(name: str, safe_types: "re.Pattern") -> bool:
    return name in ENGINE_SAFE_TYPES or safe_types.match(name) is not None


def _attr_names(getattr_node: Tree) -> List[str]:
    return [
        c.value
        for c in getattr_node.children
        if isinstance(c, Token) and c.type in ("NAME", "GET", "SET")
    ]


def _last_attr(getattr_node: Tree) -> Optional[str]:
    names = [
        c.value
        for c in getattr_node.children
        if isinstance(c, Token) and c.type in ("NAME", "GET", "SET")
    ]
    return names[-1] if names else None


def _receiver_name(getattr_node: Tree) -> Tuple[Optional[str], bool]:
    """Root name of the receiver of `root[.prop...].method(...)`.

    Returns (name, direct): direct is True for `name.method()` and
    `self.name.method()`, False when properties are read in between
    (`name.child.method()`) - the root may be alive while the property holds
    a freed node, so both shapes are reported. Calls in the chain
    (`name.get_x().method()`) are reported through the inner call instead.
    """
    children = getattr_node.children
    if not isinstance(children[0], Token) or children[0].type != "NAME":
        return None, False
    names = [
        c.value
        for c in children
        if isinstance(c, Token) and c.type in ("NAME", "GET", "SET")
    ]
    if names[0] == "self":
        names = names[1:]
        if not names:
            return None, False
    if len(names) == 1:
        return None, False
    return names[0], len(names) == 2


def _awaited_owner(await_node: Tree) -> Optional[str]:
    """NAME for `await NAME.method(...)` / `await NAME.signal`; else None."""
    if not await_node.children:
        return None
    awaited = await_node.children[-1]
    if not isinstance(awaited, Tree):
        return None
    if awaited.data == "getattr_call":
        receiver, direct = _receiver_name(awaited.children[0])
        return receiver if direct else None
    if awaited.data == "getattr":
        names = _attr_names(awaited)
        first = awaited.children[0]
        if len(names) == 2 and isinstance(first, Token) and first.type == "NAME":
            return first.value
    return None


def _single_name(node) -> Optional[str]:
    """A bare NAME token, or `self.NAME`, as a name string."""
    if isinstance(node, Tree) and node.data == "expr" and len(node.children) == 1:
        return _single_name(node.children[0])
    if isinstance(node, Token):
        return node.value if node.type == "NAME" else None
    if isinstance(node, Tree) and node.data == "getattr":
        children = node.children
        if (
            len(children) == 3
            and isinstance(children[0], Token)
            and children[0].value == "self"
            and isinstance(children[2], Token)
        ):
            return children[2].value
    return None


def _position(node) -> Position:
    return (get_line(node), get_column(node))


def _end_position(node) -> Position:
    if isinstance(node, Tree):
        return (node.meta.end_line, node.meta.end_column)
    return (node.end_line, node.end_column)


def _terminates(statements) -> bool:
    """True when the last statement of a block is return / break / continue."""
    last = None
    for statement in statements:
        if isinstance(statement, Tree):
            last = statement
    return last is not None and last.data in (
        "return_stmt",
        "break_stmt",
        "continue_stmt",
    )


def _latest(a: Optional[Position], b: Optional[Position]) -> Optional[Position]:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _is_trackable(name: str) -> bool:
    return name != "self" and not name[0].isupper()


# --- per-function walk ----------------------------------------------------


# pylint: disable-next=too-few-public-methods
class _FunctionChecker:
    # pylint: disable=too-many-instance-attributes,too-many-arguments
    # pylint: disable=too-many-positional-arguments
    def __init__(
        self,
        function: Function,
        members: Dict[str, _TrackedName],
        own_children: Set[str],
        safe_types: "re.Pattern",
        safe_names: "re.Pattern",
        enabled: Tuple[bool, bool, bool],
    ):
        self.function = function
        self.own_children = own_children
        self.safe_types = safe_types
        self.safe_names = safe_names
        self.run_await, self.run_arg, self.run_null = enabled
        self.problems = []  # type: List[Problem]
        self.reported = set()  # type: Set[Tuple[str, Position]]
        self.nearest_await = None  # type: Optional[Position]
        self.scope = _Scope()
        for member in members.values():
            self.scope.declare(
                _TrackedName(
                    member.name,
                    None,
                    member.node_like,
                    member.scene_owned,
                    member.typed_node,
                )
            )
        self._declare_parameters(function.lark_node.children[0], self.scope)

    def run(self) -> List[Problem]:
        for statement in self.function.lark_node.children[1:]:
            self._walk_statement(statement)
        return self.problems

    # -- declarations

    def _declare_parameters(self, header: Tree, scope: _Scope) -> None:
        for node in header.iter_subtrees():
            if not node.data.startswith("func_arg_"):
                continue
            name = None
            type_hint = None
            for child in node.children:
                if isinstance(child, Token) and child.type == "NAME" and name is None:
                    name = child.value
                elif isinstance(child, Token) and child.type == "TYPE_HINT":
                    type_hint = child.value
            if name is None:
                continue
            node_like = _is_node_like(name, type_hint, self.safe_types, self.safe_names)
            typed_node = _is_typed_node(type_hint, self.safe_types)
            scene_owned = name in self.own_children
            scope.declare(
                _TrackedName(name, _position(node), node_like, scene_owned, typed_node)
            )

    def _declare_local(self, var_node: Tree) -> None:
        name, type_hint, initializer = _split_var_node(var_node)
        if name is None:
            return
        if initializer is not None:
            self._walk_expr(initializer)
        node_like = _is_node_like(name, type_hint, self.safe_types, self.safe_names)
        if type_hint is None and _is_safe_initializer(initializer, self.safe_types):
            node_like = False
        typed_node = _is_typed_node(type_hint, self.safe_types)
        scene_owned = name in self.own_children
        tracked = _TrackedName(
            name, _position(var_node), node_like, scene_owned, typed_node
        )
        # A declaration is an assignment: fresh until the next await. Dated at
        # the end of the statement so `var x = await f()` counts as after it.
        tracked.cleared_at = _end_position(var_node)
        self.scope.declare(tracked)

    # -- statements

    def _walk_statements(self, nodes) -> None:
        for node in nodes:
            if isinstance(node, Tree):
                self._walk_statement(node)

    # pylint: disable-next=too-many-branches
    def _walk_statement(self, node: Tree) -> None:
        kind = node.data
        if kind in ("expr_stmt", "return_stmt"):
            for child in node.children:
                self._walk_expr(child)
        elif kind == "func_var_stmt":
            self._declare_local(node.children[0])
        elif kind == "const_stmt":
            pass
        elif kind == "if_stmt":
            # An await inside one branch is not "before" a sibling branch:
            # each branch starts from the await state at the `if`, and the
            # statement after the `if` sees the latest await of any branch.
            entry_await = self.nearest_await
            exit_await = entry_await
            for branch in node.children:
                self.nearest_await = entry_await
                if branch.data in ("if_branch", "elif_branch"):
                    self._check_condition(branch.children[0])
                    self._walk_expr(branch.children[0])
                    self._walk_statements(branch.children[1:])
                else:
                    self._walk_statements(branch.children)
                # A branch that ends in return/break/continue never reaches
                # the statement after the `if`, so its awaits do not either.
                if not _terminates(branch.children):
                    exit_await = _latest(exit_await, self.nearest_await)
            self.nearest_await = exit_await
        elif kind == "while_stmt":
            self._enter_loop(node)
            self._check_condition(node.children[0])
            self._walk_expr(node.children[0])
            self._walk_statements(node.children[1:])
        elif kind in ("for_stmt", "for_stmt_typed"):
            self._enter_loop(node)
            offset = 1 if kind == "for_stmt" else 2
            self._walk_expr(node.children[offset])
            loop_var = node.children[0]
            type_hint = node.children[1].value if kind == "for_stmt_typed" else None
            node_like = _is_node_like(
                loop_var.value, type_hint, self.safe_types, self.safe_names
            )
            typed_node = _is_typed_node(type_hint, self.safe_types)
            tracked = _TrackedName(
                loop_var.value, _position(node), node_like, False, typed_node
            )
            # Reassigned at the top of every iteration: fresh until an await
            # inside the body (the loop-start await point above is dated
            # before this, so a body that only awaits after using it is fine).
            line, column = _position(node)
            tracked.cleared_at = (line, column + 1)
            self.scope.declare(tracked)
            self._walk_statements(node.children[offset + 1 :])
        elif kind == "match_stmt":
            self._walk_expr(node.children[0])
            entry_await = self.nearest_await
            exit_await = entry_await
            for branch in node.children[1:]:
                if isinstance(branch, Tree):
                    self.nearest_await = entry_await
                    self._walk_statements(branch.children[1:])
                    if not _terminates(branch.children):
                        exit_await = _latest(exit_await, self.nearest_await)
            self.nearest_await = exit_await
        elif kind == "annotation":
            pass
        # pass/break/continue/breakpoint: nothing to do

    def _enter_loop(self, node: Tree) -> None:
        # A loop that awaits resumes at its top: everything in the body runs
        # after an await on the second iteration.
        for _ in node.find_data("await_expr"):
            self._record_await(_position(node))
            return

    # -- expressions

    # pylint: disable-next=too-many-branches,too-many-return-statements
    # Expression kinds with their own walk; everything else is walked generically.
    _EXPR_HANDLERS = {
        "lambda": "_walk_lambda",
        "await_expr": "_walk_await",
        "standalone_call": "_walk_standalone_call",
        "getattr_call": "_walk_getattr_call",
        "type_test": "_walk_type_test",
        "actual_type_cast": "_walk_type_test",
        "assnmnt_expr": "_walk_assignment",
    }

    def _walk_expr(self, node) -> None:
        if isinstance(node, Token):
            return
        handler = self._EXPR_HANDLERS.get(node.data)
        if handler is not None:
            getattr(self, handler)(node)
            return
        if node.data == "comparison":
            self._check_null_comparison(node)
        elif node.data == "asless_actual_not_test":
            self._check_bare_truthiness(node.children[-1], node)
        elif node.data in ("and_test", "or_test", "asless_and_test", "asless_or_test"):
            for child in node.children:
                self._check_bare_truthiness(child, node)
        for child in node.children:
            self._walk_expr(child)

    def _walk_await(self, node: Tree) -> None:
        # The awaited call runs before the suspension, so its receiver and
        # arguments are checked against the previous await point.
        for child in node.children:
            self._walk_expr(child)
        line, column = _position(node)
        self._record_await((line, column))
        # `await x.coroutine()` resumes synchronously when x's call completes
        # and `await x.signal` when x emits: x is alive on resume either way.
        owner = _awaited_owner(node)
        if owner is not None:
            self._record_guard(owner, (line, column + 1))

    def _walk_standalone_call(self, node: Tree) -> None:
        callee = node.children[0]
        if isinstance(callee, Token) and callee.value in GUARD_FUNCTIONS:
            guarded = _single_name(node.children[1]) if len(node.children) > 1 else None
            if guarded is not None:
                self._record_guard(guarded, _position(node))
                return
        self._check_arguments(node.children[1:], node)
        for child in node.children[1:]:
            self._walk_expr(child)

    def _walk_getattr_call(self, node: Tree) -> None:
        getattr_node = node.children[0]
        names = [
            c.value
            for c in getattr_node.children
            if isinstance(c, Token) and c.type == "NAME"
        ]
        if len(names) == 2 and tuple(names) in GUARD_METHODS and len(node.children) > 1:
            guarded = _single_name(node.children[1])
            if guarded is not None:
                self._record_guard(guarded, _position(node))
                return
        receiver, direct = _receiver_name(getattr_node)
        if receiver is not None:
            what = "call a method on" if direct else "call a method through"
            self._check_use(receiver, node, what)
        self._check_arguments(node.children[1:], node)
        # `emitter.signal.connect(func(): emitter.x())`: the emitter is alive
        # while its own signal is being delivered.
        is_connect = _last_attr(getattr_node) == "connect"
        emitter = receiver if is_connect and not direct else None
        for child in node.children:
            if (
                emitter is not None
                and isinstance(child, Tree)
                and child.data == "lambda"
            ):
                self._walk_lambda(child, alive=emitter)
            else:
                self._walk_expr(child)

    def _walk_type_test(self, node: Tree) -> None:
        left = _single_name(node.children[0])
        if left is not None:
            self._check_use(left, node, "type-test")
        for child in node.children:
            self._walk_expr(child)

    def _walk_assignment(self, node: Tree) -> None:
        target = node.children[0]
        for child in node.children[1:]:
            self._walk_expr(child)
        target_name = _single_name(target)
        operator = node.children[1]
        if (
            target_name is not None
            and isinstance(operator, Token)
            and operator.value == "="
        ):
            self._record_assign(target_name, _end_position(node))
        else:
            self._walk_expr(target)

    def _walk_lambda(self, node: Tree, alive: Optional[str] = None) -> None:
        # A lambda runs later by construction: everything it captures is
        # "after an await". Its own parameters and locals are fresh, and so
        # is `alive` (the object whose signal delivers the lambda).
        saved_scope = self.scope
        saved_await = self.nearest_await
        self.scope = _Scope(parent=saved_scope)
        self._declare_parameters(node.children[0], self.scope)
        self.nearest_await = _position(node)
        if alive is not None:
            tracked = saved_scope.lookup(alive)
            if tracked is not None:
                self._shadow_as_alive(tracked)
        self._walk_statements(node.children[1:])
        self.scope = saved_scope
        self.nearest_await = saved_await

    def _shadow_as_alive(self, tracked: _TrackedName) -> None:
        line, column = self.nearest_await or (0, 0)
        fresh = _TrackedName(
            tracked.name,
            tracked.declared_at,
            tracked.node_like,
            tracked.scene_owned,
            tracked.typed_node,
        )
        fresh.cleared_at = (line, column + 1)
        self.scope.declare(fresh)

    # -- bookkeeping

    def _record_await(self, position: Position) -> None:
        self.nearest_await = position

    def _record_guard(self, name: str, position: Position) -> None:
        tracked = self.scope.lookup(name)
        if tracked is not None:
            tracked.cleared_at = position

    def _record_assign(self, name: str, position: Position) -> None:
        tracked = self.scope.lookup(name)
        if tracked is not None:
            tracked.cleared_at = position

    def _check_use(
        self, name: str, node: Tree, what: str, rule: str = AWAIT_RULE
    ) -> None:
        run = self.run_await if rule == AWAIT_RULE else self.run_arg
        if not run or self.nearest_await is None or not _is_trackable(name):
            return
        tracked = self.scope.lookup(name)
        if tracked is None or not tracked.node_like or tracked.scene_owned:
            return
        if tracked.declared_at is not None and tracked.declared_at > self.nearest_await:
            return
        if tracked.cleared_at is not None and tracked.cleared_at > self.nearest_await:
            return
        key = (name, _position(node), rule)
        if key in self.reported:
            return
        self.reported.add(key)
        self.problems.append(
            Problem(
                name=rule,
                description=(
                    'Cannot {what} "{name}" after an await: it may have been freed '
                    "while the function was suspended. Make the async work a method "
                    "of the node, re-resolve it after the await, or cancel the work "
                    "when it is freed (is_instance_valid() only for a lifetime this "
                    "code does not own)"
                ).format(what=what, name=name),
                line=get_line(node),
                column=get_column(node),
            )
        )

    def _check_arguments(self, arguments, call_node: Tree) -> None:
        # A stale node handed to another function is dereferenced there, out
        # of sight of this rule - the top crash site found in the audit
        # (impostor_capturer.gd) was exactly that shape. Only names declared
        # with a node type are reported: untyped arguments are mostly strings
        # and dictionaries, and typing the parameter is the precise fix.
        for argument in arguments:
            name = _single_name(argument)
            if name is None:
                continue
            tracked = self.scope.lookup(name)
            if tracked is not None and tracked.typed_node and not tracked.scene_owned:
                self._check_use(name, call_node, "pass", ARG_RULE)

    def _check_condition(self, condition) -> None:
        self._check_bare_truthiness(condition, condition)

    def _check_bare_truthiness(self, operand, report_node) -> None:
        if not self.run_null:
            return
        name = _single_name(operand)
        if name is None:
            return
        self._report_null(name, report_node, "truthiness")

    def _check_null_comparison(self, node: Tree) -> None:
        if not self.run_null or len(node.children) != 3:
            return
        left, operator, right = node.children
        if not isinstance(operator, Token) or operator.value not in ("==", "!="):
            return
        for side, other in ((left, right), (right, left)):
            if isinstance(other, Token) and other.value == "null":
                name = _single_name(side)
                if name is not None:
                    self._report_null(name, node, "null comparison")

    def _report_null(self, name: str, node: Tree, what: str) -> None:
        if not _is_trackable(name):
            return
        tracked = self.scope.lookup(name)
        if tracked is None or not tracked.typed_node:
            return
        key = (name, _position(node), NULL_RULE)
        if key in self.reported:
            return
        self.reported.add(key)
        self.problems.append(
            Problem(
                name=NULL_RULE,
                description=(
                    'A {what} on "{name}" does not detect a freed node - a freed '
                    "instance is not null; use is_instance_valid({name})"
                ).format(what=what, name=name),
                line=get_line(node),
                column=get_column(node),
            )
        )
