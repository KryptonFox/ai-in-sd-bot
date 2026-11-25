import ast
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

import sympy
from sympy import Expr

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.syntax import (
    EquationSyntax,
    ExpressionSyntax,
    parse_equation,
    parse_expression,
    validate_variable,
)

FUNCTIONS: dict[str, Callable[..., Expr]] = {
    "sin": sympy.sin,
    "cos": sympy.cos,
    "tan": sympy.tan,
    "cot": sympy.cot,
    "sec": sympy.sec,
    "csc": sympy.csc,
    "asin": sympy.asin,
    "acos": sympy.acos,
    "atan": sympy.atan,
    "atan2": sympy.atan2,
    "sinh": sympy.sinh,
    "cosh": sympy.cosh,
    "tanh": sympy.tanh,
    "coth": sympy.coth,
    "sech": sympy.sech,
    "csch": sympy.csch,
    "asinh": sympy.asinh,
    "acosh": sympy.acosh,
    "atanh": sympy.atanh,
    "sqrt": sympy.sqrt,
    "exp": sympy.exp,
    "log": sympy.log,
    "ln": sympy.log,
    "abs": sympy.Abs,
    "floor": sympy.floor,
    "ceil": sympy.ceiling,
    "sign": sympy.sign,
    "factorial": sympy.factorial,
    "gamma": sympy.gamma,
    "min": sympy.Min,
    "max": sympy.Max,
}

CONSTANTS: dict[str, Expr] = {
    "pi": sympy.pi,
    "e": sympy.E,
    "E": sympy.E,
    "oo": sympy.oo,
    "inf": sympy.oo,
}

BINARY_OPERATIONS: dict[type[ast.operator], Callable[[Expr, Expr], Expr]] = {
    ast.Add: lambda left, right: left + right,
    ast.Sub: lambda left, right: left - right,
    ast.Mult: lambda left, right: left * right,
    ast.Div: lambda left, right: left / right,
    ast.Pow: lambda left, right: left**right,
}


class DomainRelation(StrEnum):
    NONZERO = "nonzero"
    POSITIVE = "positive"
    NONNEGATIVE = "nonnegative"
    NEGATIVE = "negative"
    NONPOSITIVE = "nonpositive"


@dataclass(frozen=True)
class DomainConstraint:
    expression: Expr
    relation: DomainRelation
    real_only: bool = False


@dataclass(frozen=True)
class ParsedExpression:
    text: str
    expression: Expr
    original: Expr
    constraints: tuple[DomainConstraint, ...]

    @property
    def free_symbols(self) -> set[sympy.Symbol]:
        symbols: set[sympy.Symbol] = {
            symbol
            for symbol in self.expression.free_symbols | self.original.free_symbols
            if isinstance(symbol, sympy.Symbol)
        }
        for constraint in self.constraints:
            symbols.update(
                symbol
                for symbol in constraint.expression.free_symbols
                if isinstance(symbol, sympy.Symbol)
            )
        return symbols


@dataclass(frozen=True)
class ParsedEquation:
    left: ParsedExpression
    right: ParsedExpression
    normalized: str

    @property
    def difference(self) -> Expr:
        return self.left.expression - self.right.expression

    @property
    def free_symbols(self) -> set[sympy.Symbol]:
        return self.left.free_symbols | self.right.free_symbols


@lru_cache(maxsize=512)
def build_variable(name: str) -> sympy.Symbol:
    return sympy.Symbol(validate_variable(name), real=True)


def build_expression(text: str) -> Expr:
    return build_expression_syntax(parse_expression(text))


def build_expression_syntax(syntax: ExpressionSyntax) -> Expr:
    return _convert_expression(syntax.tree.body, syntax.normalized)


def build_parsed_expression(text: str) -> ParsedExpression:
    return build_parsed_syntax(parse_expression(text))


def build_parsed_syntax(syntax: ExpressionSyntax) -> ParsedExpression:
    return ParsedExpression(
        text=syntax.normalized,
        expression=_convert_expression(syntax.tree.body, syntax.normalized),
        original=_convert_original_expression(syntax.tree.body, syntax.normalized),
        constraints=_collect_constraints(syntax.tree.body, syntax.normalized),
    )


def build_equation(text: str) -> ParsedEquation:
    return build_equation_syntax(parse_equation(text))


