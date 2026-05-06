def mask_domain(value: str) -> str:
    if len(value) <= 6:
        return "*" * len(value)
    name, _, zone = value.partition(".")
    if len(name) <= 3:
        return f"{name[0]}***.{zone}" if zone else f"{name[0]}***"
    return f"{name[:4]}***.{zone}" if zone else f"{name[:4]}***"


def mask_secret(value: str | None, keep: int = 3) -> str | None:
    if not value:
        return value
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * max(3, len(value) - keep)
