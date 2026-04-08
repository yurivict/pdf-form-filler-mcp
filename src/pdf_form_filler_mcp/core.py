"""Core PDF form filling logic.

* AcroForm — handled entirely by pypdf (pymupdf crashes on choice widgets).
* XFA      — handled entirely by pymupdf (raw XML stream editing).
"""

from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pymupdf
import pypdf

_XFA_TNS = "http://www.xfa.org/schema/xfa-template/3.6/"
_XFA_DNS = "http://www.xfa.org/schema/xfa-data/1.0/"

# Global state: handle -> pdf_state dict
_open_pdfs: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Form-type detection (pymupdf used for both form types)
# ---------------------------------------------------------------------------

def _detect_form(doc: pymupdf.Document) -> tuple[str, dict[str, int]]:
    """Return (form_type, xfa_streams).

    form_type is 'AcroForm', 'XFA', or 'none'.
    xfa_streams maps stream names to xref ints; empty for AcroForm.
    """
    cat = doc.pdf_catalog()
    acroform_ref = doc.xref_get_key(cat, "AcroForm")
    if acroform_ref[0] == "null":
        return "none", {}

    acroform_xref = int(acroform_ref[1].split()[0])
    xfa_val = doc.xref_get_key(acroform_xref, "XFA")
    if xfa_val[0] == "null":
        return "AcroForm", {}

    entries = re.findall(r"\((\w+)\)(\d+) 0 R", xfa_val[1])
    return "XFA", {name: int(xref) for name, xref in entries}


# ---------------------------------------------------------------------------
# AcroForm helpers (pypdf)
# ---------------------------------------------------------------------------

def _pypdf_field_type(field: pypdf.generic.Field) -> str:
    ft = str(field.field_type or "")
    flags = int(field.flags or 0)
    if ft == "/Tx":
        return "text"
    if ft == "/Btn":
        if flags & (1 << 15):   # Radio button
            return "radiobutton"
        if flags & (1 << 16):   # Push button
            return "button"
        return "checkbox"
    if ft == "/Ch":
        if flags & (1 << 17):   # Combo box
            return "combobox"
        return "listbox"
    if ft == "/Sig":
        return "signature"
    return "unknown"


def _acroform_fields(reader: pypdf.PdfReader, filled_values: dict[str, str]) -> list[dict]:
    raw = reader.get_fields() or {}
    result = []
    for name, field in raw.items():
        descriptor: dict[str, Any] = {
            "name": name,
            # Show filled value if we have written it, else the original
            "value": filled_values.get(name, str(field.value or "")),
            "type": _pypdf_field_type(field),
        }
        choices = getattr(field, "choices", None) or []
        if choices:
            descriptor["choices"] = list(choices)
        result.append(descriptor)
    return result


def _acroform_fill(
    writer: pypdf.PdfWriter,
    reader: pypdf.PdfReader,
    field_name: str,
    value: str,
) -> None:
    fields = reader.get_fields() or {}
    if field_name not in fields:
        raise ValueError(f"Field not found: {field_name!r}")

    for page in writer.pages:
        writer.update_page_form_field_values(
            page, {field_name: value}, auto_regenerate=False
        )


# ---------------------------------------------------------------------------
# XFA helpers (pymupdf)
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _xfa_field_paths(tmpl_root: ET.Element) -> dict[str, list[str]]:
    """Return field_name -> list of ancestor subform names (data path)."""
    paths: dict[str, list[str]] = {}

    def traverse(node: ET.Element, subform_path: list[str]) -> None:
        if node.tag == f"{{{_XFA_TNS}}}field":
            name = node.attrib.get("name", "")
            if name:
                paths[name] = list(subform_path)
            return  # don't recurse into field internals

        if node.tag == f"{{{_XFA_TNS}}}subform":
            name = node.attrib.get("name", "")
            new_path = subform_path + [name] if name else list(subform_path)
        else:
            new_path = subform_path

        for child in node:
            traverse(child, new_path)

    traverse(tmpl_root, [])
    return paths


def _xfa_field_descriptors(
    tmpl_root: ET.Element, values: dict[str, str]
) -> list[dict]:
    descriptors = []
    for field in tmpl_root.iter(f"{{{_XFA_TNS}}}field"):
        a = field.attrib
        name = a.get("name", "")
        if not name:
            continue

        ui_el = field.find(f"{{{_XFA_TNS}}}ui")
        ui_type = ""
        if ui_el is not None:
            children = list(ui_el)
            if children:
                ui_type = _local(children[0].tag)

        cap = field.find(
            f".//{{{_XFA_TNS}}}caption/{{{_XFA_TNS}}}value/{{{_XFA_TNS}}}text"
        )
        label = (cap.text or "").strip() if cap is not None else ""
        if not label:
            speak = field.find(f".//{{{_XFA_TNS}}}assist/{{{_XFA_TNS}}}speak")
            label = (speak.text or "").strip() if speak is not None else ""

        descriptors.append({
            "name": name,
            "value": values.get(name, ""),
            "type": ui_type,
            "x": a.get("x", ""),
            "y": a.get("y", ""),
            "w": a.get("w", ""),
            "h": a.get("h", ""),
            "label": label,
        })
    return descriptors


def _xfa_collect_values(datasets_root: ET.Element) -> dict[str, str]:
    values: dict[str, str] = {}

    def walk(node: ET.Element) -> None:
        children = list(node)
        if not children:
            if node.text and node.text.strip():
                values[_local(node.tag)] = node.text.strip()
        else:
            for child in children:
                walk(child)

    walk(datasets_root)
    return values


