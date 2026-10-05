"""Tests for Template.compile / render_template / Template protocol utilities."""

import pickle

import pytest

from rmote.protocol import Protocol, Template, Tool, render_template

pytestmark = pytest.mark.timeout(30)

rt = render_template  # short alias for local render assertions


# ---------------------------------------------------------------------------
# Tier 1: Template.compile() - returns a callable render function
# ---------------------------------------------------------------------------


class TestCompileTemplate:
    def test_returns_callable(self) -> None:
        assert callable(Template.compile("Hello, world!"))

    def test_simple_text(self) -> None:
        assert Template.compile("Hello, world!")() == "Hello, world!"

    def test_expression_interpolation(self) -> None:
        fn = Template.compile("Hello, ${name}!")
        assert fn(name="Alice") == "Hello, Alice!"

    def test_no_trailing_newline(self) -> None:
        assert not Template.compile("line1\nline2")().endswith("\n")

    def test_for_loop(self) -> None:
        fn = Template.compile("% for x in items:\n${x}\n% endfor")
        assert fn(items=["a", "b", "c"]) == "a\nb\nc"

    def test_if_else_block(self) -> None:
        fn = Template.compile("% if x:\nyes\n% else:\nno\n% endif")
        assert fn(x=True) == "yes"
        assert fn(x=False) == "no"

    def test_elif_block(self) -> None:
        fn = Template.compile("% if x > 0:\npos\n% elif x < 0:\nneg\n% else:\nzero\n% endif")
        assert fn(x=1) == "pos"
        assert fn(x=-1) == "neg"
        assert fn(x=0) == "zero"

    def test_comment_ignored(self) -> None:
        assert "comment" not in Template.compile("## this is a comment\nHello")()

    def test_bare_statement(self) -> None:
        assert Template.compile("% x = 42\n${x}")() == "42"

    def test_result_is_string(self) -> None:
        assert isinstance(Template.compile("hi")(), str)

    def test_cached(self) -> None:
        fn1 = Template.compile("same template")
        fn2 = Template.compile("same template")
        assert fn1 is fn2


# ---------------------------------------------------------------------------
# Tier 2: render_template() - basic syntax
# ---------------------------------------------------------------------------


class TestRenderTemplateBasics:
    def test_empty_template(self) -> None:
        assert rt("") == ""

    def test_plain_text(self) -> None:
        assert rt("Hello, world!") == "Hello, world!"

    def test_multiline_plain_text(self) -> None:
        assert rt("line1\nline2\nline3") == "line1\nline2\nline3"

    def test_no_trailing_newline(self) -> None:
        assert not rt("a\nb").endswith("\n")

    def test_single_expression(self) -> None:
        assert rt("${name}", name="Alice") == "Alice"

    def test_expression_mid_line(self) -> None:
        assert rt("Hello, ${name}!", name="Bob") == "Hello, Bob!"

    def test_multiple_expressions_same_line(self) -> None:
        assert rt("${a}+${b}=${c}", a=1, b=2, c=3) == "1+2=3"

    def test_expression_only_line(self) -> None:
        assert rt("${x}\n${y}", x="A", y="B") == "A\nB"

    def test_expression_attribute(self) -> None:
        assert rt("${s.upper()}", s="hello") == "HELLO"

    def test_expression_index(self) -> None:
        assert rt("${items[0]}", items=["first", "second"]) == "first"

    def test_expression_dict_key(self) -> None:
        assert rt("${d['k']}", d={"k": "v"}) == "v"

    def test_expression_arithmetic(self) -> None:
        assert rt("${a * b}", a=6, b=7) == "42"

    def test_expression_ternary(self) -> None:
        assert rt("${'yes' if flag else 'no'}", flag=True) == "yes"
        assert rt("${'yes' if flag else 'no'}", flag=False) == "no"

    def test_comment_not_in_output(self) -> None:
        assert "skip" not in rt("## skip this\nHello")

    def test_comment_between_text_lines(self) -> None:
        assert rt("line1\n## comment\nline2") == "line1\nline2"

    def test_bare_assignment(self) -> None:
        assert rt("% x = 'hi'\n${x}") == "hi"

    def test_bare_augmented_assignment(self) -> None:
        assert rt("% total = 0\n% total += 10\n${total}") == "10"

    def test_bare_percent_as_block_terminator(self) -> None:
        """A line with only '%' (nothing after) decrements indent - same as endfor."""
        assert rt("% for x in [1, 2]:\n${x}\n%") == "1\n2"


