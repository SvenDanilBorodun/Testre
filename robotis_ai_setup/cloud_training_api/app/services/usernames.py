import re

SYNTHETIC_EMAIL_DOMAIN = "edubotics.local"

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")


def validate_username(username: str) -> str:
    lowered = username.strip().lower()
    if not USERNAME_RE.match(lowered):
        raise ValueError(
            "Benutzername muss 3-32 Zeichen lang sein und darf nur Kleinbuchstaben, "
            "Ziffern, Punkt, Bindestrich und Unterstrich enthalten."
        )
    return lowered


def synthetic_email(username: str) -> str:
    return f"{validate_username(username)}@{SYNTHETIC_EMAIL_DOMAIN}"