def _xfa_find_element(node: ET.Element, local_name: str) -> ET.Element | None:
    """Recursively find the first element whose local tag name matches local_name."""
    tag = node.tag.split("}")[-1] if "}" in node.tag else node.tag
    if tag == local_name:
        return node
    for child in node:
        result = _xfa_find_element(child, local_name)
        if result is not None:
            return result
    return None


def _xfa_set_field(
    datasets_root: ET.Element,
    field_name: str,
    value: str,
    ancestor_path: list[str],
) -> None:
    # The datasets XML may have the element at a different nesting level than
    # the template subform path suggests (e.g. directly under topmostSubform
    # instead of topmostSubform/Page1).  Always search first; only create via
    # ancestor_path when the element is absent.
    field_el = _xfa_find_element(datasets_root, field_name)
    if field_el is not None:
        field_el.text = value
        return

    # Element absent — create it following the template's ancestor path.
    data_el = datasets_root.find(f"{{{_XFA_DNS}}}data")
    if data_el is None:
        data_el = ET.SubElement(datasets_root, f"{{{_XFA_DNS}}}data")

    current = data_el
    for segment in ancestor_path:
        child = current.find(segment)
        if child is None:
            child = ET.SubElement(current, segment)
        current = child

    new_el = ET.SubElement(current, field_name)
    new_el.text = value


def _serialize_datasets(root: ET.Element) -> bytes:
    ET.register_namespace("xfa", _XFA_DNS)
    body = ET.tostring(root, encoding="unicode")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body).encode("utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def open_pdf(path: str) -> str:
    """Open a PDF file; return an opaque handle."""
    resolved = str(Path(path).resolve())

    # Use pymupdf to detect form type
    doc = pymupdf.open(resolved)
    form_type, xfa_streams = _detect_form(doc)

    state: dict[str, Any] = {
        "path": resolved,
        "form_type": form_type,
        "filled_values": {},
    }

    if form_type == "XFA":
        state["doc"] = doc
        state["xfa_streams"] = xfa_streams
        tmpl_xref = xfa_streams.get("template")
        if tmpl_xref:
            tmpl_root = ET.fromstring(doc.xref_stream(tmpl_xref))
            state["xfa_tmpl_root"] = tmpl_root
            state["xfa_field_paths"] = _xfa_field_paths(tmpl_root)
        else:
            state["xfa_tmpl_root"] = None
            state["xfa_field_paths"] = {}
    else:
        # AcroForm or none: use pypdf; close pymupdf
        doc.close()
        reader = pypdf.PdfReader(resolved)
        writer = pypdf.PdfWriter()
        writer.append(reader)
        state["reader"] = reader
        state["writer"] = writer

    handle = str(uuid.uuid4())
    _open_pdfs[handle] = state
    return handle


def get_form_type(handle: str) -> str:
    """Return 'AcroForm', 'XFA', or 'none'."""
    _require(handle)
    return _open_pdfs[handle]["form_type"]


def get_available_fields(handle: str) -> list[dict]:
    """Return a list of field descriptor dicts."""
    _require(handle)
    state = _open_pdfs[handle]
    form_type = state["form_type"]

    if form_type == "AcroForm":
        return _acroform_fields(state["reader"], state["filled_values"])

    if form_type == "XFA":
        doc: pymupdf.Document = state["doc"]
        datasets_xref = state["xfa_streams"].get("datasets")
        values: dict[str, str] = {}
        if datasets_xref:
            datasets_root = ET.fromstring(doc.xref_stream(datasets_xref))
            values = _xfa_collect_values(datasets_root)
        values.update(state["filled_values"])
        tmpl_root = state.get("xfa_tmpl_root")
        if tmpl_root is None:
            return []
        return _xfa_field_descriptors(tmpl_root, values)

    return []


def fill_field(handle: str, field_name: str, value: str) -> None:
    """Fill a field by name."""
    _require(handle)
    state = _open_pdfs[handle]
    form_type = state["form_type"]

    if form_type == "AcroForm":
        _acroform_fill(state["writer"], state["reader"], field_name, value)
        state["filled_values"][field_name] = value
        return

    if form_type == "XFA":
        doc: pymupdf.Document = state["doc"]
        datasets_xref = state["xfa_streams"].get("datasets")
        if datasets_xref is None:
            raise ValueError("XFA form has no datasets stream")
        datasets_root = ET.fromstring(doc.xref_stream(datasets_xref))
        ancestor_path = state["xfa_field_paths"].get(field_name, [])
        _xfa_set_field(datasets_root, field_name, value, ancestor_path)
        doc.update_stream(datasets_xref, _serialize_datasets(datasets_root))
        state["filled_values"][field_name] = value
        return

    raise ValueError("PDF has no form fields")


def get_filled_field_values(handle: str) -> dict[str, str]:
    """Return dict of field_name -> value for all filled fields."""
    _require(handle)
    return dict(_open_pdfs[handle]["filled_values"])


def save_pdf(handle: str, output_path: str) -> str:
    """Save the filled PDF; return the resolved output path."""
    _require(handle)
    state = _open_pdfs[handle]
    out = str(Path(output_path).resolve())

    if state["form_type"] == "XFA":
        state["doc"].save(out)
    else:
        with open(out, "wb") as fh:
            state["writer"].write(fh)
    return out


def close_pdf(handle: str) -> None:
    """Close the PDF and invalidate the handle."""
    _require(handle)
    state = _open_pdfs.pop(handle)
    if "doc" in state:
        state["doc"].close()
    if "reader" in state:
        state["reader"].stream.close()


def _require(handle: str) -> None:
    if handle not in _open_pdfs:
        raise ValueError(f"Invalid or already-closed handle: {handle!r}")
