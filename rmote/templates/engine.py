"""Template tokens, parsing, and compiled intermediate representation."""

import ast
import keyword
import operator
import tokenize
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from functools import cache
from typing import Any, TypeVar

from rmote.immutable import DeepMappingProxy

from .filters import TEMPLATE_FILTERS, TemplateFilter, TemplateUndefined


class TemplateSyntaxError(SyntaxError):
    """A syntax error already located in the original template source."""


@dataclass(frozen=True)
class TemplateSource:
    """Source text and diagnostics at an exact character offset."""

    text: str

    def error(self, message: str, position: int) -> SyntaxError:
        lineno = self.text.count("\n", 0, position) + 1
        start = self.text.rfind("\n", 0, position) + 1
        end = self.text.find("\n", position)
        line = self.text[start:] if end < 0 else self.text[start:end]
        return TemplateSyntaxError(message, ("<template>", lineno, position - start + 1, line))


@dataclass(frozen=True)
class TemplateToken:
    """A lexical token retaining its source position and unmodified contents."""

    value: str
    position: int


class TextToken(TemplateToken):
    """Literal output, after processing source whitespace and escaped delimiters."""


@dataclass(frozen=True)
class CodeToken:
    """One code lexeme with its token type and absolute source offset."""

    type: int
    string: str
    position: int


@dataclass(frozen=True)
class ExpressionToken(TemplateToken):
    """An interpolation and its code tokens, ready for the expression parser."""

    tokens: tuple[CodeToken, ...]


@dataclass(frozen=True)
class CommandToken(TemplateToken):
    """A command name and argument tokens, separated once by the lexer."""

    name: str
    tokens: tuple[CodeToken, ...]


class CommentToken(TemplateToken):
    """A comment retained for inspection but omitted from execution."""


class TemplateLexer:
    """Split template text into typed tokens without interpreting expressions."""

    def __init__(self, source: TemplateSource) -> None:
        self.source = source

    def tokenize(self) -> tuple[TemplateToken, ...]:
        template = self.source.text
        result: list[TemplateToken] = []
        buf: list[str] = []
        i = 0
        text_start = 0
        delimiters = {
            "{{": (ExpressionToken, "expression", "}}"),
            "{%": (CommandToken, "command", "%}"),
            "{#": (CommentToken, "comment", "#}"),
        }
        while i < len(template):
            if template[i] == "\\" and template[i + 1 : i + 3] in delimiters:
                buf.append(template[i + 1 : i + 3])
                i += 3
                continue
            delimiter = template[i : i + 2]
            if delimiter not in delimiters:
                buf.append(template[i])
                i += 1
                continue
            token_type, kind, closing = delimiters[delimiter]
            start = i + 2
            trim_left = template.startswith("-", start)
            if trim_left:
                start += 1
            text = "".join(buf)
            if trim_left:
                text = text.rstrip()
            if text:
                result.append(TextToken(text, text_start))
            buf = []
            if token_type is CommentToken:
                close = template.find(closing, start)
                if close < 0:
                    raise self.source.error("Unclosed template comment", i)
                end = close - 1 if close > start and template[close - 1 : close] == "-" else close
            else:
                code, end, close = self.code(start, i, kind, closing)
            value = template[start:end]
            if token_type is CommentToken:
                result.append(CommentToken(value, i))
            elif token_type is ExpressionToken:
                result.append(ExpressionToken(value, i, code))
            else:
                if code and code[-1].string == ":":
                    code = code[:-1]
                if not code:
                    raise self.source.error("Empty template command", i)
                result.append(CommandToken(value, i, code[0].string, code[1:]))
            i = close + len(closing)
            if end < close:
                while i < len(template) and template[i].isspace():
                    i += 1
            text_start = i
        if buf:
            result.append(TextToken("".join(buf), text_start))
        return tuple(result)

    def code(self, start: int, opening: int, kind: str, closing: str) -> tuple[tuple[CodeToken, ...], int, int]:
        """Tokenize one tag through its closing delimiter, retaining code tokens."""
        template = self.source.text
        # A synthetic parenthesis keeps multiline code free of indentation
        # tokens. It is skipped and does not contribute to our bracket depth.
        offsets = [start - 1]
        cursor = start
        first_line = True

        def readline() -> str:
            nonlocal cursor, first_line
            if cursor >= len(template) and not first_line:
                return ""
            end = template.find("\n", cursor)
            end = len(template) if end < 0 else end + 1
            line = template[cursor:end]
            cursor = end
            if first_line:
                line = "(" + line
                first_line = False
            offsets.append(offsets[-1] + len(line))
            return line

        depth = 0
        result: list[CodeToken] = []
        ignored = {
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.COMMENT,
            tokenize.ENDMARKER,
        }
        try:
            tokens = tokenize.generate_tokens(readline)
            next(tokens)
            for token in tokens:
                position = offsets[token.start[0] - 1] + token.start[1]
                if token.type == tokenize.ERRORTOKEN and token.string in {"'", '"'}:
                    # Python 3.11 reports an unfinished string as error tokens.
                    raise self.source.error(f"Unterminated string in template {kind}", opening)
                if token.type == tokenize.OP:
                    if depth == 0:
                        if template.startswith(closing, position):
                            return tuple(result), position, position
                        if template.startswith("-" + closing, position):
                            return tuple(result), position, position + 1
                    if token.string in {"(", "[", "{"}:
                        depth += 1
                    elif token.string in {")", "]", "}"}:
                        depth -= 1
                if token.type not in ignored:
                    result.append(CodeToken(token.type, token.string, position))
        except (tokenize.TokenError, IndentationError) as exc:
            raise self.source.error(f"Invalid or unclosed template {kind}", opening) from exc
        raise self.source.error(f"Unclosed template {kind}", opening)


