"""Unit tests for the field-curation ranking (``_rank_fallback_fields``).

The get_record handler that once lived here moved to the ``bc_read`` executor's
``curated_query`` fallback; the end-to-end fallback behavior is now pinned by
``test_get_record_characterization.py`` (driving through ``ToolRegistry.call``).
What stays here are the direct unit tests of the ranking heuristic — the
``limit`` parameter and the per-tier demotion logic that the end-to-end tests
can only exercise through the fixed ``_MAX_FALLBACK_FIELDS`` cap.

``_rank_fallback_fields`` and its hint tuples live in
``mcp_siebel.handlers.records``; the curated per-BC field lists moved to the
spec's top-level ``preferred_fields`` section.
"""

from __future__ import annotations

from mcp_siebel.handlers.records import _rank_fallback_fields

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
