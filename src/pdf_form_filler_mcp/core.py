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


def _checkbox_value_to_bool(v: Any) -> str:
    """Return 'True' or 'False' for a checkbox/radio PDF field value."""
    if v is None:
        return "False"
    sv = str(v).lstrip("/")
    return "False" if sv.lower() in ("off", "false", "0", "") else "True"


def _acroform_fields(reader: pypdf.PdfReader, filled_values: dict[str, str]) -> list[dict]:
    raw = reader.get_fields() or {}
    result = []
    for name, field in raw.items():
        ftype = _pypdf_field_type(field)
        if ftype in ("checkbox", "radiobutton"):
            # Represent boolean fields as "True"/"False" strings
            value = filled_values.get(name, _checkbox_value_to_bool(field.value))
        else:
            value = filled_values.get(name, str(field.value or ""))
        descriptor: dict[str, Any] = {
            "name": name,
            "value": value,
            "type": ftype,
        }
        choices = getattr(field, "choices", None) or []
        if choices:
            descriptor["choices"] = list(choices)
        result.append(descriptor)
    return result


def _get_checkbox_on_state(reader: pypdf.PdfReader, field_name: str) -> str:
    """Find the 'on' export-value name for a checkbox/radio widget (e.g. 'On', 'Yes')."""
    for page in reader.pages:
        if "/Annots" not in page:
            continue
        for ref in page["/Annots"]:
            annot = ref.get_object()
            t = annot.get("/T")
            if t and str(t) == field_name:
                ap = annot.get("/AP")
                if ap:
                    ap_obj = ap.get_object()
                    n = ap_obj.get("/N")
                    if n:
                        n_obj = n.get_object()
                        for key in n_obj:
                            state = str(key).lstrip("/")
                            if state.lower() != "off":
                                return state
    return "Yes"  # PDF standard fallback


def _acroform_fill_checkbox(
    writer: pypdf.PdfWriter,
    reader: pypdf.PdfReader,
    field_name: str,
    checked: bool,
) -> None:
    """Check or uncheck a checkbox/radio, correctly setting both /V and /AS."""
    state = _get_checkbox_on_state(reader, field_name) if checked else "Off"
    pdf_name = pypdf.generic.NameObject(f"/{state}")
    for page in writer.pages:
        if "/Annots" not in page:
            continue
        for ref in page["/Annots"]:
            annot = ref.get_object()
            t = annot.get("/T")
            if t and str(t) == field_name:
                annot.update({
                    pypdf.generic.NameObject("/V"): pdf_name,
                    pypdf.generic.NameObject("/AS"): pdf_name,
                })


# ---------------------------------------------------------------------------
# XFA helpers (pymupdf)
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _xfa_field_on_states(tmpl_root: ET.Element) -> dict[str, str]:
    """Return field_name -> XFA on-state value for checkButton/radioButton fields.

    For a simple binary checkbox the XFA template's <xfa:items> holds two values:
    the first is "on" (e.g. "1"), the second is "off" (e.g. "0").  We return the
    first (on) value so callers can translate "True" → on_state at fill time.

    Only the first occurrence of each field name is recorded (radio groups have
    multiple template entries with the same name).
    """
    on_states: dict[str, str] = {}
    for field in tmpl_root.iter(f"{{{_XFA_TNS}}}field"):
        name = field.attrib.get("name", "")
        if not name or name in on_states:
            continue
        ui_el = field.find(f"{{{_XFA_TNS}}}ui")
        if ui_el is None:
            continue
        children = list(ui_el)
        if not children:
            continue
        ui_type = _local(children[0].tag)
        if ui_type not in ("checkButton", "radioButton"):
            continue
        for items_el in field.findall(f".//{{{_XFA_TNS}}}items"):
            item_children = list(items_el)
            if item_children:
                on_states[name] = (item_children[0].text or "1").strip()
                break
    return on_states


def _btn_on_state_from_raw(raw_obj: str) -> str | None:
    """Return the non-Off appearance-state key from /AP/N dict in a raw widget object.

    Returns e.g. "1", "On", "Yes"; or None if not found.
    """
    n_m = re.search(r"/AP\b.*?/N\s*<<(.*?)>>", raw_obj, re.DOTALL)
    if not n_m:
        return None
    n_section = n_m.group(1)
    for key in re.findall(r"/(\w+)\s+\d+ 0 R", n_section):
        if key.lower() != "off":
            return key
    return None


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


def _set_xfa_widget_values(doc: pymupdf.Document, filled_values: dict[str, str]) -> None:
    """Set /V (and /AS for buttons) on AcroForm widget annotations so non-XFA
    viewers display filled values.  Also sets NeedAppearances=true.

    For text widgets: /V is set as a PDF string literal.
    For button widgets (checkboxes/radio): /V and /AS are set as PDF names
    (/on_state or /Off), matching the widget's own AP/N appearance state.
    """
    cat = doc.pdf_catalog()
    acroform_ref = doc.xref_get_key(cat, "AcroForm")
    if acroform_ref[0] == "null":
        return
    acroform_xref = int(acroform_ref[1].split()[0])
    doc.xref_set_key(acroform_xref, "NeedAppearances", "true")

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
            short_name = re.sub(r"\[\d+\]$", "", _decode_pdf_string(t_match.group(1)))
            if short_name not in filled_values:
                continue
            value = filled_values[short_name]

            ft_match = re.search(r"/FT\s*/(\w+)", raw_obj)
            ft = ft_match.group(1) if ft_match else "Tx"

            if ft == "Btn":
                # For button widgets, /V and /AS must be PDF names, not strings.
                # Each radio button widget has its own on-state in AP/N.
                # Select this widget only if the field value matches its on-state.
                widget_on_state = _btn_on_state_from_raw(raw_obj)
                if widget_on_state is None:
                    continue
                state_to_set = widget_on_state if value == widget_on_state else "Off"
                doc.xref_set_key(axref, "V", f"/{state_to_set}")
                doc.xref_set_key(axref, "AS", f"/{state_to_set}")
            else:
                escaped = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                doc.xref_set_key(axref, "V", f"({escaped})")


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
            state["xfa_field_on_states"] = _xfa_field_on_states(tmpl_root)
        else:
            state["xfa_tmpl_root"] = None
            state["xfa_field_paths"] = {}
            state["xfa_field_on_states"] = {}
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
        reader = state["reader"]
        writer = state["writer"]
        fields = reader.get_fields() or {}
        if field_name not in fields:
            raise ValueError(f"Field not found: {field_name!r}")
        ftype = _pypdf_field_type(fields[field_name])
        if ftype in ("checkbox", "radiobutton"):
            if value.lower() not in ("true", "false"):
                raise ValueError(
                    f"Field {field_name!r} is a {ftype}; value must be 'True' or 'False', got {value!r}"
                )
            _acroform_fill_checkbox(writer, reader, field_name, value.lower() == "true")
        else:
            for page in writer.pages:
                writer.update_page_form_field_values(
                    page, {field_name: value}, auto_regenerate=False
                )
        state["filled_values"][field_name] = value
        return

    if form_type == "XFA":
        doc: pymupdf.Document = state["doc"]
        datasets_xref = state["xfa_streams"].get("datasets")
        if datasets_xref is None:
            raise ValueError("XFA form has no datasets stream")
        # Resolve True/False for checkButton/radioButton fields
        on_states = state.get("xfa_field_on_states", {})
        if field_name in on_states:
            if value.lower() == "true":
                value = on_states[field_name]
            elif value.lower() == "false":
                value = "0"
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
        _set_xfa_widget_values(state["doc"], state["filled_values"])
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