class Expression:
    """A compiled expression with no access to Python globals or callables."""

    def evaluate(self, context: "RenderContext") -> Any:
        """Return a value, raising on missing variables or lookups."""
        raise NotImplementedError

    def resolve(self, context: "RenderContext") -> Any:
        """Allow lookup expressions to retain missing values for ``default``."""
        return self.evaluate(context)


class ResolvableExpression(Expression):
    """A lookup or filter whose missing result can be consumed by ``default``."""

    def evaluate(self, context: "RenderContext") -> Any:
        value = self.resolve(context)
        if type(value) is TemplateUndefined:
            raise value.error
        return value

    def resolve(self, context: "RenderContext") -> Any:
        raise NotImplementedError


@dataclass(frozen=True)
class LiteralExpression(Expression):
    value: Any

    def evaluate(self, context: "RenderContext") -> Any:
        return self.value


@dataclass(frozen=True)
class VariableExpression(ResolvableExpression):
    name: str

    def resolve(self, context: "RenderContext") -> Any:
        try:
            value = context.values[self.name]
        except KeyError:
            return TemplateUndefined(NameError(f"Unknown template variable: {self.name}"))
        if self.name in context.unchecked:
            context.check(self.name)
        return value


@dataclass(frozen=True)
class CollectionExpression(Expression):
    kind: str
    items: tuple[Expression, ...]

    def evaluate(self, context: "RenderContext") -> Any:
        values = [item.evaluate(context) for item in self.items]
        return {"list": list, "tuple": tuple, "set": set}[self.kind](values)


@dataclass(frozen=True)
class DictionaryExpression(Expression):
    items: tuple[tuple[Expression, Expression], ...]

    def evaluate(self, context: "RenderContext") -> Any:
        return {key.evaluate(context): value.evaluate(context) for key, value in self.items}


@dataclass(frozen=True)
class UnaryExpression(Expression):
    operator: str
    value: Expression

    def evaluate(self, context: "RenderContext") -> Any:
        return UNARY_OPERATORS[self.operator](self.value.evaluate(context))


UNARY_OPERATORS: dict[str, Callable[[Any], Any]] = {"+": operator.pos, "-": operator.neg, "not": operator.not_}
BINARY_OPERATORS: dict[str, Callable[[Any, Any], Any]] = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
    "//": operator.floordiv,
    "%": operator.mod,
    "**": operator.pow,
}
COMPARISON_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "is": operator.is_,
    "is not": operator.is_not,
    "in": lambda left, right: left in right,
    "not in": lambda left, right: left not in right,
}


@dataclass(frozen=True)
class BinaryExpression(Expression):
    operator: str
    left: Expression
    right: Expression

    def evaluate(self, context: "RenderContext") -> Any:
        left = self.left.evaluate(context)
        if self.operator == "and":
            return self.right.evaluate(context) if left else left
        if self.operator == "or":
            return left if left else self.right.evaluate(context)
        return BINARY_OPERATORS[self.operator](left, self.right.evaluate(context))


@dataclass(frozen=True)
class ComparisonExpression(Expression):
    operands: tuple[Expression, ...]
    operators: tuple[str, ...]

    def evaluate(self, context: "RenderContext") -> Any:
        left = self.operands[0].evaluate(context)
        for op, expression in zip(self.operators, self.operands[1:], strict=True):
            right = expression.evaluate(context)
            if not COMPARISON_OPERATORS[op](left, right):
                return False
            left = right
        return True


@dataclass(frozen=True)
class ConditionalExpression(Expression):
    condition: Expression
    positive: Expression
    negative: Expression

    def evaluate(self, context: "RenderContext") -> Any:
        return (self.positive if self.condition.evaluate(context) else self.negative).evaluate(context)


