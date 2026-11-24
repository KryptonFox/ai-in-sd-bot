import math

import pytest

from mcp_servers.math_tools.engine import handle_request
from mcp_servers.math_tools.errors import MathError


def calculate(expression: str | None = None, operation: str = "simplify", **extra: object):
    payload: dict[str, object] = {
        "operation": "calculate",
        "calculation": operation,
        "variable": extra.pop("variable", "x"),
        **extra,
    }
    if expression is not None:
        payload["expression"] = expression
    return handle_request(payload)


def test_improper_integral_is_exact_and_convergent() -> None:
    result = calculate(
        "log(1+x^2)/(1+x^2)",
        "integrate",
        lower=0,
        upper="oo",
    )

    assert result["status"] == "ok"
    assert result["result"] == "pi*log(2)"
    assert result["convergence"] == "convergent"
    assert float(result["numeric"]) == pytest.approx(math.pi * math.log(2))
    assert result["exact"] is True


def test_divergent_integral_is_classified() -> None:
    result = calculate("1/x", "integrate", lower=1, upper="oo")

    assert result["status"] == "ok"
    assert result["result"] == "oo"
    assert result["convergence"] == "divergent"
    assert result["numeric"] == ""


def test_integral_without_symbolic_family_is_not_guessed() -> None:
    result = calculate("log(1+x^3)/(1+x^2)", "integrate", lower=0, upper="oo")

    assert result["status"] == "undefined"
    assert result["result"] == ""
    assert result["numeric"] == ""


def test_limit_with_cancellation_and_missing_two_sided_limit() -> None:
    cancelled = calculate("(x^2-1)/(x-1)", "limit", point=1, direction="both")
    assert cancelled["status"] == "ok"
    assert cancelled["result"] == "2"

    missing = calculate("abs(x)/x", "limit", point=0, direction="both")
    assert missing["status"] == "undefined"
    assert "односторонние" in str(missing["detail"])


def test_series_marks_absolute_and_conditional_convergence() -> None:
    absolute = calculate("1/n^2", "series", variable="n")
    assert absolute["status"] == "ok"
    assert absolute["result"] == "pi**2/6"
    assert absolute["convergence"] == "convergent"
    assert absolute["absolute_convergence"] == "convergent"
    assert absolute["conditional"] is False

    conditional = calculate("(-1)^(n-1)/n", "series", variable="n")
    assert conditional["status"] == "ok"
    assert conditional["result"] == "log(2)"
    assert conditional["convergence"] == "convergent"
    assert conditional["absolute_convergence"] == "divergent"
    assert conditional["conditional"] is True

    divergent = calculate("1/n", "series", variable="n")
    assert divergent["status"] == "ok"
    assert divergent["result"] == ""
    assert divergent["convergence"] == "divergent"


def test_series_rejects_non_integer_bounds() -> None:
    with pytest.raises(MathError) as captured:
        calculate("1/n^2", "series", variable="n", lower="0.5")
    assert captured.value.code == "invalid_arguments"


def test_partial_derivatives_are_structured() -> None:
    result = calculate(
        "x^2*y+y^3",
        "partial_diff",
        variables=["x", "y"],
    )

    assert result["status"] == "ok"
    assert result["variables"] == ["x", "y"]
    assert [partial["variable"] for partial in result["partials"]] == ["x", "y"]
    assert [partial["exact"] for partial in result["partials"]] == [
        "2*x*y",
        "x**2 + 3*y**2",
    ]
    assert result["numeric"] == ""


def test_jacobian_matrix_is_returned_exactly() -> None:
    result = calculate(
        operation="jacobian",
        functions=["x^2*y", "x+y"],
        variables=["x", "y"],
    )

    assert result["status"] == "ok"
    assert result["jacobian"] == [["2*x*y", "x**2"], ["1", "1"]]
    assert result["variables"] == ["x", "y"]


def test_constrained_extrema_on_circle_are_verified() -> None:
    result = calculate(
        "x*y",
        "constrained_extrema",
        variables=["x", "y"],
        constraints=["x^2+y^2=1"],
    )

    assert result["status"] == "ok"
    assert result["constraint"] == "x**2+y**2 = 1"
    extrema = result["extrema"]
    assert len(extrema) == 4
    values = {extreme["value"] for extreme in extrema}
    assert values == {"-1/2", "1/2"}

    for extreme in extrema:
        numeric_value = float(extreme["numeric_value"])
        assert numeric_value == pytest.approx(-0.5) or numeric_value == pytest.approx(0.5)
        point = extreme["point"]
        x_value = float(point["x"].replace("sqrt(2)/2", "0.7071067811865476"))
        y_value = float(point["y"].replace("sqrt(2)/2", "0.7071067811865476"))
        assert x_value**2 + y_value**2 == pytest.approx(1.0)
        if numeric_value < 0:
            assert extreme["kind"] == "minimum"
        else:
            assert extreme["kind"] == "maximum"


def test_constrained_extrema_report_no_solution_for_impossible_constraint() -> None:
    result = calculate(
        "x*y",
        "constrained_extrema",
        variables=["x", "y"],
        constraints=["x^2+y^2=-1"],
    )

    assert result["status"] == "no_solution"
    assert result["extrema"] == []
