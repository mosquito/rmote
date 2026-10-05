"""Filter evaluation, precedence, strict failures, and autonomous remote use."""

import ipaddress
import itertools
import json
import pickle

import pytest

from rmote.templates import Template
from rmote.templates.filters import TEMPLATE_FILTERS, TemplateFilter
from tests.support.tool_cases.filter_tool import HostFilter, PrefixFilter


@pytest.mark.parametrize(
    "expression, expected",
    [
        ("'HELLO'|lower", "hello"),
        ("42|lower", "42"),
        ("'hello'|upper", "HELLO"),
        ("'hELLO'|capitalize", "Hello"),
        ("' a '|trim", "a"),
        ("'xaxb'|trim('xb')", "a"),
        ("'aba'|replace('a', 'x')", "xbx"),
        ("'aba'|replace('a', 'x', count=1)", "xba"),
        ("'aba'|replace('a', 'x', count=None)", "xbx"),
        ("123|replace(2, 4)", "143"),
        ("['a', 2]|join(', ')", "a, 2"),
        ("[]|join", ""),
        ("[{'name': 'a'}, {'name': 'b'}]|join('/', attribute='name')", "a/b"),
        ("'abc'|length", "3"),
        ("[1,2]|count", "2"),
        ("'abc'|list", "['a', 'b', 'c']"),
        ("['b', 'A', 'a']|sort|join", "Aab"),
        ("['b', 'A', 'a']|sort(reverse=True)|join", "bAa"),
        ("['b', 'A', 'a']|sort(case_sensitive=True)|join", "Aab"),
        ("['b', 'A', 'B', 'a']|unique|join", "bA"),
        ("['b', 'A', 'B', 'a']|unique(True)|join", "bABa"),
        ("['b', 'A', 'B', 'a']|unique|sort|join(',')", "A,b"),
        ("[1, 2]|first", "1"),
        ("[1, 2]|last", "2"),
        ("'abc'|reverse", "cba"),
        ("[0,1,2]|reverse|list", "[2, 1, 0]"),
        ("[0,1,2]|first", "0"),
        ("'42'|int", "42"),
        ("'42.9'|int", "42"),
        ("'ff'|int(base=16)", "255"),
        ("'bad'|int", "0"),
        ("None|int(7)", "7"),
        ("'nan'|int(default=9)", "9"),
        ("'1.5'|float", "1.5"),
        ("'bad'|float", "0.0"),
        ("None|float(7.5)", "7.5"),
        ("42|string|length", "2"),
        ("{'b': 1, 'a': 2}|tojson", '{"a": 2, "b": 1}'),
        ("[1,2]|tojson(indent=2)", "[\n  1,\n  2\n]"),
        ("missing|default('fallback')", "fallback"),
        ("missing|d('fallback')|upper", "FALLBACK"),
        ("missing|default", ""),
        ("None|default('fallback')", "None"),
        ("None|default('fallback', True)", "fallback"),
        ("0|default(42)", "0"),
        ("0|default(42, boolean=True)", "42"),
        ("''|default('fallback')", ""),
        ("''|default('fallback', True)", "fallback"),
        ("False|default('fallback')", "False"),
        ("{}['missing']|default('fallback')", "fallback"),
        ("[].missing|default('fallback')", "fallback"),
        ("[][0]|default('fallback')", "fallback"),
        ("missing.a['b']|default('fallback')", "fallback"),
        ("[]|first|default('empty')", "empty"),
        ("[]|last|default('empty')", "empty"),
    ],
)
def test_builtin_filters(expression, expected):
    assert Template("{{ " + expression + " }}").render() == expected


