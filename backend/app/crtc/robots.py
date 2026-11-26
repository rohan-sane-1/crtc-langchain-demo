"""Minimal RFC 9309 robots.txt matcher (``*`` and ``$`` wildcards, longest match wins).

The stdlib ``urllib.robotparser`` does not understand ``*``/``$`` (crtc.gc.ca uses
``Disallow: /*.zip$``), so we implement the small subset we need ourselves.

Semantics (RFC 9309):
* Pick the group whose ``User-agent`` token matches ours (case-insensitive substring of our
  product token); otherwise the ``*`` group; otherwise everything is allowed.
* Among the rules of that group that match the path, the LONGEST pattern wins; on a tie
  ``Allow`` beats ``Disallow``.
* Matching is a prefix match against path + query, from the start of the path. A pattern
  such as ``/listes-lists/interventions/`` therefore does NOT match
  ``/portail-portal/eng/listes-lists/interventions/21``. We do not guess at the operator's
  intent beyond what the file literally says.
* robots.txt that is missing (4xx) means "allow all"; one that cannot be fetched (5xx,
  network error) means "disallow all" until it can be (handled by the caller).
"""

import re
from dataclasses import dataclass, field

MAX_ROBOTS_BYTES = 512 * 1024  # RFC 9309 says parsers must handle at least 500 KiB


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str

    def matches(self, target: str) -> bool:
        return _compile(self.pattern).match(target) is not None


_cache: dict[str, re.Pattern[str]] = {}


def _compile(pattern: str) -> re.Pattern[str]:
    rx = _cache.get(pattern)
    if rx is None:
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        rx = re.compile(".*".join(re.escape(part) for part in body.split("*")) + ("$" if anchored else ""))
        _cache[pattern] = rx
    return rx


@dataclass
class RobotsRules:
    """The rule list that applies to us (already group-selected)."""

    rules: list[Rule] = field(default_factory=list)
    disallow_all: bool = False  # robots.txt unreachable (5xx / network): assume everything disallowed

    def is_allowed(self, path_and_query: str) -> bool:
        target = path_and_query or "/"
        if target == "/robots.txt":
            return True
        if self.disallow_all:
            return False
        best: Rule | None = None
        for rule in self.rules:
            if not rule.pattern:  # an empty Disallow means "allow all"; an empty Allow is a no-op
                continue
            if rule.matches(target) and (
                best is None
                or len(rule.pattern) > len(best.pattern)
                or (len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow)
            ):
                best = rule
        return best is None or best.allow

    def matching_rule(self, path_and_query: str) -> str | None:
        """The winning Disallow pattern for a blocked path (for human-readable reasons)."""
        best: Rule | None = None
        for rule in self.rules:
            if (
                rule.pattern
                and rule.matches(path_and_query or "/")
                and (best is None or len(rule.pattern) > len(best.pattern))
            ):
                best = rule
        return best.pattern if best and not best.allow else None


def parse_robots(text: str, product_token: str) -> RobotsRules:
    """Parse a robots.txt body and return the rules that apply to `product_token`."""
    token = product_token.lower()
    groups: list[tuple[list[str], list[Rule]]] = []
    agents: list[str] = []
    rules: list[Rule] = []
    in_rules = False
    for raw in text[: MAX_ROBOTS_BYTES * 2].splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if in_rules:  # a new group starts
                groups.append((agents, rules))
                agents, rules, in_rules = [], [], False
            agents.append(value.lower())
        elif key in ("allow", "disallow"):
            in_rules = True
            rules.append(Rule(allow=key == "allow", pattern=value))
    if agents or rules:
        groups.append((agents, rules))

    specific: list[Rule] = []
    star: list[Rule] = []
    for names, group_rules in groups:
        if any(n != "*" and n and n in token for n in names):
            specific += group_rules
        if "*" in names:
            star += group_rules
    return RobotsRules(rules=specific or star)
