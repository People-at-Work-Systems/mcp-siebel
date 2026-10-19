"""Records-local helpers shared with the generic executors, plus the
``aggregate_data`` handler.

The get / create / update / delete record handlers moved to the generic
``bc_read`` / ``bc_upsert`` / ``bc_delete`` executors (see executors/). What
stays here is the field-curation ranking (``_rank_fallback_fields``) and the
scoped-query fallback (``_record_via_query`` / ``_read_fields``) that the
``bc_read`` curated_query fallback reuses, and the ``aggregate_data`` handler
(still kind ``python``)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..siebel_client import SiebelError, SiebelRecord
from ._common import (
    _error,
    _error_result,
    _require_auth,
    _text,
)

if TYPE_CHECKING:
    from ..tools import ToolContext


# ---------------------------------------------------------------------------
# Field-curation ranking — used by the get_record scoped-query fallback when no
# field list was supplied. Siebel describes alphabetically, so a plain prefix
# returns whatever sorts early; rank by what identifies a record instead.
# ---------------------------------------------------------------------------

# Cap on the fallback's field list. Asking for every described field can itself
# be rejected, so take a workable subset — chosen by usefulness, not by
# descriptor order (see _rank_fallback_fields).
_MAX_FALLBACK_FIELDS = 40

# Who/what the record is: without these it is unidentifiable.
_IDENTIFYING_FIELD_HINTS = ("name", "number", "abstract", "subject", "title")
# How to reach the person. Kept as its own tier above the qualifying fields —
# lumped together, broad hints like "account" and "date" matched filler
# (Account Currency Code, Birth Date) and crowded out the email and phone.
_CONTACT_FIELD_HINTS = ("email", "phone", "mobile", "cellular")
# Where the record is. Its own tier because an address is only useful whole:
# with "city"/"country" as mere qualifiers, street, state and postal code sat
# unhinted and dropped out, and a contact came back with no address at all.
# Checked after the contact hints so "Email Address" stays a contact detail.
_ADDRESS_FIELD_HINTS = ("street", "address", "city", "state", "postal", "zip", "province", "country")
# Useful qualifiers once the record is identified, reachable and located.
_QUALIFYING_FIELD_HINTS = (
    "status",
    "type",
    "stage",
    "priority",
    "owner",
    "revenue",
    "close date",
    "account",
)
# Yes/no flags and foreign keys. Siebel names them like the real thing —
# "Suppress All Emails" and "Email SR Updates Flag" both read as contact
# details to a keyword match — but nobody asks for them, and the prompt is
# forbidden from displaying Ids at all. The DTYPE says what a name cannot.
_LOW_VALUE_FIELD_TYPES = ("DTYPE_BOOL", "DTYPE_ID")


def _rank_fallback_fields(
    names: list[str], limit: int = _MAX_FALLBACK_FIELDS, types: dict[str, str] | None = None
) -> list[str]:
    """Pick the most useful `limit` field names.

    Siebel describes fields alphabetically, so taking a plain prefix returns
    whatever sorts early — for Contact that was Accomplishments, Account
    Currency Code and Active Status, with Last Name and Email Address falling
    outside the cap entirely. Rank by what identifies a record instead, keeping
    descriptor order within a tier so results stay predictable.

    `types` maps field name → Siebel DTYPE where known; it demotes flags and
    foreign keys that a name-only ranking mistakes for content.
    """
    types = types or {}

    def tier(name: str) -> int:
        n = name.lower()
        if n == "id":
            return 0
        if types.get(name, "").upper() in _LOW_VALUE_FIELD_TYPES:
            return 6
        if any(h in n for h in _IDENTIFYING_FIELD_HINTS):
            return 1
        if any(h in n for h in _CONTACT_FIELD_HINTS):
            return 2
        if any(h in n for h in _ADDRESS_FIELD_HINTS):
            return 3
        if any(h in n for h in _QUALIFYING_FIELD_HINTS):
            return 4
        return 5

    # sorted() is stable, so equal tiers keep their original order.
    return sorted(names, key=tier)[:limit]


# Read fields per Business Component, ordered the way someone reads a record:
# who it is, then how to reach them, where they are, and the qualifying detail.
# For the objects we know, naming the fields beats ranking 190 keyword-scored
# names — the ranking is a heuristic and the demo instance kept proving it
# (a Contact came back with two opt-out flags and no address).
#
# The mapping lives in the spec (top-level ``preferred_fields``), hot-reloaded
# and surfaced to handlers via ``ToolContext.preferred_fields``; it is treated
# as an ordered *preference list*, intersected with the fields the BC actually
# reports: naming a field this deployment lacks must cost one field, never the
# whole read (Siebel answers HTTP 400 for an unknown field). Contact's names are
# confirmed against the demo instance's describe output; the rest are standard
# Siebel names.


def _read_fields(
    business_component: str, described: list[Any], preferred_fields: dict[str, list[str]]
) -> list[str]:
    """The field list to read a whole record with.

    Curated order where the object is known (from ``preferred_fields``), ranked
    heuristics otherwise — and ranked too when a curated list matches nothing,
    so a deployment that renamed its fields still gets a usable record instead
    of an empty read.
    """
    available = {f.name for f in described if f.name}
    curated = [f for f in preferred_fields.get(business_component, []) if f in available]
    if curated:
        # Follow-up tool calls need the Id even though the prompt never shows it.
        return [*curated, "Id"] if "Id" in available else curated
    return _rank_fallback_fields(
        [f.name for f in described if f.name], types={f.name: f.type for f in described if f.name}
    )


def _ordered_record(record: SiebelRecord, fields: Any) -> SiebelRecord:
    """Reorder a record to the field order it was requested in.

    Siebel returns fields in its own order — the read that prompted this put
    "Suppress All Emails" first and the name fourth. The system prompt tells
    the model to report fields in the order they came back, so that order has
    to be worth following. Anything Siebel returned but we didn't ask for
    keeps its place at the end rather than being dropped.
    """
    if not fields:
        return record
    wanted = [f.strip() for f in (fields.split(",") if isinstance(fields, str) else fields)]
    ordered = {name: record[name] for name in wanted if name in record}
    ordered.update({k: v for k, v in record.items() if k not in ordered})
    return ordered


def _is_audit_field(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in ("created", "updated", "last upd", "conflict", "integration id"))


async def _record_via_query(
    auth: Any,
    business_object: str,
    business_component: str,
    record_id: str,
    fields: Any,
    preferred_fields: dict[str, list[str]],
) -> Any:
    """Fetch a single record by Id with an explicit field list.

    The direct `/data/{BO}/{BC}/{id}` read asks Siebel for the whole record. On
    an instance whose integration component names a field the business
    component doesn't define, Siebel 500s while serialising it — e.g. Contact
    referencing 'Primary Address Integration Id' (SBL-DAT-00398). Naming the
    fields sidesteps the broken one, which is why the navigator can display a
    record the direct read cannot return.
    """
    if not fields:
        descriptor = await auth.client.describe_bc(business_object, business_component)
        usable = [f for f in descriptor.fields if f.name and not _is_audit_field(f.name)]
        fields = _read_fields(business_component, usable, preferred_fields)
    safe_id = str(record_id).replace("'", "''")
    result = await auth.client.query(
        business_object,
        business_component,
        search_spec=f"[Id]='{safe_id}'",
        fields=fields,
        page_size=1,
    )
    items = getattr(result, "items", None) or []
    return _ordered_record(items[0], fields) if items else None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _aggregate_data(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved = await auth.cache.resolve_business_object(str(args["business_object"]))
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    bc = str(args.get("business_component") or resolved)
    field = str(args["field"])
    operation = str(args["operation"])
    if operation not in {"sum", "avg", "min", "max", "count"}:
        return _error("operation must be one of: sum, avg, min, max, count.")
    offset = 0
    record_count = 0
    values: list[float] = []
    truncated = False
    for page in range(ctx.settings.aggregate_max_pages):
        try:
            result = await auth.client.query(
                resolved,
                bc,
                search_spec=args.get("searchspec"),
                fields=[field] if operation != "count" else None,
                page_size=ctx.settings.aggregate_page_size,
                offset=offset,
            )
        except SiebelError as exc:
            if exc.status_code == 404:
                break
            return _error_result(exc)
        record_count += len(result.items)
        if operation != "count":
            for item in result.items:
                try:
                    values.append(float(item[field]))
                except (KeyError, TypeError, ValueError):
                    pass
        if not result.has_more or result.next_start_row is None:
            break
        offset = result.next_start_row
    else:
        truncated = True
    if operation == "count":
        value: float = float(record_count)
    elif not values:
        return _text(f'No numeric values found in field "{field}" across {record_count} records.')
    elif operation == "sum":
        value = sum(values)
    elif operation == "avg":
        value = sum(values) / len(values)
    elif operation == "min":
        value = min(values)
    else:
        value = max(values)
    suffix = f"\nResult truncated after {ctx.settings.aggregate_max_pages} pages." if truncated else ""
    rendered = f"{int(value):,}" if value.is_integer() else f"{value:,.2f}"
    return _text(f"{resolved}/{bc}.{field} {operation}: **{rendered}**\nRecords processed: {record_count}{suffix}")
