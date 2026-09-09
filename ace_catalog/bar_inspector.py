"""Inventories and extracts artifacts from an IBM ACE .bar (broker archive) file.

A .bar file is a zip, but for anything deployed as an ACE "Application" or
"Static/Shared Library" project (the normal case in real production
deployments) the actual message-flow resources are NOT at the top level —
each Application/Library is packaged as its own nested archive inside the
BAR, and that nested archive is what actually contains the
.msgflow/.subflow/.cmf/.esql/etc. files. Nesting can go one level deeper
still for a Static Library referenced from within an Application archive.

IBM ACE names these nested archives by project type — commonly
`<name>.appzip` (Application), `<name>.shlibzip` (Shared Library), and
`<name>.libzip` (Static Library) — rather than a generic `.jar`/`.zip`.
Rather than chase every naming variant IBM has used across versions, this
module detects a nested archive primarily by content (the ZIP local-file
magic bytes `PK\x03\x04`), with the known extensions kept only as a fast
path that skips the byte-sniff for the common case.

This module therefore walks the archive tree recursively: any member that
is itself a zip (by extension fast-path or by content sniff) is opened and
walked, to an arbitrary (depth-capped) nesting level, and every classified
resource is addressed by a compound key such as:

    HDB_Banking_API.appzip::flows/getPosition.subflow

`BarInspector.read_bytes()` / `read_text()` transparently resolve that
compound key back through the nested archives, so every other module in
this package just treats entries as opaque path-like strings.
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Dict, List, Optional

from .utils import ARCHIVE_SEP, basename_of_entry, qualify_flow_name

logger = logging.getLogger("ace_catalog.bar_inspector")

SOURCE_FLOW_EXTS = {".msgflow", ".subflow"}
COMPILED_FLOW_EXTS = {".cmf"}
SCHEMA_EXTS = {".xsd"}
WSDL_EXTS = {".wsdl"}
ESQL_EXTS = {".esql"}
CONFIG_EXTS = {".properties", ".policyxml", ".descriptor", ".yaml", ".yml", ".json", ".xml"}
# Fast-path extensions for nested archives; content-sniffing (see
# _looks_like_zip) is the real detector and catches anything not listed here.
NESTED_ARCHIVE_EXTS = {".jar", ".zip", ".appzip", ".shlibzip", ".libzip"}
_KNOWN_LEAF_EXTS = SOURCE_FLOW_EXTS | COMPILED_FLOW_EXTS | SCHEMA_EXTS | WSDL_EXTS | ESQL_EXTS | CONFIG_EXTS
_ZIP_MAGIC_PREFIXES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
MAX_NESTING_DEPTH = 6


@dataclass
class BarInventory:
    bar_path: str
    all_entries: List[str] = field(default_factory=list)
    nested_archives: List[str] = field(default_factory=list)               # compound keys of every .jar/.zip found
    source_flow_entries: Dict[str, str] = field(default_factory=dict)      # flow_name -> compound path (.msgflow + .subflow)
    msgflow_names: List[str] = field(default_factory=list)                 # subset of source_flow_entries that are .msgflow
    compiled_flow_entries: Dict[str, str] = field(default_factory=dict)    # flow_name -> compound path
    broker_xml_entries: List[str] = field(default_factory=list)
    wsdl_entries: List[str] = field(default_factory=list)
    xsd_entries: List[str] = field(default_factory=list)
    esql_entries: List[str] = field(default_factory=list)
    config_entries: List[str] = field(default_factory=list)
    embedded_swagger_entries: List[str] = field(default_factory=list)
    other_entries: List[str] = field(default_factory=list)

    def flow_names_only_compiled(self) -> List[str]:
        """Flows that exist as .cmf with no corresponding .msgflow/.subflow source."""
        return sorted(set(self.compiled_flow_entries) - set(self.source_flow_entries))


class BarInspector:
    """Opens a .bar file once and provides cached, transparent access to its
    (possibly nested-archive) member bytes/text via compound keys.
    """

    def __init__(self, bar_path: str):
        self.bar_path = bar_path
        self._root_zip = zipfile.ZipFile(bar_path, "r")
        self._archive_cache: Dict[str, zipfile.ZipFile] = {"": self._root_zip}
        self.inventory = self._build_inventory()

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        for zf in self._archive_cache.values():
            try:
                zf.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> "BarInspector":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- compound-key resolution ---------------------------------------------
    def _get_archive(self, archive_key: str) -> zipfile.ZipFile:
        """`archive_key` is a `::`-joined path to a nested .jar/.zip, or ''
        for the root BAR itself. Opened archives are cached for reuse."""
        if archive_key in self._archive_cache:
            return self._archive_cache[archive_key]
        parent_key, _, member = archive_key.rpartition(ARCHIVE_SEP)
        parent_zip = self._get_archive(parent_key)
        data = parent_zip.read(member)
        nested = zipfile.ZipFile(io.BytesIO(data))
        self._archive_cache[archive_key] = nested
        return nested

    # -- raw access ------------------------------------------------------------
    def read_bytes(self, entry: str) -> bytes:
        archive_key, _, member = entry.rpartition(ARCHIVE_SEP)
        return self._get_archive(archive_key).read(member)

    def read_text(self, entry: str, encoding: str = "utf-8") -> str:
        raw = self.read_bytes(entry)
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            return raw.decode(encoding, errors="replace")

    # -- inventory build -----------------------------------------------------
    def _build_inventory(self) -> BarInventory:
        inv = BarInventory(bar_path=self.bar_path)
        self._walk_archive(prefix="", zf=self._root_zip, inv=inv, depth=0)

        total_classified = (
            len(inv.source_flow_entries) + len(inv.compiled_flow_entries) + len(inv.wsdl_entries)
            + len(inv.xsd_entries) + len(inv.esql_entries) + len(inv.embedded_swagger_entries)
            + len(inv.broker_xml_entries)
        )
        logger.info(
            "BAR inventory: %d total entries (%d nested archive(s) expanded), %d source flows, "
            "%d compiled flows (%d compiled-only), %d wsdl, %d xsd, %d esql, "
            "%d embedded swagger candidate(s), %d broker.xml",
            len(inv.all_entries), len(inv.nested_archives),
            len(inv.source_flow_entries), len(inv.compiled_flow_entries),
            len(inv.flow_names_only_compiled()), len(inv.wsdl_entries), len(inv.xsd_entries),
            len(inv.esql_entries), len(inv.embedded_swagger_entries), len(inv.broker_xml_entries),
        )

        if total_classified == 0:
            sample = inv.all_entries[:40]
            logger.warning(
                "No recognizable ACE/Swagger artifacts were found anywhere in the BAR (including inside "
                "nested archives). This usually means the BAR uses a resource layout this tool doesn't "
                "recognize yet. First %d of %d raw entries found:\n  %s",
                len(sample), len(inv.all_entries), "\n  ".join(sample) if sample else "(archive is empty)",
            )

        return inv

    def _walk_archive(self, prefix: str, zf: zipfile.ZipFile, inv: BarInventory, depth: int) -> None:
        if depth > MAX_NESTING_DEPTH:
            logger.warning("Nested archive depth exceeded %d at '%s'; not descending further",
                            MAX_NESTING_DEPTH, prefix)
            return

        for entry in zf.namelist():
            if entry.endswith("/"):
                continue
            full_key = f"{prefix}{ARCHIVE_SEP}{entry}" if prefix else entry
            inv.all_entries.append(full_key)
            suffix = PurePosixPath(entry).suffix.lower()
            # Flow identity uses ACE's actual folder-qualified reference
            # convention (e.g. 'mdp/getMDPCreditCards.msgflow' ->
            # 'mdp_getMDPCreditCards') rather than the bare filename, so
            # folder-nested flows are addressable by the same name other
            # flows' subflow-instance xmi:type reference them by.
            stem = qualify_flow_name(str(PurePosixPath(entry).with_suffix("")))

            # Nested-archive detection: trust the extension for the common
            # ACE names (fast path, avoids a read), but for anything with an
            # unrecognized extension, sniff the actual ZIP magic bytes rather
            # than assume it's a leaf file. This is what actually catches
            # .appzip/.shlibzip/.libzip and any future IBM naming variant
            # without having to keep guessing at extensions.
            is_nested_archive = suffix in NESTED_ARCHIVE_EXTS
            if not is_nested_archive and suffix not in _KNOWN_LEAF_EXTS:
                is_nested_archive = self._looks_like_zip(zf, entry)

            if is_nested_archive:
                inv.nested_archives.append(full_key)
                try:
                    data = zf.read(entry)
                    nested = zipfile.ZipFile(io.BytesIO(data))
                except (zipfile.BadZipFile, KeyError, OSError) as exc:
                    logger.warning("Could not open nested archive '%s': %s", full_key, exc)
                    inv.other_entries.append(full_key)
                    continue
                self._archive_cache[full_key] = nested
                self._walk_archive(full_key, nested, inv, depth + 1)
                continue

            if suffix in SOURCE_FLOW_EXTS:
                if stem in inv.source_flow_entries:
                    logger.warning(
                        "Duplicate flow name '%s' found at both '%s' and '%s'; keeping the first one seen. "
                        "This can happen if the same project is packaged into more than one nested archive.",
                        stem, inv.source_flow_entries[stem], full_key,
                    )
                else:
                    inv.source_flow_entries[stem] = full_key
                    if suffix == ".msgflow":
                        inv.msgflow_names.append(stem)
            elif suffix in COMPILED_FLOW_EXTS:
                inv.compiled_flow_entries.setdefault(stem, full_key)
            elif suffix in WSDL_EXTS:
                inv.wsdl_entries.append(full_key)
            elif suffix in SCHEMA_EXTS:
                inv.xsd_entries.append(full_key)
            elif suffix in ESQL_EXTS:
                inv.esql_entries.append(full_key)
            elif PurePosixPath(entry).name.lower() == "broker.xml":
                inv.broker_xml_entries.append(full_key)
            elif suffix in (".json", ".yaml", ".yml"):
                if self._looks_like_openapi(full_key):
                    inv.embedded_swagger_entries.append(full_key)
                else:
                    inv.config_entries.append(full_key)
            elif suffix in CONFIG_EXTS:
                inv.config_entries.append(full_key)
            else:
                inv.other_entries.append(full_key)

    def _looks_like_zip(self, zf: zipfile.ZipFile, entry: str) -> bool:
        """Peek at an entry's first bytes to check for the ZIP local-file
        header magic, without reading/decompressing the whole member."""
        try:
            with zf.open(entry) as fh:
                header = fh.read(4)
        except Exception:  # noqa: BLE001 - unreadable member is just not an archive to us
            return False
        return header in _ZIP_MAGIC_PREFIXES

    def _looks_like_openapi(self, entry: str) -> bool:
        """Sniff a .json/.yaml/.yml entry for Swagger 2.0 / OpenAPI 3.x markers
        without fully parsing it yet (that happens in swagger_parser)."""
        try:
            text = self.read_text(entry)
        except Exception:  # noqa: BLE001 - never let sniffing crash the inventory pass
            return False
        head = text[:4000]
        return (
            '"swagger"' in head or "swagger:" in head
            or '"openapi"' in head or "openapi:" in head
            or ('"paths"' in head and ('"info"' in head or "info:" in head))
        )

    # -- convenience -----------------------------------------------------
    def best_embedded_swagger(self) -> Optional[str]:
        """Pick the most likely embedded API definition entry, if any."""
        if not self.inventory.embedded_swagger_entries:
            return None
        # Prefer files literally named swagger/openapi over incidental matches.
        for entry in self.inventory.embedded_swagger_entries:
            name = PurePosixPath(basename_of_entry(entry)).stem.lower()
            if "swagger" in name or "openapi" in name:
                return entry
        return self.inventory.embedded_swagger_entries[0]

    def read_json_lenient(self, entry: str) -> Optional[dict]:
        try:
            return json.loads(self.read_text(entry))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not parse %s as JSON: %s", entry, exc)
            return None
