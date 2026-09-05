"""
Unified CSV Upload — single source of truth for the upload_csv MCP tool.

Ported 2026-09-01 (Tier 2A). Trimmed from the old repo's csv_upload.py:
only the disk-storage path (used by process_data pipeline uploads) is
kept, rewritten against a DB staging table instead of disk — see
models.CsvUploadStaging's docstring for why. The db_direct storage mode
(V3 real-time ingestion, a separate code path _process_data_impl doesn't
use) is dropped, not ported — out of scope for this build's pipeline.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMAS_PATH = Path(__file__).resolve().parent.parent / 'config' / 'csv_schemas.json'


# ═══════════════════════════════════════════════════════════════════════
# FileTypeRegistry — built once from csv_schemas.json
# ═══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class FileTypeInfo:
    """Metadata for a single CSV file type."""
    canonical_filename: str
    model_category: str             # 'regular_model' | 'context_graph_model'
    required_columns: tuple[str, ...]
    optional_columns: tuple[str, ...]   # schema's optional_columns + recommended_columns
    auto_generated: bool = False
    platform_curated: bool = False


# A required column is satisfied by any of its aliases. The load-driver (and
# the old repo's REST /api/onboarding/upload path it always used) emits
# `account_id`; the schema names it `source_account_id`. The ingest reads
# both. Without this, real load-driver output failed strict validation
# here — found 2026-09-01 during the Tier 2A-3 live-parity run.
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    'source_account_id': ('account_id',),
}


def _known_columns(info: 'FileTypeInfo') -> set[str]:
    known = set(info.required_columns) | set(info.optional_columns)
    for col in info.required_columns:
        known.update(_COLUMN_ALIASES.get(col, ()))
    return known


def _missing_required(info: 'FileTypeInfo', headers: set[str]) -> list[str]:
    return sorted(
        col for col in info.required_columns
        if col not in headers and not any(a in headers for a in _COLUMN_ALIASES.get(col, ()))
    )


# Alias → canonical filename mapping
_ALIASES: dict[str, str] = {
    'account_details':               'account_details.csv',
    'accounts':                      'accounts.csv',
    'kpis':                          'kpi_measurements.csv',
    'kpi_measurements':              'kpi_measurements.csv',
    'kpi_data':                      'kpi_measurements.csv',
    'enhanced_signals':              'enhanced_qualitative_signals.csv',
    'signals':                       'enhanced_qualitative_signals.csv',
    'qualitative_signals':           'enhanced_qualitative_signals.csv',
    'products':                      'products.csv',
    'stakeholders':                  'stakeholders.csv',
    'engagement_events':             'engagement_events.csv',
    'account_business_profiles':     'account_business_profiles.csv',
    'profiles':                      'account_business_profiles.csv',
    'outcomes':                      'outcomes.csv',
    'decisions':                     'decisions.csv',
    'signal_edges':                  'signal_edges.csv',
    'industry_benchmarks':           'industry_benchmarks.csv',
    'customers':                     'accounts.csv',
}

_registry: dict[str, FileTypeInfo] | None = None


def _build_registry() -> dict[str, FileTypeInfo]:
    """Build registry from csv_schemas.json. Keyed by canonical filename."""
    if not _SCHEMAS_PATH.is_file():
        logger.warning("csv_schemas.json not found at %s — using empty registry", _SCHEMAS_PATH)
        return {}

    with open(_SCHEMAS_PATH) as f:
        schemas = json.load(f)

    reg: dict[str, FileTypeInfo] = {}

    # recommended_columns is a real key in csv_schemas.json (account_details'
    # csm/champion/products/contract fields) that the old registry never
    # read — so every one of those columns was warned as "unknown, will be
    # ignored" while the ingest read them. Folded into optional here.
    def _optional(spec: dict) -> tuple[str, ...]:
        return tuple(spec.get('optional_columns', [])) + tuple(spec.get('recommended_columns', []))

    for fname, spec in _flatten_model(schemas.get('regular_model', {})).items():
        reg[fname] = FileTypeInfo(
            canonical_filename=fname,
            model_category='regular_model',
            required_columns=tuple(spec.get('required_columns', [])),
            optional_columns=_optional(spec),
        )

    for fname, spec in _flatten_model(schemas.get('context_graph_model', {})).items():
        reg[fname] = FileTypeInfo(
            canonical_filename=fname,
            model_category='context_graph_model',
            required_columns=tuple(spec.get('required_columns', [])),
            optional_columns=_optional(spec),
            auto_generated=bool(spec.get('auto_generated')),
            platform_curated=bool(spec.get('platform_curated')),
        )

    return reg


def _flatten_model(model: dict) -> dict[str, dict]:
    """Extract {filename: spec} from a model section (flat or nested)."""
    result: dict[str, dict] = {}
    result.update(model.get('files', {}))
    for sub_key in ('customer_provided', 'auto_generated', 'platform_curated'):
        sub = model.get(sub_key, {})
        for k, v in sub.items():
            if k.endswith('.csv') and k not in result:
                result[k] = v
    return result


def get_registry() -> dict[str, FileTypeInfo]:
    """Return the file type registry (built once, cached)."""
    global _registry
    if _registry is None:
        _registry = _build_registry()
    return _registry


def resolve_file_type(file_type: str) -> FileTypeInfo:
    """
    Resolve any file_type string to a FileTypeInfo.

    Accepts: canonical filename ('kpi_measurements.csv'), short name ('kpis'),
    alias ('kpi_data'), or filename without .csv ('kpi_measurements').
    """
    reg = get_registry()

    ft = file_type if file_type.endswith('.csv') else f'{file_type}.csv'
    if ft in reg:
        return reg[ft]

    alias_target = _ALIASES.get(file_type.lower().strip())
    if alias_target and alias_target in reg:
        return reg[alias_target]

    bare = file_type.replace('.csv', '').lower().strip()
    alias_target = _ALIASES.get(bare)
    if alias_target and alias_target in reg:
        return reg[alias_target]

    available = sorted(reg.keys())
    raise ValueError(
        f"Unknown file_type '{file_type}'. "
        f"Available: {available}"
    )


# ═══════════════════════════════════════════════════════════════════════
# UploadResult — structured return value
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class UploadResult:
    """Structured result from _upload_csv_impl."""
    status: str = 'success'             # 'success' | 'validation_error' | 'error'
    customer_id: int = 0
    file_type: str = ''
    canonical_filename: str = ''
    row_count: int = 0
    bytes_written: int = 0
    dry_run: bool = False
    valid: bool = True
    columns_found: list[str] = field(default_factory=list)
    required_columns: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    message: str = ''
    upload_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v or isinstance(v, (bool, int))}


# ═══════════════════════════════════════════════════════════════════════
# _upload_csv_impl — the single upload function
# ═══════════════════════════════════════════════════════════════════════

def _upload_csv_impl(
    customer_id: int,
    file_type: str,
    csv_content: str,
    *,
    dry_run: bool = False,
    strict_validation: bool = True,
    key_kind: str | None = None,
    key_id: int | None = None,
) -> UploadResult:
    """
    Canonical CSV upload implementation — validates and stages to
    CsvUploadStaging. process_data() reads the staged rows later.

    Args:
        customer_id:        Target customer
        file_type:          Any recognized name (canonical, short, alias)
        csv_content:        Raw CSV string
        dry_run:            Validate only, do not persist
        strict_validation:  If True (default), missing required columns → error.
                            If False, missing columns → warning.

    Returns:
        UploadResult with status and validation details.
    """
    # ── 1. Resolve file type ──
    try:
        info = resolve_file_type(file_type)
    except ValueError as e:
        return UploadResult(
            status='error',
            customer_id=customer_id,
            file_type=file_type,
            valid=False,
            errors=[str(e)],
        )

    # ── 2. Parse CSV and validate structure ──
    try:
        reader = csv.DictReader(io.StringIO(csv_content))
        headers = set(reader.fieldnames or [])
        rows = list(reader)
    except Exception as e:
        return UploadResult(
            status='error',
            customer_id=customer_id,
            file_type=file_type,
            canonical_filename=info.canonical_filename,
            valid=False,
            errors=[f"CSV parse error: {e}"],
        )

    required = set(info.required_columns)

    missing_required = _missing_required(info, headers)
    unknown_columns = sorted(headers - _known_columns(info))

    errors: list[str] = []
    warnings: list[str] = []

    if missing_required:
        msg = f"Missing required columns: {missing_required}"
        if strict_validation:
            errors.append(msg)
        else:
            warnings.append(f"[non-strict] {msg} — file accepted anyway")
    if unknown_columns:
        warnings.append(f"Unknown columns (will be ignored): {unknown_columns}")
    if len(rows) == 0:
        errors.append("CSV has no data rows.")

    valid = len(errors) == 0

    # ── 3. Dry run → return validation result ──
    if dry_run:
        return UploadResult(
            status='success' if valid else 'validation_error',
            customer_id=customer_id,
            file_type=file_type,
            canonical_filename=info.canonical_filename,
            dry_run=True,
            valid=valid,
            row_count=len(rows),
            columns_found=sorted(headers),
            required_columns=sorted(required),
            missing_required=missing_required,
            errors=errors,
            warnings=warnings,
        )

    if not valid:
        return UploadResult(
            status='validation_error',
            customer_id=customer_id,
            file_type=file_type,
            canonical_filename=info.canonical_filename,
            valid=False,
            row_count=len(rows),
            errors=errors,
            warnings=warnings,
        )

    # ── 4. Stage to DB ──
    return _upload_to_staging(customer_id, info, csv_content, rows, warnings=warnings, key_kind=key_kind, key_id=key_id)


def _upload_to_staging(
    customer_id: int,
    info: FileTypeInfo,
    csv_content: str,
    rows: list,
    *,
    warnings: list[str] | None = None,
    key_kind: str | None = None,
    key_id: int | None = None,
) -> UploadResult:
    """Upsert CSV content into CsvUploadStaging, keyed by (customer_id, file_type),
    and record the upload itself in CsvUpload (lineage: hash, size, who, warnings)."""
    import hashlib
    from models import Customer, CsvUploadStaging, CsvUpload
    from extensions import db

    customer = db.session.get(Customer, int(customer_id))
    if not customer:
        return UploadResult(
            status='error',
            customer_id=customer_id,
            file_type=info.canonical_filename,
            canonical_filename=info.canonical_filename,
            valid=True,
            errors=[f"Customer {customer_id} not found."],
        )

    staged = CsvUploadStaging.query.filter_by(
        customer_id=customer_id, file_type=info.canonical_filename,
    ).first()
    byte_count = len(csv_content.encode('utf-8'))

    upload = CsvUpload(
        customer_id=customer_id, file_type=info.canonical_filename,
        sha256=hashlib.sha256(csv_content.encode('utf-8')).hexdigest(), row_count=len(rows), byte_count=byte_count,
        validation={'warnings': list(warnings or [])}, key_kind=key_kind, key_id=key_id,
    )
    db.session.add(upload)
    db.session.flush()
    if staged:
        staged.csv_content = csv_content
        staged.row_count = len(rows)
        staged.upload_id = upload.id
    else:
        staged = CsvUploadStaging(
            customer_id=customer_id,
            file_type=info.canonical_filename,
            csv_content=csv_content,
            row_count=len(rows),
            upload_id=upload.id,
        )
        db.session.add(staged)
    db.session.commit()

    logger.info(
        "upload_csv: %s staged for customer %s (%d rows, %d bytes)",
        info.canonical_filename, customer_id, len(rows), byte_count,
    )

    return UploadResult(
        status='success',
        customer_id=customer_id,
        file_type=info.canonical_filename,
        canonical_filename=info.canonical_filename,
        row_count=len(rows),
        bytes_written=byte_count,
        warnings=warnings or [],
        upload_id=upload.id,
        message=(
            f"Staged {info.canonical_filename} ({byte_count} bytes, {len(rows)} rows). "
            f"Use process_data() to ingest into the database."
        ),
    )
