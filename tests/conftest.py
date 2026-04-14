"""Shared pytest fixtures for pdf-form-filler-mcp tests."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from helpers import download


ACRO_URL = "https://freebsd.org/~yuri/pdf-form-filler-mcp-test-data/acro_form.pdf"
XFA_URL = "https://freebsd.org/~yuri/pdf-form-filler-mcp-test-data/f1040-2025.pdf"
SAMPLE_URL = "https://freebsd.org/~yuri/pdf-form-filler-mcp-test-data/Sample-Fillable-PDF.pdf"


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
def sample_pdf_path(tmp_path_factory):
    dest = str(tmp_path_factory.mktemp("pdfs") / "Sample-Fillable-PDF.pdf")
    download(SAMPLE_URL, dest)
    return dest


@pytest.fixture(scope="session")
def mcp_server_cmd():
    """Command to launch the MCP server.

    Prefers the binary from the same venv as the test runner so that
    code changes in src/ are picked up without a system-wide reinstall.
    """
    import sys
    venv_exe = Path(sys.executable).parent / "pdf-form-filler-mcp"
    if venv_exe.exists():
        return [str(venv_exe)]
    fallback = shutil.which("pdf-form-filler-mcp")
    assert fallback, "MCP server executable not found in venv or PATH"
    return [fallback]
