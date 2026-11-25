import io
from collections.abc import Mapping

import numpy as np
import pytest
from PIL import Image

from mcp_servers.math_tools.engine import handle_request
from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.expressions import build_parsed_expression, build_variable
from mcp_servers.math_tools.limits import MAX_PNG_BYTES, PLOT_SAMPLES
from mcp_servers.math_tools.plotting import render_contour, render_curve
from mcp_servers.math_tools.plotting.sampling import magnitude_mask
from mcp_servers.math_tools.schemas import CheckpointState
from tests.math_helpers import decode_png, mapping, number, sequence, text


def plot(**payload: object) -> dict[str, object]:
    return handle_request({"operation": "plot", "variable": "x", **payload})


def result_of(response: dict[str, object]) -> Mapping[str, object]:
    return mapping(response["result"])


def png_of(response: dict[str, object]) -> bytes:
    return decode_png(response["png_base64"])


def checkpoints(result: Mapping[str, object]) -> list[Mapping[str, object]]:
    return [mapping(value) for value in sequence(result["checkpoints"])]


def test_parametric_curve_keeps_segments_on_both_sides_of_hole() -> None:
    response = plot(
        plot_type="parametric",
        functions=["1/t", "t"],
        parameter="t",
        x_min=-1,
        x_max=1,
    )
    result = result_of(response)
    series = sequence(result["functions"])
    first_series = mapping(series[0])
    assert number(first_series["segments"]) == 2
    assert sequence(first_series["discontinuities"])
    png = png_of(response)
    assert number(result["png_bytes"]) == len(png)


def test_contour_uses_requested_levels_and_supports_one_level() -> None:
    response = plot(
        plot_type="contour",
        functions=["x^2+y^2"],
        y_variable="y",
        x_min=-2,
        x_max=2,
        y_min=-2,
        y_max=2,
        levels=[1, 2],
    )
    result = result_of(response)
    assert result["levels"] == [1.0, 2.0]
    assert text(result["color_scale"]).startswith("viridis")
    png_of(response)

    single = plot(
        plot_type="contour",
        functions=["x^2+y^2"],
        y_variable="y",
        x_min=-2,
        x_max=2,
        y_min=-2,
        y_max=2,
        levels=[1],
    )
    assert result_of(single)["levels"] == [1.0]
    png_of(single)


def test_domain_coloring_masks_pole_and_reports_checkpoints() -> None:
    response = plot(
        plot_type="domain_coloring",
        functions=["1/z"],
        variable="z",
        x_min=-2,
        x_max=2,
        y_min=-2,
        y_max=2,
    )
    result = result_of(response)
    assert result["status"] == "ok"
    assert text(result["color_scale"]).startswith("hue=arg")
    assert "normalized" in text(result["color_scale"])
    assert result["poles"] == ["z=0"]
    assert int(number(result["masked_points"])) >= 1
    assert int(number(result["total_points"])) == 321 * 321

    by_point = {text(checkpoint["point"]): checkpoint for checkpoint in checkpoints(result)}
    assert by_point["z=0"]["state"] == "pole"
    assert by_point["z=I"]["state"] == ""
    assert by_point["z=I"]["value"] == "-I"
    png_of(response)


def test_domain_coloring_log_reports_branch_cut_and_branch_point() -> None:
    response = plot(
        plot_type="domain_coloring",
        functions=["log(z)"],
        variable="z",
        x_min=-2,
        x_max=2,
        y_min=-2,
        y_max=2,
    )
    result = result_of(response)
    assert result["branch_cuts"] == ["log(z): arg=pi"]
    assert int(number(result["masked_points"])) >= 150

    by_point = {text(checkpoint["point"]): checkpoint for checkpoint in checkpoints(result)}
    assert by_point["z=0"]["state"] == "branch_point"
    assert by_point["z=-1"]["state"] == "branch_cut"
    assert by_point["z=I"]["state"] == ""
    assert by_point["z=-2"]["state"] == "branch_cut"
    png_of(response)


def test_complex_height_reports_magnitudes_and_marks_zeros() -> None:
    response = plot(
        plot_type="complex_height",
        functions=["z^2-1"],
        variable="z",
        x_min=-2,
        x_max=2,
        y_min=-2,
        y_max=2,
    )
    result = result_of(response)
    assert result["status"] == "ok"

    by_point = {text(checkpoint["point"]): checkpoint for checkpoint in checkpoints(result)}
    assert by_point["z=1"]["value"] == "0"
    assert by_point["z=1"]["height"] == "0"
    assert by_point["z=-1"]["height"] == "0"
    assert by_point["z=I"]["value"] == "-2"
    assert by_point["z=I"]["height"] == "2"
    assert float(text(by_point["z=I"]["numeric_height"])) == pytest.approx(2.0)
    zero_markers = {
        (number(mapping(marker)["re"]), number(mapping(marker)["im"]))
        for marker in sequence(result["zero_markers"])
    }
    assert zero_markers == {(-1.0, 0.0), (1.0, 0.0)}
    assert "Нули отмечены" in text(result["detail"])
    png_of(response)