@pytest.mark.parametrize(
    "expression, expected",
    [
        ("1 + '2'|int", "3"),
        ("2 * '3'|int", "6"),
        ("10 / '2'|int", "5.0"),
        ("2 ** '3'|int", "8"),
        ("3 % '2'|int", "1"),
        ("True - '2'|int", "-1"),
        ("1 + -2|string", TypeError),
        ("(-2)|string", "-2"),
        ("-2|string", "-2"),
        ("(1 + 2)|string", "3"),
        ("'x' + 'a'|upper", "xA"),
        ("'12'|int > 2", "True"),
        ("not 'x'|length", "False"),
        ("'abc'|upper if flag else missing", "ABC"),
        ("missing if not flag else 'abc'|upper", "ABC"),
        ("False and missing|upper", "False"),
        ("True or missing|upper", "True"),
        ("'a'|upper|string", "A"),
        ("['a'|upper, 'b'|upper]|join('-')", "A-B"),
        ("{'a': 'b'|upper}['a']|lower", "b"),
        ("['1'|int + 1, '2'|int + 1]|join(',')", "2,3"),
        ("'ab'|replace('a', 'x'|upper)|upper", "XB"),
        ("'ab'|replace('a', 'x', count=1)", "xb"),
        ("' a '|trim()|upper()", "A"),
        ("('a' 'b')|upper", "AB"),
        ("'a' 'b'|upper", "AB"),
        ("'a|upper'|upper", "A|UPPER"),
        ("'{{ x }}'|upper", "{{ X }}"),
        ("((10 + 1)|string) + ('z'|upper)", "11Z"),
        ("('Hi'|lower)|upper", "HI"),
        ("'Hi'|lower|upper", "HI"),
        ("['a'][0]|upper", "A"),
        ("(1 +\n  '2'|int)|string", "3"),
        ("'a' # comment | nonsense\n |upper", "A"),
        ("['a'|upper,'b'|upper]|join", "AB"),
    ],
)
def test_filter_precedence_and_python_context(expression, expected):
    template = Template("{{ " + expression + "\n }}")
    if isinstance(expected, type) and issubclass(expected, Exception):
        with pytest.raises(expected):
            template.render(flag=True)
    else:
        assert template.render(flag=True) == expected


def test_filters_in_commands_and_scopes():
    template = Template(
        "{% set names = hosts|unique|sort %}"
        "{% if names|length > 0 %}"
        "{% for name in names|reverse %}{{ name|upper }};{% endfor %}"
        "{% elif missing|default(False) %}bad{% else %}empty{% endif %}"
    )
    assert template.render(hosts=["b", "A", "B"]) == "B;A;"
    assert template.render(hosts=[]) == "empty"
    assert Template("{{ [{}['a']|default('none'), {'a': 2}['a']|default('none')] }}").render() == "['none', 2]"


def test_collection_attribute_paths():
    hosts = [
        {"info": {"name": "b"}, "priority": 2, "addresses": ["B"]},
        {"info": {"name": "A"}, "priority": 1, "addresses": ["a"]},
        {"info": {"name": "a"}, "priority": 0, "addresses": ["a"]},
    ]
    assert (
        Template("{{ hosts|sort(attribute='info.name,priority')|join(',', attribute='priority') }}").render(
            hosts=hosts
        )
        == "0,1,2"
    )
    assert (
        Template("{{ hosts|unique(attribute='info.name')|join(',', attribute='info.name') }}").render(hosts=hosts)
        == "b,A"
    )
    assert Template("{{ hosts|join(',', attribute='addresses.0') }}").render(hosts=hosts) == "B,a,a"
    assert Template("{{ hosts|join(attribute=0) }}").render(hosts=[["a"], ["b"]]) == "ab"
    assert (
        Template("{{ hosts|sort(attribute='name')|join(',', attribute='name') }}").render(
            hosts=[{"name": "b"}, {"name": "a"}]
        )
        == "a,b"
    )


@pytest.mark.parametrize("address", ["192.0.2.7/24", "2001:db8::123/64", "192.0.2.1", "::1"])
def test_ipaddress_preserves_interface_prefix(address):
    expected = ipaddress.ip_interface(address)
    assert Template("{{ address|ipaddress }}").render(address=address) == str(expected)
    assert Template("{{ address|ipaddress('network') }}").render(address=address) == str(expected.network)
    assert (
        Template(
            "{% set iface = address|ipaddress %}{{ iface|ipaddress('address') }} {{ iface|ipaddress('prefix') }}"
        ).render(address=address)
        == f"{expected.ip} {expected.network.prefixlen}"
    )
    assert TEMPLATE_FILTERS["ipaddress"]()(address) == expected
    assert Template("{{ (address|ipaddress).network.network_address }}").render(address=address) == str(
        expected.network.network_address
    )


def test_ipaddress_invalid_input():
    with pytest.raises(ValueError):
        Template("{{ 'not-an-ip'|ipaddress }}").render()