# ---------------------------------------------------------------------------
# Tier 3: Template syntax - escaping and special characters
# ---------------------------------------------------------------------------


class TestTemplateEscaping:
    # --- %% → literal % ---------------------------------------------------

    def test_double_percent_alone(self) -> None:
        """A line starting with %% outputs a literal %."""
        assert rt("%%") == "%"

    def test_double_percent_with_text(self) -> None:
        assert rt("%% for x in items:") == "% for x in items:"

    def test_double_percent_not_control_flow(self) -> None:
        """%%for must NOT enter a loop - it is plain text."""
        result = rt("%% for x in range(3):\nhello\n%% endfor")
        assert result == "% for x in range(3):\nhello\n% endfor"

    def test_double_percent_preserves_if_keyword(self) -> None:
        assert rt("%% if True:") == "% if True:"

    def test_double_percent_with_expressions(self) -> None:
        """%% lines still interpolate ${} expressions in the remainder."""
        assert rt("%% price: ${amount}", amount=9) == "% price: 9"

    def test_double_percent_mixed_with_control_flow(self) -> None:
        tmpl = "% for x in [1, 2]:\n%% item ${x}\n% endfor"
        assert rt(tmpl) == "% item 1\n% item 2"

    def test_double_percent_leading_whitespace_preserved(self) -> None:
        """Indentation before %% is part of the text output."""
        assert rt("  %% note") == "  % note"

    # --- \${ → literal ${ (no interpolation) --------------------------------

    def test_escaped_dollar_brace_is_literal(self) -> None:
        assert rt("price: \\${amount}", amount=99) == "price: ${amount}"

    def test_escaped_dollar_brace_no_variable_needed(self) -> None:
        """\\${ requires no variable in ctx."""
        assert rt("\\${missing}") == "${missing}"

    def test_escaped_dollar_brace_before_real_expr(self) -> None:
        assert rt("\\${x} = ${x}", x=42) == "${x} = 42"

    def test_escaped_dollar_brace_after_real_expr(self) -> None:
        assert rt("${x} \\${x}", x=42) == "42 ${x}"

    def test_escaped_dollar_brace_multiple(self) -> None:
        assert rt("\\${a} \\${b}") == "${a} ${b}"

    def test_escaped_dollar_brace_mixed_multiple(self) -> None:
        assert rt("\\${a} + ${b} = \\${c}", b=2) == "${a} + 2 = ${c}"

    # --- bare $ and { } are always literal -----------------------------------

    def test_bare_dollar_is_literal(self) -> None:
        assert rt("$100") == "$100"

    def test_dollar_in_text(self) -> None:
        assert rt("Price: $5.99") == "Price: $5.99"

    def test_bare_open_brace_is_literal(self) -> None:
        assert rt("dict: {key}") == "dict: {key}"

    def test_bare_close_brace_is_literal(self) -> None:
        assert rt("end: }") == "end: }"

    def test_both_braces_literal(self) -> None:
        assert rt("{hello}") == "{hello}"

    def test_dollar_at_end_of_line(self) -> None:
        assert rt("total: $") == "total: $"

    # --- nested {} inside expressions ----------------------------------------

    def test_expression_dict_literal_access(self) -> None:
        assert rt("${ {'k': 'v'}['k'] }") == "v"

    def test_expression_set_len(self) -> None:
        assert rt("${len({1, 2, 3})}") == "3"

    def test_expression_nested_dict(self) -> None:
        assert rt("${ {'a': {'b': 1}}['a']['b'] }") == "1"

    def test_expression_set_sorted(self) -> None:
        assert rt("${sorted({3, 1, 2})[0]}") == "1"

    def test_expression_dict_comprehension_len(self) -> None:
        assert rt("${len({k: v for k, v in pairs})}", pairs=[("a", 1), ("b", 2)]) == "2"

    def test_expression_set_comprehension(self) -> None:
        assert rt("${len({x*x for x in range(4)})}") == "4"

    def test_expression_fstring_with_braces(self) -> None:
        assert rt("${f'{name}!'}", name="Alice") == "Alice!"

    def test_expression_fstring_format_spec(self) -> None:
        assert rt("${f'{val:.2f}'}", val=3.14159) == "3.14"

    def test_expression_deeply_nested_dict(self) -> None:
        data = {"a": {"b": {"c": 42}}}
        assert rt("${d['a']['b']['c']}", d=data) == "42"

    def test_expression_dict_constructor(self) -> None:
        assert rt("${dict(x=1, y=2)['x']}") == "1"

    def test_multiple_nested_exprs_on_one_line(self) -> None:
        assert rt("${len({1,2})} and ${len({3,4,5})}") == "2 and 3"

    # --- combining escapes with control flow ---------------------------------

    def test_double_percent_inside_for_loop(self) -> None:
        tmpl = "% for x in items:\n%% ${x}\n% endfor"
        assert rt(tmpl, items=["a", "b"]) == "% a\n% b"

    def test_escaped_dollar_inside_for_loop(self) -> None:
        tmpl = "% for x in items:\n\\${x} = ${x}\n% endfor"
        assert rt(tmpl, items=[1, 2]) == "${x} = 1\n${x} = 2"

    def test_nested_braces_inside_if(self) -> None:
        tmpl = "% if flag:\n${len({1,2,3})}\n% endif"
        assert rt(tmpl, flag=True) == "3"
        assert rt(tmpl, flag=False) == ""


