import pytest

from gdtoolkit.linter import lint_code, DEFAULT_CONFIG

from .common import simple_ok_check, simple_nok_check

AWAIT_RULE = "unguarded-node-access-after-await"
ARG_RULE = "unguarded-node-argument-after-await"
NULL_RULE = "node-null-comparison"

# Every file below is a Node script with an async helper; `disable` keeps the
# unrelated naming / async-name rules out of the way.
DISABLE = ["async-function-name", "function-name", "class-variable-name"]


def _names(code, **config_overrides):
    config = DEFAULT_CONFIG.copy()
    config.update({"disable": DISABLE})
    config.update(config_overrides)
    return [(p.name, p.line) for p in lint_code(code, config)]


# fmt: off
@pytest.mark.parametrize('code', [
# member call after await, no guard
"""
var modal: Control
func f():
    await g()
    modal.show()
""",
# parameter call after await
"""
func f(card: Control):
    await g()
    card.set_data()
""",
# untyped parameter with a node-looking name
"""
func f(avatar):
    await g()
    avatar.show()
""",
# local declared before the await
"""
func f():
    var card := Control.new()
    await g()
    card.show()
""",
# `is` and `as` are receiver dereferences too
"""
func f(card: Control):
    await g()
    var x = card as Button
""",
# `self.member` spelling
"""
var modal: Control
func f():
    await g()
    self.modal.show()
""",
# use at the top of a loop that awaits later in its body
"""
var modal: Control
func f():
    while true:
        modal.show()
        await g()
""",
# a guard before the await does not cover the resume
"""
var modal: Control
func f():
    if not is_instance_valid(modal):
        return
    await g()
    modal.show()
""",
# each await re-arms
"""
var modal: Control
func f():
    await g()
    if not is_instance_valid(modal):
        return
    await g()
    modal.show()
""",
# call through a property of a stale root
"""
var manager: Object
func f():
    await g()
    manager.instance.show()
""",
# lambda capturing a parameter
"""
func f(card: Control):
    card.pressed.connect(func(): card.hide())
""",
# static functions are checked too
"""
static func f(node: Node):
    await g()
    node.show()
""",
])
def test_unguarded_access_after_await_nok(code):
    outcome = _names(code)
    assert [n for n, _ in outcome] == [AWAIT_RULE], outcome


@pytest.mark.parametrize('code', [
# is_instance_valid after the await
"""
var modal: Control
func f():
    await g()
    if not is_instance_valid(modal):
        return
    modal.show()
""",
# NodeGuard.is_alive after the await
"""
var modal: Control
func f():
    await g()
    if NodeGuard.is_alive(modal, "X.f"):
        modal.show()
""",
# guard and use in one expression, guard first
"""
var modal: Control
func f():
    await g()
    if is_instance_valid(modal) and modal.is_visible():
        pass
""",
# reassigned after the await
"""
var modal: Control
func f():
    await g()
    modal = Control.new()
    modal.show()
""",
# local assigned from the await itself
"""
func f():
    var node = await g()
    node.show()
""",
# local declared after the await
"""
func f():
    await g()
    var fresh := Control.new()
    fresh.show()
""",
# no await at all
"""
var modal: Control
func f():
    modal.show()
    var r = await g()
""",
# self and autoloads are never stale
"""
func f():
    await g()
    self.show()
    show()
    Global.bar()
""",
# @onready members live and die with self
"""
@onready var label = %Label
@onready var button: Button = $Button
func f():
    await g()
    label.show()
    button.show()
""",
# a member the instance parents itself is freed with it
"""
var _timer: Timer
func _ready():
    _timer = Timer.new()
    add_child(_timer)
func f():
    await g()
    _timer.start()
""",
# a local the instance parents itself, through any parent in its tree
"""
func f():
    var http := HTTPRequest.new()
    add_child(http)
    await http.request_completed
    http.queue_free()
""",
"""
func f():
    var card := Control.new()
    %Container.add_child(card)
    await g()
    card.show()
""",
# an engine singleton lives for the whole process
"""
var plugin = Engine.get_singleton("DclIosPlugin")
func f():
    var local_plugin = Engine.get_singleton("DclIosPlugin")
    await g()
    plugin.foo()
    local_plugin.foo()
""",
# declared types that cannot hold a node
"""
var image: Image
var promise: RefCounted
var items: Array[Node]
var tween: Tween
func f(data: Dictionary, name: String):
    await g()
    image.get_size()
    promise.get_reference_count()
    items.size()
    tween.kill()
    data.get("x")
    name.length()
""",
# untyped names that look like plain data
"""
var config
func f(result, url):
    await g()
    result.get("x")
    url.length()
    config.get("y")
""",
# untyped local constructed from a safe class
"""
func f():
    var rng = RandomNumberGenerator.new()
    await g()
    rng.randi()
""",
# property access is validated by the engine in every build
"""
var modal: Control
func f():
    await g()
    modal.visible = false
    var v = modal.visible
""",
# loop variable used before the await in its body
"""
func f(cards: Array):
    for card in cards:
        card.show()
        await g()
""",
# a lambda's own parameters are fresh
"""
func f():
    x.pressed.connect(func(card: Control): card.hide())
""",
])
def test_unguarded_access_after_await_ok(code):
    assert _names(code) == []


