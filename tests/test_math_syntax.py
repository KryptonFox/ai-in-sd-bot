import pytest

from mcp_servers.math_tools import expressions as expressions_module
from mcp_servers.math_tools import syntax as syntax_module
from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.limits import MAX_AST_NODES, MAX_EXPRESSION_LENGTH
from mcp_servers.math_tools.syntax import (
    equation_sides,
    equation_variables,
    expression_variables,
    normalize_expression,
    validate_variable,
)


def error_code(callable_object, *args) -> str:
    with pytest.raises(MathError) as captured:
        callable_object(*args)
    return captured.value.code


def test_normalizes_power_and_preserves_expression_text() -> None:
    assert normalize_expression("x^2 + 2*x + 1") == "x**2 + 2*x + 1"
    assert normalize_expression("(a - b) / 2") == "(a - b) / 2"
    assert normalize_expression("sin(pi / 2)") == "sin(pi / 2)"


def test_rejects_python_injection_and_unknown_syntax() -> None:
    attacks = (
        "__import__('os').system('id')",
        "().__class__.__mro__",
        "x.__dict__",
        "[x for x in [1]]",
        "lambda x: x",
        "x if x else 0",
        "x == 1",
        "x.real",
        "open('/etc/passwd')",
        "x[0]",
        "True",
        "1j",
        "π",
        "x; y",
    )
    for attack in attacks:
        assert error_code(normalize_expression, attack) == "unsupported_syntax", attack
    assert error_code(normalize_expression, "1e999") == "invalid_expression"


def test_rejects_unknown_functions_and_wrong_arity() -> None:
    assert error_code(normalize_expression, "danger(x)") == "unknown_function"
    assert error_code(normalize_expression, "sin()") == "invalid_arguments"
    assert error_code(normalize_expression, "sin(x, x)") == "invalid_arguments"
    assert error_code(normalize_expression, "sin(x=1)") == "unsupported_syntax"


def test_enforces_expression_character_limit() -> None:
    maximum = "x" + " " * (MAX_EXPRESSION_LENGTH - 1)
    assert normalize_expression(maximum).strip() == "x"
    assert error_code(normalize_expression, maximum + " ") == "expression_too_long"


def test_enforces_ast_node_limit() -> None:
    valid = "+".join(["x"] * 20)
    assert normalize_expression(valid)
    overly_complex = "+".join(["x"] * 60)
    assert error_code(normalize_expression, overly_complex) == "expression_too_complex"


def test_reports_invalid_python_syntax_as_invalid_expression() -> None:
    assert error_code(normalize_expression, "") == "invalid_expression"
    assert error_code(normalize_expression, "x +") == "invalid_expression"
    assert error_code(normalize_expression, "(x") == "invalid_expression"
    assert error_code(normalize_expression, "2^^3") == "unsupported_syntax"


def test_extracts_and_validates_variables() -> None:
    assert expression_variables("x^2 + y + sin(x) + pi") == ("x", "y")
    assert expression_variables("sqrt(2) + E") == ()
    assert validate_variable(" t ") == "t"
    assert error_code(validate_variable, "pi") == "invalid_variable"
    assert error_code(validate_variable, "sin") == "invalid_variable"
    assert error_code(validate_variable, "lambda") == "invalid_variable"
    assert error_code(validate_variable, "2x") == "invalid_variable"
    assert error_code(validate_variable, "__x") == "invalid_variable"


def test_equation_sides_support_equality_and_zero_form() -> None:
    assert equation_sides("x^2 = 4") == ("x**2", "4")
    assert equation_sides("x - 1") == ("x - 1", "0")
    assert equation_variables("x + y = 1") == ("x", "y")
    assert error_code(equation_sides, "x = y = 1") == "invalid_expression"
    assert error_code(equation_sides, "= 1") == "invalid_expression"
    assert error_code(equation_sides, "x = ") == "invalid_expression"


def test_equation_uses_shared_node_budget() -> None:
    left = "+".join(["x"] * 30)
    right = "+".join(["y"] * 30)
    assert error_code(equation_sides, f"{left} = {right}") == "expression_too_complex"
    assert MAX_AST_NODES == 128


def test_parse_expression_keeps_source_and_normalized_forms() -> None:
    parsed = syntax_module.parse_expression("  x^2 + 1  ")
    assert parsed.text == "x^2 + 1"
    assert parsed.normalized == "x**2 + 1"
    assert parsed.variables == ("x",)


def test_equation_variables_parse_each_side_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    original = syntax_module._parse_expression

    def counting(text: str):
        nonlocal calls
        calls += 1
        return original(text)

    monkeypatch.setattr(syntax_module, "_parse_expression", counting)

    assert equation_variables("x^2 + y = 1") == ("x", "y")
    assert calls == 2


def test_build_parsed_expression_reuses_parsed_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    original = syntax_module._parse_expression

    def counting(text: str):
        nonlocal calls
        calls += 1
        return original(text)

    monkeypatch.setattr(syntax_module, "_parse_expression", counting)

    parsed = expressions_module.build_parsed_expression("x^2 + 1")
    assert parsed.text == "x**2 + 1"
    assert calls == 1