# ---------------------------------------------------------------------------
# Tier 4: render_template() - for-loop cases
# ---------------------------------------------------------------------------


class TestRenderTemplateForLoops:
    def test_simple_for(self) -> None:
        assert rt("% for x in items:\n${x}\n% endfor", items=["a", "b", "c"]) == "a\nb\nc"

    def test_for_with_prefix(self) -> None:
        assert rt("% for x in items:\n- ${x}\n% endfor", items=["a", "b"]) == "- a\n- b"

    def test_for_with_trailing_text(self) -> None:
        assert rt("% for x in items:\n${x}\n% endfor\nDone.", items=["a", "b"]) == "a\nb\nDone."

    def test_for_with_leading_text(self) -> None:
        assert rt("Start\n% for x in items:\n${x}\n% endfor", items=["a", "b"]) == "Start\na\nb"

    def test_for_empty_iterable(self) -> None:
        assert rt("% for x in items:\n${x}\n% endfor", items=[]) == ""

    def test_for_single_item(self) -> None:
        assert rt("% for x in items:\n${x}\n% endfor", items=["only"]) == "only"

    def test_for_with_range(self) -> None:
        assert rt("% for i in range(3):\n${i}\n% endfor") == "0\n1\n2"

    def test_for_endfor_terminator(self) -> None:
        assert rt("% for x in [1,2]:\n${x}\n% endfor") == "1\n2"

    def test_for_end_terminator(self) -> None:
        assert rt("% for x in [1,2]:\n${x}\n% end") == "1\n2"

    def test_for_multiline_body(self) -> None:
        result = rt("% for x in items:\nfirst: ${x}\nsecond: ${x.upper()}\n% endfor", items=["a", "b"])
        assert result == "first: a\nsecond: A\nfirst: b\nsecond: B"

    def test_for_multiple_exprs_per_body_line(self) -> None:
        assert rt("% for x in items:\n${x}: ${x.upper()}\n% endfor", items=["a", "b"]) == "a: A\nb: B"

    def test_for_with_enumerate(self) -> None:
        assert rt("% for i, v in enumerate(items):\n${i}: ${v}\n% endfor", items=["a", "b"]) == "0: a\n1: b"

    def test_for_with_zip(self) -> None:
        assert rt("% for k, v in zip(keys, vals):\n${k}=${v}\n% endfor", keys=["x", "y"], vals=[1, 2]) == "x=1\ny=2"

    def test_for_break(self) -> None:
        tmpl = "% for x in range(10):\n% if x == 3:\n% break\n% endif\n${x}\n% endfor"
        assert rt(tmpl) == "0\n1\n2"

    def test_for_continue(self) -> None:
        tmpl = "% for x in range(5):\n% if x % 2 == 0:\n% continue\n% endif\n${x}\n% endfor"
        assert rt(tmpl) == "1\n3"

    def test_nested_for_2_levels(self) -> None:
        tmpl = "% for i in rows:\n% for j in cols:\n${i}${j}\n% endfor\n% endfor"
        assert rt(tmpl, rows=["A", "B"], cols=[1, 2]) == "A1\nA2\nB1\nB2"

    def test_nested_for_3_levels(self) -> None:
        tmpl = (
            "% for a in [1,2]:\n"
            "% for b in ['x','y']:\n"
            "% for c in [True,False]:\n"
            "${a}${b}${c}\n"
            "% endfor\n"
            "% endfor\n"
            "% endfor"
        )
        lines = rt(tmpl).splitlines()
        assert lines[0] == "1xTrue"
        assert lines[1] == "1xFalse"
        assert lines[2] == "1yTrue"
        assert len(lines) == 8  # 2 * 2 * 2

    def test_nested_for_with_separator_text(self) -> None:
        tmpl = "% for i in [1,2]:\nrow ${i}\n% for j in ['a','b']:\n  ${i}${j}\n% endfor\n% endfor"
        result = rt(tmpl)
        assert result == "row 1\n  1a\n  1b\nrow 2\n  2a\n  2b"

    def test_nested_for_accumulate(self) -> None:
        tmpl = "% total = 0\n% for row in matrix:\n% for v in row:\n% total += v\n% endfor\n% endfor\n${total}"
        assert rt(tmpl, matrix=[[1, 2], [3, 4]]) == "10"

    def test_comment_inside_for(self) -> None:
        assert rt("% for x in items:\n## skip\n${x}\n% endfor", items=["a", "b"]) == "a\nb"


