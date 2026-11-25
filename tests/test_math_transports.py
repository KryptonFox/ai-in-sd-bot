import os
import sys

import httpx2
import pytest
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from mcp_servers.math_tools.__main__ import http_app
from mcp_servers.math_tools.server import build_server
from mcp_servers.math_tools.service import MathService
from tests.math_helpers import decode_png, mapping, sequence

SECRET = "math-transport-secret-0123456789abcdef"
TOOL_NAMES = {"math_calculate", "math_solve", "math_plot"}


def assert_png(result: types.CallToolResult) -> None:
    images = [content for content in result.content if isinstance(content, types.ImageContent)]
    assert len(images) == 1
    image = images[0]
    assert image.mime_type == "image/png"
    decode_png(image.data)


def assert_discovery(tools: list[types.Tool]) -> dict[str, types.Tool]:
    discovered = {tool.name: tool for tool in tools}
    assert set(discovered) == TOOL_NAMES
    for tool in discovered.values():
        assert tool.output_schema is not None
        assert tool.input_schema["additionalProperties"] is False
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
    return discovered


@pytest.mark.asyncio
async def test_stdio_transport_discovery_and_calls() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "mcp_servers.math_tools",
            "--transport",
            "stdio",
            "--env-file",
            os.devnull,
        ],
    )

    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            discovered = assert_discovery(tools.tools)
            calculate_input = discovered["math_calculate"].input_schema["properties"]
            expression_schema = calculate_input["expression"]
            assert expression_schema.get("maxLength") == 512 or any(
                part.get("maxLength") == 512 for part in expression_schema.get("anyOf", [])
            )
            assert discovered["math_calculate"].input_schema["properties"]["operation"]["enum"] == [
                "simplify",
                "evaluate",
                "numeric",
                "diff",
                "integrate",
                "limit",
                "partial_diff",
                "jacobian",
                "constrained_extrema",
                "series",
            ]
            calculate_schema = discovered["math_calculate"].output_schema
            assert calculate_schema is not None
            assert calculate_schema["properties"]["status"]["enum"] == [
                "ok",
                "undefined",
                "no_solution",
            ]
            assert calculate_schema["properties"]["operation"]["enum"] == [
                "simplify",
                "evaluate",
                "numeric",
                "diff",
                "integrate",
                "limit",
                "partial_diff",
                "jacobian",
                "constrained_extrema",
                "series",
            ]
            assert discovered["math_solve"].input_schema["properties"]["equations"]["maxItems"] == 3
            assert discovered["math_plot"].input_schema["properties"]["functions"]["maxItems"] == 4
            assert discovered["math_plot"].input_schema["properties"]["plot_type"]["enum"] == [
                "curve",
                "parametric",
                "contour",
                "domain_coloring",
                "surface",
                "complex_height",
            ]

            calculated = await session.call_tool(
                "math_calculate",
                {"expression": "sqrt(2)", "operation": "numeric"},
            )
            assert calculated.is_error is not True
            payload = mapping(calculated.structured_content)
            assert payload["status"] == "ok"
            assert str(payload["result"]).startswith("1.4142135623731")

            solved = await session.call_tool("math_solve", {"equations": ["x^2 = 4"]})
            assert solved.is_error is not True
            assert mapping(solved.structured_content)["solutions"] == [{"x": "-2"}, {"x": "2"}]

            plotted = await session.call_tool("math_plot", {"functions": ["1/x"]})
            assert plotted.is_error is not True
            plot_payload = mapping(plotted.structured_content)
            series = sequence(plot_payload["functions"])
            assert mapping(series[0])["segments"] == 2
            assert_png(plotted)

            attack = await session.call_tool(
                "math_calculate",
                {"expression": "__import__('os').system('id')"},
            )
            assert attack.is_error is True


@pytest.mark.asyncio
async def test_streamable_http_transport_auth_discovery_and_calls() -> None:
    service = MathService()
    server = build_server(service)
    app = http_app(server, SECRET)

    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://test",
        ) as raw_client:
            assert (await raw_client.get("/mcp/")).status_code == 401
            assert (
                await raw_client.get("/mcp/", headers={"Authorization": "Bearer wrong"})
            ).status_code == 401
            health = await raw_client.get("/health")
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}

        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {SECRET}"},
        ) as client:
            async with streamable_http_client(
                url="http://test/mcp/",
                http_client=client,
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert_discovery(tools.tools)

                    calculated = await session.call_tool(
                        "math_calculate",
                        {"expression": "__import__('os').system('id')"},
                    )
                    assert calculated.is_error is True

                    derivative = await session.call_tool(
                        "math_calculate",
                        {"expression": "x^3", "operation": "diff"},
                    )
                    assert derivative.is_error is not True
                    assert mapping(derivative.structured_content)["result"] == "3*x**2"

                    integral = await session.call_tool(
                        "math_calculate",
                        {"expression": "x^2", "operation": "integrate", "lower": 0, "upper": 1},
                    )
                    assert integral.is_error is not True
                    assert mapping(integral.structured_content)["result"] == "1/3"

                    solved = await session.call_tool(
                        "math_solve",
                        {"equations": ["x + y = 3", "x - y = 1"]},
                    )
                    assert solved.is_error is not True
                    assert mapping(solved.structured_content)["solutions"] == [{"x": "2", "y": "1"}]

                    inferred = await session.call_tool(
                        "math_solve",
                        {"equations": ["x^2 = 4"], "variables": []},
                    )
                    assert inferred.is_error is not True
                    assert mapping(inferred.structured_content)["variables"] == ["x"]

                    plotted = await session.call_tool("math_plot", {"functions": ["x^2"]})
                    assert plotted.is_error is not True
                    assert_png(plotted)

    assert service.runtime.worker.is_running is False