@pytest.mark.parametrize(
    "address, expected",
    [
        (
            "192.0.2.7/24",
            {
                "interface": "192.0.2.7/24",
                "address": "192.0.2.7",
                "network": "192.0.2.0/24",
                "network_address": "192.0.2.0",
                "netmask": "255.255.255.0",
                "hostmask": "0.0.0.255",
                "broadcast": "192.0.2.255",
                "prefix": 24,
                "version": 4,
            },
        ),
        (
            "2001:db8::123/64",
            {
                "interface": "2001:db8::123/64",
                "address": "2001:db8::123",
                "network": "2001:db8::/64",
                "network_address": "2001:db8::",
                "netmask": "ffff:ffff:ffff:ffff::",
                "hostmask": "::ffff:ffff:ffff:ffff",
                "broadcast": "2001:db8::ffff:ffff:ffff:ffff",
                "prefix": 64,
                "version": 6,
            },
        ),
    ],
)
def test_ipaddress_actions_return_plain_data(address, expected):
    template = Template("{{ address|ipaddress(action=action)|tojson }}")
    for action, value in expected.items():
        assert json.loads(template.render(address=address, action=action)) == value
        result = TEMPLATE_FILTERS["ipaddress"]()(address, action)
        assert type(result) is type(value)
        assert result == value


@pytest.mark.parametrize("action", ["unknown", "__class__", "network.supernet"])
def test_ipaddress_rejects_unknown_actions(action):
    with pytest.raises(ValueError, match="Unknown ipaddress action"):
        Template("{{ address|ipaddress(action) }}").render(address="192.0.2.7/24", action=action)


def test_tojson_escapes_html_characters():
    result = Template("{{ value|tojson }}").render(value={"x": "<>&'"})
    assert result == r'{"x": "\u003c\u003e\u0026\u0027"}'
    assert json.loads(result) == {"x": "<>&'"}


def test_filter_errors_are_not_hidden_by_default():
    class FailureFilter(TemplateFilter):
        def __call__(self, value):
            raise KeyError("inside user filter")

    with pytest.raises(KeyError, match="inside user filter"):
        Template("{{ 1|fail|default('hidden') }}", filters={**TEMPLATE_FILTERS, "fail": FailureFilter}).render()
    with pytest.raises(ZeroDivisionError):
        Template("{{ (1/0)|default('hidden') }}").render()
    with pytest.raises(NameError):
        Template("{{ missing|upper }}").render()
    with pytest.raises(ValueError):
        Template("{{ []|first }}").render()
    with pytest.raises(TypeError):
        Template("{{ 'abc'|lower(1) }}").render()


def test_each_value_and_argument_is_evaluated_once():
    calls = []

    class CaptureFilter(TemplateFilter):
        def __call__(self, value):
            calls.append(value)
            return value

    template = Template(
        "{{ 'abc'|capture|replace('a', 'x'|capture)|upper }}", filters={**TEMPLATE_FILTERS, "capture": CaptureFilter}
    )
    assert template.render() == "XBC"
    assert calls == ["abc", "x"]


@pytest.mark.parametrize(
    "source",
    [
        "{{ 1|unknown }}",
        "{{ 1| }}",
        "{{ |lower }}",
        "{{ 1|42 }}",
        "{{ value|replace( }}",
        "{{ value|replace(old=) }}",
        "{% set x = 1|unknown %}",
        "{% if x|unknown %}yes{% endif %}",
        "{% for x in items|unknown %}{{ x }}{% endfor %}",
    ],
)
def test_filter_syntax_errors_reference_template(source):
    source = "first\n  " + source
    with pytest.raises(SyntaxError) as caught:
        Template(source)
    assert caught.value.filename == "<template>"
    assert caught.value.lineno == 2
    assert caught.value.offset == 3


def test_pickle_cache_and_context_isolation():
    source = '{{ items|unique|sort|join(",") }}'
    template = pickle.loads(pickle.dumps(Template(source)))
    assert Template.compile(source) is Template.compile(source)
    assert template.render(items=["b", "a", "B"]) == "a,b"
    assert template.render(items=[]) == ""
    assert template.render(items=["c"], _template_filters_={}) == "c"


def test_custom_filter_classes_and_instances_are_picklable():
    class LocalFilter(TemplateFilter):
        def __call__(self, value, count=2):
            return str(value) * count

    for cls in (LocalFilter, HostFilter, *TEMPLATE_FILTERS.values()):
        restored = pickle.loads(pickle.dumps(cls))
        assert issubclass(restored, TemplateFilter)
        assert pickle.loads(pickle.dumps(restored)) is restored
    assert pickle.loads(pickle.dumps(LocalFilter))()("x", 3) == "xxx"
    assert pickle.loads(pickle.dumps(LocalFilter()))("x", 3) == "xxx"
    assert pickle.loads(pickle.dumps(HostFilter()))("Web Server", "!") == "host:web-server!"


