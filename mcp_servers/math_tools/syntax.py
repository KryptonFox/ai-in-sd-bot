import ast
import math
import re

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.limits import MAX_AST_NODES, MAX_EXPRESSION_LENGTH

FUNCTION_ARITIES: dict[str, frozenset[int]] = {
    "sin": frozenset({1}),
    "cos": frozenset({1}),
    "tan": frozenset({1}),
    "cot": frozenset({1}),
    "sec": frozenset({1}),
    "csc": frozenset({1}),
    "asin": frozenset({1}),
    "acos": frozenset({1}),
    "atan": frozenset({1}),
    "atan2": frozenset({2}),
    "sinh": frozenset({1}),
    "cosh": frozenset({1}),
    "tanh": frozenset({1}),
    "coth": frozenset({1}),
    "sech": frozenset({1}),
    "csch": frozenset({1}),
    "asinh": frozenset({1}),
    "acosh": frozenset({1}),
    "atanh": frozenset({1}),
    "sqrt": frozenset({1}),
    "exp": frozenset({1}),
    "log": frozenset({1, 2}),
    "ln": frozenset({1}),
    "abs": frozenset({1}),
    "floor": frozenset({1}),
    "ceil": frozenset({1}),
    "sign": frozenset({1}),
    "factorial": frozenset({1}),
    "gamma": frozenset({1}),
    "min": frozenset({1, 2, 3, 4}),
    "max": frozenset({1, 2, 3, 4}),
}
CONSTANT_NAMES = frozenset({"pi", "e", "E", "oo", "inf"})
RESERVED_NAMES = CONSTANT_NAMES | frozenset({"I", "nan", "zoo"})
VARIABLE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
ALLOWED_CHARACTERS = re.compile(r"^[0-9A-Za-z_+\-*/^().,\s]+$")
CARET_PATTERN = re.compile(r"(?<!\*)\^(?!\*)")
MAX_VARIABLE_LENGTH = 128

_ALLOWED_NODE_TYPES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Load,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Pow,
    ast.USub,
    ast.UAdd,
)


def _count_nodes(tree: ast.Expression) -> int:
    return sum(1 for _ in ast.walk(tree))


def _invalid(code: str = "invalid_expression") -> MathError:
    return MathError(code)


def _parse_expression(text: str) -> tuple[ast.Expression, str]:
    if not isinstance(text, str):
        raise _invalid()
    if len(text) > MAX_EXPRESSION_LENGTH:
        raise MathError("expression_too_long")

    stripped = text.strip()
    if not stripped:
        raise _invalid()
    if not stripped.isascii() or not ALLOWED_CHARACTERS.fullmatch(stripped):
        raise MathError("unsupported_syntax")

    normalized = CARET_PATTERN.sub("**", stripped)
    if "^" in normalized or "****" in normalized:
        raise MathError("unsupported_syntax")

    try:
        tree = ast.parse(normalized, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        raise _invalid() from None

    if _count_nodes(tree) > MAX_AST_NODES:
        raise MathError("expression_too_complex")

    _validate_nodes(tree)
    return tree, normalized


def _validate_nodes(tree: ast.Expression) -> None:
    call_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    for node in ast.walk(tree):
        if isinstance(node, _ALLOWED_NODE_TYPES):
            continue

        if isinstance(node, ast.Constant):
            if type(node.value) not in (int, float):
                raise MathError("unsupported_syntax")
            if isinstance(node.value, float) and not math.isfinite(node.value):
                raise _invalid()
            continue

        if isinstance(node, ast.Name):
            if node.id in call_names:
                continue
            if node.id in CONSTANT_NAMES:
                continue
            if node.id in RESERVED_NAMES or node.id in FUNCTION_ARITIES:
                raise MathError("unsupported_syntax")
            if not VARIABLE_PATTERN.fullmatch(node.id):
                raise MathError("unsupported_syntax")
            continue

        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise MathError("unsupported_syntax")
            if node.keywords:
                raise MathError("unsupported_syntax")
            allowed_arities = FUNCTION_ARITIES.get(node.func.id)
            if allowed_arities is None:
                raise MathError("unknown_function")
            if len(node.args) not in allowed_arities:
                raise MathError("invalid_arguments")
            continue

        raise MathError("unsupported_syntax")


def normalize_expression(text: str) -> str:
    _, normalized = _parse_expression(text)
    return normalized


def expression_variables(text: str) -> tuple[str, ...]:
    tree, _ = _parse_expression(text)
    call_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    variables = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id not in call_names
        and node.id not in CONSTANT_NAMES
    }
    return tuple(sorted(variables))


def validate_variable(name: str) -> str:
    if not isinstance(name, str):
        raise MathError("invalid_variable")
    stripped = name.strip()
    if (
        not stripped
        or len(stripped) > MAX_VARIABLE_LENGTH
        or not stripped.isascii()
        or not VARIABLE_PATTERN.fullmatch(stripped)
        or stripped in RESERVED_NAMES
        or stripped in FUNCTION_ARITIES
    ):
        raise MathError("invalid_variable")
    return stripped


def equation_sides(text: str) -> tuple[str, str]:
    if not isinstance(text, str):
        raise _invalid()
    if len(text) > MAX_EXPRESSION_LENGTH:
        raise MathError("expression_too_long")

    stripped = text.strip()
    if not stripped:
        raise _invalid()

    parts = stripped.split("=")
    if len(parts) > 2:
        raise _invalid()
    left_text = parts[0].strip()
    right_text = parts[1].strip() if len(parts) == 2 else "0"
    if not left_text or not right_text:
        raise _invalid()

    left_tree, left_normalized = _parse_expression(left_text)
    right_tree, right_normalized = _parse_expression(right_text)
    if _count_nodes(left_tree) + _count_nodes(right_tree) > MAX_AST_NODES:
        raise MathError("expression_too_complex")
    return left_normalized, right_normalized


def equation_variables(text: str) -> tuple[str, ...]:
    left, right = equation_sides(text)
    variables = set(expression_variables(left)) | set(expression_variables(right))
    return tuple(sorted(variables))