# ---------------------------------------------------------------------------
# Tier 5: render_template() - conditional cases
# ---------------------------------------------------------------------------


class TestRenderTemplateConditionals:
    def test_if_true(self) -> None:
        assert rt("% if flag:\nyes\n% endif", flag=True) == "yes"

    def test_if_false_empty(self) -> None:
        assert rt("% if flag:\nyes\n% endif", flag=False) == ""

    def test_if_else_true(self) -> None:
        assert rt("% if flag:\nyes\n% else:\nno\n% endif", flag=True) == "yes"

    def test_if_else_false(self) -> None:
        assert rt("% if flag:\nyes\n% else:\nno\n% endif", flag=False) == "no"

    def test_if_elif_else(self) -> None:
        tmpl = "% if x > 0:\npos\n% elif x < 0:\nneg\n% else:\nzero\n% endif"
        assert rt(tmpl, x=5) == "pos"
        assert rt(tmpl, x=-3) == "neg"
        assert rt(tmpl, x=0) == "zero"

    def test_multiple_elif(self) -> None:
        tmpl = "% if x == 1:\none\n% elif x == 2:\ntwo\n% elif x == 3:\nthree\n% else:\nother\n% endif"
        assert rt(tmpl, x=1) == "one"
        assert rt(tmpl, x=2) == "two"
        assert rt(tmpl, x=3) == "three"
        assert rt(tmpl, x=99) == "other"

    def test_nested_if_inside_if(self) -> None:
        tmpl = "% if outer:\n% if inner:\nboth\n% else:\nonly_outer\n% endif\n% else:\nnone\n% endif"
        assert rt(tmpl, outer=True, inner=True) == "both"
        assert rt(tmpl, outer=True, inner=False) == "only_outer"
        assert rt(tmpl, outer=False, inner=True) == "none"
        assert rt(tmpl, outer=False, inner=False) == "none"

    def test_nested_if_3_levels(self) -> None:
        tmpl = "% if a:\n% if b:\n% if c:\nabc\n% else:\nab\n% endif\n% else:\na\n% endif\n% else:\nnone\n% endif"
        assert rt(tmpl, a=True, b=True, c=True) == "abc"
        assert rt(tmpl, a=True, b=True, c=False) == "ab"
        assert rt(tmpl, a=True, b=False, c=True) == "a"
        assert rt(tmpl, a=False, b=True, c=True) == "none"

    def test_if_with_text_before_and_after(self) -> None:
        tmpl = "before\n% if flag:\nmiddle\n% endif\nafter"
        assert rt(tmpl, flag=True) == "before\nmiddle\nafter"
        assert rt(tmpl, flag=False) == "before\nafter"

    def test_if_with_expression_in_condition(self) -> None:
        tmpl = "% if len(items) > 0:\nhas items\n% else:\nempty\n% endif"
        assert rt(tmpl, items=[1]) == "has items"
        assert rt(tmpl, items=[]) == "empty"

    def test_comment_inside_if(self) -> None:
        tmpl = "% if flag:\n## internal\nyes\n% endif"
        assert rt(tmpl, flag=True) == "yes"

    def test_elif_no_else(self) -> None:
        tmpl = "% if x == 1:\none\n% elif x == 2:\ntwo\n% endif"
        assert rt(tmpl, x=1) == "one"
        assert rt(tmpl, x=2) == "two"
        assert rt(tmpl, x=3) == ""