def test_custom_mapping_pickle_and_cache_isolation():
    source = "{{ name | custom('!') | upper }}"
    mappings: dict[str, type[TemplateFilter]] = {"custom": HostFilter, "upper": TEMPLATE_FILTERS["upper"]}
    first = Template(source, filters=mappings)
    mappings["custom"] = PrefixFilter
    second = Template(source, filters=mappings)
    for _ in range(2):
        assert first.render(name="Web Server") == "HOST:WEB-SERVER!"
        assert second.render(name="Web Server") == "HOST:WEB SERVER!"
        first = pickle.loads(pickle.dumps(first))
        second = pickle.loads(pickle.dumps(second))


def test_filter_overrides_and_undefined_aliases():
    from rmote.templates.filters import DefaultFilter

    assert Template("{{ 'web'|upper }}", filters={"upper": PrefixFilter}).render() == "host:web"
    assert Template("{{ missing|fallback('ok') }}", filters={"fallback": DefaultFilter}).render() == "ok"
    with pytest.raises(NameError):
        Template("{{ missing|default }}", filters={"default": PrefixFilter}).render()


def test_filter_instances_are_fresh_on_every_render():
    class CounterFilter(TemplateFilter):
        def __init__(self):
            self.count = 0

        def __call__(self, value):
            self.count += 1
            return self.count

    template = Template("{{ 0|count }}{{ 0|count }}", filters={"count": CounterFilter})
    assert template.render() == "12"
    assert template.render() == "12"


@pytest.mark.parametrize(
    "filters, error",
    [
        ({"bad-name": HostFilter}, ValueError),
        ({"if": HostFilter}, ValueError),
        ({"custom": str}, TypeError),
        ({"custom": HostFilter()}, TypeError),
    ],
)
def test_invalid_filter_mapping(filters, error):
    with pytest.raises(error):
        Template("plain text", filters=filters)


def test_dynamic_filter_without_source_fails_clearly():
    dynamic = type("DynamicFilter", (TemplateFilter,), {"__call__": lambda self, value: value})
    with pytest.raises(TypeError, match="class source is unavailable"):
        pickle.dumps(Template("{{ 1|dynamic }}", filters={"dynamic": dynamic}))


def test_explicit_mapping_replaces_defaults_even_when_empty():
    assert Template("{{ 'abc'|upper }}").render() == "ABC"
    for filters in ({}, {"custom": HostFilter}):
        with pytest.raises(SyntaxError, match="Unknown template filter: upper"):
            Template("{{ 'abc'|upper }}", filters=filters)
    template = pickle.loads(pickle.dumps(Template("{{ 1 + 2 }}", filters={})))
    assert template.program.filters == ()
    assert template.render() == "3"


def test_filter_token_offsets_preserve_literal_line_separators():
    assert Template("{{ ('a\u2028b' + '\\n' + 'x'|upper)|upper }}").render() == "A\u2028B\nX"


def test_filter_mapping_is_copied_and_transferred():
    filters = dict(TEMPLATE_FILTERS)
    old = Template("{{ name|upper }}", filters=filters)
    filters["upper"] = PrefixFilter
    new = Template("{{ name|upper }}", filters=filters)
    assert Template("{{ name|upper }}").render(name="web") == "WEB"
    assert old.render(name="web") == "WEB"
    assert new.render(name="web") == "host:web"
    assert pickle.loads(pickle.dumps(old)).render(name="web") == "WEB"
    assert pickle.loads(pickle.dumps(new)).render(name="web") == "host:web"


@pytest.mark.parametrize(
    "expression, expected",
    [
        ("d|reverse|list|join(',')", "b,a"),
        ("d|reverse|length", "2"),
        ("d|reverse|last", "a"),
    ],
)
def test_reverse_dictionary_results_are_materialized(expression, expected):
    assert Template("{{ " + expression + " }}").render(d={"a": 1, "b": 2}) == expected


@pytest.mark.parametrize(
    "iterator, expected",
    [
        (lambda values: map(str, values), '["1", "2"]'),
        (lambda values: filter(None, values), "[1, 2]"),
        (lambda values: zip(values, values, strict=True), "[[1, 1], [2, 2]]"),
        (lambda values: enumerate(values), "[[0, 1], [1, 2]]"),
        (lambda values: itertools.chain(values), "[1, 2]"),
        (lambda values: itertools.islice(values, 2), "[1, 2]"),
        (lambda values: iter(reversed(dict.fromkeys(values))), "[2, 1]"),
    ],
)
def test_standard_iterator_results_can_be_reused(iterator, expected):
    class IterateFilter(TemplateFilter):
        def __call__(self, value):
            return iterator(value)

    template = Template(
        "{% set items = values|iterate %}{{ items|length }}:{{ items|tojson }}:{{ items|tojson }}",
        filters={**TEMPLATE_FILTERS, "iterate": IterateFilter},
    )
    assert template.render(values=[1, 2]) == f"2:{expected}:{expected}"


