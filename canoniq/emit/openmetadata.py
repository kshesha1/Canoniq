"""OpenMetadata JSON emitter (Module G) — the third emitter alongside
MetricFlow and OSI.

Files only, no API client: Canoniq is a POPULATION ENGINE for empty
OpenMetadata catalogs, not a catalog. Live OM API integration is v2.

Emits, per bootstrap run:
  glossary.json        — one Glossary entity
  glossary_terms.json  — one GlossaryTerm per confirmed/probable metric,
                         synonyms fed by drift-detected renames
  tables.json          — table/column entities with tags linking columns
                         to glossary terms
  lineage.json         — report metric instance -> physical column edges,
                         the report modeled as a dashboard-like entity

Every term carries a `canoniq_provenance` extension block (confidence
band, constraints satisfied, source locators). OM allows custom
properties; provenance is never silently dropped to fit the schema.

Emitted payloads are validated against pinned subset schemas of the
OpenMetadata JSON schemas (OM_SCHEMA_VERSION) in tests.
"""

import json
import re
from pathlib import Path

import jsonschema

from canoniq.models import ConfidenceBand, DriftFinding, DriftKind, ResolvedMapping

# Pinned OpenMetadata schema version the emitted shapes track
# (https://github.com/open-metadata/OpenMetadata/tree/1.5.0/openmetadata-spec).
OM_SCHEMA_VERSION = "1.5.0"

_ACCEPTED = (ConfidenceBand.CONFIRMED, ConfidenceBand.PROBABLE)

_NAME_RE = r"^[a-z][a-z0-9_]*$"

# Subset schemas: the fields Canoniq emits, constrained the way OM 1.5
# constrains them. Full OM schemas pull in dozens of $refs; pinning a
# validated subset keeps the guarantee without vendoring the tree.
GLOSSARY_SCHEMA = {
    "type": "object",
    "required": ["name", "displayName", "description"],
    "properties": {
        "name": {"type": "string", "pattern": _NAME_RE},
        "displayName": {"type": "string"},
        "description": {"type": "string", "minLength": 1},
    },
}

GLOSSARY_TERM_SCHEMA = {
    "type": "object",
    "required": ["name", "displayName", "description", "glossary", "extension"],
    "properties": {
        "name": {"type": "string", "pattern": _NAME_RE},
        "displayName": {"type": "string"},
        "description": {"type": "string", "minLength": 1},
        "glossary": {"type": "string"},
        "synonyms": {"type": "array", "items": {"type": "string"}},
        "relatedTerms": {"type": "array", "items": {"type": "string"}},
        "extension": {
            "type": "object",
            "required": ["canoniq_provenance"],
            "properties": {
                "canoniq_provenance": {
                    "type": "object",
                    "required": [
                        "confidence_band", "constraints_satisfied",
                        "constraints_total", "physical_expression",
                        "source_locators",
                    ],
                    "properties": {
                        "confidence_band": {
                            "enum": ["CONFIRMED", "PROBABLE", "WEAK"]
                        },
                        "constraints_satisfied": {"type": "integer"},
                        "constraints_total": {"type": "integer"},
                        "physical_expression": {"type": "string"},
                        "report_id": {"type": "string"},
                        "tiers_run": {"type": "array"},
                        "corroborations": {"type": "array"},
                        "source_locators": {"type": "array"},
                    },
                }
            },
        },
    },
}

TABLE_SCHEMA = {
    "type": "object",
    "required": ["name", "columns"],
    "properties": {
        "name": {"type": "string"},
        "databaseSchema": {"type": "string"},
        "columns": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "dataType"],
                "properties": {
                    "name": {"type": "string"},
                    "dataType": {"type": "string"},
                    "tags": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["tagFQN", "source"],
                            "properties": {
                                "tagFQN": {"type": "string"},
                                "source": {"enum": ["Glossary", "Classification"]},
                                "labelType": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
}

LINEAGE_SCHEMA = {
    "type": "object",
    "required": ["edges"],
    "properties": {
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["fromEntity", "toEntity"],
                "properties": {
                    "fromEntity": {
                        "type": "object",
                        "required": ["type", "fullyQualifiedName"],
                    },
                    "toEntity": {
                        "type": "object",
                        "required": ["type", "fullyQualifiedName"],
                    },
                    "lineageDetails": {"type": "object"},
                },
            },
        }
    },
}

_PAYLOAD_SCHEMAS = {
    "glossary": GLOSSARY_SCHEMA,
    "glossary_terms": GLOSSARY_TERM_SCHEMA,
    "tables": TABLE_SCHEMA,
    "lineage": LINEAGE_SCHEMA,
}

_DATA_TYPE_MAP = {"numeric": "DOUBLE", "string": "VARCHAR", "other": "UNKNOWN"}


def term_name(metric_name: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "_", metric_name.lower()).strip("_")
    return name if re.match(_NAME_RE, name) else f"m_{name}"


def _dedupe_mappings(mappings: list[ResolvedMapping]) -> list[ResolvedMapping]:
    """One term per metric name; prefer the latest edition's mapping."""
    best: dict[str, ResolvedMapping] = {}
    for m in sorted(mappings, key=lambda m: m.report_id):
        if m.band in _ACCEPTED:
            best[m.metric_name] = m
    return list(best.values())


def _rename_synonyms(drift_findings: list[DriftFinding]) -> dict[str, list[str]]:
    """new metric name -> [old names], from drift-detected renames."""
    out: dict[str, list[str]] = {}
    for f in drift_findings:
        if f.kind == DriftKind.RENAMED and f.old_name and f.new_name:
            out.setdefault(f.new_name, []).append(f.old_name)
    return out