# ---------------------------------------------------------------------------
# Tier 6: render_template() - mixed control flow
# ---------------------------------------------------------------------------


class TestRenderTemplateMixed:
    def test_if_inside_for(self) -> None:
        tmpl = "% for x in items:\n% if x > 0:\n+${x}\n% else:\n${x}\n% endif\n% endfor"
        assert rt(tmpl, items=[1, -2, 3]) == "+1\n-2\n+3"

    def test_for_inside_if(self) -> None:
        tmpl = "% if show:\n% for x in items:\n${x}\n% endfor\n% endif"
        assert rt(tmpl, show=True, items=["a", "b"]) == "a\nb"
        assert rt(tmpl, show=False, items=["a", "b"]) == ""

    def test_for_inside_else(self) -> None:
        tmpl = "% if empty:\nnone\n% else:\n% for x in items:\n${x}\n% endfor\n% endif"
        assert rt(tmpl, empty=False, items=["a", "b"]) == "a\nb"
        assert rt(tmpl, empty=True, items=[]) == "none"

    def test_nested_for_with_if(self) -> None:
        tmpl = "% for row in matrix:\n% for val in row:\n% if val > 0:\n+\n% else:\n-\n% endif\n% endfor\n|\n% endfor"
        result = rt(tmpl, matrix=[[1, -1], [-1, 1]])
        lines = result.splitlines()
        assert lines[0] == "+"
        assert lines[1] == "-"
        assert lines[2] == "|"

    def test_if_inside_nested_for(self) -> None:
        tmpl = "% for i in [1,2]:\n% for j in [1,2]:\n% if i == j:\n${i}\n% endif\n% endfor\n% endfor"
        assert rt(tmpl) == "1\n2"

    def test_assignment_before_loop(self) -> None:
        tmpl = "% total = 0\n% for x in items:\n% total += x\n% endfor\n${total}"
        assert rt(tmpl, items=[1, 2, 3, 4]) == "10"

    def test_assignment_inside_loop(self) -> None:
        tmpl = "% for x in items:\n% y = x * 2\n${y}\n% endfor"
        assert rt(tmpl, items=[1, 2, 3]) == "2\n4\n6"

    def test_assignment_accumulate_string(self) -> None:
        tmpl = "% out = ''\n% for x in items:\n% out += x\n% endfor\n${out}"
        assert rt(tmpl, items=["a", "b", "c"]) == "abc"

    def test_listcomp_then_loop(self) -> None:
        tmpl = "% squares = [x**2 for x in range(4)]\n% for v in squares:\n${v}\n% endfor"
        assert rt(tmpl) == "0\n1\n4\n9"

    def test_multiline_header_footer(self) -> None:
        tmpl = "=== Report ===\n% for item in items:\n  * ${item}\n% endfor\n=== End ==="
        result = rt(tmpl, items=["foo", "bar"])
        assert result.startswith("=== Report ===\n")
        assert result.endswith("\n=== End ===")
        assert "  * foo" in result

    def test_indentation_preserved_in_text(self) -> None:
        tmpl = "% for x in items:\n    ${x}\n% endfor"
        assert rt(tmpl, items=["a"]) == "    a"

    def test_break_in_nested_for(self) -> None:
        tmpl = "% for i in [1,2,3]:\n% for j in [1,2,3]:\n% if j == 2:\n% break\n% endif\n${i}${j}\n% endfor\n% endfor"
        assert rt(tmpl) == "11\n21\n31"

    def test_for_if_elif_mixed(self) -> None:
        tmpl = "% for x in items:\n% if x < 0:\nneg\n% elif x == 0:\nzero\n% else:\npos\n% endif\n% endfor"
        assert rt(tmpl, items=[-1, 0, 1]) == "neg\nzero\npos"


