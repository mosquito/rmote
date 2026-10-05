"""Jinja-like syntax, Python semantics, source diagnostics, and remote rendering."""

import pickle

import pytest

from rmote.immutable import freeze
from rmote.templates.engine import RenderContext, Template, render_template

pytestmark = pytest.mark.timeout(30)


@pytest.mark.parametrize(
    "source",
    [
        "",
        "Hello, world!",
        "a\nb",
        "a\n",
        "\n\n",
        "a\r\nb\r\n",
        "a\rb",
        " \t text \t ",
        "$100",
        "{key}",
        "${missing}",
        "% if missing:\ntext\n% endif",
        "%% literal",
        "## literal",
        "end: }",
        "}} %} #}",
    ],
)
def test_literal_text_is_preserved(source):
    assert render_template(source) == source


@pytest.mark.parametrize(
    "expression, expected",
    [
        ("name", "Alice"),
        ("name|upper", "ALICE"),
        ("items[0]", "1"),
        ("d['k']", "v"),
        ("6 * 7", "42"),
        ("'yes' if items else 'no'", "yes"),
        ("{1, 2, 3}|length", "3"),
        ("{'a': {'b': 1}}['a']['b']", "1"),
        ("({3, 1, 2}|sort)[0]", "1"),
        ("items|length", "3"),
        ("[0,1,4,9]|length", "4"),
        ("[items[0]*2, items[1]*2, items[2]*2]", "[2, 4, 6]"),
        ("{'x':1,'y':2}['x']", "1"),
        ("name + '!'", "Alice!"),
        ("'%.2f' % 3.14159", "3.14"),
        ("(42|string) + '}'", "42}"),
        ("'%}'", "%}"),
        ("'}}'", "}}"),
        ("'{# ignored? #}'", "{# ignored? #}"),
        ("'{% if missing %}'", "{% if missing %}"),
        ("'{{ missing }}'", "{{ missing }}"),
        ("10 + 1", "11"),
        ("None", "None"),
        ("True", "True"),
        ("\n  1 +\n  2\n", "3"),
        ("'''one\ntwo'''", "one\ntwo"),
    ],
)
def test_restricted_expressions(expression, expected):
    source = "before {{ " + expression + " }} after {{ 2 }}"
    assert render_template(source, name="Alice", items=[1, 2, 3], d={"k": "v"}) == f"before {expected} after 2"


def test_missing_values_are_errors():
    with pytest.raises(NameError):
        render_template("{{ missing }}")
    assert render_template("{{ data.key }}", data={"key": 1}) == "1"


@pytest.mark.parametrize("ssl, expected", [(True, "listen 443 ssl;"), (False, "listen 443;")])
def test_conditional_suffix(ssl, expected):
    assert render_template("listen {{ port }}{% if ssl %} ssl{% endif %};", port=443, ssl=ssl) == expected


@pytest.mark.parametrize("value, expected", [(1, "positive"), (0, "zero"), (-1, "negative")])
def test_if_elif_else(value, expected):
    source = "{% if value > 0: %}positive{% elif value == 0 %}zero{% else: %}negative{% endif %}"
    assert render_template(source, value=value) == expected


@pytest.mark.parametrize("value, expected", [(1, "one"), (2, "two"), (3, "")])
def test_elif_without_else(value, expected):
    assert render_template("{% if value == 1 %}one{% elif value == 2 %}two{% endif %}", value=value) == expected


@pytest.mark.parametrize(
    "outer, inner, expected",
    [(True, True, "both"), (True, False, "outer"), (False, True, "none"), (False, False, "none")],
)
def test_nested_conditions(outer, inner, expected):
    source = "{% if outer %}{% if inner %}both{% else %}outer{% endif %}{% else %}none{% endif %}"
    assert render_template(source, outer=outer, inner=inner) == expected


@pytest.mark.parametrize("items, expected", [([], "hosts=;"), (["a"], "hosts=[a];"), (["a", "b"], "hosts=[a][b];")])
@pytest.mark.parametrize("end", ["endfor", "end"])
def test_for_loop(items, expected, end):
    source = "hosts={% for host in items %}[{{ host }}]{% " + end + " %};"
    assert render_template(source, items=items) == expected


