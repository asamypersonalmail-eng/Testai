"""ace_catalog: IBM ACE BAR analysis -> Service Catalog generation.

Package layout
--------------
models.py               Plain dataclasses shared across the pipeline.
utils.py                 Small stateless helpers (XML, URL, JSON-ref utilities).
bar_inspector.py         Opens a .bar (zip) file and inventories/extracts artifacts.
swagger_parser.py        Normalizes Swagger 2.0 / OpenAPI 3.x into OperationSpec objects.
msgflow_parser.py        Parses .msgflow / .subflow XML into FlowDefinition objects.
dependency_resolver.py   Recursive, cycle-safe call-graph + backend resolution.
catalog_builder.py       Assembles the final service_catalog.json / dependency_graph.json.
report_writer.py         Renders analysis_report.txt.

Every module treats "unresolved" as an acceptable, first-class outcome. Nothing
in this package invents a relationship it cannot point to evidence for.
"""
