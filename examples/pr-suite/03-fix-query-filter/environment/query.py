"""Broken query matcher — evaluates left-to-right, ignoring AND-over-OR precedence."""


def _clause(clause: str, record: dict) -> bool:
    field, _, val = clause.partition("==")
    field, val = field.strip(), val.strip()
    return record.get(field) == val


def matches(query: str, record: dict) -> bool:
    # BUG: flat left-to-right; should group AND before OR
    tokens = query.replace("(", " ").replace(")", " ").split()
    if not tokens:
        return False
    result = _clause(tokens[0], record)
    i = 1
    while i < len(tokens):
        op = tokens[i]
        rhs = _clause(tokens[i + 1], record)
        if op == "AND":
            result = result and rhs
        elif op == "OR":
            result = result or rhs
        else:
            raise ValueError(f"unknown op {op}")
        i += 2
    return result
