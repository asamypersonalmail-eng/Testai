"""Renders the human-readable analysis_report.txt summary."""
from __future__ import annotations

from typing import Any, Dict

from .bar_inspector import BarInventory


def _section(title: str) -> str:
    bar = "-" * len(title)
    return f"\n{title}\n{bar}\n"


def render_report(catalog: Dict[str, Any], inventory: BarInventory, graph: Dict[str, Any]) -> str:
    lines = []
    lines.append("=" * 70)
    lines.append("ACE SERVICE CATALOG - ANALYSIS REPORT")
    lines.append("=" * 70)
    lines.append(f"BAR file          : {catalog['source']['bar_file']}")
    lines.append(f"Catalog name      : {catalog['catalog_name']} (v{catalog['catalog_version']})")
    lines.append(f"Swagger version   : {catalog['source']['swagger_version']}")
    lines.append(f"Base path         : {catalog['source']['base_path']}")
    lines.append(f"Broker main flow  : {catalog['source']['broker_main_flow'] or '(could not be determined)'}")

    lines.append(_section("BAR CONTENTS"))
    lines.append(f"Total entries in archive        : {len(inventory.all_entries)}")
    lines.append(f"Source .msgflow files            : {len(inventory.msgflow_names)}")
    lines.append(f"Source .subflow files             : {len(inventory.source_flow_entries) - len(inventory.msgflow_names)}")
    lines.append(f"Compiled .cmf files                : {len(inventory.compiled_flow_entries)}")
    lines.append(f"  ...of which compiled-only (no source): {len(inventory.flow_names_only_compiled())}")
    lines.append(f"WSDL files                        : {len(inventory.wsdl_entries)}")
    lines.append(f"XSD schema files                   : {len(inventory.xsd_entries)}")
    lines.append(f"ESQL modules                       : {len(inventory.esql_entries)}")
    lines.append(f"Other config/metadata files        : {len(inventory.config_entries)}")
    lines.append(f"broker.xml present                 : {'yes' if inventory.broker_xml_entries else 'no'}")
    lines.append(f"Embedded Swagger/OpenAPI candidates : {len(inventory.embedded_swagger_entries)}")

    stats = catalog["statistics"]
    lines.append(_section("SERVICE INVENTORY"))
    lines.append(f"Swagger operations                         : {stats['swagger_operations']}")
    lines.append(f"Matched to a broker flow/subflow           : {stats['matched_to_broker_flow_or_subflow']}")
    lines.append(f"Unmatched Swagger operations                : {stats['unmatched_swagger_operations']}")
    lines.append(f"Services with resolved backend URL(s)       : {stats['services_with_backend_urls']}")

    lines.append(_section("CALL GRAPH"))
    lines.append(f"Total accepted dependency edges             : {stats['call_graph_edges']}")
    lines.append(f"  ...verified normal SubFlow edges           : {stats['verified_normal_subflow_edges']}")
    lines.append(f"  ...Callable Flow edges                     : {stats['callable_flow_edges']}")
    lines.append(f"Services with recursive downstream calls    : {stats['services_with_recursive_flow_calls']}")
    lines.append(f"Backend calls inherited via downstream flows: {stats['inherited_backend_calls']}")

    audit = catalog["dependency_resolution_audit"]
    lines.append(_section("DEPENDENCY RESOLUTION AUDIT"))
    lines.append(f"Accepted edges     : {len(audit['accepted_edges'])}")
    lines.append(f"Rejected edges     : {len(audit['rejected_edges'])}")
    for e in audit["rejected_edges"][:25]:
        lines.append(f"  - REJECTED  {e.get('from_flow')} -> {e.get('to_flow')}  "
                     f"[{e.get('call_type')}] reason={e.get('reason')}")
    if len(audit["rejected_edges"]) > 25:
        lines.append(f"  ... and {len(audit['rejected_edges']) - 25} more (see dependency_graph.json / service_catalog.json)")

    lines.append(f"Unresolved edges   : {len(audit['unresolved_edges'])}")
    for e in audit["unresolved_edges"][:25]:
        lines.append(f"  - UNRESOLVED {e.get('from_flow')} -> {e.get('to_flow')}  "
                     f"[{e.get('call_type')}] reason={e.get('reason')}")
    if len(audit["unresolved_edges"]) > 25:
        lines.append(f"  ... and {len(audit['unresolved_edges']) - 25} more (see dependency_graph.json / service_catalog.json)")

    lines.append(_section("CMF-ONLY (COMPILED-ONLY) FLOWS"))
    if catalog["cmf_only_flows"]:
        lines.append("The following flows exist only as compiled CMF resources; their internal")
        lines.append("SubFlow topology could not be reconstructed and downstream dependencies")
        lines.append("beneath them may be incomplete:")
        for name in catalog["cmf_only_flows"]:
            lines.append(f"  - {name}")
    else:
        lines.append("None. Every flow reached during traversal had usable source (.msgflow/.subflow).")

    lines.append(_section("WARNINGS"))
    if catalog["analysis_warnings"]:
        for w in catalog["analysis_warnings"]:
            lines.append(f"  [{w['severity'].upper():7s}] ({w['type']}) {w['message']}")
    else:
        lines.append("None.")

    lines.append(_section("OUTPUT GRAPH SIZE"))
    lines.append(f"Nodes: {len(graph['nodes'])}   Edges: {len(graph['edges'])}")

    lines.append("")
    return "\n".join(lines)
