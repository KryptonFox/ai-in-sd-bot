import base64
import io
from collections.abc import Mapping
from typing import cast

import pytest
from PIL import Image

from mcp_servers.math_tools.engine import handle_request
from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.limits import MAX_PNG_BYTES


def plot(**payload: object) -> dict[str, object]:
    return handle_request({"operation": "plot", "variable": "x", **payload})


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


def result_of(response: dict[str, object]) -> Mapping[str, object]:
    return mapping(response["result"])


def png_of(response: dict[str, object]) -> bytes:
    encoded = response["png_base64"]
    assert isinstance(encoded, str)
    png = base64.b64decode(encoded)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) <= MAX_PNG_BYTES
    with Image.open(io.BytesIO(png)) as image:
        image.load()
        assert image.format == "PNG"
        assert image.width > 0
        assert image.height > 0
    return png


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
