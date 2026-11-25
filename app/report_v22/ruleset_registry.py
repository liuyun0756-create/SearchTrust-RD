"""Version numbers for known public rulesets and reserved public versions."""

_PUBLIC_RULESET_NUMBERS = {
    f"v22_capability_public_v{number}": number
    for number in range(1, 23)
}
# Version 19 is reserved but registered for version-based policies.
# Contract validation and intake still decide which reports can be generated.


def public_ruleset_number(ruleset_version: str | None) -> int | None:
    return _PUBLIC_RULESET_NUMBERS.get(ruleset_version)
