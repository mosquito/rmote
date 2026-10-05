"""Compilation boundaries, token structure, and rejection of unintended execution."""

import builtins
import pickle
import tokenize
from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest

from rmote.templates.engine import (
    AttributeExpression,
    CommandToken,
    CommentToken,
    ExpressionToken,
    ForNode,
    IfNode,
    LookupExpression,
    OutputNode,
    RenderContext,
    Template,
    TemplateLexer,
    TemplateParser,
    TemplateProgram,
    TemplateSource,
    TextNode,
    TextToken,
)
from rmote.templates.filters import TEMPLATE_FILTERS, TemplateFilter


def test_typed_tokens_keep_source_positions_and_whitespace():
    source = "start \n{#- comment -#}\n{{ value }}{% if ok %}yes{% endif %}"
    tokens = TemplateLexer(TemplateSource(source)).tokenize()
    assert [type(t) for t in tokens] == [
        TextToken,
        CommentToken,
        ExpressionToken,
        CommandToken,
        TextToken,
        CommandToken,
    ]
    assert tokens[0].value == "start"
    assert tokens[1].position == source.index("{#")
    assert tokens[2].position == source.index("{{")
    assert tokens[2].value == " value "
    assert TemplateLexer(TemplateSource(r"\{{ x }}")).tokenize() == (TextToken("{{ x }}", 0),)


def test_nested_program_is_built_at_construction():
    template = Template("a{{ value }}{% if active %}{% for n in items %}{{ n }}{% endfor %}{% else %}no{% endif %}")
    assert isinstance(template.program, TemplateProgram)
    assert tuple(type(node) for node in template.program.nodes) == (TextNode, OutputNode, IfNode)
    condition = template.program.nodes[2]
    assert isinstance(condition, IfNode)
    branch = condition.branches[0]
    assert isinstance(branch.body[0], ForNode)
    with pytest.raises(FrozenInstanceError):
        cast(Any, template.program).nodes = ()


def test_attribute_and_item_lookups_have_separate_picklable_nodes():
    template = Template("{{ host.name }}{{ host['name'] }}")
    attribute, item = template.program.nodes
    assert isinstance(attribute, OutputNode)
    assert isinstance(item, OutputNode)
    assert isinstance(attribute.expression, AttributeExpression)
    assert attribute.expression.name == "name"
    assert isinstance(item.expression, LookupExpression)
    assert pickle.loads(pickle.dumps(template)).render(host={"name": "web"}) == "webweb"


def test_each_tag_is_tokenized_once_and_parser_reuses_lexemes(monkeypatch):
    source = TemplateSource(
        "{# ignored {{ expression }} #}{% set names = items|sort %}"
        "{% if names: %}{% for name in names: %}{{ name|upper }}{% endfor %}"
        "{% else: %}empty{% endif %}"
    )
    generate_tokens = tokenize.generate_tokens
    calls = 0

    def counted(readline):
        nonlocal calls
        calls += 1
        return generate_tokens(readline)

    monkeypatch.setattr(tokenize, "generate_tokens", counted)
    tokens = TemplateLexer(source).tokenize()
    assert calls == 7

    def forbidden(*args, **kwargs):
        raise AssertionError("The parser must reuse the lexer's code tokens")

    monkeypatch.setattr(tokenize, "generate_tokens", forbidden)
    nodes = TemplateParser(source, tokens, TEMPLATE_FILTERS).body()
    program = TemplateProgram(source, nodes, tuple(TEMPLATE_FILTERS.items()))
    assert program(items=["b", "a"]) == "AB"
    assert program(items=[]) == "empty"


def test_code_token_positions_refer_to_original_multiline_source():
    source = "привет\r\n{% set\r\n answer = {'x': '}}', 'y': 7} : -%}\r\n{{- answer['x'] -}}"
    tokens = TemplateLexer(TemplateSource(source)).tokenize()
    command, expression = tokens[1:]
    assert isinstance(command, CommandToken)
    assert isinstance(expression, ExpressionToken)
    assert command.name == "set"
    assert [token.string for token in command.tokens] == [
        "answer",
        "=",
        "{",
        "'x'",
        ":",
        "'}}'",
        ",",
        "'y'",
        ":",
        "7",
        "}",
    ]
    assert [token.string for token in expression.tokens] == ["answer", "[", "'x'", "]"]
    assert command.tokens[0].position == source.index("answer")
    assert expression.tokens[0].position == source.rindex("answer")
    for token in (*command.tokens, *expression.tokens):
        assert source[token.position : token.position + len(token.string)] == token.string
    assert Template(source).render() == "привет\r\n}}"


