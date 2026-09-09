"""Parses IBM ACE .msgflow / .subflow XML source into FlowDefinition objects.

Background (why this parser is heuristic, and why that is disclosed rather
than hidden): .msgflow/.subflow files are an EMF/XMI serialization of the
ACE flow editor's model. The exact property-attribute spelling used by a
given node type (e.g. the HTTPRequest node's URL property) has varied
across WMB/IIB/ACE versions and toolkit releases, and node XMI type strings
are versioned too. Rather than hard-coding one exact schema and silently
mis-parsing anything that deviates from it, this module:

  1. Always locates <nodes> elements structurally (namespace-agnostic) —
     this part of the format is stable.
  2. Classifies each node's *kind* from clearly-documented substrings in its
     xmi:type (subflow reference, callable-flow invoke/input, HTTP/SOAP
     request) — these class-name fragments are stable IBM node-type names.
  3. Extracts the *value* of a property (a URL, an endpoint name, ...) by
     scanning attribute names for case-insensitive fragments rather than one
     exact name, and records which attribute it used as `evidence`.

Every classification decision is recorded on the FlowNode so a human (or
the dependency resolver) can audit *why* something was treated as a
subflow call, a callable-flow edge, or a backend call.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

from .models import FlowDefinition, FlowNode
from .utils import basename_of_entry, find_attr_ci, local_name, protocol_for, qualify_flow_name

logger = logging.getLogger("ace_catalog.msgflow_parser")

XMI_TYPE_KEYS = ("{http://www.omg.org/XMI}type", "xmi:type", "type")


def _get_xmi_type(elem: ET.Element) -> Optional[str]:
    for key in XMI_TYPE_KEYS:
        val = elem.get(key)
        if val:
            return val
    return None


def _collect_translation_label(elem: ET.Element, fallback: str) -> str:
    """ACE nodes store their on-canvas display label under a nested
    <translation><...string="..."/></translation> structure. We walk all
    descendants (namespace-agnostic) looking for any element carrying a
    'string' attribute, since the exact nesting/tag names differ by node type.
    """
    for child in elem.iter():
        if child is elem:
            continue
        if local_name(child.tag).lower() in ("translation", "string"):
            val = child.get("string")
            if val:
                return val
    return fallback


def _collect_node_attributes(elem: ET.Element) -> Dict[str, str]:
    """Merge the node's own attributes with attributes of its direct child
    elements (some ACE properties are serialized as child elements rather
    than attributes when their value is long or structured). Namespace
    prefixes are stripped from keys for uniform matching.
    """
    attrs: Dict[str, str] = {}
    for key, value in elem.attrib.items():
        attrs[local_name(key)] = value
    for child in list(elem):
        child_tag = local_name(child.tag).lower()
        if child_tag in ("translation",):
            continue
        for key, value in child.attrib.items():
            attrs.setdefault(f"{child_tag}.{local_name(key)}", value)
    return attrs


def _classify_node(xmi_type: str, attrs: Dict[str, str]) -> Dict[str, object]:
    """Return classification fields for one node based on its xmi:type.

    Order matters: subflow-reference detection is checked before the
    generic callable/HTTP/SOAP checks because a subflow-reference xmi:type
    can (rarely) also contain substrings like 'Request' in a custom project
    naming scheme.
    """
    t = xmi_type or ""
    t_lower = t.lower()
    result: Dict[str, object] = {
        "kind": "other", "evidence": "unclassified", "signals": [],
        "target_flow_name": None, "target_endpoint_name": None,
        "callable_input_endpoint": None, "url": None, "protocol": None,
    }

    # --- 1. Normal SubFlow reference -------------------------------------
    # An embedded subflow instance's xmi:type directly references the
    # compiled subflow resource, e.g. "getPrepaidCards.subflow:FCMComposite_1"
    # or, for a folder-nested subflow, "mdp_getMDPCreditCards.subflow:FCMComposite_1"
    # (verified against a real BAR: ACE joins the project-relative folder
    # path with '_' for the compiled reference). This is the strongest,
    # least ambiguous signal in the whole format for a normal SubFlow edge.
    # qualify_flow_name() is applied defensively (it's a no-op here since
    # the head is normally already underscore-joined) in case some
    # toolkit/version variant instead emits a literal '/' in the type string.
    if ".subflow" in t_lower:
        head = t.split(".subflow", 1)[0]
        flow_name = qualify_flow_name(head)
        result.update(kind="subflow_ref", target_flow_name=flow_name,
                       evidence="explicit_subflow_reference", signals=["xmi_type_contains_.subflow"])
        return result

    # --- 2. Callable Flow nodes -------------------------------------------
    if "callable" in t_lower:
        if "input" in t_lower:
            endpoint = find_attr_ci(attrs, "callableinputendpoint", "callableInputEndpoint", "endpointname")
            result.update(kind="callable_input", callable_input_endpoint=endpoint,
                           evidence="explicit_callable_input_definition",
                           signals=["xmi_type_contains_callable+input"])
            return result
        if "invoke" in t_lower or "async" in t_lower:
            endpoint = find_attr_ci(attrs, "targetendpointname", "endpointname")
            result.update(kind="callable_invoke", target_endpoint_name=endpoint,
                           evidence="explicit_callable_endpoint_reference",
                           signals=["xmi_type_contains_callable+invoke"])
            return result
        # Callable-related node we don't specifically need (e.g. a Reply
        # node for a callable flow) — preserved in inventory, not used
        # for graph edges.
        result.update(kind="callable_other", evidence="callable_node_not_used_for_graph",
                       signals=["xmi_type_contains_callable"])
        return result

    # --- 3. Outbound HTTP Request ------------------------------------------
    # Exclude *Input/*Reply — those are inbound nodes, not backend calls.
    # ComIbmHTTPRequestNode is the "textbook" node name, but real-world ACE
    # flows commonly use the generic ComIbmWSRequest node for outbound
    # HTTP calls instead (verified against a real BAR, where its URL lives
    # in a 'URLSpecifier' attribute, not 'URL'/'webServiceURL') — both are
    # matched here so genuine backend calls aren't missed just because the
    # project used the WSRequest node rather than the plain HTTP one.
    if any(p in t_lower.replace(" ", "") for p in ("httprequest", "wsrequest")) \
            and "input" not in t_lower and "reply" not in t_lower:
        url = find_attr_ci(attrs, "urlspecifier", "url", "webserviceurl", "requesturl", "destination")
        result.update(kind="http_request", url=url, protocol=protocol_for(url, "HTTP"),
                       evidence="direct_broker_config" if url else "http_request_node_no_literal_url",
                       signals=["xmi_type_contains_HTTPRequest_or_WSRequest"])
        return result

    # --- 4. Outbound SOAP Request -------------------------------------------
    if "soaprequest" in t_lower.replace(" ", "") and "input" not in t_lower and "reply" not in t_lower:
        url = find_attr_ci(attrs, "webserviceurl", "url", "endpointaddress", "address")
        result.update(kind="soap_request", url=url, protocol=protocol_for(url, "HTTPS"),
                       evidence="direct_broker_config" if url else "soap_request_node_no_literal_url",
                       signals=["xmi_type_contains_SOAPRequest"])
        return result

    return result


class MsgFlowParser:
    """Parses a single .msgflow/.subflow XML document into a FlowDefinition."""

    def parse(self, xml_text: str, source_file: str, flow_name: str) -> FlowDefinition:
        # `flow_name` must be the already-qualified name (see
        # bar_inspector.qualify_flow_name / BarInventory.source_flow_entries'
        # keys) — it is passed in rather than re-derived here so there is
        # exactly one place that computes it; re-deriving it independently
        # from `source_file`'s bare basename previously caused folder-nested
        # flows (e.g. 'mdp/getMDPConfig.msgflow') to register under the
        # wrong, unqualified name ('getMDPConfig') and silently fail to
        # resolve every subflow_ref that correctly targeted 'mdp_getMDPConfig'.
        raw_name = basename_of_entry(source_file)
        file_type = "msgflow" if raw_name.lower().endswith(".msgflow") else "subflow"
        flow = FlowDefinition(flow_name=flow_name, source_file=source_file, file_type=file_type)

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            flow.parse_warnings.append(f"XML parse error: {exc}")
            logger.warning("Failed to parse %s: %s", source_file, exc)
            return flow

        node_elements = [e for e in root.iter() if local_name(e.tag) == "nodes"]
        if not node_elements:
            flow.parse_warnings.append("No <nodes> elements found — file may use an unrecognized flow format")

        for idx, elem in enumerate(node_elements):
            xmi_type = _get_xmi_type(elem)
            if not xmi_type:
                flow.parse_warnings.append(f"Node at index {idx} has no xmi:type attribute; skipped")
                continue
            node_id = elem.get("id") or f"node_{idx}"
            attrs = _collect_node_attributes(elem)
            label = _collect_translation_label(elem, fallback=node_id)
            classification = _classify_node(xmi_type, attrs)

            flow.nodes.append(FlowNode(
                node_id=node_id,
                label=label,
                xmi_type=xmi_type,
                raw_attributes=attrs,
                kind=classification["kind"],
                target_flow_name=classification["target_flow_name"],
                target_endpoint_name=classification["target_endpoint_name"],
                callable_input_endpoint=classification["callable_input_endpoint"],
                url=classification["url"],
                protocol=classification["protocol"],
                evidence=classification["evidence"],
                classification_signals=classification["signals"],
            ))

        logger.debug("Parsed %s: %d nodes (%s)", source_file, len(flow.nodes),
                     ", ".join(sorted({n.kind for n in flow.nodes})) or "none")
        return flow