def build_equation_syntax(equation: EquationSyntax) -> ParsedEquation:
    left = build_parsed_syntax(equation.left)
    right = build_parsed_syntax(equation.right)
    return ParsedEquation(
        left=left,
        right=right,
        normalized=equation.normalized,
    )


def _convert_expression(node: ast.expr, source: str) -> Expr:
    return _convert(node, source, preserve_structure=False)


def _convert_original_expression(node: ast.expr, source: str) -> Expr:
    return _convert(node, source, preserve_structure=True)


def _convert(node: ast.expr, source: str, *, preserve_structure: bool) -> Expr:
    if isinstance(node, ast.Constant):
        return _constant_expression(node, source)

    if isinstance(node, ast.Name):
        if node.id in CONSTANTS:
            return CONSTANTS[node.id]
        return build_variable(node.id)

    if isinstance(node, ast.BinOp):
        left = _convert(node.left, source, preserve_structure=preserve_structure)
        right = _convert(node.right, source, preserve_structure=preserve_structure)
        return _convert_binary(node.op, left, right, preserve_structure=preserve_structure)

    if isinstance(node, ast.UnaryOp):
        operand = _convert(node.operand, source, preserve_structure=preserve_structure)
        if isinstance(node.op, ast.USub):
            if preserve_structure:
                return sympy.Mul(sympy.Integer(-1), operand, evaluate=False)
            return -operand
        if isinstance(node.op, ast.UAdd):
            return operand
        raise MathError("unsupported_syntax")

    if isinstance(node, ast.Call):
        function = FUNCTIONS.get(node.func.id) if isinstance(node.func, ast.Name) else None
        if function is None:
            raise MathError("unknown_function")
        arguments = [
            _convert(argument, source, preserve_structure=preserve_structure)
            for argument in node.args
        ]
        if preserve_structure and isinstance(node.func, ast.Name) and node.func.id == "sqrt":
            return sympy.Pow(arguments[0], sympy.Rational(1, 2), evaluate=False)
        return function(*arguments)

    raise MathError("unsupported_syntax")


def _convert_binary(
    operation: ast.operator,
    left: Expr,
    right: Expr,
    *,
    preserve_structure: bool,
) -> Expr:
    if not preserve_structure:
        function = BINARY_OPERATIONS.get(type(operation))
        if function is None:
            raise MathError("unsupported_syntax")
        return function(left, right)

    if isinstance(operation, ast.Add):
        return sympy.Add(left, right, evaluate=False)
    if isinstance(operation, ast.Sub):
        return sympy.Add(
            left,
            sympy.Mul(sympy.Integer(-1), right, evaluate=False),
            evaluate=False,
        )
    if isinstance(operation, ast.Mult):
        return sympy.Mul(left, right, evaluate=False)
    if isinstance(operation, ast.Div):
        return sympy.Mul(
            left,
            sympy.Pow(right, sympy.Integer(-1), evaluate=False),
            evaluate=False,
        )
    if isinstance(operation, ast.Pow):
        return sympy.Pow(left, right, evaluate=False)
    raise MathError("unsupported_syntax")


def _constant_expression(node: ast.Constant, source: str) -> Expr:
    if type(node.value) is int:
        return sympy.Integer(node.value)
    if type(node.value) is float:
        rational = _constant_rational(node, source)
        if rational is None:
            raise MathError("invalid_expression")
        return rational
    raise MathError("unsupported_syntax")


def _constant_rational(
    node: ast.Constant,
    source: str,
) -> sympy.Rational | None:
    if type(node.value) is int:
        return sympy.Rational(node.value)
    if type(node.value) is not float:
        return None
    token = ast.get_source_segment(source, node)
    if token is None:
        return None
    try:
        return sympy.Rational(token.replace("_", ""))
    except (TypeError, ValueError):
        return None


def _collect_constraints(node: ast.expr, source: str) -> tuple[DomainConstraint, ...]:
    constraints: list[DomainConstraint] = []
    _visit_constraints(node, source, constraints)
    return _deduplicate_constraints(constraints)