@dataclass(frozen=True)
class AttributeExpression(ResolvableExpression):
    value: Expression
    name: str

    # A mapping answers the subscript, and everything else refuses it with a
    # TypeError. The subscript therefore chooses the way itself, and a read of
    # a field costs no test of the type of the value. Both a dict and a frozen
    # snapshot answer at the speed of dict, because a snapshot is a dict.
    def evaluate(self, context: "RenderContext") -> Any:
        value = self.value.evaluate(context)
        try:
            result = value[self.name]
        except TypeError:
            result = getattr(value, self.name)
        if type(result) is TemplateUndefined:
            raise result.error
        return result

    def resolve(self, context: "RenderContext") -> Any:
        value = self.value.resolve(context)
        if type(value) is TemplateUndefined:
            return value
        try:
            try:
                return value[self.name]
            except TypeError:
                return getattr(value, self.name)
        except (KeyError, IndexError, AttributeError) as exc:
            return TemplateUndefined(exc)


@dataclass(frozen=True)
class LookupExpression(ResolvableExpression):
    value: Expression
    key: Expression

    def evaluate(self, context: "RenderContext") -> Any:
        value = self.value.evaluate(context)
        result = value[self.key.evaluate(context)]
        if type(result) is TemplateUndefined:
            raise result.error
        return result

    def resolve(self, context: "RenderContext") -> Any:
        value = self.value.resolve(context)
        if type(value) is TemplateUndefined:
            return value
        key = self.key.evaluate(context)
        try:
            return value[key]
        except (KeyError, IndexError, AttributeError) as exc:
            return TemplateUndefined(exc)


@dataclass(frozen=True)
class SliceExpression(Expression):
    start: Expression | None
    stop: Expression | None
    step: Expression | None

    def evaluate(self, context: "RenderContext") -> Any:
        return slice(*(part.evaluate(context) if part else None for part in (self.start, self.stop, self.step)))


@dataclass(frozen=True)
class FilterExpression(ResolvableExpression):
    name: str
    value: Expression
    arguments: tuple[Expression, ...]
    keywords: tuple[tuple[str, Expression], ...]

    def resolve(self, context: "RenderContext") -> Any:
        function = context.filters[self.name]
        value = self.value.resolve(context) if function.handles_undefined else self.value.evaluate(context)
        args = [arg.evaluate(context) for arg in self.arguments]
        kwargs = {name: arg.evaluate(context) for name, arg in self.keywords}
        return context.normalise(function(value, *args, **kwargs), trusted=True)


def template_identifier(value: str) -> str:
    """Validate a public name in the restricted language."""
    if not value.isidentifier() or keyword.iskeyword(value) or value.startswith("_"):
        raise SyntaxError(f"Invalid or private name: {value!r}")
    return value


@cache
def checked_filters(
    filters: tuple[tuple[str, type[TemplateFilter]], ...],
) -> tuple[tuple[str, type[TemplateFilter]], ...]:
    """Check filter names and classes once for one set of pairs.

    The answer depends only on the pairs, so it is cached by them. A refused
    set raises instead of entering the cache, and is refused again next time.
    """
    for name, cls in filters:
        try:
            template_identifier(name)
        except (SyntaxError, AttributeError) as exc:
            raise ValueError(f"Invalid template filter name: {name!r}") from exc
        if not isinstance(cls, type) or not issubclass(cls, TemplateFilter):
            raise TypeError(f"Filter {name!r} must be a TemplateFilter class")
    return filters


DEFAULT_FILTERS = checked_filters(tuple(sorted(TEMPLATE_FILTERS.items())))


SequenceItem = TypeVar("SequenceItem")


