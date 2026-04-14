"""Tests for AcroForm filling (Sample-Fillable-PDF.pdf): 2 Python API + 2 MCP protocol.

Covers all field types: text, checkbox, and combobox.
"""

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
from helpers import McpTestClient, verify_acroform_saved

# ---------------------------------------------------------------------------
# Field values for Sample-Fillable-PDF.pdf
#
# Field names exactly as returned by pypdf (note literal tab in "Age\t of Dependent").
# Checkboxes use "True"/"False" strings per the fill_field API convention.
# ---------------------------------------------------------------------------

SAMPLE_FILL_VALUES: dict[str, str] = {
    "Name": "John Johnson",
    "Name of Dependent": "Jane Johnson",
    "Age\t of Dependent": "46",
    "Option 1": "True",
    "Option 2": "True",
    "Option 3": "False",
    "Dropdown2": "Choice 2",
}


# ---------------------------------------------------------------------------
# Python API tests
# ---------------------------------------------------------------------------

def test_sample_form_type(sample_pdf_path):
    """API: open Sample-Fillable-PDF.pdf and verify it is detected as AcroForm."""
    handle = open_pdf(sample_pdf_path)
    try:
        assert get_form_type(handle) == "AcroForm"
    finally:
        close_pdf(handle)


def test_sample_fill_all_fields(sample_pdf_path, tmp_path_factory):
    """API: fill all fields (text, checkbox, combobox), save, verify all saved values."""
    handle = open_pdf(sample_pdf_path)
    try:
        fields = get_available_fields(handle)
        assert len(fields) > 0, "Expected at least one field"
        field_names = {f["name"] for f in fields}

        for name in SAMPLE_FILL_VALUES:
            assert name in field_names, f"Expected field {name!r} in PDF"

        for name, value in SAMPLE_FILL_VALUES.items():
            fill_field(handle, name, value)

        # In-memory tracking
        reported = get_filled_field_values(handle)
        for name, value in SAMPLE_FILL_VALUES.items():
            assert reported.get(name) == value, f"In-memory mismatch for {name!r}"

        # get_available_fields reflects filled values
        re_fields = {f["name"]: f for f in get_available_fields(handle)}
        for name, value in SAMPLE_FILL_VALUES.items():
            assert re_fields[name]["value"] == value, (
                f"get_available_fields mismatch for {name!r}: "
                f"expected {value!r}, got {re_fields[name]['value']!r}"
            )

        # Save
        out_path = str(tmp_path_factory.mktemp("out") / "sample_filled.pdf")
        save_pdf(handle, out_path)
        assert os.path.getsize(out_path) > 0

        # Read-back verification using pypdf (checkboxes verified as "True"/"False")
        verify_acroform_saved(out_path, SAMPLE_FILL_VALUES)
    finally:
        close_pdf(handle)


# ---------------------------------------------------------------------------
# MCP protocol tests
# ---------------------------------------------------------------------------

def test_sample_form_type_mcp(sample_pdf_path, mcp_server_cmd):
    """MCP: open Sample-Fillable-PDF.pdf via MCP server and verify form type."""
    client = McpTestClient(mcp_server_cmd)
    handle = ""
    try:
        handle = client.call_tool("open_pdf", path=sample_pdf_path)
        assert handle, "Expected a non-empty handle"

        form_type = client.call_tool("get_form_type", handle=handle)
        assert form_type == "AcroForm"
    finally:
        if handle:
            try:
                client.call_tool("close_pdf", handle=handle)
            except Exception:
                pass
        client.close()


def test_sample_fill_all_fields_mcp(sample_pdf_path, mcp_server_cmd, tmp_path_factory):
    """MCP: fill all fields via MCP server, save, verify all saved values."""
    client = McpTestClient(mcp_server_cmd)
    handle = ""
    try:
        handle = client.call_tool("open_pdf", path=sample_pdf_path)

        fields_json = client.call_tool("get_available_fields", handle=handle)
        fields = json.loads(fields_json)
        assert len(fields) > 0, "Expected at least one field"
        field_names = {f["name"] for f in fields}

        for name in SAMPLE_FILL_VALUES:
            assert name in field_names, f"Expected field {name!r} in PDF"

        for name, value in SAMPLE_FILL_VALUES.items():
            result = client.call_tool("fill_field", handle=handle, field_name=name, value=value)
            assert result == "ok", f"fill_field returned {result!r} for {name!r}"

        # In-memory verification via MCP
        vals_json = client.call_tool("get_filled_field_values", handle=handle)
        vals = json.loads(vals_json)
        for name, value in SAMPLE_FILL_VALUES.items():
            assert vals.get(name) == value, f"MCP in-memory mismatch for {name!r}"

        # Save via MCP
        out_path = str(tmp_path_factory.mktemp("out") / "sample_filled_mcp.pdf")
        saved_path = client.call_tool("save_pdf", handle=handle, output_path=out_path)
        assert os.path.exists(saved_path)
        assert os.path.getsize(saved_path) > 0

        # Read-back verification using pypdf (checkboxes verified as "True"/"False")
        verify_acroform_saved(saved_path, SAMPLE_FILL_VALUES)
    finally:
        if handle:
            try:
                client.call_tool("close_pdf", handle=handle)
            except Exception:
                pass
        client.close()
