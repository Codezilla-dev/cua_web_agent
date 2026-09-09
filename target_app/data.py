"""Seed data. A dict, deliberately: no database, no ORM, no migrations.

Balances are strings because that is how a legacy screen renders them, and
because the agent has to read what is on screen rather than a typed field.
"""

OPERATOR_USERNAME = "operator"
# Deliberately not a dictionary word. The redaction requirement is "grep the
# evidence for the secret and find nothing", and that check is worthless if the
# secret is a common English word -- "operator" matched the CLI verb
# `cua operator take` and the prose "waiting for an operator", so a real leak
# and a false positive were indistinguishable. A distinctive fixture makes the
# claim checkable by anyone, without trusting the argument.
OPERATOR_PASSWORD = "vault-echo-77-quill"

MEMBERS: dict[str, dict] = {
    "12345": {
        "member_id": "12345",
        "name": "Dolores Haze",
        "status": "Active",
        "branch": "Northgate",
        "accounts": [
            {"number": "SAV-40118", "kind": "Savings", "balance": "4,812.55"},
            {"number": "CHK-40119", "kind": "Checking", "balance": "1,204.09"},
        ],
    },
    "22841": {
        "member_id": "22841",
        "name": "Marcus Whitfield",
        "status": "Active",
        "branch": "Riverside",
        "accounts": [
            {"number": "SAV-51002", "kind": "Savings", "balance": "918.40"},
            {"number": "CHK-51003", "kind": "Checking", "balance": "77.15"},
        ],
    },
    "30017": {
        "member_id": "30017",
        "name": "Aisha Bello",
        "status": "Dormant",
        "branch": "Northgate",
        "accounts": [
            {"number": "SAV-66210", "kind": "Savings", "balance": "12,003.00"},
        ],
    },
}


def find_members(query: str) -> list[dict]:
    """Match on member id or a case-insensitive substring of the name."""
    if not query:
        return []
    needle = query.strip().casefold()
    return [
        member
        for member in MEMBERS.values()
        if member["member_id"] == query.strip() or needle in member["name"].casefold()
    ]


def get_member(member_id: str) -> dict | None:
    return MEMBERS.get(member_id)