@pytest.mark.parametrize(
    "source, expected",
    [
        ("{% for n in [0,1,2] %}{{ n }}{% endfor %}", "012"),
        ("{% for i, v in [(0, 'a'), (1, 'b')] %}{{ i }}:{{ v }};{% endfor %}", "0:a;1:b;"),
        ("{% for k, v in [('a',1), ('b',2)] %}{{ k }}={{ v }};{% endfor %}", "a=1;b=2;"),
        (
            "{% for row in [[1, -1], [], [2, 3]] %}[{% for n in row %}{% if n > 0 %}{{ n }}{% else %}x{% endif %}{% endfor %}]{% endfor %}",
            "[1x][][23]",
        ),
        (
            "{% for a in [1,2] %}{% for b in ['x','y'] %}{% for c in ['+','-'] %}{{ a }}{{ b }}{{ c }};{% endfor %}{% endfor %}{% endfor %}",
            "1x+;1x-;1y+;1y-;2x+;2x-;2y+;2y-;",
        ),
        ("{% if True %}{% for x in [1,2] %}{{ x }}{% endfor %}{% endif %}", "12"),
        ("{% if False %}{% for x in [1,2] %}{{ x }}{% endfor %}{% endif %}", ""),
        (
            "{% for n in [0,1,2,3,4] %}{% if n == 1 %}{% continue %}{% endif %}{{ n }}{% if n == 2 %}{% break %}{% endif %}{% endfor %}",
            "02",
        ),
        ("{% for n in [] %}{{ n }}{% else %}empty{% endfor %}", "empty"),
        ("{% for n in [1] %}{{ n }}{% else %}!{% endfor %}", "1!"),
        ("{% for n in [1] %}{% break %}{% else %}empty{% endfor %}", ""),
        (
            "{% set total = 0 %}{% for row in [[1,2], [3,4]] %}{% for n in row %}{% set total = total + n %}{% endfor %}{% endfor %}{{ total }}",
            "10",
        ),
        ("{% set a, b = 1, 2 %}{{ a + b }}", "3"),
        ("{% if True %}{% endif %}", ""),
        ("{% if False %}{% else %}yes{% endif %}", "yes"),
        ("{% for n in [1, 2] %}{% endfor %}", ""),
        ("{% if '%}' == '%}' %}yes{% endif %}", "yes"),
        ('{% if "%}" == "%}" %}yes{% endif %}', "yes"),
        ("{% if '''%}''' %}yes{% endif %}", "yes"),
        ("{% if '\\'%}' %}yes{% endif %}", "yes"),
        ("{% for n in {'%}': '{{', '{%': '}'} %}{{ n }}{% endfor %}", "%}{%"),
        ("{% if (5 % 2) == {'k': 1}['k'] %}yes{% endif %}", "yes"),
        ("{% for n in (\n  1,\n  2,\n) %}{{ n }}{% endfor %}", "12"),
        ("{% if (\n  True\n) %}yes{% endif %}", "yes"),
        ("{% set text = '''one\ntwo''' %}{{ text }}", "one\ntwo"),
        ("{% if True %}{{ '''one\ntwo''' }}{% endif %}", "one\ntwo"),
    ],
)
def test_control_flow(source, expected):
    assert render_template(source) == expected


@pytest.mark.parametrize(
    "source, expected",
    [
        ("a{# ignored #}b", "ab"),
        ("a{# ignored\n{{bad}} {%bad%} #}b", "ab"),
        ("{# comment #}\ntext\n", "\ntext\n"),
        ("{##}", ""),
        (r"\{{ missing }}", "{{ missing }}"),
        (r"\{% if missing %}", "{% if missing %}"),
        (r"\{# literal #}", "{# literal #}"),
        (r"\{{x}} = {{x}}", "{{x}} = 42"),
        ("{{ '{{' }}{{ '{%' }}{{ '{#' }}", "{{{%{#"),
        ("  {% if True %} x {% endif %}  ", "   x   "),
        ("a\n{% if False %}hidden{% endif %}\nb", "a\n\nb"),
        ("a{% if False %}hidden\n{% endif %}b", "ab"),
        ("a{% for n in [1, 2] %}{{ n }}\nb{% endfor %}c", "a1\nb2\nbc"),
        ("{% for n in [1, 2] %}\n{{ n }}\n{% endfor %}", "\n1\n\n2\n"),
        ("a \n\t{%- if True %} b {% endif -%}\n\t c", "a b c"),
        ("a \n {{- x -}} \n b", "a42b"),
        ("a \n {#- comment -#} \n b", "ab"),
        ("a {#-#} b", "a b"),
        ("a {#--#} b", "ab"),
        ("{% for n in [1,2] -%}\n  {{ n }}\n{% endfor -%}\n", "1\n2\n"),
        ("{% for n in [1,2] %}{{ n }} \n{%- endfor %}", "12"),
        ("a \t{% if False -%}\nignored{% endif %} b", "a \t b"),
        ("{{ ' x ' }}{%- if True %}!{% endif %}", " x !"),
        ("a {% if True %} {{ ' y ' -}} {% endif %} z", "a   y  z"),
        ("a\r\n {%- if True -%}\r\n b\r\n {%- endif -%}\r\n c", "abc"),
        ("{{ -2 }}", "-2"),
        ("{{2-1}}", "1"),
        ("{{ {'k': 1}}}", "{'k': 1}"),
    ],
)
def test_comments_escapes_and_whitespace(source, expected):
    assert render_template(source, x=42) == expected