def test_render_and_pickle_do_not_parse_compile_or_execute_python(monkeypatch):
    template = Template("{% set n = values|length %}{% if n %}{{ values|join(',') }}{% endif %}")

    def forbidden(*args, **kwargs):
        raise AssertionError("Rendering must only invoke IR")

    with monkeypatch.context() as patch:
        patch.setattr(Template, "compile", forbidden)
        patch.setattr(TemplateLexer, "tokenize", forbidden)
        patch.setattr(builtins, "compile", forbidden)
        patch.setattr(builtins, "eval", forbidden)
        patch.setattr(builtins, "exec", forbidden)
        assert template.render(values=[1, 2]) == "1,2"
        assert template.render(values=[]) == ""
        assert pickle.loads(pickle.dumps(template)).render(values=[3]) == "3"
        assert pickle.loads(pickle.dumps(template.program))(values=[4]) == "4"


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').getcwd()",
        "open('/tmp/forbidden', 'w')",
        "eval('1')",
        "exec('x=1')",
        "globals()",
        "locals()",
        "callable_from_context()",
        "obj.method()",
        "obj.__class__",
        "obj._private",
        "obj.__dict__",
        "().__class__.__mro__",
        "(lambda: 1)()",
        "[x for x in values]",
        "(x for x in values)",
        "f'{callback()}'",
        "f'{value}'",
        "(x := 1)",
        "value|unknown",
        "value|attr('__globals__')",
        "'x'.format(value)",
        "'x'|replace(*args)",
    ],
)
def test_execution_syntax_is_rejected_even_in_unreachable_branch(expression):
    with pytest.raises(SyntaxError):
        Template("{% if False %}{{ " + expression + " }}{% endif %}")


@pytest.mark.parametrize(
    "command",
    [
        "import os",
        "from os import system",
        "while True",
        "def fn()",
        "class Value",
        "try",
        "with resource",
        "raise error",
        "del value",
        "set obj.attr = 1",
        "set values[0] = 1",
        "set x = 1; action()",
        "set _private = 1",
    ],
)
def test_only_allowlisted_statements_compile(command):
    with pytest.raises(SyntaxError):
        Template("{% " + command + " %}")


def test_context_objects_are_rejected_without_invoking_magic_or_descriptors():
    touched = []

    class ObjectTrap:
        @property
        def value(self):
            touched.append("property")
            return 1

        @property  # type: ignore[misc]  # Deliberately override object.__class__ with a hostile descriptor.
        def __class__(self):
            touched.append("class")
            return dict

        def __str__(self):
            touched.append("str")
            return "bad"

        def __bool__(self):
            touched.append("bool")
            return True

        def __iter__(self):
            touched.append("iter")
            return iter(())

    class ListTrap(list[Any]):
        def __iter__(self):
            touched.append("list iter")
            return super().__iter__()

    class DictTrap(dict[str, Any]):
        def items(self):
            touched.append("dict items")
            return super().items()

    class IntTrap(int):
        def __add__(self, other):
            touched.append("add")
            return 1

    class StringTrap(str):
        def __str__(self):
            touched.append("string str")
            return super().__str__()

    template = Template("{{ value }}")
    for value in (ObjectTrap(), ListTrap(), DictTrap(), IntTrap(1), StringTrap("x"), lambda: touched.append("call")):
        for wrapped in (value, [value], {"nested": value}):
            with pytest.raises(TypeError, match="plain data"):
                template.render(value=wrapped)
    assert touched == []


def test_context_cycles_fail_without_recursive_execution():
    value: list[Any] = []
    value.append(value)
    with pytest.raises(ValueError, match="Cyclic"):
        Template("{{ value }}").render(value=value)


def test_context_validation_rejects_cycles_through_dictionaries_and_tuples():
    mapping: dict[str, Any] = {}
    mapping["self"] = mapping
    items: list[Any] = []
    items.append((items,))
    shared: list[Any] = [1, "ok"]
    for value in (mapping, items, {"valid": shared, "nested": [mapping]}):
        seen: set[int] = set()
        with pytest.raises(ValueError, match="Cyclic"):
            RenderContext.validate(value, seen)
        assert not seen
    assert RenderContext.normalise([shared, shared]) == [shared, shared]


