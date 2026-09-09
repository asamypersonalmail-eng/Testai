"""Recursive, cycle-safe resolution of the ACE call graph and backend calls.

This is the only module that walks flow -> subflow -> callable-flow ->
backend chains. It never silently invents an edge: every edge it produces
is either recorded as `accepted` (with evidence + confidence) or explicitly
logged as `rejected` / `unresolved` in the ResolutionAudit the caller
inspects — "unresolved" is a normal, expected outcome, not a failure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .models import (BackendCallResult, DependencyEdge, FlowDefinition, FlowNode,
                      OperationSpec, ServiceResult, TopologyResolution)
from .utils import derive_backend_system, dynamic_config_hint, looks_like_placeholder_host, parse_url

logger = logging.getLogger("ace_catalog.dependency_resolver")

# Guards against pathological/malformed input causing unbounded recursion.
# Real ACE call chains are never anywhere close to this deep.
MAX_TRAVERSAL_DEPTH = 40


@dataclass
class ResolutionAudit:
    """Catalog-wide record of every dependency-edge resolution decision."""

    accepted_edges: List[Dict] = field(default_factory=list)
    rejected_edges: List[Dict] = field(default_factory=list)
    unresolved_edges: List[Dict] = field(default_factory=list)
    cmf_only_flows: Set[str] = field(default_factory=set)

    @staticmethod
    def _key(e: Dict) -> Tuple:
        return (e.get("from_flow"), e.get("to_flow"), e.get("node_name"), e.get("call_type"))

    def add_accepted(self, edge: Dict) -> None:
        key = self._key(edge)
        if not any(self._key(existing) == key for existing in self.accepted_edges):
            self.accepted_edges.append(edge)

    def add_rejected(self, edge: Dict) -> None:
        self.rejected_edges.append(edge)

    def add_unresolved(self, edge: Dict) -> None:
        self.unresolved_edges.append(edge)


class FlowRegistry:
    """Holds every parsed FlowDefinition plus the set of flow names that
    exist only as compiled .cmf (no usable source XML)."""

    def __init__(self, flows: List[FlowDefinition], cmf_only_names: Set[str]):
        self.flows: Dict[str, FlowDefinition] = {f.flow_name: f for f in flows}
        self.cmf_only_names: Set[str] = set(cmf_only_names)
        self._callable_input_index = self._build_callable_input_index()

    def _build_callable_input_index(self) -> Dict[str, List[Tuple[str, FlowNode]]]:
        index: Dict[str, List[Tuple[str, FlowNode]]] = {}
        for flow in self.flows.values():
            for node in flow.nodes_by_kind("callable_input"):
                if node.callable_input_endpoint:
                    index.setdefault(node.callable_input_endpoint, []).append((flow.flow_name, node))
        return index

    def get(self, flow_name: str) -> Optional[FlowDefinition]:
        return self.flows.get(flow_name)

    def resolve_callable_endpoint(self, endpoint_name: str) -> Tuple[Optional[str], str]:
        """Return (flow_name_or_None, status); status in
        {'resolved', 'ambiguous', 'not_found'}. Rule 5: targetEndpointName ->
        callableInputEndpoint is treated as high-confidence *only* when it
        resolves to exactly one candidate flow.
        """
        matches = self._callable_input_index.get(endpoint_name, [])
        if len(matches) == 1:
            return matches[0][0], "resolved"
        if len(matches) > 1:
            return None, "ambiguous"
        return None, "not_found"


def resolve_root_flow(operation: OperationSpec, registry: FlowRegistry,
                       broker_mapping: Dict[str, str]) -> Tuple[Optional[str], bool, str]:
    """Map one Swagger operationId to its implementing ACE flow.

    Returns (flow_name_or_None, matched_in_broker_xml, evidence).

    Priority:
      1. An explicit broker.xml mapping (evidence="explicit_broker_config", high confidence).
      2. ACE's own convention of naming the REST-dispatch subflow identically
         to the operationId (evidence="name_convention_match") — this is a
         real, common ACE REST-API project pattern, not a blind guess, but
         it is NOT the same as verified broker.xml evidence, so
         matched_in_broker_xml stays False for it.
      3. Otherwise: unresolved.
    """
    op_id = operation.operation_id

    if op_id in broker_mapping:
        candidate = broker_mapping[op_id]
        if candidate in registry.flows or candidate in registry.cmf_only_names:
            return candidate, True, "explicit_broker_config"
        logger.warning("broker.xml maps '%s' -> '%s' but no such flow exists in the BAR", op_id, candidate)

    if op_id in registry.flows or op_id in registry.cmf_only_names:
        return op_id, False, "name_convention_match"

    return None, False, "unresolved"


class DependencyResolver:
    def __init__(self, registry: FlowRegistry):
        self.registry = registry

    # -- public entry point --------------------------------------------------
    def resolve_service(self, operation: OperationSpec, root_flow_name: Optional[str],
                         matched_in_broker_xml: bool, match_evidence: str,
                         audit: ResolutionAudit) -> ServiceResult:
        if not root_flow_name:
            topology = TopologyResolution(
                status="partial", reason="unresolved_reference",
                message="No implementing flow could be identified for this operation.",
            )
            return ServiceResult(operation=operation, root_flow_name=None, matched_in_broker_xml=False,
                                  match_evidence=match_evidence, topology=topology)

        if root_flow_name not in self.registry.flows:
            if root_flow_name in self.registry.cmf_only_names:
                audit.cmf_only_flows.add(root_flow_name)
                topology = TopologyResolution(
                    status="partial", reason="cmf_only",
                    message="Flow topology could not be fully reconstructed because only compiled "
                            "CMF resources are available for this flow.",
                )
            else:
                topology = TopologyResolution(
                    status="partial", reason="unresolved_reference",
                    message=f"Flow '{root_flow_name}' was matched by name but no source or compiled "
                            "resource for it was found in the BAR.",
                )
            return ServiceResult(operation=operation, root_flow_name=root_flow_name,
                                  matched_in_broker_xml=matched_in_broker_xml, match_evidence=match_evidence,
                                  topology=topology)

        downstream_calls: List[DependencyEdge] = []
        backends: List[BackendCallResult] = []
        cmf_hits: Set[str] = set()

        self._traverse(flow_name=root_flow_name, path=[root_flow_name], via_nodes=[], depth=0,
                        downstream_calls=downstream_calls, backends=backends, audit=audit, cmf_hits=cmf_hits)

        topology = TopologyResolution(
            status="partial" if cmf_hits else "complete",
            reason="cmf_only" if cmf_hits else None,
            message=(
                "Some downstream flows in this call chain exist only as compiled CMF resources; "
                f"their internal topology is not reflected below (affected: {', '.join(sorted(cmf_hits))})."
            ) if cmf_hits else None,
        )

        return ServiceResult(operation=operation, root_flow_name=root_flow_name,
                              matched_in_broker_xml=matched_in_broker_xml, match_evidence=match_evidence,
                              topology=topology, downstream_calls=downstream_calls, backends=backends)

    # -- internals ------------------------------------------------------------
    def _traverse(self, flow_name: str, path: List[str], via_nodes: List[str], depth: int,
                   downstream_calls: List[DependencyEdge], backends: List[BackendCallResult],
                   audit: ResolutionAudit, cmf_hits: Set[str]) -> None:
        if depth > MAX_TRAVERSAL_DEPTH:
            logger.warning("Max traversal depth (%d) exceeded at '%s' (chain: %s); stopping this branch",
                            MAX_TRAVERSAL_DEPTH, flow_name, " -> ".join(path))
            return

        flow = self.registry.get(flow_name)
        if flow is None:
            return  # only reachable for a child edge already logged as unresolved/cmf_only by the caller

        for node in flow.nodes:
            if node.kind == "subflow_ref":
                self._follow_child_edge(
                    call_type="subflow", from_flow=flow_name, to_flow=node.target_flow_name, node=node,
                    confidence="high", evidence=node.evidence, path=path, via_nodes=via_nodes, depth=depth,
                    downstream_calls=downstream_calls, backends=backends, audit=audit, cmf_hits=cmf_hits,
                )

            elif node.kind == "callable_invoke":
                self._follow_callable_invoke(node, flow_name, path, via_nodes, depth,
                                              downstream_calls, backends, audit, cmf_hits)

            elif node.kind in ("http_request", "soap_request"):
                backends.append(self._resolve_backend(node, flow_name, path, via_nodes, depth))

            # kind in ("callable_other", "other"): preserved on the FlowNode
            # inventory already; deliberately not used to build graph edges.

    def _follow_callable_invoke(self, node: FlowNode, flow_name: str, path: List[str], via_nodes: List[str],
                                 depth: int, downstream_calls: List[DependencyEdge],
                                 backends: List[BackendCallResult], audit: ResolutionAudit,
                                 cmf_hits: Set[str]) -> None:
        base_edge = {"from_flow": flow_name, "call_type": "callable_flow", "node_name": node.label}

        if not node.target_endpoint_name:
            audit.add_unresolved({**base_edge, "to_flow": None,
                                   "reason": "callable_invoke_missing_targetEndpointName"})
            return

        target, status = self.registry.resolve_callable_endpoint(node.target_endpoint_name)
        if status == "resolved":
            self._follow_child_edge(
                call_type="callable_flow", from_flow=flow_name, to_flow=target, node=node,
                confidence="high", evidence="callable_endpoint_match", path=path, via_nodes=via_nodes,
                depth=depth, downstream_calls=downstream_calls, backends=backends, audit=audit,
                cmf_hits=cmf_hits,
            )
        elif status == "ambiguous":
            audit.add_rejected({**base_edge, "to_flow": None,
                                 "target_endpoint_name": node.target_endpoint_name,
                                 "reason": "ambiguous_callable_endpoint_multiple_matches"})
        else:
            audit.add_unresolved({**base_edge, "to_flow": None,
                                   "target_endpoint_name": node.target_endpoint_name,
                                   "reason": "no_matching_callable_input_endpoint"})

    def _follow_child_edge(self, call_type: str, from_flow: str, to_flow: Optional[str], node: FlowNode,
                            confidence: str, evidence: str, path: List[str], via_nodes: List[str], depth: int,
                            downstream_calls: List[DependencyEdge], backends: List[BackendCallResult],
                            audit: ResolutionAudit, cmf_hits: Set[str]) -> None:
        if not to_flow:
            audit.add_unresolved({"from_flow": from_flow, "to_flow": None, "call_type": call_type,
                                   "node_name": node.label, "reason": "target_flow_name_could_not_be_determined"})
            return

        # Cycle protection: a flow already on the *current* DFS stack means a
        # real circular reference, not a legitimate diamond dependency
        # (the same flow reused from two different branches is fine and is
        # simply traversed twice, each with its own call_chain).
        if to_flow in path:
            audit.add_rejected({"from_flow": from_flow, "to_flow": to_flow, "call_type": call_type,
                                 "node_name": node.label, "reason": "circular_reference_detected",
                                 "cycle_path": path + [to_flow]})
            return

        edge = DependencyEdge(from_flow=from_flow, to_flow=to_flow, call_type=call_type,
                               node_name=node.label, confidence=confidence, evidence=evidence,
                               depth=depth + 1, status="accepted")
        downstream_calls.append(edge)
        audit.add_accepted({"from_flow": from_flow, "to_flow": to_flow, "call_type": call_type,
                             "node_name": node.label, "confidence": confidence, "evidence": evidence,
                             "depth": depth + 1})

        if to_flow not in self.registry.flows:
            if to_flow in self.registry.cmf_only_names:
                cmf_hits.add(to_flow)
                audit.cmf_only_flows.add(to_flow)
                audit.add_unresolved({"from_flow": from_flow, "to_flow": to_flow, "call_type": call_type,
                                       "node_name": node.label, "reason": "cmf_only"})
            else:
                audit.add_unresolved({"from_flow": from_flow, "to_flow": to_flow, "call_type": call_type,
                                       "node_name": node.label, "reason": "referenced_flow_not_found_in_bar"})
            return

        self._traverse(flow_name=to_flow, path=path + [to_flow], via_nodes=via_nodes + [node.label],
                        depth=depth + 1, downstream_calls=downstream_calls, backends=backends,
                        audit=audit, cmf_hits=cmf_hits)

    # -- backend resolution (rule 8: dynamic vs static URLs) -------------------
    def _resolve_backend(self, node: FlowNode, source_flow_name: str, path: List[str],
                          via_nodes: List[str], depth: int) -> BackendCallResult:
        url = node.url
        host, port, scheme = parse_url(url)
        protocol = node.protocol or (scheme.upper() if scheme else "HTTP")
        backend_system = derive_backend_system(host)

        resolution = "static"
        configured_url: Optional[str] = None
        configuration_source: Optional[str] = None
        confidence = "high" if url else "low"
        evidence = node.evidence
        url_out = url

        if looks_like_placeholder_host(host):
            hint = dynamic_config_hint(node.raw_attributes)
            if hint:
                # Rule 8: placeholder host AND real evidence the flow sources
                # its runtime URL dynamically (policy / configurable service /
                # promoted property). We do not fabricate the real backend
                # name — we report the design-time placeholder separately
                # from the (unknown) runtime value.
                resolution = "dynamic"
                configured_url = url
                url_out = None
                configuration_source = hint
                confidence = "medium"
                evidence = "dynamic_configuration"
            else:
                # Placeholder host with NO evidence of dynamic resolution:
                # per rule 8 we must not silently reclassify it, so it stays
                # a literal (low-confidence) static value for human review.
                confidence = "low"
                evidence = f"{evidence}+unverified_placeholder_url"

        return BackendCallResult(
            node_name=node.label, protocol=protocol, backend_system=backend_system,
            url=url_out, configured_url=configured_url, host=host, port=port,
            backend_source_flow=source_flow_name, direct=(depth == 0), depth=depth,
            call_chain=list(path), call_path=list(via_nodes), resolution=resolution,
            configuration_source=configuration_source, confidence=confidence, evidence=evidence,
        )