class ExpressionParser:
    """Pratt parser for the expression allowlist, including postfix filters."""

    COMPARISON_PRECEDENCE = 40
    CONDITIONAL_PRECEDENCE = 5
    PREFIX_PRECEDENCE = {"not": 35, "+": 95, "-": 95}
    PRECEDENCE = {
        "if": CONDITIONAL_PRECEDENCE,
        "or": 10,
        "and": 20,
        **dict.fromkeys(COMPARISON_OPERATORS, COMPARISON_PRECEDENCE),
        "+": 50,
        "-": 50,
        "*": 60,
        "/": 60,
        "//": 60,
        "%": 60,
        "**": 80,
        "|": 90,
        ".": 100,
        "[": 100,
    }
    LITERALS = {"True": True, "true": True, "False": False, "false": False, "None": None, "none": None}

    def __init__(self, tokens: tuple[CodeToken, ...], filters: Mapping[str, type[TemplateFilter]]) -> None:
        self.tokens = tokens
        self.index = 0
        self.filters = filters

    def peek(self, distance: int = 0) -> str:
        index = self.index + distance
        return self.tokens[index].string if index < len(self.tokens) else ""

    def take(self, expected: str | None = None) -> CodeToken:
        if self.index == len(self.tokens):
            raise SyntaxError("Unexpected end of expression")
        token = self.tokens[self.index]
        if expected is not None and token.string != expected:
            raise SyntaxError(f"Expected {expected!r}, got {token.string!r}")
        self.index += 1
        return token

    def sequence(
        self, closing: str, parse_item: Callable[[], SequenceItem], first: tuple[SequenceItem, ...] = ()
    ) -> tuple[SequenceItem, ...]:
        """Read comma-separated items with an optional trailing comma."""
        items = list(first)
        if not items and self.peek() != closing:
            items.append(parse_item())
        while self.peek() == ",":
            self.take()
            if self.peek() == closing:
                break
            items.append(parse_item())
        if closing:
            self.take(closing)
        return tuple(items)

    def parse(self) -> Expression:
        if not self.peek():
            raise SyntaxError("Unexpected end of expression" if self.index else "Empty template expression")
        expression = self.expression()
        if self.peek() == ",":
            expression = CollectionExpression("tuple", self.sequence("", self.expression, (expression,)))
        self.finish()
        return expression

    def finish(self) -> None:
        if self.peek() == "(":
            raise SyntaxError("Function and method calls are not allowed; use a filter")
        if self.peek():
            raise SyntaxError(f"Unexpected token: {self.peek()!r}")

    def operation(self) -> str:
        op = self.peek()
        if (op, self.peek(1)) in {("not", "in"), ("is", "not")}:
            return op + " " + self.peek(1)
        return op

    def take_operation(self, op: str) -> None:
        for part in op.split():
            self.take(part)

    def expression(self, minimum: int = 0) -> Expression:
        left = self.primary()
        while self.PRECEDENCE.get(op := self.operation(), -1) >= minimum:
            self.take_operation(op)
            left = self.infix(left, op)
        return left

    def primary(self) -> Expression:
        token = self.take()
        text = token.string
        if text in self.PREFIX_PRECEDENCE:
            return UnaryExpression(text, self.expression(self.PREFIX_PRECEDENCE[text]))
        if token.type in {tokenize.NUMBER, tokenize.STRING}:
            value = ast.literal_eval(text)
            if type(value) not in (str, bytes, int, float):
                raise SyntaxError("Unsupported literal")
            while token.type == tokenize.STRING and self.index < len(self.tokens):
                if self.tokens[self.index].type != tokenize.STRING:
                    break
                value += ast.literal_eval(self.take().string)
            return LiteralExpression(value)
        if text in self.LITERALS:
            return LiteralExpression(self.LITERALS[text])
        if text == "(":
            if self.peek() == ")":
                self.take()
                return CollectionExpression("tuple", ())
            first = self.expression()
            if self.peek() == ",":
                return CollectionExpression("tuple", self.sequence(")", self.expression, (first,)))
            self.take(")")
            return first
        if text == "[":
            return CollectionExpression("list", self.sequence("]", self.expression))
        if text == "{":
            return self.dictionary_or_set()
        if token.type == tokenize.NAME:
            return VariableExpression(template_identifier(text))
        raise SyntaxError(f"Unsupported expression token: {text!r}")

    def dictionary_or_set(self) -> Expression:
        if self.peek() == "}":
            self.take()
            return DictionaryExpression(())
        first = self.expression()
        if self.peek() != ":":
            return CollectionExpression("set", self.sequence("}", self.expression, (first,)))
        self.take(":")
        pair = (first, self.expression())

        def parse_pair() -> tuple[Expression, Expression]:
            key = self.expression()
            self.take(":")
            return key, self.expression()

        return DictionaryExpression(self.sequence("}", parse_pair, (pair,)))

    def infix(self, left: Expression, op: str) -> Expression:
        if op == ".":
            return AttributeExpression(left, template_identifier(self.take().string))
        if op == "[":
            return self.subscript(left)
        if op == "|":
            return self.filter_call(left)
        if op == "if":
            condition = self.expression(self.CONDITIONAL_PRECEDENCE + 1)
            self.take("else")
            return ConditionalExpression(condition, left, self.expression(self.CONDITIONAL_PRECEDENCE))
        if op in COMPARISON_OPERATORS:
            operands, operators = [left], []
            while True:
                operators.append(op)
                operands.append(self.expression(self.COMPARISON_PRECEDENCE + 1))
                op = self.operation()
                if op not in COMPARISON_OPERATORS:
                    break
                self.take_operation(op)
            return ComparisonExpression(tuple(operands), tuple(operators))
        precedence = self.PRECEDENCE[op]
        return BinaryExpression(op, left, self.expression(precedence if op == "**" else precedence + 1))

    def subscript(self, left: Expression) -> Expression:
        start = None if self.peek() == ":" else self.expression()
        key: Expression
        if self.peek() == ":":
            self.take()
            stop = None if self.peek() in {":", "]"} else self.expression()
            step = None
            if self.peek() == ":":
                self.take()
                step = None if self.peek() == "]" else self.expression()
            key = SliceExpression(start, stop, step)
        else:
            if start is None:
                raise SyntaxError("Missing subscript")
            key = start
        self.take("]")
        return LookupExpression(left, key)

    def filter_call(self, left: Expression) -> Expression:
        name = template_identifier(self.take().string)
        if name not in self.filters:
            raise SyntaxError(f"Unknown template filter: {name}")
        args: list[Expression] = []
        kwargs: dict[str, Expression] = {}

        def argument() -> None:
            if self.peek(1) == "=":
                key = template_identifier(self.take().string)
                if key in kwargs:
                    raise SyntaxError(f"Duplicate filter argument: {key}")
                self.take("=")
                kwargs[key] = self.expression()
            else:
                if kwargs:
                    raise SyntaxError("Positional argument after keyword argument")
                args.append(self.expression())

        if self.peek() == "(":
            self.take()
            self.sequence(")", argument)
        return FilterExpression(name, left, tuple(args), tuple(kwargs.items()))

    def target(self, until: str) -> "AssignmentTarget":
        parenthesized = self.peek() == "("
        if parenthesized:
            self.take()
        name = template_identifier(self.take().string)
        unpack = self.peek() == ","
        names = self.sequence(
            ")" if parenthesized else until, lambda: template_identifier(self.take().string), (name,)
        )
        if parenthesized:
            self.take(until)
        return AssignmentTarget(names, unpack)

    def assignment(self) -> tuple["AssignmentTarget", Expression]:
        return self.target("="), self.parse()

    def for_header(self) -> tuple["AssignmentTarget", Expression]:
        return self.target("in"), self.parse()