# ---------------------------------------------------------------------------
# Tier 7: Template - picklability and reuse
# ---------------------------------------------------------------------------


class TestTemplate:
    def test_render_matches_render_template(self) -> None:
        tmpl = "Hello, ${name}!"
        assert Template(tmpl).render(name="Alice") == rt(tmpl, name="Alice")

    def test_render_loop(self) -> None:
        assert Template("% for x in items:\n${x}\n% endfor").render(items=["a", "b"]) == "a\nb"

    def test_reuse_different_ctx(self) -> None:
        ct = Template("${n}")
        assert ct.render(n=1) == "1"
        assert ct.render(n=2) == "2"
        assert ct.render(n="three") == "three"

    def test_pickle_simple(self) -> None:
        ct = Template("Hello, ${name}!")
        assert pickle.loads(pickle.dumps(ct)).render(name="Pickle") == "Hello, Pickle!"

    def test_pickle_loop(self) -> None:
        ct = Template("% for x in items:\n${x}\n% endfor")
        assert pickle.loads(pickle.dumps(ct)).render(items=["a", "b"]) == "a\nb"

    def test_pickle_nested_for(self) -> None:
        ct = Template("% for i in rows:\n% for j in cols:\n${i}${j}\n% endfor\n% endfor")
        assert pickle.loads(pickle.dumps(ct)).render(rows=["A", "B"], cols=[1, 2]) == "A1\nA2\nB1\nB2"

    def test_pickle_if_else(self) -> None:
        ct = Template("% if flag:\nyes\n% else:\nno\n% endif")
        restored: Template = pickle.loads(pickle.dumps(ct))
        assert restored.render(flag=True) == "yes"
        assert restored.render(flag=False) == "no"

    def test_pickle_nested_if_in_for(self) -> None:
        ct = Template("% for x in items:\n% if x > 0:\n+${x}\n% else:\n${x}\n% endif\n% endfor")
        assert pickle.loads(pickle.dumps(ct)).render(items=[1, -2, 3]) == "+1\n-2\n+3"

    def test_pickle_all_protocols(self) -> None:
        ct = Template("${v}")
        for proto in range(pickle.HIGHEST_PROTOCOL + 1):
            assert pickle.loads(pickle.dumps(ct, protocol=proto)).render(v=proto) == str(proto)

    def test_repr(self) -> None:
        assert "Template" in repr(Template("hi"))


