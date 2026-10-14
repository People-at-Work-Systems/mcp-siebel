"""get_record must survive a Siebel instance whose integration component
references a field the business component doesn't define.

Observed on the demo instance:

    GET /data/Contact/Contact/7SIA-5TYFM → HTTP 500
    "Field 'Primary Address Integration Id' does not exist in definition for
     business component 'Contact'. (SBL-DAT-00398)"

get_record asks for the whole record (no field list), so Siebel tries to
serialise every field on the integration component and trips over its own
misconfiguration. A query that names its fields reads the same record fine —
which is why the navigator displays it while the chat could not.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_siebel.handlers.records import _get_record, _rank_fallback_fields
from mcp_siebel.siebel_client import SiebelBCDescriptor, SiebelError, SiebelFieldDescriptor, SiebelQueryResult

_BROKEN_FIELD_ERROR = SiebelError(
    "Siebel API returned 500",
    500,
    "{\"ERROR\":\"Method 'FieldValue' of business component 'Contact' returned the "
    "following error:\\\"Field 'Primary Address Integration Id' does not exist in "
    "definition for business component 'Contact'.(SBL-DAT-00398)\\\"(SBL-EAI-04376)\"}",
)


def _descriptor(*names: str, types: dict[str, str] | None = None) -> SiebelBCDescriptor:
    types = types or {}
    return SiebelBCDescriptor(
        "Contact",
        "Contact",
        [SiebelFieldDescriptor(name=n, type=types.get(n, "DTYPE_TEXT")) for n in names],
    )


def _result(record: dict[str, str]) -> SiebelQueryResult:
    return SiebelQueryResult(
        items=[record],
        total_count=1,
        has_more=False,
        next_start_row=None,
        offset=0,
        page_size=1,
    )


class _Ctx:
    """Minimal stand-in for the authenticated tool context."""

    def __init__(self, client) -> None:  # noqa: ANN001
        self.client = client
        self.cache = AsyncMock()
        self.cache.resolve_business_object = AsyncMock(return_value="Contact")


@pytest.fixture()
def ctx(monkeypatch):
    client = AsyncMock()
    context = _Ctx(client)
    monkeypatch.setattr("mcp_siebel.handlers.records._require_auth", lambda _c: context)
    return context


@pytest.mark.asyncio
async def test_falls_back_to_a_field_scoped_query(ctx):
    ctx.client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    ctx.client.describe_bc = AsyncMock(return_value=_descriptor("Last Name", "First Name", "Id"))
    ctx.client.query = AsyncMock(
        return_value=SiebelQueryResult(
            items=[{"Last Name": "Taylor", "First Name": "Chris", "Id": "7SIA-5TYFM"}],
            total_count=1,
            has_more=False,
            next_start_row=None,
            offset=0,
            page_size=1,
        )
    )

    result = await _get_record({"business_object": "Contact", "id": "7SIA-5TYFM"}, None)

    assert "Taylor" in result["content"][0]["text"]
    # Scoped by Id, and naming its fields — that is what avoids the broken one.
    assert "[Id]='7SIA-5TYFM'" in ctx.client.query.call_args.kwargs["search_spec"]
    assert ctx.client.query.call_args.kwargs["fields"]


@pytest.mark.asyncio
async def test_fallback_reuses_caller_supplied_fields(ctx):
    # Caller already named its fields — no need to describe the BC again.
    ctx.client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    ctx.client.describe_bc = AsyncMock()
    ctx.client.query = AsyncMock(
        return_value=SiebelQueryResult(
            items=[{"Last Name": "Taylor"}],
            total_count=1,
            has_more=False,
            next_start_row=None,
            offset=0,
            page_size=1,
        )
    )

    await _get_record({"business_object": "Contact", "id": "7SIA-5TYFM", "fields": ["Last Name"]}, None)

    ctx.client.describe_bc.assert_not_awaited()
    assert ctx.client.query.call_args.kwargs["fields"] == ["Last Name"]


@pytest.mark.asyncio
async def test_record_ids_are_escaped_in_the_fallback(ctx):
    ctx.client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    ctx.client.describe_bc = AsyncMock(return_value=_descriptor("Name", "Id"))
    ctx.client.query = AsyncMock(
        return_value=SiebelQueryResult(
            items=[{"Name": "x"}], total_count=1, has_more=False, next_start_row=None, offset=0, page_size=1
        )
    )

    await _get_record({"business_object": "Contact", "id": "O'Brien"}, None)

    assert "[Id]='O''Brien'" in ctx.client.query.call_args.kwargs["search_spec"]


@pytest.mark.asyncio
async def test_happy_path_does_not_query(ctx):
    ctx.client.get_record = AsyncMock(return_value={"Last Name": "Taylor"})
    ctx.client.query = AsyncMock()

    result = await _get_record({"business_object": "Contact", "id": "7SIA-5TYFM"}, None)

    assert "Taylor" in result["content"][0]["text"]
    ctx.client.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_surfaces_the_siebel_error_when_the_fallback_also_fails(ctx):
    # Better a specific, actionable message than "could not be retrieved".
    ctx.client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    ctx.client.describe_bc = AsyncMock(side_effect=SiebelError("nope", 500, "{}"))

    with pytest.raises(SiebelError):
        await _get_record({"business_object": "Contact", "id": "7SIA-5TYFM"}, None)


@pytest.mark.asyncio
async def test_missing_record_still_reports_not_found(ctx):
    ctx.client.get_record = AsyncMock(return_value=None)
    ctx.client.query = AsyncMock()

    result = await _get_record({"business_object": "Contact", "id": "nope"}, None)

    assert "No record found" in result["content"][0]["text"]
    ctx.client.query.assert_not_awaited()


# The fallback first shipped taking the first N fields in descriptor order.
# Siebel describes alphabetically, so Contact yielded Accomplishments, Account
# Currency Code, Active Status … and never reached Last Name / First Name /
# Email Address — the same alphabetical-prefix trap the navigator had.
_ALPHABETICAL_CONTACT = [
    "Accomplishments",
    "Account",
    "Account Currency Code",
    "Active Status",
    "Alias",
    "Birth Date",
    "Cellular Phone #",
    "Comment",
    "Email Address",
    "First Name",
    "Job Title",
    "Last Name",
    "Notes",
    "Work Phone #",
    "Id",
]


def test_fallback_prefers_identifying_fields_over_alphabetical_order():
    ranked = _rank_fallback_fields(_ALPHABETICAL_CONTACT, limit=6)
    for wanted in ("Last Name", "First Name"):
        assert wanted in ranked
    # Beat the junk that merely sorts earlier.
    assert "Accomplishments" not in ranked
    assert "Birth Date" not in ranked


def test_fallback_keeps_contact_details_ahead_of_filler():
    ranked = _rank_fallback_fields(_ALPHABETICAL_CONTACT, limit=8)
    for wanted in ("Email Address", "Work Phone #", "Job Title"):
        assert wanted in ranked


def test_fallback_respects_the_limit():
    assert len(_rank_fallback_fields(_ALPHABETICAL_CONTACT, limit=5)) == 5


def test_fallback_keeps_everything_when_under_the_limit():
    assert set(_rank_fallback_fields(["Name", "Id"], limit=40)) == {"Name", "Id"}


def test_fallback_ranking_is_stable_within_a_tier():
    # Equal-priority fields keep descriptor order, so results stay predictable.
    ranked = _rank_fallback_fields(["Zeta Thing", "Alpha Thing"], limit=40)
    assert ranked == ["Zeta Thing", "Alpha Thing"]


def test_ranking_demotes_flags_and_foreign_keys():
    # "Suppress All Emails" and "Email SR Updates Flag" both contain "email",
    # so a name-only ranking promoted them into the reachability tier — ahead
    # of the address, and into the answer the user actually saw. Their DTYPE
    # says what they are: booleans and Ids are never what someone asked for.
    names = ["Suppress All Emails", "Email SR Updates Flag", "Account Id", "Email Address", "Last Name"]
    types = {
        "Suppress All Emails": "DTYPE_BOOL",
        "Email SR Updates Flag": "DTYPE_BOOL",
        "Account Id": "DTYPE_ID",
    }
    ranked = _rank_fallback_fields(names, limit=2, types=types)
    assert ranked == ["Last Name", "Email Address"]


def test_ranking_keeps_an_address_together():
    # Only "city" and "country" used to be hinted, so a partial address came
    # back: street, state and postal code sat in the unhinted tier and fell
    # outside the cap.
    address = [
        "Primary Street Address",
        "Primary City",
        "Primary State",
        "Primary Postal Code",
        "Primary Country",
    ]
    names = ["Accomplishments", "Agenda", "Alignment", *address, "Last Name"]
    ranked = _rank_fallback_fields(names, limit=6)
    assert set(address) <= set(ranked)


def test_ranking_still_puts_the_email_address_above_the_address():
    # "Email Address" contains "address" — it must stay a contact detail.
    ranked = _rank_fallback_fields(["Primary City", "Email Address"], limit=1)
    assert ranked == ["Email Address"]


# --- curated field selection -------------------------------------------------
#
# Contact describes 190 fields on the demo instance. Ranking 190 keyword-scored
# names is a guess; for the objects we know, name the fields outright.

_CONTACT_BC_FIELDS = (
    "Accomplishments",
    "Account Id",
    "Active Status",
    "Birth Date",
    "Cellular Phone #",
    "Email Address",
    "Email SR Updates Flag",
    "First Name",
    "Job Title",
    "Last Name",
    "Party Name",
    "Primary Account Name",
    "Primary City",
    "Primary Country",
    "Primary Postal Code",
    "Primary State",
    "Primary Street Address",
    "Suppress All Emails",
    "Work Phone #",
    "Id",
)

_CONTACT_BC_TYPES = {
    "Account Id": "DTYPE_ID",
    "Active Status": "DTYPE_BOOL",
    "Email SR Updates Flag": "DTYPE_BOOL",
    "Suppress All Emails": "DTYPE_BOOL",
    "Id": "DTYPE_ID",
}


async def _fallback_fields(ctx, *, bc_fields=_CONTACT_BC_FIELDS, args=None) -> list[str]:
    """Run get_record through the fallback and return the fields it asked for."""
    ctx.client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    ctx.client.describe_bc = AsyncMock(return_value=_descriptor(*bc_fields, types=_CONTACT_BC_TYPES))
    ctx.client.query = AsyncMock(return_value=_result({"Last Name": "Taylor"}))
    await _get_record({"business_object": "Contact", "id": "7SIA-5TYFM", **(args or {})}, None)
    return list(ctx.client.query.call_args.kwargs["fields"])


@pytest.mark.asyncio
async def test_curated_read_asks_for_the_contacts_address(ctx):
    # The reported gap: "Show Contact for Contact Taylor" answered with phones
    # and email but no address, because no address field was ever requested.
    requested = await _fallback_fields(ctx)
    for wanted in (
        "Primary Street Address",
        "Primary City",
        "Primary State",
        "Primary Postal Code",
        "Primary Country",
    ):
        assert wanted in requested
    # …and in reading order: who, then how to reach them, then where they are.
    assert requested.index("Last Name") < requested.index("Work Phone #")
    assert requested.index("Work Phone #") < requested.index("Primary Street Address")


@pytest.mark.asyncio
async def test_curated_read_leaves_out_flags_and_foreign_keys(ctx):
    requested = await _fallback_fields(ctx)
    for unwanted in ("Suppress All Emails", "Email SR Updates Flag", "Account Id", "Accomplishments"):
        assert unwanted not in requested


@pytest.mark.asyncio
async def test_curated_read_always_includes_the_id(ctx):
    # Follow-up tool calls need it, even though the prompt never displays it.
    assert "Id" in await _fallback_fields(ctx)


@pytest.mark.asyncio
async def test_curated_names_the_instance_lacks_are_skipped_not_requested(ctx):
    # Siebel answers HTTP 400 for an unknown field, so a name this deployment
    # doesn't have must cost one field, never the whole read.
    requested = await _fallback_fields(ctx, bc_fields=("Last Name", "First Name", "Id"))
    assert requested == ["Last Name", "First Name", "Id"]


@pytest.mark.asyncio
async def test_uncurated_business_components_still_rank(ctx):
    ctx.cache.resolve_business_object = AsyncMock(return_value="Widget")
    requested = await _fallback_fields(
        ctx,
        bc_fields=("Accomplishments", "Widget Name", "Id"),
        args={"business_object": "Widget"},
    )
    assert "Widget Name" in requested


@pytest.mark.asyncio
async def test_curated_read_falls_back_to_ranking_when_no_name_matches(ctx):
    # A deployment that renamed everything still gets a usable record.
    requested = await _fallback_fields(ctx, bc_fields=("Nachname", "Vorname", "Id"))
    assert set(requested) == {"Nachname", "Vorname", "Id"}


@pytest.mark.asyncio
async def test_record_is_reported_in_the_order_it_was_asked_for(ctx):
    # Siebel returns fields in its own order — the observed read led with
    # "Suppress All Emails" and buried the name. The prompt tells the model to
    # report fields "in the order returned", so the order has to be worth
    # following.
    ctx.client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    ctx.client.describe_bc = AsyncMock(return_value=_descriptor(*_CONTACT_BC_FIELDS, types=_CONTACT_BC_TYPES))
    ctx.client.query = AsyncMock(
        return_value=_result(
            {
                "Primary City": "Sydney",
                "Work Phone #": "9727471400",
                "Last Name": "Taylor",
                "First Name": "Chris",
            }
        )
    )

    text = (await _get_record({"business_object": "Contact", "id": "7SIA-5TYFM"}, None))["content"][0]["text"]

    assert text.index("Last Name") < text.index("Work Phone #") < text.index("Primary City")


@pytest.mark.asyncio
async def test_caller_supplied_field_order_is_preserved_too(ctx):
    ctx.client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    ctx.client.query = AsyncMock(return_value=_result({"Last Name": "Taylor", "First Name": "Chris"}))

    result = await _get_record(
        {
            "business_object": "Contact",
            "id": "7SIA-5TYFM",
            "fields": ["First Name", "Last Name"],
        },
        None,
    )
    text = result["content"][0]["text"]

    assert text.index("First Name") < text.index("Last Name")


@pytest.mark.asyncio
async def test_unrequested_fields_are_still_reported(ctx):
    # Siebel can return more than it was asked for. Reordering must never drop
    # a field — the prompt promises the user every field that came back.
    ctx.client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    ctx.client.query = AsyncMock(return_value=_result({"Surprise": "value", "Last Name": "Taylor"}))

    result = await _get_record({"business_object": "Contact", "id": "7SIA-5TYFM", "fields": ["Last Name"]}, None)

    assert "Surprise: value" in result["content"][0]["text"]
