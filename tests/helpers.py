"""Shared test helpers for pdf-form-filler-mcp tests."""

from __future__ import annotations

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET

import pymupdf
import pypdf
import requests

# ---------------------------------------------------------------------------
# Fake personal data used in fill tests
# ---------------------------------------------------------------------------

FILL_VALUES = [
    "John",
    "Johnson",
    "05/15/1977",
    "100 Flower Str, Ste. 1309, San Diego, CA, 91239",
    "555-123-4567",
    "john@example.com",
    "98765",
]


# ---------------------------------------------------------------------------
# Download helper
# ---------------------------------------------------------------------------

def download(url: str, dest: str, max_retries: int = 3) -> None:
    """Download with retries; falls back to http:// if https:// fails."""
    urls = [url]
    if url.startswith("https://"):
        urls.append(url.replace("https://", "http://", 1))

    last_exc: Exception = RuntimeError("no attempts")
    for try_url in urls:
        for attempt in range(max_retries):
            try:
                resp = requests.get(
                    try_url,
                    stream=True,
                    timeout=60,
                    allow_redirects=True,
                    headers={"Accept-Encoding": "identity"},
                )
                resp.raise_for_status()
                with open(dest, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=65536):
                        fh.write(chunk)
                return
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    time.sleep(1)
    raise last_exc


# ---------------------------------------------------------------------------
# Post-save verification helpers
# ---------------------------------------------------------------------------

def verify_acroform_saved(saved_path: str, expected: dict[str, str]) -> None:
    """Re-open saved AcroForm PDF with pypdf and verify expected field values."""
    reader = pypdf.PdfReader(saved_path)
    raw = reader.get_fields() or {}
    saved: dict[str, str] = {}
    for name, field in raw.items():
        v = field.value
        saved[name] = str(v) if v is not None else ""

    for name, value in expected.items():
        assert saved.get(name) == value, (
            f"AcroForm field {name!r}: expected {value!r}, got {saved.get(name)!r}"
        )


def verify_xfa_saved(saved_path: str, expected: dict[str, str]) -> None:
    """Re-open saved XFA PDF with pymupdf and verify expected field values in datasets."""
    doc = pymupdf.open(saved_path)
    try:
        cat = doc.pdf_catalog()
        acroform_ref = doc.xref_get_key(cat, "AcroForm")
        acroform_xref = int(acroform_ref[1].split()[0])
        xfa_val = doc.xref_get_key(acroform_xref, "XFA")
        entries = re.findall(r"\((\w+)\)(\d+) 0 R", xfa_val[1])
        streams = {name: int(xref) for name, xref in entries}

        datasets_xref = streams.get("datasets")
        assert datasets_xref, "No datasets stream in saved XFA PDF"

        datasets_root = ET.fromstring(doc.xref_stream(datasets_xref))

        saved: dict[str, str] = {}

        def walk(node: ET.Element) -> None:
            children = list(node)
            tag = node.tag.split("}")[-1] if "}" in node.tag else node.tag
            if not children:
                if node.text and node.text.strip():
                    saved[tag] = node.text.strip()
            else:
                for child in children:
                    walk(child)

        walk(datasets_root)

        for name, value in expected.items():
            assert saved.get(name) == value, (
                f"XFA field {name!r}: expected {value!r}, got {saved.get(name)!r}"
            )
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Minimal synchronous MCP client over stdio
# ---------------------------------------------------------------------------

class McpTestClient:
    """Drives an MCP server subprocess via newline-delimited JSON-RPC over stdio."""

    def __init__(self, cmd: list[str]) -> None:
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._next_id = 0
        self._initialize()

    # --- low-level I/O ---

    def _send(self, msg: dict) -> None:
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

    def _recv(self) -> dict:
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise EOFError("MCP server closed stdout unexpectedly")
            stripped = line.strip()
            if stripped:
                return json.loads(stripped)

    def _request(self, method: str, params: dict) -> dict:
        """Send a JSON-RPC request and return the matching response."""
        self._next_id += 1
        req_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        while True:
            resp = self._recv()
            if resp.get("id") == req_id:
                return resp
            # Skip server notifications or unrelated messages

    def _initialize(self) -> None:
        resp = self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest-mcp-client", "version": "1.0"},
            },
        )
        if "error" in resp:
            raise RuntimeError(f"MCP initialize failed: {resp['error']}")
        # Send initialized notification back to server
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # --- high-level helpers ---

    def call_tool(self, name: str, **kwargs) -> str:
        """Call an MCP tool; return its text response. Raises RuntimeError on error."""
        resp = self._request("tools/call", {"name": name, "arguments": kwargs})
        if "error" in resp:
            raise RuntimeError(f"MCP error calling {name!r}: {resp['error']}")
        result = resp.get("result", {})
        if result.get("isError"):
            content = result.get("content", [])
            text = content[0].get("text", "") if content else ""
            raise RuntimeError(f"Tool {name!r} returned error: {text}")
        content = result.get("content", [])
        return content[0].get("text", "") if content else ""

    def close(self) -> None:
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            self._proc.wait()