def test_custom_iterator_is_consumed_only_as_a_trusted_filter_result():
    touched: list[str] = []

    class CustomIterator:
        def __init__(self) -> None:
            self.values = iter([1, 2])

        def __iter__(self):
            touched.append("iter")
            return self

        def __next__(self):
            touched.append("next")
            return next(self.values)

    class IterateFilter(TemplateFilter):
        def __call__(self, value):
            return CustomIterator()

    with pytest.raises(TypeError, match="plain data"):
        Template("{{ values|list }}").render(values=CustomIterator())
    assert touched == []
    template = Template("{{ values|iterate|join(',') }}", filters={**TEMPLATE_FILTERS, "iterate": IterateFilter})
    assert template.render(values=None) == "1,2"
    assert touched == ["iter", "next", "next", "next"]


def test_iterator_items_from_trusted_filters_expose_public_attributes():
    class ObjectFilter(TemplateFilter):
        def __call__(self, value):
            from types import SimpleNamespace

            return iter([SimpleNamespace(name="web")])

    template = Template(
        "{{ 0|objects|join(attribute='name') }}", filters={**TEMPLATE_FILTERS, "objects": ObjectFilter}
    )
    assert template.render() == "web"


def test_only_the_filters_a_template_names_are_built():
    """A render builds the filters of its own template and no others."""
    built = []

    class Counted(TemplateFilter):
        def __init__(self):
            built.append(type(self).__name__)

        def __call__(self, value):
            return value

    class Used(Counted):
        pass

    class Unused(Counted):
        pass

    template = Template("{{ value|used }}", filters={"used": Used, "unused": Unused})
    assert template.render(value="ok") == "ok"
    assert built == ["Used"]
    # Every render still starts with fresh instances.
    assert template.render(value="ok") == "ok"
    assert built == ["Used", "Used"]
    assert [name for name, _ in template.program.active] == ["used"]
    assert {name for name, _ in template.program.filters} == {"used", "unused"}


def test_a_template_without_filters_builds_none():
    built = []

    class Counted(TemplateFilter):
        def __init__(self):
            built.append("built")

        def __call__(self, value):
            return value

    template = Template("{{ value }}", filters={"any": Counted})
    assert template.render(value="ok") == "ok"
    assert built == []
    assert template.program.active == ()


def test_a_filter_named_in_every_position_is_built():
    """Nested expressions, commands and loop headers all name their filters."""
    sources = (
        "{{ value|upper }}",
        "{% if value|upper == 'A' %}yes{% endif %}",
        "{% set other = value|upper %}{{ other }}",
        "{% for item in values|sort %}{{ item }}{% endfor %}",
        "{{ (value|upper) if value else '' }}",
        "{{ [value|upper] }}",
        "{{ {'key': value|upper} }}",
        "{{ values|default([])|length }}",
    )
    for source in sources:
        program = Template(source).program
        assert program.active, source


@pytest.mark.parametrize(
    ("mapping", "error", "message"),
    [
        ({"_private": TEMPLATE_FILTERS["upper"]}, ValueError, "Invalid template filter name"),
        ({"for": TEMPLATE_FILTERS["upper"]}, ValueError, "Invalid template filter name"),
        ({"two words": TEMPLATE_FILTERS["upper"]}, ValueError, "Invalid template filter name"),
        ({"upper": str}, TypeError, "must be a TemplateFilter class"),
        ({"upper": lambda value: value}, TypeError, "must be a TemplateFilter class"),
    ],
)
def test_a_refused_filter_mapping_is_refused_every_time(mapping, error, message):
    """The check is cached by its pairs, and a refusal never enters the cache."""
    for _ in range(3):
        with pytest.raises(error, match=message):
            Template("{{ value }}", filters=mapping)


def test_the_checked_pairs_of_a_mapping_are_reused():
    from rmote.templates.engine import DEFAULT_FILTERS, checked_filters

    pairs = tuple(sorted({"upper": TEMPLATE_FILTERS["upper"]}.items()))
    assert checked_filters(pairs) is pairs
    assert checked_filters(pairs) is pairs
    # The default set is checked once, when the module is imported.
    assert DEFAULT_FILTERS == tuple(sorted(TEMPLATE_FILTERS.items()))
    assert Template("{{ value|upper }}").program.filters == DEFAULT_FILTERS
    # A copy of the default mapping gives the same program as the default.
    assert (
        Template("{{ value|upper }}", filters=dict(TEMPLATE_FILTERS)).program is Template("{{ value|upper }}").program
    )