SCALAR_TYPES = frozenset({str, bytes, int, float, bool, type(None), range})
SEQUENCE_TYPES = frozenset({list, tuple, set, frozenset})


class RenderContext:
    """Per-render values, trusted filter instances, and output buffer."""

    def __init__(self, values: Mapping[str, Any], filters: tuple[tuple[str, type[TemplateFilter]], ...]) -> None:
        self.values = dict(values)
        # Names whose value is not checked yet. A template reads a few names of
        # a large context, and a check costs as much as the value is deep, so a
        # value is checked when it is first read and never otherwise. Every
        # value of a frozen snapshot is plain data already.
        self.unchecked = set() if type(values) is DeepMappingProxy else set(self.values)
        self.filters = {name: cls() for name, cls in filters}
        self.output: list[str] = []

    def add(self, values: Mapping[str, Any]) -> None:
        """Add values that still need a check to a context already built."""
        self.values.update(values)
        self.unchecked.update(values)

    def check(self, name: str) -> None:
        """Check one context value, once, before the template reads it."""
        self.unchecked.discard(name)
        self.normalise(self.values[name])

    @staticmethod
    def validate(value: Any, seen: set[int]) -> None:
        """Walk only containers recursively; scalar children need no function call."""
        kind = type(value)
        if kind is not dict and kind is not list:
            if kind is DeepMappingProxy:
                # A frozen snapshot owns plain data only and cannot hold a
                # cycle, so it is accepted whole, without a walk. The test is
                # on identity, before the guard against foreign metaclasses,
                # because an abstract base class is itself such a metaclass.
                return
            if type(kind) is not type:
                raise TypeError("Template values must be plain data; objects and callable values are not allowed")
            if kind in SCALAR_TYPES:
                return
            if kind not in SEQUENCE_TYPES:
                raise TypeError("Template values must be plain data; objects and callable values are not allowed")
        # A container of scalars cannot participate in a cycle. Track its
        # identity only when descending into another container.
        identity = None
        try:
            if kind is dict:
                for key, item in value.items():
                    if type(key) is not str:
                        RenderContext.validate(key, seen)
                    item_type = type(item)
                    if (
                        item_type is str
                        or item_type is int
                        or item_type is bool
                        or (type(item_type) is type and item_type in SCALAR_TYPES)
                    ):
                        continue
                    if identity is None:
                        candidate = id(value)
                        if candidate in seen:
                            raise ValueError("Cyclic template data is not supported")
                        seen.add(candidate)
                        identity = candidate
                    RenderContext.validate(item, seen)
                return
            for item in value:
                item_type = type(item)
                if (
                    item_type is str
                    or item_type is int
                    or item_type is bool
                    or (type(item_type) is type and item_type in SCALAR_TYPES)
                ):
                    continue
                if identity is None:
                    candidate = id(value)
                    if candidate in seen:
                        raise ValueError("Cyclic template data is not supported")
                    seen.add(candidate)
                    identity = candidate
                RenderContext.validate(item, seen)
        finally:
            if identity is not None:
                seen.remove(identity)

    @staticmethod
    def normalise(value: Any, *, trusted: bool = False, seen: set[int] | None = None) -> Any:
        """Validate context without copying; materialize trusted iterator results."""
        kind = type(value)
        # Reject custom metaclasses before set membership can call their hash
        # or equality; never inspect an untrusted object's __class__ property.
        builtin_type = type(kind) is type
        if builtin_type and kind in SCALAR_TYPES:
            return value
        if seen is None:
            seen = set()
        if not trusted:
            RenderContext.validate(value, seen)
            return value
        if id(value) in seen:
            raise ValueError("Cyclic template data is not supported")
        seen.add(id(value))
        try:
            if kind is dict:
                return {
                    RenderContext.normalise(key, trusted=trusted, seen=seen): RenderContext.normalise(
                        item, trusted=trusted, seen=seen
                    )
                    for key, item in value.items()
                }
            if builtin_type and kind in SEQUENCE_TYPES:
                return kind(RenderContext.normalise(item, trusted=trusted, seen=seen) for item in value)
            # Only filter results may execute an iterator protocol. Context
            # values never reach this isinstance check or invoke user code.
            if trusted and isinstance(value, Iterator):
                return tuple(RenderContext.normalise(item, trusted=True, seen=seen) for item in value)
        finally:
            seen.remove(id(value))
        return value

    def invoke(self, nodes: tuple["TemplateNode", ...]) -> None:
        for node in nodes:
            node.invoke(self)


