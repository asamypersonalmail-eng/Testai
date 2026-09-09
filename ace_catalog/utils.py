"""Small, stateless helpers shared across the pipeline."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

# Hostnames/values that commonly appear as design-time placeholders in ACE
# flows before a policy / configurable service / ESQL substitution takes
# over at runtime. Matching one of these is a *hint* only — rule 8 in the
# spec still requires separate evidence of dynamic configuration before we
# stop treating it as a literal backend system name.
PLACEHOLDER_HOSTS = {
    "test", "localhost", "127.0.0.1", "example", "example.com",
    "changeme", "placeholder", "tbd", "dummy", "sample",
}

# Attribute-name fragments (case-insensitive) that, on an HTTP/SOAP request
# node, indicate the target URL is resolved via ACE's configurable-services /
# policy layer rather than being a hard literal in the flow XML.
DYNAMIC_CONFIG_ATTR_HINTS = (
    "policy", "configurableservice", "urlspecificationtype",
    "policyname", "endpointidentifier",
)


ARCHIVE_SEP = "::"


def qualify_flow_name(path_no_ext: str) -> str:
    """Convert a folder-qualified, extension-stripped path into ACE's actual
    compiled flow-reference convention, e.g. 'mdp/getMDPConfig' -> 'mdp_getMDPConfig'.

    Verified directly against a real production BAR: a subflow living in a
    project subfolder is referenced by other flows' subflow-instance
    xmi:type using the folder path with '/' replaced by '_' (e.g.
    'mdp/settleCreditCard.subflow' is referenced as
    'mdp_settleCreditCard.subflow:FCMComposite_1', and
    'Masking/ConfigRead.subflow' as 'Masking_ConfigRead...', confirmed
    alongside a *separate*, unrelated top-level 'ConfigRead.subflow' that
    must resolve to a different flow rather than collide with it). A flat
    name (no folder) passes through unchanged.
    """
    parts = [p for p in path_no_ext.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "_".join(parts)


def basename_of_entry(entry: str) -> str:
    """Return the raw member name of a (possibly nested-archive) compound
    entry key such as 'MyApp.jar::flows/getPosition.subflow', stripping any
    '<archive>::' prefix so path/name derivation (extension, stem) always
    operates on the real file name rather than the archive chain.
    """
    return entry.rsplit(ARCHIVE_SEP, 1)[-1]


def local_name(tag: str) -> str:
    """Strip an XML namespace URI from an ElementTree tag, e.g. '{ns}nodes' -> 'nodes'."""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def find_attr_ci(attrs: Dict[str, str], *fragments: str) -> Optional[str]:
    """Return the value of the first attribute whose key contains any of the
    given case-insensitive fragments. Used because ACE node property names
    vary subtly across toolkit/broker versions (e.g. 'URL' vs 'webServiceURL'
    vs 'requestURL') and we deliberately do not hard-code a single spelling.
    """
    for key, value in attrs.items():
        key_l = key.lower()
        for frag in fragments:
            if frag.lower() in key_l:
                if value is not None and str(value).strip() != "":
                    return value
    return None


def find_attr_key_ci(attrs: Dict[str, str], *fragments: str) -> Optional[str]:
    """Like find_attr_ci but returns the matching attribute *name* instead of value."""
    for key, value in attrs.items():
        key_l = key.lower()
        for frag in fragments:
            if frag.lower() in key_l:
                if value is not None and str(value).strip() != "":
                    return key
    return None


def looks_like_placeholder_host(host: Optional[str]) -> bool:
    if not host:
        return False
    return host.strip().lower() in PLACEHOLDER_HOSTS


def dynamic_config_hint(attrs: Dict[str, str]) -> Optional[str]:
    """Return a human-readable configuration source if the node's raw
    attributes show evidence of dynamic (policy/configurable-service) URL
    resolution, else None. This is a *heuristic*, not a certainty — callers
    must still record confidence="medium" rather than "high" when relying on it.
    """
    for key, value in attrs.items():
        key_l = key.lower()
        if not value or str(value).strip() == "":
            continue
        for hint in DYNAMIC_CONFIG_ATTR_HINTS:
            if hint in key_l:
                return f"{key}={value}"
    return None


def parse_url(url: Optional[str]) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """Return (host, port, scheme) for a URL string, tolerant of missing scheme."""
    if not url:
        return None, None, None
    candidate = url if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url) else "http://" + url
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None, None, None
    host = parsed.hostname
    port = parsed.port
    scheme = parsed.scheme
    return host, port, scheme


def derive_backend_system(host: Optional[str]) -> str:
    """Derive a short backend-system label from a hostname.

    Mirrors the convention observed in hand-curated catalogs: an IP address
    is kept verbatim, a plain hostname/service-name is upper-cased and the
    first DNS label is used (e.g. 'ebc.internal.bank' -> 'EBC').
    """
    if not host:
        return "UNKNOWN"
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host):
        return host
    first_label = host.split(".")[0]
    return first_label.upper() if first_label else host.upper()


def protocol_for(url: Optional[str], node_default: Optional[str] = None) -> str:
    _, _, scheme = parse_url(url)
    if scheme:
        return scheme.upper()
    if node_default:
        return node_default.upper()
    return "HTTP"


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def resolve_json_ref(ref: str, document: Dict[str, Any]) -> Optional[Any]:
    """Resolve a local ('#/a/b/c') JSON pointer against `document`.

    Returns None (never raises) if any segment is missing — callers must
    treat that as "could not resolve", not as an empty-but-valid schema.
    """
    if not ref.startswith("#/"):
        return None
    node: Any = document
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def stable_unique(items):
    """Deduplicate while preserving first-seen order (avoids relying on set
    iteration order anywhere output-facing, keeping runs deterministic)."""
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
