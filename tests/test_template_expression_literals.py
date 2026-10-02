"""Regression tests for template expression literals."""

import pytest

from rmote.protocol import Template


@pytest.mark.parametrize(("source", "expected"), [('${"}"}', "}"), ('${"{"}', "{")])
def test_template_braces_in_strings(source, expected):
    assert Template(source).render() == expected


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("'}'", "}"),
        ("'{'", "{"),
        ('"\\"}"', '"}'),
        ("'\\'{'", "'{"),
        ('r"\\}"', "\\}"),
        ('"""{quoted}"""', "{quoted}"),
        ("'''{quoted}'''", "{quoted}"),
        ('{"}": "{"}["}"]', "{"),
        ('len({"{", "}"})', "2"),
        ('f"{{braces}} {1 + 1}"', "{braces} 2"),
    ],
)
def test_template_string_literals_and_nested_expressions(expression, expected):
    assert Template("before ${" + expression + "} after ${2}").render() == f"before {expected} after 2"


def test_template_unclosed_expression():
    with pytest.raises(SyntaxError):
        Template('${"}"')
