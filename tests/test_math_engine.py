import base64
import io
import json
from collections.abc import Mapping
from enum import StrEnum
from fractions import Fraction
from typing import cast

import pytest
from PIL import Image

from mcp_servers.math_tools.engine import handle_request
from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.limits import MAX_PNG_BYTES
from mcp_servers.math_tools.schemas import (
    CalculateOperation,
    CalculateStatus,
    LimitDirection,
    MathCalculateResult,
    PlotStatus,
    SolveStatus,
)


def mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, dict)
    return cast(Mapping[str, object], value)


def sequence(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def text(value: object) -> str:
    assert isinstance(value, str)
    return value


def number(value: object) -> float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


def calculate(expression: str, operation: str = "simplify", **extra: object) -> dict[str, object]:
    return handle_request(
        {
            "operation": "calculate",
            "calculation": operation,
            "expression": expression,
            "variable": "x",
            **extra,
        }
    )


def solve(equations: list[str], **extra: object) -> dict[str, object]:
    return handle_request(
        {
            "operation": "solve",
            "equations": equations,
            "variables": None,
            **extra,
        }
    )


def test_closed_math_values_are_strenums_with_stable_json_values() -> None:
    for enum_type in (
        CalculateOperation,
        LimitDirection,
        CalculateStatus,
        SolveStatus,
        PlotStatus,
    ):
        assert issubclass(enum_type, StrEnum)
        for member in enum_type:
            assert str(member) == member.value
            assert json.loads(json.dumps(member)) == member.value

    result = MathCalculateResult.model_validate(
        {
            "status": "ok",
            "operation": "numeric",
            "expression": "sqrt(2)",
            "variable": "x",
            "result": "1.41421356237310",
            "numeric": "1.41421356237310",
            "exact": False,
            "detail": "ok",
        }
    )
    assert result.status is CalculateStatus.OK
    assert result.operation is CalculateOperation.NUMERIC
    assert json.loads(result.model_dump_json())["status"] == "ok"


def test_calculate_preserves_decimal_literal_digits() -> None:
    literal = "0.12345678901234567890123456789"
    expected = Fraction(literal)
    result = calculate(literal, "evaluate")
    assert result["status"] == "ok"
    assert result["result"] == f"{expected.numerator}/{expected.denominator}"


def test_calculate_preserves_scientific_notation_literal_digits() -> None:
    literal = "1.2345678901234567890123456789e-12"
    expected = Fraction(literal)
    result = calculate(literal, "evaluate")
    assert result["status"] == "ok"
    assert result["result"] == f"{expected.numerator}/{expected.denominator}"


def test_simplifies_and_evaluates_exactly() -> None:
    simplified = calculate("(x^2 - 1) / (x - 1)")
    assert simplified["status"] == "ok"
    assert simplified["result"] == "x + 1"
    assert simplified["exact"] is True

    exact = calculate("sqrt(8)", "evaluate")
    assert exact["status"] == "ok"
    assert exact["result"] == "2*sqrt(2)"
    assert text(exact["numeric"]).startswith("2.8284271247461")


def test_numeric_calculation_uses_fifteen_significant_digits() -> None:
    numeric = calculate("sqrt(2)", "numeric")
    assert numeric["status"] == "ok"
    assert numeric["exact"] is False
    numeric_result = text(numeric["result"])
    assert numeric_result.startswith("1.4142135623731")
    assert len(numeric_result.replace(".", "").lstrip("-")) >= 15


def test_derivative_integral_and_limit() -> None:
    derivative = calculate("x^3", "diff")
    assert derivative["result"] == "3*x**2"

    indefinite = calculate("x^2", "integrate")
    assert indefinite["result"] == "x**3/3"

    definite = calculate("x^2", "integrate", lower=0, upper=1)
    assert definite["result"] == "1/3"
    assert text(definite["numeric"]).startswith("0.333333333333")

    limit = calculate("sin(x)/x", "limit", point="0")
    assert limit["result"] == "1"

    infinite_limit = calculate("1/x", "limit", point="oo")
    assert infinite_limit["result"] == "0"


def test_calculate_reports_undefined_values() -> None:
    undefined = calculate("1/0")
    assert undefined["status"] == "undefined"
    assert undefined["result"] == ""
    assert "не определён" in str(undefined["detail"])

    oscillation = calculate("sin(x)", "limit", point="oo")
    assert oscillation["status"] == "undefined"
    assert "колеблются" in str(oscillation["detail"])


def test_calculate_rejects_invalid_arguments_and_syntax() -> None:
    with pytest.raises(MathError) as captured:
        calculate("x^2", "diff", order=0)
    assert captured.value.code == "invalid_arguments"

    with pytest.raises(MathError) as captured:
        calculate("sin(x)", "integrate", lower="0")
    assert captured.value.code == "invalid_arguments"

    with pytest.raises(MathError) as captured:
        calculate("__import__('os')")
    assert captured.value.code == "unsupported_syntax"


def test_solves_quadratic_equation_with_verification() -> None:
    result = solve(["x^2 = 4"])
    assert result["status"] == "solutions"
    assert result["verified"] is True
    assert result["solutions"] == [{"x": "-2"}, {"x": "2"}]
    assert result["variables"] == ["x"]


def test_solves_system_of_three_equations() -> None:
    result = solve(["x + y + z = 6", "x - y = 0", "z = 2"], variables=["x", "y", "z"])
    assert result["status"] == "solutions"
    assert result["solutions"] == [{"x": "2", "y": "2", "z": "2"}]
    assert result["verified"] is True


def test_solve_distinguishes_no_solution_and_infinite_solutions() -> None:
    no_real_solution = solve(["x^2 + 1 = 0"])
    assert no_real_solution["status"] == "no_solution"
    assert no_real_solution["solutions"] == []

    singular = solve(["1/x = 0"])
    assert singular["status"] == "no_solution"

    infinite = solve(["x + y = 1"])
    assert infinite["status"] == "infinite_solutions"

    periodic = solve(["sin(x) = 0"])
    assert periodic["status"] == "infinite_solutions"


def test_solve_rejects_candidates_outside_domain() -> None:
    result = solve(["sqrt(x - 2) = x - 3"])
    assert result["status"] == "solutions"
    assert len(sequence(result["solutions"])) == 1
    numeric = mapping(sequence(result["numeric_solutions"])[0])
    assert float(text(numeric["x"])) == pytest.approx(4.618033988749895)

    rational = solve(["(x^2 - 1) / (x - 1) = 0"])
    assert rational["solutions"] == [{"x": "-1"}]

    impossible_root = solve(["sqrt(x) = -1"])
    assert impossible_root["status"] == "no_solution"


def test_solve_can_return_complex_solutions_on_request() -> None:
    result = solve(["x^2 + 1 = 0"], real_only=False)
    assert result["status"] == "solutions"
    values = {text(mapping(item)["x"]) for item in sequence(result["solutions"])}
    assert values == {"-I", "I"}
    assert result["real_only"] is False


def test_solve_rejects_candidates_erased_by_cancellation() -> None:
    rational = solve(["(x - 1) / (x - 1) = x"])
    assert rational["status"] == "no_solution"
    assert rational["solutions"] == []
    assert rational["discarded"] == [{"x": "1"}]
    assert rational["verified"] is False

    square_root = solve(["sqrt(x) * sqrt(x) = -1"])
    assert square_root["status"] == "no_solution"
    assert square_root["discarded"] == [{"x": "-1"}]

    logarithm = solve(["log(x) - log(x) = x"])
    assert logarithm["status"] == "no_solution"
    assert logarithm["discarded"] == [{"x": "0"}]


def test_solve_keeps_small_positive_and_nonzero_exact_values() -> None:
    logarithm = solve(["log(x) = log(1e-12)"])
    assert logarithm["status"] == "solutions"
    assert logarithm["solutions"] == [{"x": "1/1000000000000"}]
    assert logarithm["verified"] is True

    reciprocal = solve(["1/x = 1000000000000"])
    assert reciprocal["status"] == "solutions"
    assert reciprocal["solutions"] == [{"x": "1/1000000000000"}]
    assert reciprocal["verified"] is True


def test_plot_keeps_cancelled_domain_holes() -> None:
    rational = handle_request(
        {
            "operation": "plot",
            "functions": ["(x - 1) / (x - 1)"],
            "variable": "x",
            "x_min": -10,
            "x_max": 10,
        }
    )
    rational_series = mapping(sequence(mapping(rational["result"])["functions"])[0])
    assert rational_series["segments"] == 2
    assert "1" in sequence(rational_series["discontinuities"])

    square_root = handle_request(
        {
            "operation": "plot",
            "functions": ["sqrt(x) * sqrt(x)"],
            "variable": "x",
            "x_min": -10,
            "x_max": 10,
        }
    )
    root_series = mapping(sequence(mapping(square_root["result"])["functions"])[0])
    assert root_series["segments"] == 1
    assert sequence(root_series["discontinuities"])


def test_solve_preserves_high_precision_literals_in_domain_trees() -> None:
    tiny = Fraction("1.2345678901234567890123456789e-12")
    logarithm = solve(["log(x) = log(1.2345678901234567890123456789e-12)"])
    assert logarithm["status"] == "solutions"
    assert logarithm["solutions"] == [{"x": f"{tiny.numerator}/{tiny.denominator}"}]
    assert logarithm["verified"] is True

    root = Fraction("0.12345678901234567890123456789")
    squared = root * root
    square_root = solve(["sqrt(x) = 0.12345678901234567890123456789"])
    assert square_root["status"] == "solutions"
    assert square_root["solutions"] == [{"x": f"{squared.numerator}/{squared.denominator}"}]
    assert square_root["verified"] is True


def test_solve_requires_all_unknowns_when_variables_are_given() -> None:
    with pytest.raises(MathError) as captured:
        solve(["x + y = 1"], variables=["x"])
    assert captured.value.code == "invalid_arguments"


def test_plot_splits_pole_of_reciprocal_function() -> None:
    result = handle_request(
        {
            "operation": "plot",
            "functions": ["1/x"],
            "variable": "x",
            "x_min": -10,
            "x_max": 10,
        }
    )
    metadata = mapping(result["result"])
    assert metadata["status"] == "ok"
    first_series = mapping(sequence(metadata["functions"])[0])
    assert first_series["segments"] == 2
    assert "0" in sequence(first_series["discontinuities"])

    png = base64.b64decode(text(result["png_base64"]))
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) <= MAX_PNG_BYTES
    assert metadata["png_bytes"] == len(png)

    with Image.open(io.BytesIO(png)) as image:
        assert image.format == "PNG"
        image.load()
        assert image.width > 0
        assert image.height > 0


