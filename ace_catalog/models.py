"""Shared data structures for the ACE service-catalog pipeline.

Kept as plain dataclasses (no behaviour beyond simple helpers) so every stage
of the pipeline can pass structured data around instead of loose dicts, while
still being trivially convertible to JSON via ``dataclasses.asdict`` where useful.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------- #
# Swagger / OpenAPI side
# --------------------------------------------------------------------------- #

@dataclass
class OperationSpec:
    """One REST operation extracted from a Swagger 2.0 / OpenAPI 3.x document."""

    operation_id: str
    method: str
    base_path: str
    path: str
    full_path: str
    schemes: List[str] = field(default_factory=list)
    consumes: List[str] = field(default_factory=list)
    produces: List[str] = field(default_factory=list)
    parameters: List[Dict[str, Any]] = field(default_factory=list)
    summary: Optional[str] = None
    description: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    request_schema: Any = None
    response_schemas: Dict[str, Any] = field(default_factory=dict)
    # Populated with a synthetic id when the source document has no
    # operationId, so downstream code always has a stable service_name.
    operation_id_synthetic: bool = False


# --------------------------------------------------------------------------- #
# ACE message flow side
# --------------------------------------------------------------------------- #

@dataclass
class FlowNode:
    """A single <nodes> element from a parsed .msgflow / .subflow document."""

    node_id: str
    label: str
    xmi_type: str
    raw_attributes: Dict[str, str] = field(default_factory=dict)

    # Classification result. One of:
    #   subflow_ref | callable_invoke | callable_input | callable_other |
    #   http_request | soap_request | other
    kind: str = "other"

    # Populated depending on `kind`:
    target_flow_name: Optional[str] = None        # subflow_ref
    target_endpoint_name: Optional[str] = None     # callable_invoke
    callable_input_endpoint: Optional[str] = None  # callable_input
    url: Optional[str] = None                      # http_request / soap_request
    protocol: Optional[str] = None                 # http_request / soap_request

    # Evidence trail — always populated, never left implicit.
    evidence: str = "unclassified"
    classification_signals: List[str] = field(default_factory=list)


@dataclass
class FlowDefinition:
    """One parsed .msgflow or .subflow source file."""

    flow_name: str
    source_file: str
    file_type: str  # "msgflow" | "subflow"
    nodes: List[FlowNode] = field(default_factory=list)
    parse_warnings: List[str] = field(default_factory=list)

    def nodes_by_kind(self, kind: str) -> List[FlowNode]:
        return [n for n in self.nodes if n.kind == kind]


@dataclass
class BackendCallResult:
    """A resolved (or partially resolved) outbound HTTP/SOAP backend call."""

    node_name: str
    protocol: str
    backend_system: str
    url: Optional[str]
    configured_url: Optional[str]
    host: Optional[str]
    port: Optional[int]
    backend_source_flow: str
    direct: bool
    depth: int
    call_chain: List[str]
    call_path: List[str]
    resolution: str  # "static" | "dynamic"
    configuration_source: Optional[str]
    confidence: str  # "high" | "medium" | "low"
    evidence: str


@dataclass
class DependencyEdge:
    """One flow-to-flow (subflow or callable-flow) dependency edge."""

    from_flow: str
    to_flow: Optional[str]
    call_type: str  # "subflow" | "callable_flow"
    node_name: str
    confidence: str  # "high" | "medium" | "low"
    evidence: str
    depth: int
    status: str  # "accepted" | "rejected" | "unresolved"
    reason: Optional[str] = None


@dataclass
class TopologyResolution:
    status: str = "complete"          # "complete" | "partial"
    reason: Optional[str] = None      # e.g. "cmf_only" | "unresolved_reference"
    message: Optional[str] = None


@dataclass
class ServiceResult:
    """Everything the catalog builder needs to render one `services[]` entry."""

    operation: OperationSpec
    root_flow_name: Optional[str]
    matched_in_broker_xml: bool
    match_evidence: str
    topology: TopologyResolution
    downstream_calls: List[DependencyEdge] = field(default_factory=list)
    backends: List[BackendCallResult] = field(default_factory=list)
