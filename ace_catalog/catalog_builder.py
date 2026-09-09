"""Assembles the final service_catalog.json and dependency_graph.json payloads.

This module performs no resolution logic of its own — it only shapes the
already-resolved ServiceResult / ResolutionAudit objects into the JSON
structure required by the spec, computing the roll-up statistics along the
way. Dict keys are inserted in the documented order so the emitted JSON
reads the same way the spec lays it out (Python dicts preserve insertion
order, and json.dump is called with sort_keys=False).
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

from .bar_inspector import BarInventory
from .dependency_resolver import ResolutionAudit
from .models import ServiceResult

logger = logging.getLogger("ace_catalog.catalog_builder")


def determine_main_flow(inventory: BarInventory, broker_mapping: Dict[str, str],
                         registry=None) -> Tuple[Optional[str], str]:
    """Identify the single overarching REST-dispatch main flow.

    ACE REST API projects conventionally contain exactly one .msgflow (the
    main flow that owns the REST API definition and routes each operation to
    an identically-named .subflow). We only report a main flow name when
    that convention — or explicit broker.xml evidence — actually holds;
    otherwise we say so rather than guessing among several candidates.

    A Callable Flow is also packaged as a top-level .msgflow (it needs its
    own Input/Reply pair, just like a main flow does), so a .msgflow whose
    parsed nodes include a callable_input is excluded from candidacy here —
    it is a callable-flow entry point, not a REST-dispatch main flow, and
    counting it would make an otherwise-unambiguous BAR look ambiguous.
    """
    candidates = list(inventory.msgflow_names)
    if registry is not None:
        candidates = [
            name for name in candidates
            if not (registry.get(name) and registry.get(name).nodes_by_kind("callable_input"))
        ]

    if len(candidates) == 1:
        return candidates[0], "single_msgflow_in_bar"

    if broker_mapping:
        counts = Counter(broker_mapping.values())
        if counts:
            top_flow, top_count = counts.most_common(1)[0]
            if top_count > max(1, len(broker_mapping) // 2):
                return top_flow, "broker_xml_majority_reference"

    if len(candidates) > 1:
        logger.warning("Multiple non-callable-flow .msgflow candidates found (%s); cannot determine a "
                        "single main flow", ", ".join(sorted(candidates)))
        return None, "multiple_msgflow_candidates_ambiguous"

    return None, "no_msgflow_found"


def _topology_dict(sr: ServiceResult) -> Dict[str, Any]:
    return {"status": sr.topology.status, "reason": sr.topology.reason, "message": sr.topology.message}


def _subflow_type_for(sr: ServiceResult, registry) -> str:
    if sr.root_flow_name is None:
        return "unresolved"
    flow = registry.get(sr.root_flow_name)
    if flow is not None:
        return flow.file_type  # "msgflow" | "subflow"
    if sr.root_flow_name in registry.cmf_only_names:
        return "compiled_only"
    return "unresolved"


def _backend_dict(b) -> Dict[str, Any]:
    d = {
        "node_name": b.node_name,
        "protocol": b.protocol,
        "backend_system": b.backend_system,
        "url": b.url,
        "host": b.host,
        "port": b.port,
        "backend_source_flow": b.backend_source_flow,
        "direct": b.direct,
        "depth": b.depth,
        "call_chain": b.call_chain,
        "call_path": b.call_path,
        "resolution": b.resolution,
        "confidence": b.confidence,
        "evidence": b.evidence,
        # Statically detected via the flow's <connections> wiring: which
        # Compute/Mapping node builds the request sent to this backend node,
        # and which one processes its response — a pointer for the reviewer,
        # not the literal payload (see "request"/"response" below).
        "request_construction": b.request_construction,
        "response_construction": b.response_construction,
        # Filled in later by a human reviewer (e.g. via catalog_explorer.html) —
        # static analysis of the flow cannot know the literal payload a
        # backend call sends/receives at runtime.
        "request": None,
        "response": None,
    }
    # Only present these keys for dynamically-resolved backends, matching
    # rule 8's example shape (a purely-static backend has no configured_url
    # / configuration_source noise in its record).
    if b.resolution == "dynamic":
        d["configured_url"] = b.configured_url
        d["configuration_source"] = b.configuration_source
    return d


def _service_dict(sr: ServiceResult, registry) -> Dict[str, Any]:
    op = sr.operation
    downstream_calls = [
        {
            "from_flow": e.from_flow, "to_flow": e.to_flow, "call_type": e.call_type,
            "node_name": e.node_name, "confidence": e.confidence, "evidence": e.evidence, "depth": e.depth,
        }
        for e in sr.downstream_calls
    ]
    backends = [_backend_dict(b) for b in sr.backends]
    direct_count = sum(1 for b in sr.backends if b.direct)
    inherited_count = sum(1 for b in sr.backends if not b.direct)

    return {
        "service_name": op.operation_id,
        "input": {
            "protocol": "REST",
            "method": op.method,
            "base_path": op.base_path,
            "path": op.path,
            "full_path": op.full_path,
            "schemes": op.schemes,
            "consumes": op.consumes,
            "produces": op.produces,
            "parameters": op.parameters,
        },
        "swagger": {
            "summary": op.summary,
            "description": op.description,
            "tags": op.tags,
            "request_schema": op.request_schema,
            "response_schemas": op.response_schemas,
        },
        "implementation": {
            "flow_name": None,  # filled in by build_catalog() once the main flow is known
            "subflow_name": sr.root_flow_name,
            "subflow_type": _subflow_type_for(sr, registry),
            "matched_in_broker_xml": sr.matched_in_broker_xml,
            "match_evidence": sr.match_evidence,
            "topology_resolution": _topology_dict(sr),
            "downstream_calls": downstream_calls,
            "recursive_flow_count": len(downstream_calls),
        },
        "backends": backends,
        "backend_summary": {
            "direct_backend_calls": direct_count,
            "inherited_backend_calls": inherited_count,
            "total_backend_calls": direct_count + inherited_count,
        },
        # Set by a human reviewer (e.g. via catalog_explorer.html), not by
        # this analysis — a freshly generated catalog is always unreviewed.
        "reviewed": False,
    }


def build_analysis_warnings(audit: ResolutionAudit, main_flow_name: Optional[str], main_flow_evidence: str,
                             flow_parse_warnings: Dict[str, List[str]],
                             synthesized_operation_ids: List[str]) -> List[Dict[str, str]]:
    warnings: List[Dict[str, str]] = []

    if audit.cmf_only_flows:
        warnings.append({
            "type": "cmf_only_topology",
            "severity": "warning",
            "message": "One or more flows are available only as compiled CMF resources. "
                       "Recursive SubFlow dependencies may therefore be incomplete.",
        })

    if main_flow_name is None:
        warnings.append({
            "type": "main_flow_not_determined",
            "severity": "warning" if main_flow_evidence == "multiple_msgflow_candidates_ambiguous" else "info",
            "message": f"Could not determine a single main flow (reason: {main_flow_evidence}). "
                       "'implementation.flow_name' is left null for all services.",
        })

    if audit.unresolved_edges:
        warnings.append({
            "type": "unresolved_dependencies",
            "severity": "warning",
            "message": f"{len(audit.unresolved_edges)} dependency reference(s) could not be resolved "
                       "to a known flow, callable endpoint, or compiled resource. See "
                       "dependency_resolution_audit.unresolved_edges for details.",
        })

    if audit.rejected_edges:
        warnings.append({
            "type": "rejected_dependencies",
            "severity": "info",
            "message": f"{len(audit.rejected_edges)} candidate edge(s) were explicitly rejected "
                       "(e.g. circular references, ambiguous callable-endpoint matches) rather than guessed. "
                       "See dependency_resolution_audit.rejected_edges for details.",
        })

    for flow_name, msgs in flow_parse_warnings.items():
        for msg in msgs:
            warnings.append({
                "type": "flow_parse_warning",
                "severity": "warning",
                "message": f"{flow_name}: {msg}",
            })

    if synthesized_operation_ids:
        warnings.append({
            "type": "missing_operation_id",
            "severity": "info",
            "message": f"{len(synthesized_operation_ids)} Swagger operation(s) had no operationId and were "
                       f"assigned a synthesized service_name: {', '.join(synthesized_operation_ids)}.",
        })

    return warnings


def build_catalog(catalog_name: str, catalog_version: str, swagger_meta: Dict[str, Any],
                   bar_path: str, main_flow_name: Optional[str], main_flow_evidence: str,
                   service_results: List[ServiceResult], registry, audit: ResolutionAudit,
                   flow_parse_warnings: Dict[str, List[str]]) -> Dict[str, Any]:
    services = [_service_dict(sr, registry) for sr in service_results]
    for svc in services:
        svc["implementation"]["flow_name"] = main_flow_name

    matched = sum(1 for sr in service_results if sr.root_flow_name is not None)
    services_with_backend_urls = sum(
        1 for sr in service_results if any(b.url for b in sr.backends)
    )
    call_graph_edges = len(audit.accepted_edges)
    callable_flow_edges = sum(1 for e in audit.accepted_edges if e["call_type"] == "callable_flow")
    verified_normal_subflow_edges = sum(1 for e in audit.accepted_edges if e["call_type"] == "subflow")
    services_with_recursive_flow_calls = sum(1 for sr in service_results if sr.downstream_calls)
    inherited_backend_calls = sum(1 for sr in service_results for b in sr.backends if not b.direct)

    synthesized_ids = [sr.operation.operation_id for sr in service_results if sr.operation.operation_id_synthetic]

    catalog: Dict[str, Any] = {
        "catalog_name": catalog_name,
        "catalog_version": catalog_version,
        "source": {
            "swagger_version": swagger_meta.get("swagger_version"),
            "base_path": swagger_meta.get("base_path"),
            "host": swagger_meta.get("host"),
            "broker_main_flow": main_flow_name,
            "bar_file": os.path.basename(bar_path),
        },
        "statistics": {
            "swagger_operations": len(service_results),
            "matched_to_broker_flow_or_subflow": matched,
            "unmatched_swagger_operations": len(service_results) - matched,
            "services_with_backend_urls": services_with_backend_urls,
            "call_graph_edges": call_graph_edges,
            "callable_flow_edges": callable_flow_edges,
            "verified_normal_subflow_edges": verified_normal_subflow_edges,
            "services_with_recursive_flow_calls": services_with_recursive_flow_calls,
            "inherited_backend_calls": inherited_backend_calls,
        },
        "analysis_warnings": build_analysis_warnings(
            audit, main_flow_name, main_flow_evidence, flow_parse_warnings, synthesized_ids,
        ),
        "cmf_only_flows": sorted(audit.cmf_only_flows),
        "services": services,
        "extraction_notes": {
            "recursive_backend_resolution": True,
            "callable_flow_rule": (
                "A callable-flow invoke node's targetEndpointName is matched against a "
                "callableInputEndpoint defined on a Callable Flow Input node found anywhere in the "
                "BAR. The edge is only accepted when exactly one candidate matches "
                "(confidence=high, evidence=callable_endpoint_match); multiple matches are "
                "recorded as rejected (ambiguous), not guessed."
            ),
            "normal_subflow_rule": (
                "A normal SubFlow dependency is recorded only when the calling node's xmi:type "
                "explicitly references the target .subflow resource "
                "(evidence=explicit_subflow_reference). Node-label/name-only matches are never "
                "used to create this edge."
            ),
            "name_only_matches_are_rejected": True,
            "cycle_protection": True,
        },
        "dependency_resolution_audit": {
            "accepted_edges": audit.accepted_edges,
            "rejected_edges": audit.rejected_edges,
            "unresolved_edges": audit.unresolved_edges,
        },
    }
    return catalog


def build_dependency_graph(catalog: Dict[str, Any]) -> Dict[str, Any]:
    """A compact nodes/edges view of the same catalog, suitable for feeding
    into a graph visualization tool (Gephi, D3, Cytoscape, ...).
    """
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []

    def add_node(node_id: str, node_type: str, **attrs: Any) -> None:
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "type": node_type, **attrs}

    for svc in catalog["services"]:
        service_id = f"service:{svc['service_name']}"
        add_node(service_id, "service", method=svc["input"]["method"], path=svc["input"]["full_path"])

        root_flow = svc["implementation"]["subflow_name"]
        if root_flow:
            flow_id = f"flow:{root_flow}"
            add_node(flow_id, "flow", subflow_type=svc["implementation"]["subflow_type"])
            edges.append({"source": service_id, "target": flow_id, "type": "implements", "confidence": "high"})

        for call in svc["implementation"]["downstream_calls"]:
            src_id = f"flow:{call['from_flow']}"
            dst_id = f"flow:{call['to_flow']}" if call["to_flow"] else None
            add_node(src_id, "flow")
            if dst_id:
                add_node(dst_id, "flow")
                edges.append({
                    "source": src_id, "target": dst_id, "type": call["call_type"],
                    "confidence": call["confidence"], "evidence": call["evidence"],
                })

        for backend in svc["backends"]:
            backend_id = f"backend:{backend['backend_system']}:{backend['node_name']}:{backend['backend_source_flow']}"
            add_node(backend_id, "backend", protocol=backend["protocol"], backend_system=backend["backend_system"],
                      url=backend["url"], host=backend["host"], port=backend["port"],
                      resolution=backend["resolution"])
            src_id = f"flow:{backend['backend_source_flow']}"
            add_node(src_id, "flow")
            edges.append({
                "source": src_id, "target": backend_id, "type": "backend_call",
                "confidence": backend["confidence"], "resolution": backend["resolution"],
            })

    return {"nodes": list(nodes.values()), "edges": edges}