@pytest.mark.parametrize(
    "source, message, marker",
    [
        ("text\n  {% if True %}yes{% endfor %}", "Expected endif", "{% endfor"),
        ("{% endif %}", "Unexpected block terminator", "{% endif"),
        ("{% else %}", "Unexpected else", "{% else"),
        ("text\n  {% if True %}yes", "Unclosed if", "{% if"),
        ("{% if True %}yes{% else %}no{% elif True %}bad{% endif %}", "Unexpected elif after else", "{% elif"),
        ("{% for n in [] %}{% elif True %}bad{% endfor %}", "Unexpected elif", "{% elif"),
        ("{% if True %}{% endif extra %}", "Unexpected text", "{% endif"),
        ("hello {% %}", "Empty template command", "{%"),
        ("hello {{ }}", "Empty template expression", "{{"),
        ("hello {% if True", "unclosed template command", "{%"),
        ("hello {{value", "unclosed template expression", "{{"),
        ("text\n{# comment", "unclosed template comment", "{#"),
        ("text\n  {% if + %}bad{% endif %}", "Unexpected end", "{% if"),
        ("text\n  {{ 1 + }}", "Unexpected end", "{{"),
        ("first\n{{\n  1 +\n}}", "Unexpected end", "{{"),
        ("{% set n = %}", "Unexpected end", "{% set"),
        ("{% set n %}", "Unexpected", "{% set"),
        ("{% set %}", "Unexpected", "{% set"),
        ("{% set a = 1; b = 2 %}", "Unexpected", "{% set"),
        ("{% break %}", "outside loop", "{% break"),
        ("{% if True %}{% finally %}{% endif %}", "Unsupported template command", "{% finally"),
        ("{% if True %}{% else %}{% else %}{% endif %}", "Unexpected else after else", "{% else %}{% endif"),
        ("\n{#- ignore -#}\n\n   {% if + %}{% endif %}", "Unexpected end", "{% if"),
        ('{{ "unterminated }}', "unterminated|unclosed", "{{"),
        ("{{ 'unterminated }}", "unterminated|unclosed", "{{"),
    ],
)
def test_source_diagnostics(source, message, marker):
    with pytest.raises(SyntaxError, match=f"(?i){message}") as caught:
        Template(source)
    position = source.index(marker)
    error = caught.value
    assert error.filename == "<template>"
    assert error.lineno == source.count("\n", 0, position) + 1
    assert error.offset == position - source.rfind("\n", 0, position)
    assert error.text == source.splitlines()[error.lineno - 1]


def test_compile_cache_and_fresh_context():
    source = "{% set total = 0 %}{% for n in items %}{% set total = total + n %}{% endfor %}{{ total }}"
    fn = Template.compile(source)
    assert callable(fn)
    assert fn is Template.compile(source)
    assert fn(items=[1, 2]) == "3"
    assert fn(items=[]) == "0"
    template = Template(source)
    assert template.render(items=[4, 5]) == "9"
    assert template.render(items=[]) == "0"
    assert "Template" in repr(template)


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
@pytest.mark.parametrize(
    "source, expected",
    [
        ("{{ value }}", "42"),
        ("{% for n in [1,2] %}{{ n }}{% if n == 2 %}!{% endif %}{% endfor %}\n", "12!\n"),
        ("  {#- remove -#} {{- value -}} \n", "42"),
    ],
)
def test_pickle(source, expected, protocol):
    template = pickle.loads(pickle.dumps(Template(source), protocol=protocol))
    assert template.render(value=42) == expected