def test_complex_height_does_not_mark_cancelled_zero() -> None:
    response = plot(
        plot_type="complex_height",
        functions=["((z-1)/(z-1))*(z-1)"],
        variable="z",
        x_min=-2,
        x_max=2,
        y_min=-2,
        y_max=2,
    )
    result = result_of(response)
    assert result["status"] == "ok"
    assert sequence(result["zero_markers"]) == []
    assert "Нули отмечены" not in text(result["detail"])


def test_surface_masks_invalid_domain_and_keeps_valid_points() -> None:
    response = plot(
        plot_type="surface",
        functions=["sqrt(x^2+y^2-1)"],
        y_variable="y",
        x_min=-2,
        x_max=2,
        y_min=-2,
        y_max=2,
    )
    result = result_of(response)
    assert result["status"] == "ok"
    assert int(number(result["masked_points"])) > 0
    assert int(number(result["total_points"])) == 161 * 161

    by_point = {text(checkpoint["point"]): checkpoint for checkpoint in checkpoints(result)}
    assert by_point["(x, y)=(0, 0)"]["value"] == "undefined"
    assert by_point["(x, y)=(1, 1)"]["value"] == "1"
    png_of(response)


def test_plot_rejects_invalid_type_specific_arguments() -> None:
    with pytest.raises(MathError) as captured:
        plot(plot_type="parametric", functions=["t"], parameter="t")
    assert captured.value.code == "invalid_arguments"

    with pytest.raises(MathError) as captured:
        plot(plot_type="surface", functions=["x+y"], variable="x", y_variable="x")
    assert captured.value.code == "invalid_arguments"

    with pytest.raises(MathError) as captured:
        plot(
            plot_type="contour",
            functions=["x+y+z"],
            variable="x",
            y_variable="y",
            levels=[1, 2],
        )
    assert captured.value.code == "invalid_expression"

    with pytest.raises(MathError) as captured:
        plot(plot_type="curl", functions=["x"])
    assert captured.value.code == "invalid_arguments"


def test_curve_checkpoint_uses_actual_variable_name() -> None:
    response = plot(functions=["t^2"], variable="t", x_min=-2, x_max=2)
    result = result_of(response)

    assert text(checkpoints(result)[0]["point"]) == "t=0"


def test_complex_poles_and_checkpoints_use_actual_variable_name() -> None:
    response = plot(
        plot_type="domain_coloring",
        functions=["1/w"],
        variable="w",
        x_min=-2,
        x_max=2,
        y_min=-2,
        y_max=2,
    )
    result = result_of(response)
    assert result["poles"] == ["w=0"]

    by_point = {text(checkpoint["point"]): checkpoint for checkpoint in checkpoints(result)}
    assert by_point["w=0"]["state"] == CheckpointState.POLE


def test_surface_labels_use_actual_variable_names() -> None:
    response = plot(
        plot_type="surface",
        functions=["u*v"],
        variable="u",
        y_variable="v",
        x_min=-2,
        x_max=2,
        y_min=-2,
        y_max=2,
    )
    result = result_of(response)

    assert text(result["color_scale"]) == "viridis: f(u, v)"
    by_point = {text(checkpoint["point"]): checkpoint for checkpoint in checkpoints(result)}
    assert by_point["(u, v)=(0, 0)"]["value"] == "0"


def test_contour_sorts_requested_levels_before_rendering() -> None:
    parsed = build_parsed_expression("x^2+y^2")
    rendering = render_contour(
        parsed,
        build_variable("x"),
        build_variable("y"),
        -2,
        2,
        -2,
        2,
        [2.0, 1.0],
    )

    assert rendering.has_points
    assert rendering.levels == (1.0, 2.0)


def test_real_checkpoint_reports_undefined_for_cancelled_domain() -> None:
    parsed = build_parsed_expression("sqrt(x)*sqrt(x)")
    rendering = render_curve(
        [(parsed.text, parsed)],
        "x",
        build_variable("x"),
        -2.0,
        -0.5,
        PLOT_SAMPLES,
    )
    checkpoint = rendering.checkpoints[0]

    assert checkpoint.point == "x=-1.25"
    assert checkpoint.value == "undefined"
    assert checkpoint.numeric == ""
    assert checkpoint.state is CheckpointState.UNDEFINED