def test_child_the_class_frees_itself_is_not_exempt():
    # A list that rebuilds its rows frees them independently of self.
    code = """
var card: Control
func _rebuild():
    card = Control.new()
    add_child(card)
func _clear():
    card.queue_free()
func f():
    await g()
    card.show()
"""
    assert _names(code) == [(AWAIT_RULE, 10)]


def test_non_node_script_gets_no_add_child_exemption():
    code = """
extends RefCounted
func f(root: Node):
    var preview := Control.new()
    root.add_child(preview)
    await g()
    preview.show()
"""
    assert _names(code) == [(AWAIT_RULE, 7)]


def test_inner_data_class_is_safe():
    code = """
class Response:
    var elements := []
class Row extends Control:
    var x := 1
func f():
    var response := Response.new()
    var row := Row.new()
    await g()
    response.elements.append(1)
    row.show()
"""
    assert _names(code) == [(AWAIT_RULE, 11)]


def test_loop_variable_after_await_in_body():
    code = """
func f(cards: Array):
    for card in cards:
        await g()
        card.show()
"""
    assert _names(code) == [(AWAIT_RULE, 5)]


def test_null_check_is_not_a_guard():
    code = """
var modal: Control
func f():
    await g()
    if modal == null:
        return
    modal.show()
"""
    assert _names(code) == [(NULL_RULE, 5), (AWAIT_RULE, 7)]


def test_project_safe_types_config():
    code = """
var manager: PlaceholderManager
func f():
    await g()
    manager.queue_free_instance()
"""
    assert _names(code) == [(AWAIT_RULE, 5)]
    assert _names(code, **{"lifetime-safe-types": r"^(PlaceholderManager)$"}) == []


@pytest.mark.parametrize('code', [
"""
func f(avatar: Node):
    await g()
    h(avatar)
""",
"""
var modal: Control
func f():
    await g()
    self.h(modal)
""",
])
def test_unguarded_argument_after_await_nok(code):
    outcome = _names(code)
    assert [n for n, _ in outcome] == [ARG_RULE], outcome


@pytest.mark.parametrize('code', [
# untyped arguments are not reported
"""
func f(avatar):
    await g()
    h(avatar)
""",
# guarded
"""
func f(avatar: Node):
    await g()
    if not is_instance_valid(avatar):
        return
    h(avatar)
""",
# scene-owned
"""
@onready var label = %Label
func f():
    await g()
    h(label)
""",
])
def test_unguarded_argument_after_await_ok(code):
    assert _names(code) == []


@pytest.mark.parametrize('code', [
"""
var modal: Control
func f():
    if modal == null:
        pass
""",
"""
var modal: Control
func f():
    if null != modal:
        pass
""",
"""
var modal: Control
func f():
    if not modal:
        pass
""",
"""
var modal: Control
func f():
    if modal:
        pass
""",
"""
var modal: Control
func f():
    if modal and modal.visible:
        pass
""",
# @onready members count even without a type
"""
@onready var label = %Label
func f():
    if label == null:
        pass
""",
"""
func f(card: Control):
    while card:
        pass
""",
])
def test_node_null_comparison_nok(code):
    outcome = _names(code)
    assert [n for n, _ in outcome] == [NULL_RULE], outcome


@pytest.mark.parametrize('code', [
# untyped names are not reported
"""
var modal
func f(data):
    if modal == null or not data:
        pass
""",
# safe declared types
"""
var image: Image
func f(name: String, ok: bool):
    if image == null or not ok or name:
        pass
""",
# is_instance_valid is the right test
"""
var modal: Control
func f():
    if not is_instance_valid(modal):
        pass
""",
# comparisons other than null
"""
var modal: Control
func f(other: Control):
    if modal == other:
        pass
""",
])
def test_node_null_comparison_ok(code):
    assert _names(code) == []


def test_rules_can_be_ignored_inline():
    code = """
var modal: Control
func f():
    await g()
    modal.show()  # gdlint: ignore=unguarded-node-access-after-await
    if modal == null:  # gdlint: ignore=node-null-comparison
        pass
"""
    assert _names(code) == []


def test_rules_can_be_disabled():
    code = """
var modal: Control
func f(node: Node):
    await g()
    modal.show()
    h(node)
    if modal == null:
        pass
"""
    simple_ok_check(code, disable=DISABLE + [AWAIT_RULE, ARG_RULE, NULL_RULE])
    simple_nok_check(code, AWAIT_RULE, line=5, disable=DISABLE + [ARG_RULE, NULL_RULE])
# fmt: on