def _provenance(mapping: ResolvedMapping, locators: dict[str, str]) -> dict:
    best = mapping.best
    return {
        "confidence_band": mapping.band.value,
        "constraints_satisfied": best.satisfied,
        "constraints_total": best.total,
        "physical_expression": best.expr.canonical_key(),
        "report_id": mapping.report_id,
        "tiers_run": mapping.tiers_run,
        "corroborations": mapping.corroborations,
        "source_locators": sorted(
            {
                locators[c.instance_id]
                for c in best.constraints
                if c.satisfied and c.instance_id in locators
            }
        ),
    }


def build_payloads(
    mappings: list[ResolvedMapping],
    tables: dict[str, dict[str, str]],
    drift_findings: list[DriftFinding] | None = None,
    instance_locators: dict[str, str] | None = None,
    glossary_name: str = "canoniq_bootstrap",
    service_name: str = "canoniq",
) -> dict:
    """Build the four OM payloads (pure; file writing is separate)."""
    drift_findings = drift_findings or []
    locators = instance_locators or {}
    accepted = _dedupe_mappings(mappings)
    synonyms = _rename_synonyms(drift_findings)

    glossary = {
        "name": term_name(glossary_name),
        "displayName": "Canoniq bootstrapped business glossary",
        "description": (
            "Business metrics recovered from published board reports and "
            "empirically mapped to physical columns by Canoniq. Generated "
            "artifact — review before import."
        ),
    }

    terms = []
    column_tags: dict[tuple[str, str], set[str]] = {}
    for mapping in accepted:
        name = term_name(mapping.metric_name)
        description = mapping.prose_formula_hint or (
            f"{mapping.metric_name}, as published in report "
            f"{mapping.report_id}; reproduced by "
            f"{mapping.best.expr.canonical_key()}."
        )
        terms.append(
            {
                "name": name,
                "displayName": mapping.metric_name,
                "description": description,
                "glossary": glossary["name"],
                "synonyms": sorted(synonyms.get(mapping.metric_name, [])),
                "extension": {"canoniq_provenance": _provenance(mapping, locators)},
            }
        )
        term_fqn = f"{glossary['name']}.{name}"
        for term in mapping.best.expr.terms():
            if term.column != "*":
                column_tags.setdefault((term.table, term.column), set()).add(term_fqn)
        for binding in mapping.best.dimension_bindings:
            column_tags.setdefault(
                (binding.group_table, binding.group_column), set()
            ).add(term_fqn)

    table_entities = []
    for table, columns in sorted(tables.items()):
        table_entities.append(
            {
                "name": table,
                "databaseSchema": service_name,
                "columns": [
                    {
                        "name": column,
                        "dataType": _DATA_TYPE_MAP.get(kind, "UNKNOWN"),
                        "tags": [
                            {
                                "tagFQN": fqn,
                                "source": "Glossary",
                                "labelType": "Automated",
                            }
                            for fqn in sorted(column_tags.get((table, column), ()))
                        ],
                    }
                    for column, kind in columns.items()
                ],
            }
        )

    edges = []
    for mapping in accepted:
        # the report is represented as a dashboard-like entity; a custom
        # entity type would also work but dashboards are universally valid
        report_fqn = f"{service_name}.reports.{mapping.report_id}"
        for term in mapping.best.expr.terms():
            if term.column == "*":
                continue
            edges.append(
                {
                    "fromEntity": {
                        "type": "table",
                        "fullyQualifiedName": f"{service_name}.{term.table}.{term.column}",
                    },
                    "toEntity": {
                        "type": "dashboard",
                        "fullyQualifiedName": report_fqn,
                    },
                    "lineageDetails": {
                        "description": (
                            f"{mapping.metric_name} "
                            f"({mapping.band.value}) — "
                            f"{mapping.best.expr.canonical_key()}"
                        ),
                        "glossaryTerm": f"{glossary['name']}.{term_name(mapping.metric_name)}",
                    },
                }
            )

    return {
        "om_schema_version": OM_SCHEMA_VERSION,
        "glossary": glossary,
        "glossary_terms": terms,
        "tables": table_entities,
        "lineage": {"edges": edges},
    }


def validate_payloads(payloads: dict) -> None:
    """Raise jsonschema.ValidationError if any emitted entity violates the
    pinned OM subset schemas."""
    jsonschema.validate(payloads["glossary"], GLOSSARY_SCHEMA)
    for entity in payloads["glossary_terms"]:
        jsonschema.validate(entity, GLOSSARY_TERM_SCHEMA)
    for entity in payloads["tables"]:
        jsonschema.validate(entity, TABLE_SCHEMA)
    jsonschema.validate(payloads["lineage"], LINEAGE_SCHEMA)


def emit_openmetadata(
    out_dir: str | Path,
    mappings: list[ResolvedMapping],
    tables: dict[str, dict[str, str]],
    drift_findings: list[DriftFinding] | None = None,
    instance_locators: dict[str, str] | None = None,
) -> dict[str, str]:
    """Write the four OM JSON files. Returns {payload name -> path}."""
    payloads = build_payloads(
        mappings, tables, drift_findings, instance_locators
    )
    validate_payloads(payloads)

    out_dir = Path(out_dir) / "openmetadata"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for key in ("glossary", "glossary_terms", "tables", "lineage"):
        path = out_dir / f"{key}.json"
        path.write_text(
            json.dumps(
                {"omSchemaVersion": OM_SCHEMA_VERSION, key: payloads[key]},
                indent=2,
            )
        )
        written[key] = str(path)
    return written