def test_plot_breaks_domain_gap_and_trigonometric_poles() -> None:
    square_root = handle_request(
        {
            "operation": "plot",
            "functions": ["sqrt(x)"],
            "variable": "x",
            "x_min": -10,
            "x_max": 10,
        }
    )
    root_series = mapping(sequence(mapping(square_root["result"])["functions"])[0])
    assert root_series["segments"] == 1
    assert sequence(root_series["discontinuities"])

    tangent = handle_request(
        {
            "operation": "plot",
            "functions": ["tan(x)"],
            "variable": "x",
            "x_min": -10,
            "x_max": 10,
        }
    )
    tangent_series = mapping(sequence(mapping(tangent["result"])["functions"])[0])
    assert number(tangent_series["segments"]) > 2
    assert len(sequence(tangent_series["discontinuities"])) >= 6


def test_plot_four_functions_fits_image_limit() -> None:
    result = handle_request(
        {
            "operation": "plot",
            "functions": ["sin(x)", "cos(x)", "x^2", "sqrt(x)"],
            "variable": "x",
            "x_min": -10,
            "x_max": 10,
        }
    )
    png = base64.b64decode(text(result["png_base64"]))
    assert len(png) <= MAX_PNG_BYTES
    assert len(sequence(mapping(result["result"])["functions"])) == 4


def test_plot_rejects_foreign_symbols_and_invalid_range() -> None:
    with pytest.raises(MathError) as captured:
        handle_request(
            {
                "operation": "plot",
                "functions": ["x + y"],
                "variable": "x",
                "x_min": -10,
                "x_max": 10,
            }
        )
    assert captured.value.code == "invalid_expression"

    with pytest.raises(MathError) as captured:
        handle_request(
            {
                "operation": "plot",
                "functions": ["x"],
                "variable": "x",
                "x_min": 10,
                "x_max": 10,
            }
        )
    assert captured.value.code == "invalid_arguments"