# ---------------------------------------------------------------------------
# Tier 8: Integration - inline tools using template utilities over subprocess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_render_simple(protocol: Protocol) -> None:
    class T(Tool):
        @staticmethod
        def run(tmpl: str, **ctx: object) -> str:
            return render_template(tmpl, **ctx)

    assert await protocol(T.run, "Hello, ${name}!", name="Remote") == "Hello, Remote!"


@pytest.mark.asyncio
async def test_inline_render_for_loop(protocol: Protocol) -> None:
    class T(Tool):
        @staticmethod
        def run(items: list[str]) -> str:
            return render_template("% for x in items:\n${x}\n% endfor", items=items)

    assert await protocol(T.run, ["a", "b", "c"]) == "a\nb\nc"


@pytest.mark.asyncio
async def test_inline_render_nested_for(protocol: Protocol) -> None:
    class T(Tool):
        @staticmethod
        def run(rows: list[str], cols: list[int]) -> str:
            return render_template(
                "% for r in rows:\n% for c in cols:\n${r}${c}\n% endfor\n% endfor",
                rows=rows,
                cols=cols,
            )

    assert await protocol(T.run, ["A", "B"], [1, 2]) == "A1\nA2\nB1\nB2"


@pytest.mark.asyncio
async def test_inline_render_if_else(protocol: Protocol) -> None:
    class T(Tool):
        @staticmethod
        def run(flag: bool) -> str:
            return render_template("% if flag:\nyes\n% else:\nno\n% endif", flag=flag)

    assert await protocol(T.run, True) == "yes"
    assert await protocol(T.run, False) == "no"


@pytest.mark.asyncio
async def test_inline_render_if_in_for(protocol: Protocol) -> None:
    class T(Tool):
        @staticmethod
        def run(items: list[int]) -> str:
            return render_template(
                "% for x in items:\n% if x > 0:\n+${x}\n% else:\n${x}\n% endif\n% endfor",
                items=items,
            )

    assert await protocol(T.run, [1, -2, 3]) == "+1\n-2\n+3"


@pytest.mark.asyncio
async def test_inline_compiled_template_as_argument(protocol: Protocol) -> None:
    """Template compiled locally, pickled, rendered on the remote."""

    class T(Tool):
        @staticmethod
        def run(tmpl: Template, **ctx: object) -> str:
            return tmpl.render(**ctx)

    ct = Template("Hello, ${name}! Count: ${count}")
    assert await protocol(T.run, ct, name="Remote", count=42) == "Hello, Remote! Count: 42"


@pytest.mark.asyncio
async def test_inline_compiled_template_nested_loop(protocol: Protocol) -> None:
    class T(Tool):
        @staticmethod
        def run(tmpl: Template, rows: list[str], cols: list[int]) -> str:
            return tmpl.render(rows=rows, cols=cols)

    ct = Template("% for r in rows:\n% for c in cols:\n${r}${c}\n% endfor\n% endfor")
    assert await protocol(T.run, ct, ["A", "B"], [1, 2]) == "A1\nA2\nB1\nB2"


@pytest.mark.asyncio
async def test_inline_compiled_template_nested_if_in_for(protocol: Protocol) -> None:
    class T(Tool):
        @staticmethod
        def run(tmpl: Template, items: list[int]) -> str:
            return tmpl.render(items=items)

    ct = Template("% for x in items:\n% if x > 0:\n+${x}\n% else:\n${x}\n% endif\n% endfor")
    assert await protocol(T.run, ct, [1, -2, 3]) == "+1\n-2\n+3"
