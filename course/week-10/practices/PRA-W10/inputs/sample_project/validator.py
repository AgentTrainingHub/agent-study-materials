def parse_port(text: str) -> int:
    if not isinstance(text, str) or not text or not text.isascii() or not text.isdecimal() or text[0] == "0":
        raise ValueError("port must contain ASCII decimal digits only")
    port = int(text)
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return port


def parse_ports(text):
    if not isinstance(text, str) or not text:
        raise ValueError("nonempty str required")
    values = [parse_port(item) for item in text.split(';')]
    return values