@pytest.mark.parametrize(
    "source",
    [
        "{{ name.upper() }}",
        "{{ len({1, 2, 3}) }}",
        "{{ sorted({3, 1, 2})[0] }}",
        "{{ len({x: x * x for x in items}) }}",
        "{{ len({x*x for x in range(4)}) }}",
        "{{ [x * 2 for x in items] }}",
        "{{ dict(x=1, y=2)['x'] }}",
        "{{ f'{name}!' }}",
        "{{ f'{3.14159:.2f}' }}",
        "{{ f'{42}}}' }}",
        "{{ (10).__or__(3) }}",
        "{% set n = 2 %}{% while n %}{{ n }}{% set n -= 1 %}{% endwhile %}",
        "{% set n = 0 %}{% while n %}bad{% else %}empty{% endwhile %}",
        "{% set n: int = 42 %}{{ n }}",
        "{% set squares = [x**2 for x in range(4)] %}{% for x in squares %}{{ x }};{% endfor %}",
        "{% try %}{{ 1 / 0 }}{% except ZeroDivisionError %}zero{% finally %}!{% endtry %}",
        "{% try %}ok{% except ValueError %}bad{% else %}!{% finally %}?{% endtry %}",
        "{% with __import__('contextlib').nullcontext(42) as n %}{{ n }}{% endwith %}",
        "{% def twice(n) %}{% return n * 2 %}{% enddef %}{{ twice(21) }}",
        "{% class Value %}{% set n = 42 %}{% endclass %}{{ Value.n }}",
        "{% if f'{5 % 2}%}}' == '1%}' %}yes{% endif %}",
    ],
)
def test_python_execution_syntax_is_rejected(source):
    with pytest.raises(SyntaxError):
        Template(source)


def test_a_value_the_template_never_reads_is_not_checked():
    """A check costs as much as the data is deep, so it waits for the read."""

    class Trap:
        def __getattr__(self, name):
            raise AssertionError("the template touched an unread value")

    template = Template("{{ used }}")
    assert template.render(used="ok", unused=Trap(), also=[Trap()]) == "ok"
    with pytest.raises(TypeError, match="plain data"):
        Template("{{ unused }}").render(unused=Trap())


def test_a_value_is_checked_once_however_often_it_is_read(monkeypatch):
    checked = []
    original = RenderContext.validate

    def counted(value, seen):
        checked.append(value)
        return original(value, seen)

    monkeypatch.setattr(RenderContext, "validate", staticmethod(counted))
    assert (
        Template("{% for item in items %}{{ item }}{% endfor %}{{ items|length }}").render(items=[1, 2, 3]) == "1233"
    )
    assert checked == [[1, 2, 3]]


def test_a_frozen_snapshot_is_accepted_without_a_walk(monkeypatch):
    checked = []
    original = RenderContext.validate

    def counted(value, seen):
        checked.append(value)
        return original(value, seen)

    snapshot = freeze({"hosts": [{"name": "one", "port": 1}, {"name": "two", "port": 2}], "hostname": "controller"})
    template = Template("{% for host in hosts %}{{ host.name }}:{{ host.port }} {% endfor %}on {{ hostname }}")
    with monkeypatch.context() as patch:
        patch.setattr(RenderContext, "validate", staticmethod(counted))
        assert template.render(snapshot) == "one:1 two:2 on controller"
        assert checked == []
    # The same snapshot renders like the plain data it was made from.
    assert template.render(hosts=[{"name": "one", "port": 1}, {"name": "two", "port": 2}], hostname="controller") == (
        template.render(snapshot)
    )


def test_a_frozen_snapshot_accepts_extra_keyword_values():
    snapshot = freeze({"hosts": [{"name": "one"}]})
    template = Template("{% for host in hosts %}{{ host.name }}{% endfor %}@{{ hostname }}")
    assert template.render(snapshot, hostname="controller") == "one@controller"

    class Trap:
        pass

    # A keyword value added beside a snapshot is still checked when it is read.
    with pytest.raises(TypeError, match="plain data"):
        template.render(snapshot, hostname=Trap())


def test_a_plain_mapping_passed_whole_is_still_checked():
    class Trap:
        pass

    assert Template("{{ a }}").render({"a": 1}) == "1"
    with pytest.raises(TypeError, match="plain data"):
        Template("{{ a }}").render({"a": Trap()})


def test_a_frozen_branch_inside_a_plain_context_is_accepted():
    snapshot = freeze({"name": "one", "port": 1})
    assert Template("{{ host.name }}:{{ host.port }}").render(host=snapshot) == "one:1"
    assert Template("{{ hosts|length }}").render(hosts=[snapshot, snapshot]) == "2"


def test_a_field_of_a_value_that_is_not_a_mapping_is_an_attribute():
    """The subscript chooses the way: a value that refuses it gives an attribute."""
    assert Template("{{ number.real }}:{{ number.denominator }}").render(number=7) == "7:1"
    assert Template("{{ host.name }}").render(host={"name": "one"}) == "one"
    assert Template("{{ host.name }}").render(host=freeze({"name": "one"})) == "one"
    with pytest.raises(AttributeError):
        Template("{{ number.missing }}").render(number=7)
    with pytest.raises(KeyError):
        Template("{{ host.missing }}").render(host={"a": 1})
