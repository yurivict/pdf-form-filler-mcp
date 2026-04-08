"""Tests for XFA form filling (IRS f1040-2025.pdf): 2 Python API + 2 MCP protocol."""

from __future__ import annotations

import json
import os

import pytest

from pdf_form_filler_mcp.core import (
    close_pdf,
    fill_field,
    get_available_fields,
    get_filled_field_values,
    get_form_type,
    open_pdf,
    save_pdf,
)
from helpers import FILL_VALUES, McpTestClient, verify_xfa_saved


# ---------------------------------------------------------------------------
# Python API tests
# ---------------------------------------------------------------------------

def test_xfa_form_type(xfa_pdf_path):
    """API: open f1040-2025.pdf and verify it is detected as XFA."""
    handle = open_pdf(xfa_pdf_path)
    try:
        assert get_form_type(handle) == "XFA"
    finally:
        close_pdf(handle)


def test_xfa_fill_fields(xfa_pdf_path, tmp_path_factory):
    """API: fill XFA text fields with fake personal data, save, verify saved values."""
    handle = open_pdf(xfa_pdf_path)
    try:
        fields = get_available_fields(handle)
        assert len(fields) > 0, "Expected at least one XFA field"

        text_fields = [f for f in fields if f.get("type") in ("textEdit", "text", "")]
        sample = text_fields[: len(FILL_VALUES)]
        assert len(sample) > 0, "Expected at least one text field"

        filled: dict[str, str] = {}
        for i, field in enumerate(sample):
            name = field["name"]
            value = FILL_VALUES[i]
            fill_field(handle, name, value)
            filled[name] = value

        # In-memory tracking
        reported = get_filled_field_values(handle)
        for name, value in filled.items():
            assert reported.get(name) == value, f"In-memory mismatch for {name!r}"

        # Save
        out_path = str(tmp_path_factory.mktemp("out") / "f1040-2025-filled.pdf")
        save_pdf(handle, out_path)
        assert os.path.getsize(out_path) > 0

        # Read-back verification using pymupdf
        verify_xfa_saved(out_path, filled)
    finally:
        close_pdf(handle)


# ---------------------------------------------------------------------------
# MCP protocol tests
# ---------------------------------------------------------------------------

def test_xfa_form_type_mcp(xfa_pdf_path, mcp_server_cmd):
    """MCP: open f1040-2025.pdf via MCP server subprocess and verify form type."""
    client = McpTestClient(mcp_server_cmd)
    handle = ""
    try:
        handle = client.call_tool("open_pdf", path=xfa_pdf_path)
        assert handle, "Expected a non-empty handle"

        form_type = client.call_tool("get_form_type", handle=handle)
        assert form_type == "XFA"
    finally:
        if handle:
            try:
                client.call_tool("close_pdf", handle=handle)
            except Exception:
                pass
        client.close()


def test_xfa_fill_fields_mcp(xfa_pdf_path, mcp_server_cmd, tmp_path_factory):
    """MCP: fill XFA text fields via MCP server, save, verify saved values."""
    client = McpTestClient(mcp_server_cmd)
    handle = ""
    try:
        handle = client.call_tool("open_pdf", path=xfa_pdf_path)

        fields_json = client.call_tool("get_available_fields", handle=handle)
        fields = json.loads(fields_json)
        assert len(fields) > 0, "Expected at least one XFA field"

        text_fields = [f for f in fields if f.get("type") in ("textEdit", "text", "")]
        sample = text_fields[: len(FILL_VALUES)]
        assert len(sample) > 0, "Expected at least one text field"

        filled: dict[str, str] = {}
        for i, field in enumerate(sample):
            name = field["name"]
            value = FILL_VALUES[i]
            result = client.call_tool("fill_field", handle=handle, field_name=name, value=value)
            assert result == "ok"
            filled[name] = value

        # In-memory verification via MCP
        vals_json = client.call_tool("get_filled_field_values", handle=handle)
        vals = json.loads(vals_json)
        for name, value in filled.items():
            assert vals.get(name) == value, f"MCP in-memory mismatch for {name!r}"

        # Save via MCP
        out_path = str(tmp_path_factory.mktemp("out") / "f1040-2025-filled-mcp.pdf")
        saved_path = client.call_tool("save_pdf", handle=handle, output_path=out_path)
        assert os.path.exists(saved_path)
        assert os.path.getsize(saved_path) > 0

        # Read-back verification using pymupdf
        verify_xfa_saved(saved_path, filled)
    finally:
        if handle:
            try:
                client.call_tool("close_pdf", handle=handle)
            except Exception:
                pass
        client.close()
