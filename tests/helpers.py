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

def _field_value_normalized(field: pypdf.generic.Field) -> str:
    """Normalize pypdf field value to string.

    Checkbox/radiobutton fields are returned as 'True'/'False'.
    """
    ft = str(field.field_type or "")
    v = field.value
    if ft == "/Btn":
        if v is None:
            return "False"
        sv = str(v).lstrip("/")
        return "False" if sv.lower() in ("off", "false", "0", "") else "True"
    return str(v) if v is not None else ""


def verify_acroform_saved(saved_path: str, expected: dict[str, str]) -> None:
    """Re-open saved AcroForm PDF with pypdf and verify expected field values.

    Checkbox/radiobutton values are normalized to 'True'/'False' strings so
    they can be compared directly to the values passed to fill_field.
    """
    reader = pypdf.PdfReader(saved_path)
    raw = reader.get_fields() or {}
    saved: dict[str, str] = {name: _field_value_normalized(field) for name, field in raw.items()}

    for name, value in expected.items():
        assert saved.get(name) == value, (
            f"AcroForm field {name!r}: expected {value!r}, got {saved.get(name)!r}"
        )


def _decode_pdf_string(raw: str) -> str:
    """Decode a PDF string literal: either <hexhex> (UTF-16BE) or (text)."""
    raw = raw.strip()
    if raw.startswith("<") and raw.endswith(">"):
        hex_data = raw[1:-1].replace(" ", "")
        raw_bytes = bytes.fromhex(hex_data)
        if raw_bytes[:2] == b"\xfe\xff":
            return raw_bytes[2:].decode("utf-16-be")
        return raw_bytes.decode("latin-1")
    if raw.startswith("(") and raw.endswith(")"):
        return raw[1:-1]
    return raw


def verify_xfa_saved(saved_path: str, expected: dict[str, str]) -> None:
    """Re-open saved XFA PDF with pymupdf and verify expected field values.

    Checks both the XFA datasets XML stream AND AcroForm widget annotations,
    because non-XFA viewers (mupdf, evince, okular) read from widget values.

    For text widgets: verifies /V string value.
    For button widgets (checkboxes/radio): verifies /AS appearance state.
      - expected value is a specific on-state string like "1", "On", "Yes":
          asserts that at least one widget for that field has /AS == on-state.
      - expected value is "True": asserts at least one widget /AS is not "Off".
      - expected value is "False" or "0": asserts all widgets /AS are "Off".
    """
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

        xml_saved: dict[str, str] = {}

        def walk(node: ET.Element) -> None:
            children = list(node)
            tag = node.tag.split("}")[-1] if "}" in node.tag else node.tag
            if not children:
                if node.text and node.text.strip():
                    xml_saved[tag] = node.text.strip()
            else:
                for child in children:
                    walk(child)

        walk(datasets_root)

        for name, value in expected.items():
            assert xml_saved.get(name) == value, (
                f"XFA datasets XML field {name!r}: expected {value!r}, got {xml_saved.get(name)!r}"
            )

        # Collect widget annotations: text fields → /V string; Btn fields → set of /AS states
        widget_text: dict[str, str] = {}
        widget_btn_as: dict[str, set[str]] = {}
        seen: set[int] = set()
        for page_num in range(doc.page_count):
            page = doc[page_num]
            annots_ref = doc.xref_get_key(page.xref, "Annots")
            if annots_ref[0] == "null":
                continue
            if annots_ref[0] == "array":
                raw_arr = annots_ref[1]
            elif annots_ref[0] == "xref":
                arr_xref = int(annots_ref[1].split()[0])
                raw_arr = doc.xref_object(arr_xref)
            else:
                continue
            for axref in (int(x) for x in re.findall(r"(\d+) 0 R", raw_arr)):
                if axref in seen:
                    continue
                seen.add(axref)
                raw_obj = doc.xref_object(axref)
                if "/Widget" not in raw_obj:
                    continue
                t_match = re.search(r"/T\s*(<[^>]+>|\([^)]*\))", raw_obj)
                if not t_match:
                    continue
                short = re.sub(r"\[\d+\]$", "", _decode_pdf_string(t_match.group(1)))
                ft_match = re.search(r"/FT\s*/(\w+)", raw_obj)
                ft = ft_match.group(1) if ft_match else "Tx"
                if ft == "Btn":
                    as_match = re.search(r"/AS\s*/(\w+)", raw_obj)
                    as_val = as_match.group(1) if as_match else "Off"
                    widget_btn_as.setdefault(short, set()).add(as_val)
                else:
                    v_match = re.search(r"/V\s*(<[^>]+>|\([^)]*\))", raw_obj)
                    if v_match:
                        widget_text[short] = _decode_pdf_string(v_match.group(1))

        for name, value in expected.items():
            if name in widget_btn_as:
                as_vals = widget_btn_as[name]
                if value.lower() == "true":
                    assert any(s.lower() != "off" for s in as_vals), (
                        f"XFA Btn widget {name!r}: expected checked, but all /AS are Off"
                    )
                elif value.lower() in ("false", "0"):
                    assert all(s.lower() == "off" for s in as_vals), (
                        f"XFA Btn widget {name!r}: expected unchecked, got /AS states {as_vals}"
                    )
                else:
                    assert value in as_vals, (
                        f"XFA Btn widget {name!r}: expected /AS /{value}, got {as_vals}"
                    )
            elif name in widget_text:
                assert widget_text[name] == value, (
                    f"XFA widget /V {name!r}: expected {value!r}, got {widget_text.get(name)!r}"
                )
            else:
                assert False, (
                    f"XFA widget {name!r}: no widget annotation found in saved PDF"
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
