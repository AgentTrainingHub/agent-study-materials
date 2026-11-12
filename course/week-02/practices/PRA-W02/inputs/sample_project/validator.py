def parse_port(value: str) -> int:
    if not value.isdecimal():
        raise ValueError("port must contain decimal digits")
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("port out of range")
    return port