def test_type_checks_do_not_invoke_application_metaclasses():
    touched = []

    class MetaTrap(type):
        def __eq__(cls, other):
            touched.append("eq")
            return True

        def __hash__(cls):
            touched.append("hash")
            return 0

    class ObjectTrap(metaclass=MetaTrap):
        pass

    for value in (ObjectTrap(), [ObjectTrap()], {"item": ObjectTrap()}):
        with pytest.raises(TypeError, match="plain data"):
            Template("{{ value }}").render(value=value)
    assert touched == []


def test_unknown_end_prefix_is_rejected_at_its_own_location():
    with pytest.raises(SyntaxError, match="Unsupported template command: endless") as caught:
        Template("{% if True %}\n{% endless %}\n{% endif %}")
    assert caught.value.filename == "<template>"
    assert caught.value.lineno == 2
    assert caught.value.offset == 1


@pytest.mark.parametrize("path", ["__class__", "__dict__", "_private", "network.__class__"])
def test_collection_filters_cannot_traverse_private_attributes(path):
    class ObjectFilter(TemplateFilter):
        def __call__(self, value):
            from types import SimpleNamespace

            return SimpleNamespace(network=SimpleNamespace())

    template = Template("{{ [0|object]|join(attribute=path) }}", filters={**TEMPLATE_FILTERS, "object": ObjectFilter})
    with pytest.raises(AttributeError):
        template.render(path=path)


def test_plain_dictionary_keys_and_ip_actions_remain_available():
    assert (
        Template("{{ data.value }} {{ data['__class__'] }}").render(data={"value": 3, "__class__": "data"}) == "3 data"
    )
    assert Template("{{ address|ipaddress('network_address') }}").render(address="192.0.2.7/24") == "192.0.2.0"


def test_trusted_filter_is_responsible_for_context_mutations():
    class AppendFilter(TemplateFilter):
        def __call__(self, value):
            value.append("extra")
            return iter(value)

    values = ["first"]
    template = Template("{{ values|append|join(',') }}", filters={**TEMPLATE_FILTERS, "append": AppendFilter})
    assert template.render(values=values) == "first,extra"
    assert template.render(values=values) == "first,extra,extra"
    assert values == ["first", "extra", "extra"]


def test_context_validation_retains_containers_and_aliases():
    shared = [1, {"name": "web"}]
    values = {"first": shared, "second": shared, "other": ({1, 2}, frozenset({3}), range(4))}
    assert RenderContext.normalise(values) is values
    context = RenderContext(values, ())
    assert context.values["first"] is shared
    assert context.values["second"] is shared
    assert context.values["other"] is values["other"]
    context.values["first"] = "rebound"
    assert values["first"] is shared


def test_trusted_filter_result_properties_are_available():
    touched: list[str] = []

    class Result:
        @property
        def value(self):
            touched.append("property")
            return 42

        def method(self):
            touched.append("method")
            return 0

    class ObjectFilter(TemplateFilter):
        def __call__(self, value):
            return Result()

    assert Template("{{ (0|object).value }}", filters={"object": ObjectFilter}).render() == "42"
    with pytest.raises(SyntaxError, match="calls are not allowed"):
        Template("{{ (0|object).method() }}", filters={"object": ObjectFilter})
    with pytest.raises(SyntaxError, match="private"):
        Template("{{ (0|object).__class__ }}", filters={"object": ObjectFilter})
    assert touched == ["property"]


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("values[1:]|join(',')", "2,3"),
        ("values[::-1]|join(',')", "3,2,1"),
        ("1 < 2 < 3", "True"),
        ("1 < 0 < missing", "False"),
        ("(2 < 1) < 1", "True"),
        ("1 < (2 < 3)", "False"),
        ("True or missing", "True"),
        ("False and missing", "False"),
        ("'yes' if True else missing", "yes"),
        ("None is none", "True"),
        ("2 not in [1,3]", "True"),
    ],
)
def test_compiled_expression_nodes_preserve_short_circuiting(expression, expected):
    assert Template("{{ " + expression + " }}").render(values=[1, 2, 3]) == expected