@dataclass(frozen=True)
class TemplateNode:
    """An executable instruction in the immutable template program."""

    position: int

    def invoke(self, context: RenderContext) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class TextNode(TemplateNode):
    text: str

    def invoke(self, context: RenderContext) -> None:
        context.output.append(self.text)


@dataclass(frozen=True)
class OutputNode(TemplateNode):
    expression: Expression

    def invoke(self, context: RenderContext) -> None:
        context.output.append(str(self.expression.evaluate(context)))


@dataclass(frozen=True)
class AssignmentTarget:
    names: tuple[str, ...]
    unpack: bool = False

    def assign(self, context: RenderContext, value: Any) -> None:
        if not self.unpack:
            context.values[self.names[0]] = value
            return
        values = tuple(value)
        if len(values) != len(self.names):
            raise ValueError("Wrong number of values to unpack")
        context.values.update(zip(self.names, values, strict=True))


@dataclass(frozen=True)
class SetNode(TemplateNode):
    target: AssignmentTarget
    expression: Expression

    def invoke(self, context: RenderContext) -> None:
        self.target.assign(context, self.expression.evaluate(context))


@dataclass(frozen=True)
class Branch:
    condition: Expression | None
    body: tuple[TemplateNode, ...]


@dataclass(frozen=True)
class IfNode(TemplateNode):
    branches: tuple[Branch, ...]

    def invoke(self, context: RenderContext) -> None:
        for branch in self.branches:
            if branch.condition is None or branch.condition.evaluate(context):
                context.invoke(branch.body)
                break


class TemplateBreak(Exception):
    """Internal control-flow signal for a loop break."""


class TemplateContinue(Exception):
    """Internal control-flow signal for a loop continue."""


@dataclass(frozen=True)
class BreakNode(TemplateNode):
    def invoke(self, context: RenderContext) -> None:
        raise TemplateBreak


@dataclass(frozen=True)
class ContinueNode(TemplateNode):
    def invoke(self, context: RenderContext) -> None:
        raise TemplateContinue


@dataclass(frozen=True)
class ForNode(TemplateNode):
    target: AssignmentTarget
    iterable: Expression
    body: tuple[TemplateNode, ...]
    otherwise: tuple[TemplateNode, ...]

    def invoke(self, context: RenderContext) -> None:
        for value in self.iterable.evaluate(context):
            self.target.assign(context, value)
            try:
                context.invoke(self.body)
            except TemplateBreak:
                break
            except TemplateContinue:
                continue
        else:
            context.invoke(self.otherwise)


