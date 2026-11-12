def parse_port(text: str) -> int:
    if not text or not text.isascii() or not text.isdecimal():
        raise ValueError("port must contain ASCII decimal digits only")
    port = int(text)
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return port