def _visit_constraints(
    node: ast.expr,
    source: str,
    constraints: list[DomainConstraint],
) -> None:
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Div):
            constraints.append(
                DomainConstraint(
                    _convert_expression(node.right, source),
                    DomainRelation.NONZERO,
                )
            )
        elif isinstance(node.op, ast.Pow):
            _add_power_constraints(node, source, constraints)

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        _add_function_constraints(node.func.id, node.args, source, constraints)

    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.expr):
            _visit_constraints(child, source, constraints)


def _add_power_constraints(
    node: ast.BinOp,
    source: str,
    constraints: list[DomainConstraint],
) -> None:
    exponent = _numeric_exponent(node.right, source)
    if exponent is None:
        return
    base = _convert_expression(node.left, source)
    if exponent.is_negative:
        constraints.append(DomainConstraint(base, DomainRelation.NONZERO))
    if exponent.is_rational and exponent.q % 2 == 0:
        if exponent.is_negative:
            constraints.append(DomainConstraint(base, DomainRelation.POSITIVE, real_only=True))
        else:
            constraints.append(DomainConstraint(base, DomainRelation.NONNEGATIVE, real_only=True))


def _add_function_constraints(
    name: str,
    arguments: list[ast.expr],
    source: str,
    constraints: list[DomainConstraint],
) -> None:
    if not arguments:
        return
    argument = _convert_expression(arguments[0], source)

    if name == "sqrt":
        constraints.append(DomainConstraint(argument, DomainRelation.NONNEGATIVE, real_only=True))
    elif name in {"log", "ln"}:
        constraints.append(DomainConstraint(argument, DomainRelation.NONZERO))
        constraints.append(DomainConstraint(argument, DomainRelation.POSITIVE, real_only=True))
        if len(arguments) == 2:
            base = _convert_expression(arguments[1], source)
            constraints.append(DomainConstraint(base, DomainRelation.NONZERO))
            constraints.append(DomainConstraint(base - 1, DomainRelation.NONZERO))
            constraints.append(DomainConstraint(base, DomainRelation.POSITIVE, real_only=True))
    elif name in {"tan", "sec"}:
        constraints.append(DomainConstraint(sympy.cos(argument), DomainRelation.NONZERO))
    elif name in {"cot", "csc"}:
        constraints.append(DomainConstraint(sympy.sin(argument), DomainRelation.NONZERO))
    elif name in {"asin", "acos"}:
        constraints.append(
            DomainConstraint(argument + 1, DomainRelation.NONNEGATIVE, real_only=True)
        )
        constraints.append(
            DomainConstraint(1 - argument, DomainRelation.NONNEGATIVE, real_only=True)
        )
    elif name == "acosh":
        constraints.append(
            DomainConstraint(argument - 1, DomainRelation.NONNEGATIVE, real_only=True)
        )
    elif name == "atanh":
        constraints.append(DomainConstraint(argument + 1, DomainRelation.NONZERO))
        constraints.append(DomainConstraint(1 - argument, DomainRelation.NONZERO))
        constraints.append(DomainConstraint(argument + 1, DomainRelation.POSITIVE, real_only=True))
        constraints.append(DomainConstraint(1 - argument, DomainRelation.POSITIVE, real_only=True))
    elif name == "atan2" and len(arguments) == 2:
        second = _convert_expression(arguments[1], source)
        constraints.append(DomainConstraint(argument**2 + second**2, DomainRelation.NONZERO))
    elif name == "factorial":
        constraints.append(DomainConstraint(argument, DomainRelation.NONNEGATIVE, real_only=True))


def _numeric_exponent(node: ast.expr, source: str) -> sympy.Rational | None:
    if isinstance(node, ast.Constant):
        return _constant_rational(node, source)

    if isinstance(node, ast.UnaryOp):
        value = _numeric_exponent(node.operand, source)
        if value is None:
            return None
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.UAdd):
            return value
        return None

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _numeric_exponent(node.left, source)
        right = _numeric_exponent(node.right, source)
        if left is None or right is None or right == 0:
            return None
        return left / right

    return None


def _deduplicate_constraints(
    constraints: list[DomainConstraint],
) -> tuple[DomainConstraint, ...]:
    result: list[DomainConstraint] = []
    seen: set[tuple[str, str, bool]] = set()
    for constraint in constraints:
        key = (
            sympy.sstr(constraint.expression),
            constraint.relation.value,
            constraint.real_only,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(constraint)
    return tuple(result)