class TemplateParser:
    """Compile typed source tokens into nested executable instructions."""

    def __init__(
        self, source: TemplateSource, tokens: tuple[TemplateToken, ...], filters: Mapping[str, type[TemplateFilter]]
    ) -> None:
        self.source, self.tokens, self.filters = source, tokens, filters
        self.index = 0
        self.loop_depth = 0

    @contextmanager
    def location(self, token: TemplateToken) -> Iterator[None]:
        """Attach source diagnostics once, retaining nested command locations."""
        try:
            yield
        except TemplateSyntaxError:
            raise
        except (SyntaxError, tokenize.TokenError, ValueError, RecursionError) as exc:
            raise self.source.error(str(exc), token.position) from exc

    def expression(self, token: ExpressionToken | CommandToken) -> Expression:
        with self.location(token):
            return ExpressionParser(token.tokens, self.filters).parse()

    TERMINATORS = frozenset({"end", "endif", "endfor"})

    def body(self, stops: frozenset[str] = frozenset()) -> tuple[TemplateNode, ...]:
        nodes: list[TemplateNode] = []
        while self.index < len(self.tokens):
            token = self.tokens[self.index]
            with self.location(token):
                if isinstance(token, CommandToken):
                    if token.name in stops or (stops and token.name in self.TERMINATORS):
                        break
                self.index += 1
                node = self.statement(token)
                if node is not None:
                    nodes.append(node)
        return tuple(nodes)

    def statement(self, token: TemplateToken) -> TemplateNode | None:
        if isinstance(token, TextToken):
            return TextNode(token.position, token.value)
        if isinstance(token, ExpressionToken):
            return OutputNode(token.position, self.expression(token))
        if isinstance(token, CommentToken):
            return None
        assert isinstance(token, CommandToken)
        handlers = {"if": self.condition, "for": self.loop, "set": self.assignment}
        if token.name in handlers:
            return handlers[token.name](token)
        if token.name in {"break", "continue"} and not token.tokens:
            if not self.loop_depth:
                raise SyntaxError(f"{token.name} outside loop")
            return BreakNode(token.position) if token.name == "break" else ContinueNode(token.position)
        if token.name in self.TERMINATORS:
            raise SyntaxError("Unexpected block terminator")
        if token.name in {"elif", "else"}:
            raise SyntaxError(f"Unexpected {token.name}")
        raise SyntaxError(f"Unsupported template command: {token.name}")

    def assignment(self, token: CommandToken) -> SetNode:
        target, expression = ExpressionParser(token.tokens, self.filters).assignment()
        return SetNode(token.position, target, expression)

    def end(self, opening: CommandToken, expected: str) -> None:
        if self.index == len(self.tokens):
            raise self.source.error(f"Unclosed {expected}", opening.position)
        token = self.tokens[self.index]
        assert isinstance(token, CommandToken)
        if token.name not in {"end", "end" + expected}:
            raise self.source.error(f"Expected end{expected}, got {token.name}", token.position)
        if token.tokens:
            raise self.source.error("Unexpected text after block terminator", token.position)
        self.index += 1

    def condition(self, opening: CommandToken) -> IfNode:
        condition: Expression | None = self.expression(opening)
        branches = [Branch(condition, self.body(frozenset({"elif", "else"})))]
        seen_else = False
        while self.index < len(self.tokens):
            token = self.tokens[self.index]
            assert isinstance(token, CommandToken)
            if token.name not in {"elif", "else"}:
                break
            if seen_else:
                raise self.source.error(f"Unexpected {token.name} after else", token.position)
            self.index += 1
            if token.name == "else":
                if token.tokens:
                    raise self.source.error("Unexpected text after else", token.position)
                condition = None
                seen_else = True
            else:
                condition = self.expression(token)
            branches.append(Branch(condition, self.body(frozenset({"elif", "else"}))))
        self.end(opening, "if")
        return IfNode(opening.position, tuple(branches))

    def loop(self, opening: CommandToken) -> ForNode:
        target, iterable = ExpressionParser(opening.tokens, self.filters).for_header()
        self.loop_depth += 1
        body = self.body(frozenset({"else"}))
        self.loop_depth -= 1
        otherwise: tuple[TemplateNode, ...] = ()
        if self.index < len(self.tokens):
            token = self.tokens[self.index]
            assert isinstance(token, CommandToken)
            if token.name == "else":
                if token.tokens:
                    raise self.source.error("Unexpected text after else", token.position)
                self.index += 1
                otherwise = self.body(frozenset({"else"}))
        self.end(opening, "for")
        return ForNode(opening.position, target, iterable, body, otherwise)


