"""One client's stored configuration, and the two ways it can be written.

PURE LOGIC ONLY -- `_config_fields` is the shared normaliser behind both
`create` and `upsert` and touches no database, which is what lets it be
tested here (see this suite's own scope note).

THE DEFECTS THESE GUARD

  1. TWO CLIENTS BECOMING ONE. `create` and `upsert` are separate entry
     points -- one refuses an org id that is taken, the other edits what is
     already there. They must normalise a client's keywords IDENTICALLY, or
     a client created through one and edited through the other ends up with
     different structure for the same input.

  2. THE TWO KEYWORD LISTS MERGING. Individual names (people to protect)
     and domain/brand terms are different kinds of search term, curated
     separately and capped separately. Flattening them into one bag loses
     the distinction the Individual/Domain filter and the per-type scrape
     caps are both built on.
"""

from backend.database.repositories.client_repository import _config_fields


def fields(**over):
    base = dict(
        name="Acme", domain="acme.com",
        name_keywords=None, domain_keywords=None,
        platform_limits_individual=None, platform_limits_domain=None,
        platform_tab_limits=None, cron=None, keyword_groups=None,
    )
    base.update(over)
    return _config_fields(**base)


def test_the_two_keyword_kinds_are_stored_separately():
    """Never one merged bag: an individual name must not turn up in the
    domain list, or it would be swept under the domain cap and filed as a
    brand term."""
    out = fields(name_keywords=["jane doe"], domain_keywords=["acme"])

    assert out["name_keywords"] == ["jane doe"]
    assert out["domain_keywords"] == ["acme"]
    assert "acme" not in out["name_keywords"]
    assert "jane doe" not in out["domain_keywords"]


def test_groups_win_over_the_flat_lists_when_both_are_sent():
    """`keyword_groups` is authoritative, so the flat lists are re-derived
    from it rather than trusted. A caller sending the two disagreeing
    cannot store them disagreeing."""
    out = fields(
        name_keywords=["stale"], domain_keywords=["stale-domain"],
        keyword_groups={
            "individual": [{"parent": "jane doe", "children": ["j. doe"]}],
            "domain": [{"parent": "acme", "children": []}],
        },
    )

    assert out["name_keywords"] == ["jane doe"]
    assert out["domain_keywords"] == ["acme"]
    assert "stale" not in out["name_keywords"]


def test_flat_lists_synthesise_groups_when_no_groups_are_sent():
    """A caller with no groups still gets a group per keyword -- one
    childless parent, which searches itself. Without this the keyword would
    be stored and never actually swept, since non-empty groups are treated
    as authoritative downstream."""
    out = fields(name_keywords=["jane doe"], domain_keywords=["acme"])

    assert out["keyword_groups"]["individual"] == [{"parent": "jane doe", "children": []}]
    assert out["keyword_groups"]["domain"] == [{"parent": "acme", "children": []}]


def test_create_and_edit_normalise_a_client_identically():
    """The whole reason the normaliser is shared. Same input through either
    path must produce the same stored shape."""
    same_input = dict(
        name="Acme", domain="acme.com",
        name_keywords=["jane doe"], domain_keywords=["acme"],
        keyword_groups={"individual": [{"parent": "jane doe", "children": ["j doe"]}]},
    )

    assert fields(**same_input) == fields(**same_input)


def test_per_type_caps_stay_on_their_own_side():
    """An individual-keyword cap must not leak onto domain sweeps: they are
    tuned independently and mean different volumes of work."""
    out = fields(
        platform_limits_individual={"instagram": 5},
        platform_limits_domain={"instagram": 40},
    )

    assert out["platform_limits_individual"] == {"instagram": 5}
    assert out["platform_limits_domain"] == {"instagram": 40}


def test_absent_optional_config_becomes_empty_not_none():
    """Mongo stores what it is given; `None` for a cap map would read back
    as a missing map and defeat `_to_out`'s own defaults."""
    out = fields()

    assert out["platform_limits_individual"] == {}
    assert out["platform_limits_domain"] == {}
    assert out["platform_tab_limits"] == {}
    assert out["name_keywords"] == []
    assert out["domain_keywords"] == []


def test_scheduler_fields_defaults():
    from backend.database.repositories.client_repository import _to_out
    doc = {"_id": "test-org", "name": "Test Org"}
    client = _to_out(doc)
    assert client["scheduler_platforms"] == []
    assert client["scheduler_keyword_scope"] == ""
    assert client["scheduler_facebook_tabs"] == []
    assert client["scheduler_budget_minutes"] == 0

    # Non-empty values
    doc_configured = {
        "_id": "test-org",
        "name": "Test Org",
        "scheduler_platforms": ["facebook"],
        "scheduler_keyword_scope": "domain",
        "scheduler_facebook_tabs": ["pages", "groups"],
        "scheduler_budget_minutes": 25,
    }
    client_conf = _to_out(doc_configured)
    assert client_conf["scheduler_platforms"] == ["facebook"]
    assert client_conf["scheduler_keyword_scope"] == "domain"
    assert client_conf["scheduler_facebook_tabs"] == ["pages", "groups"]
    assert client_conf["scheduler_budget_minutes"] == 25

