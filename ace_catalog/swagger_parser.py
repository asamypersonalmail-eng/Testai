"""Normalizes a Swagger 2.0 or OpenAPI 3.x document into OperationSpec objects.

Design choices worth calling out:

* $ref values are preserved as bare strings (e.g. "#/definitions/Profile")
  rather than silently inlined, mirroring how hand-curated catalogs in this
  domain represent them. We only "resolve" a $ref when it is needed to
  build a *parameter* shape, and even then we keep the original ref string
  under `schema_ref` instead of pretending we inlined it.
* Nothing here raises on a missing operationId, missing basePath, etc. —
  every gap degrades to a documented default or an explicit None/[] rather
  than crashing the whole run.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .models import OperationSpec
from .utils import resolve_json_ref

logger = logging.getLogger("ace_catalog.swagger_parser")

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head")


def load_document(text: str, filename_hint: str = "") -> Dict[str, Any]:
    """Parse JSON first (cheap, no dependency); fall back to YAML.

    YAML support is optional — PyYAML is only imported if we actually need
    it, so a pure-JSON Swagger file works with zero extra dependencies.
    """
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning("%s looked like JSON but failed to parse (%s); trying YAML", filename_hint, exc)

    try:
        import yaml  # local import: optional dependency
    except ImportError as exc:
        raise RuntimeError(
            f"Could not parse '{filename_hint}' as JSON and PyYAML is not installed "
            "to attempt a YAML parse. Install it with: pip install pyyaml"
        ) from exc

    return yaml.safe_load(text)


def _is_openapi3(document: Dict[str, Any]) -> bool:
    return isinstance(document.get("openapi"), str) and document["openapi"].startswith("3")


def _base_path_and_host(document: Dict[str, Any]) -> Tuple[str, Optional[str], str]:
    """Returns (base_path, host, swagger_version_label)."""
    if _is_openapi3(document):
        servers = document.get("servers") or []
        base_path, host = "", None
        if servers and isinstance(servers[0], dict) and servers[0].get("url"):
            parsed = urlparse(servers[0]["url"])
            base_path = parsed.path or ""
            host = parsed.netloc or None
        return base_path, host, document.get("openapi", "3.0.0")

    return document.get("basePath", "") or "", document.get("host"), document.get("swagger", "2.0")


def _pure_ref(schema: Any) -> Optional[str]:
    """Return the $ref string if `schema` is *only* a $ref wrapper, else None."""
    if isinstance(schema, dict) and set(schema.keys()) <= {"$ref"} and "$ref" in schema:
        return schema["$ref"]
    return None


def _normalize_parameter(raw: Dict[str, Any], document: Dict[str, Any]) -> Dict[str, Any]:
    if "$ref" in raw:
        resolved = resolve_json_ref(raw["$ref"], document)
        if isinstance(resolved, dict):
            raw = resolved
        else:
            return {"name": raw["$ref"].rsplit("/", 1)[-1], "in": "unknown", "ref": raw["$ref"], "unresolved": True}

    param: Dict[str, Any] = {
        "name": raw.get("name"),
        "in": raw.get("in"),
        "required": bool(raw.get("required", raw.get("in") == "path")),
    }
    if raw.get("type"):
        param["type"] = raw["type"]
    if raw.get("description") is not None:
        param["description"] = raw["description"]
    if raw.get("enum") is not None:
        param["enum"] = raw["enum"]
    if raw.get("default") is not None:
        param["default"] = raw["default"]
    if raw.get("format"):
        param["format"] = raw["format"]

    schema = raw.get("schema")
    if schema is not None:
        ref = _pure_ref(schema)
        if ref:
            param["schema_ref"] = ref
        else:
            param["schema"] = schema
            if not param.get("type"):
                param["type"] = schema.get("type")
    return param


def _openapi3_body_as_parameter(operation: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    body = operation.get("requestBody")
    if not body:
        return None
    content = body.get("content", {})
    media = content.get("application/json") or (next(iter(content.values())) if content else {})
    schema = media.get("schema") if isinstance(media, dict) else None
    param: Dict[str, Any] = {"name": "body", "in": "body", "required": bool(body.get("required", False))}
    if schema is not None:
        ref = _pure_ref(schema)
        if ref:
            param["schema_ref"] = ref
        else:
            param["schema"] = schema
    return param


def _extract_request_schema(parameters: List[Dict[str, Any]]) -> Any:
    for param in parameters:
        if param.get("in") == "body":
            if "schema_ref" in param:
                return param["schema_ref"]
            if "schema" in param:
                return param["schema"]
    return None


def _extract_response_schemas(operation: Dict[str, Any], document: Dict[str, Any], is_oas3: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for code, resp in (operation.get("responses") or {}).items():
        if not isinstance(resp, dict):
            continue
        schema = None
        if is_oas3:
            content = resp.get("content", {})
            media = content.get("application/json") or (next(iter(content.values())) if content else None)
            if isinstance(media, dict):
                schema = media.get("schema")
        else:
            schema = resp.get("schema")
        if schema is None:
            continue
        ref = _pure_ref(schema)
        out[str(code)] = ref if ref else schema
    return out


def _synthesize_operation_id(method: str, path: str) -> str:
    slug = re.sub(r"[{}]", "", path).strip("/").replace("/", "_")
    return f"{method}_{slug}" if slug else method


def parse_operations(document: Dict[str, Any]) -> Tuple[List[OperationSpec], Dict[str, Any]]:
    """Return (operations, meta) where meta = {swagger_version, base_path, host}."""
    is_oas3 = _is_openapi3(document)
    base_path, host, version_label = _base_path_and_host(document)
    global_schemes = document.get("schemes", ["https", "http"])
    global_consumes = document.get("consumes", ["application/json"])
    global_produces = document.get("produces", ["application/json"])

    operations: List[OperationSpec] = []
    paths = document.get("paths") or {}

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        shared_params_raw = path_item.get("parameters", []) or []

        for method in HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue

            operation_id = op.get("operationId")
            synthetic = not bool(operation_id)
            if synthetic:
                operation_id = _synthesize_operation_id(method, path)
                logger.warning(
                    "Operation %s %s has no operationId; using synthesized id '%s'",
                    method.upper(), path, operation_id,
                )

            raw_params = list(shared_params_raw) + list(op.get("parameters", []) or [])
            parameters = [_normalize_parameter(p, document) for p in raw_params]
            if is_oas3:
                body_param = _openapi3_body_as_parameter(op)
                if body_param:
                    parameters.append(body_param)

            full_path = (base_path.rstrip("/") + path) if base_path else path

            operations.append(OperationSpec(
                operation_id=operation_id,
                method=method.upper(),
                base_path=base_path,
                path=path,
                full_path=full_path,
                schemes=op.get("schemes", global_schemes),
                consumes=op.get("consumes", global_consumes) if not is_oas3 else ["application/json"],
                produces=op.get("produces", global_produces) if not is_oas3 else ["application/json"],
                parameters=parameters,
                summary=op.get("summary"),
                description=op.get("description"),
                tags=op.get("tags", []) or [],
                request_schema=_extract_request_schema(parameters),
                response_schemas=_extract_response_schemas(op, document, is_oas3),
                operation_id_synthetic=synthetic,
            ))

    meta = {"swagger_version": str(version_label), "base_path": base_path, "host": host}
    logger.info("Parsed %d REST operations from Swagger/OpenAPI document (version=%s)", len(operations), version_label)
    return operations, meta
