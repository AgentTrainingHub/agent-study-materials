from decimal import Decimal, InvalidOperation

ORDERS = {
    "A-1042": {
        "paid": "279.00",
        "days_after_receipt": 3,
        "used": False,
        "risk": False
    },
    "B-1043": {
        "paid": "199.00",
        "days_after_receipt": 12,
        "used": False,
        "risk": False
    }
}


def rule(order):
    eligible = (
        0 <= order["days_after_receipt"] <= 7
        and not order["used"]
        and not order["risk"]
    )

    if eligible:
        return {
            "action": "REFUND",
            "amount": order["paid"]
        }

    return {
        "action": "REVIEW",
        "amount": None
    }


def inspect(order, proposal):
    expected = rule(order)

    if expected["action"] == "REVIEW":
        verdict = (
            "ACCEPTED"
            if proposal == expected
            else "REJECTED"
        )
        return verdict, expected

    try:
        valid = (
            proposal.get("action") == "REFUND"
            and Decimal(proposal["amount"])
            == Decimal(expected["amount"])
        )
    except (KeyError, TypeError, InvalidOperation):
        valid = False

    return (
        "ACCEPTED" if valid else "CORRECTED",
        expected
    )


CASES = [
    ("规则直算", "A-1042", None),
    (
        "预设正确提议",
        "A-1042",
        {"action": "REFUND", "amount": "279.00"}
    ),
    (
        "注入超额提议",
        "A-1042",
        {"action": "REFUND", "amount": "299.00"}
    ),
    (
        "注入越界提议",
        "B-1043",
        {"action": "REFUND", "amount": "99.50"}
    )
]

for label, order_id, proposal in CASES:
    if proposal is None:
        verdict = "RULE_ONLY"
        decision = rule(ORDERS[order_id])
    else:
        verdict, decision = inspect(
            ORDERS[order_id],
            proposal
        )

    print(label, verdict, decision)