def test_gamma_and_factorial_plot_real_values() -> None:
    gamma_response = plot(functions=["gamma(x)"], x_min=1, x_max=3)
    gamma_result = result_of(gamma_response)
    assert gamma_result["status"] == "ok"
    gamma_checkpoint = checkpoints(gamma_result)[0]
    assert text(gamma_checkpoint["point"]) == "x=2"
    assert text(gamma_checkpoint["value"]) == "1"
    png_of(gamma_response)

    factorial_response = plot(functions=["factorial(x)"], x_min=1, x_max=3)
    factorial_result = result_of(factorial_response)
    assert factorial_result["status"] == "ok"
    factorial_checkpoint = checkpoints(factorial_result)[0]
    assert text(factorial_checkpoint["point"]) == "x=2"
    assert text(factorial_checkpoint["value"]) == "2"
    png_of(factorial_response)


@pytest.mark.parametrize("function", ["gamma", "factorial", "floor", "ceil"])
@pytest.mark.parametrize("plot_type", ["domain_coloring", "complex_height"])
def test_unsupported_complex_function_is_rejected_explicitly(
    function: str,
    plot_type: str,
) -> None:
    with pytest.raises(MathError) as captured:
        plot(
            plot_type=plot_type,
            functions=[f"{function}(z)"],
            variable="z",
            x_min=-2,
            x_max=2,
            y_min=-2,
            y_max=2,
        )

    assert captured.value.code == "unsupported_syntax"
    assert function in captured.value.message


def test_complex_plot_accepts_floor_with_a_real_argument() -> None:
    response = plot(plot_type="domain_coloring", functions=["floor(abs(z))"], variable="z")

    assert result_of(response)["status"] == "ok"
    png_of(response)


def test_magnitude_mask_rejects_non_finite_points() -> None:
    values = np.array([0.0, 1.0, np.inf, np.nan])

    assert magnitude_mask(values).tolist() == [True, True, False, False]


def test_render_curve_honors_configured_png_limit() -> None:
    parsed = build_parsed_expression("sin(50*x)+cos(37*x)")
    functions = [(parsed.text, parsed)]
    symbol = build_variable("x")

    default = render_curve(functions, "x", symbol, -10, 10, PLOT_SAMPLES)
    limit = int(len(default.png) * 0.8)
    reduced = render_curve(
        functions,
        "x",
        symbol,
        -10,
        10,
        PLOT_SAMPLES,
        max_png_bytes=limit,
    )

    assert len(default.png) <= MAX_PNG_BYTES
    assert len(reduced.png) <= limit
    assert len(reduced.png) < len(default.png)
    with Image.open(io.BytesIO(reduced.png)) as image:
        assert image.width < 640


def test_render_curve_rejects_png_over_configured_limit() -> None:
    parsed = build_parsed_expression("sin(50*x)+cos(37*x)")

    with pytest.raises(MathError) as captured:
        render_curve(
            [(parsed.text, parsed)],
            "x",
            build_variable("x"),
            -10,
            10,
            PLOT_SAMPLES,
            max_png_bytes=1024,
        )

    assert captured.value.code == "resource_limit"
    assert "1024" in captured.value.message


def test_complex_poles_include_imaginary_roots() -> None:
    response = plot(
        plot_type="domain_coloring",
        functions=["1/(z^2+1)"],
        variable="z",
        x_min=-2,
        x_max=2,
        y_min=-2,
        y_max=2,
    )
    result = result_of(response)

    assert set(sequence(result["poles"])) == {"z=-I", "z=I"}
    by_point = {text(checkpoint["point"]): checkpoint for checkpoint in checkpoints(result)}
    assert by_point["z=I"]["state"] == CheckpointState.POLE


def test_complex_height_marks_imaginary_zeros() -> None:
    response = plot(
        plot_type="complex_height",
        functions=["z^2+1"],
        variable="z",
        x_min=-2,
        x_max=2,
        y_min=-2,
        y_max=2,
    )
    result = result_of(response)

    zero_markers = {
        (number(mapping(marker)["re"]), number(mapping(marker)["im"]))
        for marker in sequence(result["zero_markers"])
    }
    assert zero_markers == {(0.0, -1.0), (0.0, 1.0)}


def test_plot_payload_honors_lower_png_limit() -> None:
    limit = 64 * 1024
    response = plot(
        functions=["sin(50*x)+cos(37*x)"],
        x_min=-10,
        x_max=10,
        max_png_bytes=limit,
    )

    assert len(png_of(response)) <= limit
