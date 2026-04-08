"""Shared pytest fixtures for pdf-form-filler-mcp tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import download


ACRO_URL = "https://freebsd.org/~yuri/acro_form.pdf"
XFA_URL = "https://freebsd.org/~yuri/f1040-2025.pdf"


@pytest.fixture(scope="session")
def acro_pdf_path(tmp_path_factory):
    dest = str(tmp_path_factory.mktemp("pdfs") / "acro_form.pdf")
    download(ACRO_URL, dest)
    return dest


@pytest.fixture(scope="session")
def xfa_pdf_path(tmp_path_factory):
    dest = str(tmp_path_factory.mktemp("pdfs") / "f1040-2025.pdf")
    download(XFA_URL, dest)
    return dest


@pytest.fixture(scope="session")
def mcp_server_cmd():
    """Command to launch the MCP server (in PATH)."""
    # find it in path
    exe = Path("pdf-form-filler-mcp")
    assert exe.exists(), f"MCP server executable not found: {exe}"
    return [str(exe)]
