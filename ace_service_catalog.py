#!/usr/bin/env python3
"""ace_service_catalog.py

Analyzes an IBM ACE BAR file (optionally together with an external
Swagger/OpenAPI document) and reconstructs, as far as the available
artifacts genuinely support:

    REST Service -> Main Flow -> Subflows / Callable Flows
                  -> recursive downstream dependencies
                  -> HTTP / SOAP backend calls

Every relationship in the output carries a confidence/evidence indicator.
Nothing is guessed silently: where the BAR does not contain enough
information to resolve a dependency, that is reported explicitly
(`"unresolved": true`-style records) rather than invented.

Usage
-----
    python ace_service_catalog.py --bar HDB_Banking_API.bar --output ./output
    python ace_service_catalog.py --bar HDB_Banking_API.bar --swagger swagger.yaml --output ./output

Outputs (written to --output)
------------------------------
    service_catalog.json    Full catalog: services, implementation, backends, audit trail.
    dependency_graph.json   Compact nodes/edges view of the same data.
    analysis_report.txt     Human-readable summary.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List

from ace_catalog.bar_inspector import BarInspector
from ace_catalog.broker_xml import parse_operation_flow_mapping
from ace_catalog.catalog_builder import build_catalog, build_dependency_graph, determine_main_flow
from ace_catalog.dependency_resolver import DependencyResolver, FlowRegistry, ResolutionAudit, resolve_root_flow
from ace_catalog.msgflow_parser import MsgFlowParser
from ace_catalog.report_writer import render_report
from ace_catalog.swagger_parser import load_document, parse_operations

logger = logging.getLogger("ace_catalog")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ace_service_catalog.py",
        description="Reconstruct a Service Catalog (REST -> flows -> backends) from an IBM ACE BAR file.",
    )
    parser.add_argument("--bar", required=True, help="Path to the IBM ACE .bar file to analyze.")
    parser.add_argument("--swagger", required=False, default=None,
                         help="Optional path to an external Swagger/OpenAPI file. "
                              "If omitted, the tool looks for one embedded inside the BAR.")
    parser.add_argument("--output", required=True, help="Output directory for the generated files.")
    parser.add_argument("--catalog-name", default=None,
                         help="Name recorded as catalog_name. Defaults to the BAR file's base name.")
    parser.add_argument("--catalog-version", default="1.0.0",
                         help="Version recorded as catalog_version. Default: 1.0.0")
    parser.add_argument("--log-level", default="INFO",
                         choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity.")
    return parser.parse_args(argv)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def load_swagger_source(bar: BarInspector, swagger_arg: str | None) -> tuple[str, str]:
    """Return (document_text, source_description). Exits with a clear error
    if no Swagger/OpenAPI source can be found anywhere.
    """
    if swagger_arg:
        path = Path(swagger_arg)
        if not path.is_file():
            logger.error("Swagger/OpenAPI file not found: %s", swagger_arg)
            sys.exit(1)
        return path.read_text(encoding="utf-8"), f"external file '{swagger_arg}'"

    embedded = bar.best_embedded_swagger()
    if embedded:
        return bar.read_text(embedded), f"embedded in BAR at '{embedded}'"

    logger.error(
        "No Swagger/OpenAPI definition found embedded in the BAR, and none was provided via --swagger. "
        "A REST service inventory cannot be built without one."
    )
    sys.exit(1)


def parse_all_flows(bar: BarInspector) -> tuple[list, Dict[str, List[str]]]:
    """Parse every .msgflow/.subflow source file found in the BAR."""
    parser = MsgFlowParser()
    flows = []
    parse_warnings: Dict[str, List[str]] = {}
    for flow_name, entry in bar.inventory.source_flow_entries.items():
        try:
            text = bar.read_text(entry)
        except Exception as exc:  # noqa: BLE001 - a single unreadable member must not abort the run
            logger.warning("Could not read %s from BAR: %s", entry, exc)
            parse_warnings[flow_name] = [f"Could not read archive member: {exc}"]
            continue
        flow = parser.parse(text, entry, flow_name)
        flows.append(flow)
        if flow.parse_warnings:
            parse_warnings[flow_name] = flow.parse_warnings
    return flows, parse_warnings


def load_broker_mapping(bar: BarInspector) -> Dict[str, str]:
    entries = bar.inventory.broker_xml_entries
    if not entries:
        return {}
    if len(entries) > 1:
        logger.warning("Multiple broker.xml-named files found; using the first one: %s", entries[0])
    return parse_operation_flow_mapping(bar.read_text(entries[0]))


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    configure_logging(args.log_level)

    bar_path = Path(args.bar)
    if not bar_path.is_file():
        logger.error("BAR file not found: %s", args.bar)
        return 1

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    catalog_name = args.catalog_name or bar_path.stem

    try:
        with BarInspector(str(bar_path)) as bar:
            swagger_text, swagger_source_desc = load_swagger_source(bar, args.swagger)
            logger.info("Using Swagger/OpenAPI source: %s", swagger_source_desc)

            document = load_document(swagger_text, filename_hint=swagger_source_desc)
            operations, swagger_meta = parse_operations(document)
            if not operations:
                logger.error("Swagger/OpenAPI document contained zero operations; nothing to catalog.")
                return 1

            flows, flow_parse_warnings = parse_all_flows(bar)
            cmf_only_names = set(bar.inventory.flow_names_only_compiled())
            registry = FlowRegistry(flows, cmf_only_names)

            broker_mapping = load_broker_mapping(bar)
            main_flow_name, main_flow_evidence = determine_main_flow(bar.inventory, broker_mapping, registry)
            logger.info("Main flow: %s (evidence=%s)", main_flow_name, main_flow_evidence)

            audit = ResolutionAudit()
            resolver = DependencyResolver(registry)

            service_results = []
            for op in operations:
                root_flow_name, matched_in_broker_xml, match_evidence = resolve_root_flow(op, registry, broker_mapping)
                service_results.append(
                    resolver.resolve_service(op, root_flow_name, matched_in_broker_xml, match_evidence, audit)
                )

            catalog = build_catalog(
                catalog_name=catalog_name,
                catalog_version=args.catalog_version,
                swagger_meta=swagger_meta,
                bar_path=str(bar_path),
                main_flow_name=main_flow_name,
                main_flow_evidence=main_flow_evidence,
                service_results=service_results,
                registry=registry,
                audit=audit,
                flow_parse_warnings=flow_parse_warnings,
            )
            graph = build_dependency_graph(catalog)
            report_text = render_report(catalog, bar.inventory, graph)
    except Exception:
        logger.exception("Unrecoverable error while analyzing '%s'", args.bar)
        return 1

    (output_dir / "service_catalog.json").write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False, sort_keys=False), encoding="utf-8",
    )
    (output_dir / "dependency_graph.json").write_text(
        json.dumps(graph, indent=2, ensure_ascii=False, sort_keys=False), encoding="utf-8",
    )
    (output_dir / "analysis_report.txt").write_text(report_text, encoding="utf-8")

    logger.info("Wrote service_catalog.json, dependency_graph.json, analysis_report.txt to %s", output_dir)
    logger.info(
        "%d services, %d matched to a flow, %d call-graph edges, %d cmf-only flows, %d unresolved edges",
        catalog["statistics"]["swagger_operations"],
        catalog["statistics"]["matched_to_broker_flow_or_subflow"],
        catalog["statistics"]["call_graph_edges"],
        len(catalog["cmf_only_flows"]),
        len(catalog["dependency_resolution_audit"]["unresolved_edges"]),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
