"""MCP server entry point for pdf-form-filler-mcp."""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from . import core

mcp = FastMCP("pdf-form-filler-mcp")


@mcp.tool()
def open_pdf(path: str) -> str:
    """Open a PDF file and return a handle for subsequent operations.

    Multiple PDFs can be open simultaneously; use the returned handle in all
    other tool calls to identify which file to operate on.
    """
    return core.open_pdf(path)


@mcp.tool()
def get_form_type(handle: str) -> str:
    """Return the form type for the given PDF handle.

    Returns 'AcroForm', 'XFA', or 'none'.
    """
    return core.get_form_type(handle)


@mcp.tool()
def get_available_fields(handle: str) -> str:
    """Return a JSON array of field descriptors for the given PDF handle.

    Each descriptor includes at minimum: name, value, type.
    AcroForm fields also include: page, rect.
    XFA fields also include: x, y, w, h, label.
    The 'name' property uniquely identifies each field for fill_field calls.
    """
    fields = core.get_available_fields(handle)
    return json.dumps(fields, indent=2)


@mcp.tool()
def fill_field(handle: str, field_name: str, value: str) -> str:
    """Fill a single form field by name.

    Works for both AcroForm and XFA forms.
    For checkboxes use 'Yes' / 'Off'; for radio buttons use the option value.
    Returns 'ok' on success.
    """
    core.fill_field(handle, field_name, value)
    return "ok"


@mcp.tool()
def get_filled_field_values(handle: str) -> str:
    """Return a JSON object mapping field names to their filled values.

    Only fields explicitly filled via fill_field in this session are included.
    """
    return json.dumps(core.get_filled_field_values(handle), indent=2)


@mcp.tool()
def save_pdf(handle: str, output_path: str) -> str:
    """Save the filled PDF to output_path.

    Returns the resolved absolute path where the file was written.
    """
    return core.save_pdf(handle, output_path)


@mcp.tool()
def close_pdf(handle: str) -> str:
    """Close the PDF and invalidate the handle.

    Returns 'closed' on success.
    """
    core.close_pdf(handle)
    return "closed"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
