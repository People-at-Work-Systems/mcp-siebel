"""The curated-query fallback for the ``bc_read`` executor.

When a direct ``/data/{BO}/{BC}/{id}`` read fails because the integration
component names a field the business component doesn't define (Siebel 500s
serialising it — e.g. Contact referencing 'Primary Address Integration Id',
SBL-DAT-00398), the ``bc_read`` executor retries as a scoped query that names
the fields. The helpers here pick that field list and run the query:

* ``_rank_fallback_fields`` — rank described fields by what identifies a
  record (name/number/...) over alphabetical filler, with per-type demotion of
  flags and foreign keys.
* ``_read_fields`` — the curated ``preferred_fields`` list (from the spec)
  intersected with what the BC actually reports, falling back to the ranked
  heuristic when there is no curated list (or it matches nothing).
* ``_record_via_query`` — run the scoped ``[Id]='...'`` query and reorder the
  record to the field order it asked for.
* ``_ordered_record`` / ``_is_audit_field`` — helpers for the above.

This module is framework code: it imports only ``siebel_client``. It used to
live in ``mcp_siebel.handlers.records``; it moved here so the ``bc_read``
executor (``executors/read.py``) does not import from ``mcp_siebel.handlers``
(the framework layering invariant pinned by
``tests/test_framework_no_handler_imports.py``). The ``aggregate_data``
``python``-kind handler stayed in ``handlers.records``.

No tool names live in this module.
"""

from __future__ import annotations

from typing import Any

from ..siebel_client import SiebelRecord

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
# and surfaced to the bc_read executor via ``ToolContext.preferred_fields``; it
# is treated as an ordered *preference list*, intersected with the fields the
# BC actually reports: naming a field this deployment lacks must cost one
# field, never the whole read (Siebel answers HTTP 400 for an unknown field).
# Contact's names are confirmed against the demo instance's describe output;
# the rest are standard Siebel names.


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

    Returns ``None`` when the scoped query matches no items (the record was
    deleted between the direct read and the retry, or the Id is wrong); the
    caller re-raises the original error in that case.
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
