"""robots.txt matcher, exercised against the REAL robots.txt files fetched from the three hosts."""

from pathlib import Path

import pytest

from app.crtc.robots import RobotsRules, parse_robots

FIX = Path(__file__).parent / "fixtures"
TOKEN = "crtc-copilot"


def rules(name: str) -> RobotsRules:
    return parse_robots((FIX / name).read_text(), TOKEN)


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/DocWebBroker/OpenDocument.aspx?DMID=1663270", False),  # where every filing lives
        ("/DocWebBroker/", False),
        ("/ListeInterventionList/x", False),
        ("/listes-lists/interventions/21", False),  # the rule as literally written...
        # ...does not match the real URLs, which sit under /portail-portal/eng/. RFC 9309
        # matches from the start of the path, so these are allowed. Documented in
        # docs/crtc-documents.md; the choice was flagged to the project owner.
        ("/portail-portal/eng/listes-lists/interventions/21?f=2026-259&dt=i", True),
        ("/portail-portal/eng/listes-lists/21/data?f=2026-259&g=num", True),
        ("/portail-portal/eng/listes-lists/18/data", True),
        ("/TransferToWeb/2026/2026-259_Procedural%20requests.zip", True),
        ("/robots.txt", True),
    ],
)
def test_applications_host_rules(path, allowed):
    assert rules("robots_applications.txt").is_allowed(path) is allowed


def test_matching_rule_is_reported_for_blocked_paths():
    r = rules("robots_applications.txt")
    assert r.matching_rule("/DocWebBroker/OpenDocument.aspx?DMID=1") == "/DocWebBroker/"
    assert r.matching_rule("/TransferToWeb/x.zip") is None


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/public/otf/2012/8661/x.PDF", False),  # Disallow: /public/
        ("/some/dir/file.zip", False),  # Disallow: /*.zip$  (wildcard + end anchor)
        ("/some/dir/file.zip?x=1", True),  # $ anchors: a query string defeats the pattern
        ("/eng/archive/2026/2026-259.htm", True),
        ("/eng/archive/2014/lt140606a.htm", False),
        ("/hearing/x", False),
    ],
)
def test_crtc_host_wildcards_and_anchors(path, allowed):
    assert rules("robots_crtc.txt").is_allowed(path) is allowed


def test_www_host_has_no_path_restrictions_for_us():
    r = rules("robots_www.txt")
    assert r.is_allowed("/eng/archive/2026/2026-259.htm")
    assert r.is_allowed("/public/otf/x.pdf")


def test_named_groups_for_other_bots_do_not_apply_to_us():
    # ClaudeBot / GPTBot / ... are disallowed on crtc.gc.ca; "crtc-copilot" is none of them.
    assert rules("robots_crtc.txt").is_allowed("/eng/archive/2026/2026-259.htm")
    assert not parse_robots((FIX / "robots_crtc.txt").read_text(), "ClaudeBot").is_allowed("/x")


def test_longest_match_wins_and_allow_beats_disallow_on_tie():
    text = "User-agent: *\nDisallow: /a/\nAllow: /a/b/\nDisallow: /c\nAllow: /c\n"
    r = parse_robots(text, TOKEN)
    assert not r.is_allowed("/a/x")
    assert r.is_allowed("/a/b/x")  # longer Allow wins
    assert r.is_allowed("/c")  # equal length: Allow wins


def test_specific_group_overrides_star_group():
    text = "User-agent: *\nDisallow: /\n\nUser-agent: crtc-copilot\nAllow: /\n"
    assert parse_robots(text, "crtc-copilot").is_allowed("/anything")
    assert not parse_robots(text, "otherbot").is_allowed("/anything")


def test_empty_disallow_means_allow_everything_and_missing_file_allows_all():
    assert parse_robots("User-agent: *\nDisallow:\n", TOKEN).is_allowed("/x")
    assert parse_robots("", TOKEN).is_allowed("/x")
    assert parse_robots("garbage with no colons", TOKEN).is_allowed("/x")


def test_disallow_all_flag():
    assert not RobotsRules(disallow_all=True).is_allowed("/x")
    assert RobotsRules(disallow_all=True).is_allowed("/robots.txt")


def test_comments_and_case_insensitive_directives():
    text = "USER-AGENT: *  # everyone\nDISALLOW: /private  # keep out\n"
    r = parse_robots(text, TOKEN)
    assert not r.is_allowed("/private/x") and r.is_allowed("/public")
