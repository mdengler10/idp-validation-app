"""UI-only helpers: field name display (last segment after final period)."""


def field_display_name(key: str) -> str:
    """
    Show only the last part of the field name (after the last period), unchanged.
    E.g. "fields.section_1.continuing_omnitrope" -> "continuing_omnitrope".
    """
    if not key or not isinstance(key, str):
        return key or ""
    if "." in key:
        return key.split(".")[-1]
    return key
