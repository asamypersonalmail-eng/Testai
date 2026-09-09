"""Best-effort parser for an organization-specific broker.xml manifest.

Unlike .msgflow/.subflow, there is no single IBM-standard schema for a
"broker.xml" file bundled inside a BAR — different integration teams use
different custom manifest formats to record which REST operation maps to
which deployed flow/subflow. This parser therefore does not assume one
fixed structure: it walks the whole XML tree and builds a permissive
name-based mapping, used only to *confirm* (with high confidence) a root
flow that name-convention matching would already suggest, and to set
`implementation.matched_in_broker_xml` truthfully rather than by assumption.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Dict

from .utils import local_name

logger = logging.getLogger("ace_catalog.broker_xml")

_ID_KEYS = ("operationid", "operation", "service", "servicename", "name")
_FLOW_KEYS = ("flow", "subflow", "flowname", "subflowname", "target", "implementation")


def parse_operation_flow_mapping(xml_text: str) -> Dict[str, str]:
    """Return {operation_id_or_service_name: flow_name} pairs found anywhere
    in broker.xml. An element only contributes a mapping if it carries both
    an identifier-like attribute and a flow-like attribute with distinct
    values — this avoids treating an element's own name/id as a self-edge.
    """
    mapping: Dict[str, str] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("Could not parse broker.xml (%s); proceeding without it", exc)
        return mapping

    for elem in root.iter():
        attrs = {local_name(k).lower(): v for k, v in elem.attrib.items() if v}
        op_val = next((attrs[k] for k in _ID_KEYS if k in attrs), None)
        flow_val = next((attrs[k] for k in _FLOW_KEYS if k in attrs), None)
        if op_val and flow_val and op_val != flow_val:
            mapping[op_val] = flow_val

    logger.info("broker.xml contributed %d operation->flow mapping(s)", len(mapping))
    return mapping