def named_filters(nodes: tuple[TemplateNode, ...]) -> frozenset[str]:
    """Collect the filters a program names, by walking its nodes once."""
    found: set[str] = set()
    stack: list[Any] = list(nodes)
    while stack:
        item = stack.pop()
        if type(item) is FilterExpression:
            found.add(item.name)
        if is_dataclass(item) and not isinstance(item, type):
            stack.extend(getattr(item, member.name) for member in fields(item))
        elif type(item) is tuple:
            stack.extend(item)
    return frozenset(found)


@dataclass(frozen=True)
class TemplateProgram:
    """Immutable, pickleable IR. Invocation never parses or compiles source."""

    source: TemplateSource
    nodes: tuple[TemplateNode, ...]
    filters: tuple[tuple[str, type[TemplateFilter]], ...]

    @property
    def active(self) -> tuple[tuple[str, type[TemplateFilter]], ...]:
        """The filters this template names, found once for the program.

        A render builds an instance of each of these and of no other. The whole
        mapping stays in ``filters`` for transfer and for pickle, including a
        program pickled before this answer existed.
        """
        memo: tuple[tuple[str, type[TemplateFilter]], ...] | None = self.__dict__.get("_active")
        if memo is None:
            named = named_filters(self.nodes)
            memo = tuple(item for item in self.filters if item[0] in named)
            object.__setattr__(self, "_active", memo)
        return memo

    def __call__(self, context: Mapping[str, Any] | None = None, /, **values: Any) -> str:
        """Render the program. A frozen snapshot passed whole is not re-checked."""
        render = RenderContext(values if context is None else context, self.active)
        if context is not None and values:
            render.add(values)
        render.invoke(self.nodes)
        return "".join(render.output)


class Template:
    """Compile a restricted template language to reusable intermediate nodes.

    Args:
        template: Jinja-like source with expressions, if/for/set and filters.
        filters: Complete name-to-class mapping; defaults to TEMPLATE_FILTERS.

    Construction tokenizes and compiles the template once. ``program`` exposes
    the resulting immutable :class:`TemplateProgram`. Rendering only invokes
    that program with fresh variable bindings and filter instances. Context
    containers are checked without copying, and a container is checked when the
    template first reads its name, so a value the template never reads costs
    nothing. A whole frozen snapshot, passed as the only positional argument,
    is accepted as it is. Pickle transfers the program and filter classes;
    remote rendering does not reparse the source.

    There are no Python statements, arbitrary calls, or private attributes.
    Context values must be plain data. Registered filters and pickle inputs
    are trusted code; this language is not an OS or resource-isolation sandbox.

        >>> Template("{{ names | unique | sort | join(', ') }}").render(names=["b", "a", "b"])
        'a, b'
        >>> Template("listen {{ port }}{% if ssl %} ssl{% endif %};").render(port=443, ssl=True)
        'listen 443 ssl;'
    """

    def __init__(self, template: str, filters: Mapping[str, type[TemplateFilter]] = TEMPLATE_FILTERS) -> None:
        # The default set is checked and sorted once, at import. Any other
        # mapping is checked by its contents, so a repeated build of the same
        # template does nothing but look up its program.
        pairs = DEFAULT_FILTERS if filters is TEMPLATE_FILTERS else checked_filters(tuple(sorted(filters.items())))
        self.program = self.compile(template, pairs)

    @staticmethod
    @cache
    def compile(
        template: str, filters: tuple[tuple[str, type[TemplateFilter]], ...] = DEFAULT_FILTERS
    ) -> TemplateProgram:
        """Cache compilation by source and filter classes, returning callable IR."""
        source = TemplateSource(template)
        tokens = TemplateLexer(source).tokenize()
        nodes = TemplateParser(source, tokens, dict(filters)).body()
        return TemplateProgram(source, nodes, filters)

    def render(self, context: Mapping[str, Any] | None = None, /, **ctx: Any) -> str:
        """Invoke the compiled program using plain context data.

        Keyword values are checked when the template first reads them. A
        mapping passed as the only positional argument is used as the whole
        context; a frozen snapshot of :func:`rmote.immutable.freeze` is then
        accepted as it is, because it holds plain data by construction.
        """
        return self.program(context, **ctx)

    @classmethod
    def from_program(cls, program: TemplateProgram) -> "Template":
        """Restore an already compiled program without lexing or parsing."""
        instance = cls.__new__(cls)
        instance.program = program
        return instance

    def __reduce__(self) -> tuple[Any, tuple[TemplateProgram]]:
        return self.from_program, (self.program,)

    def __repr__(self) -> str:
        preview = self.program.source.text[:40].replace("\n", "\\n")
        return f"Template({preview!r})"


def render_template(template: str, context: Mapping[str, Any] | None = None, /, **ctx: Any) -> str:
    """Compile a template to IR and render it with the supplied data."""
    return Template(template).render(context, **ctx)


__tool_package__ = "rmote.templates"
