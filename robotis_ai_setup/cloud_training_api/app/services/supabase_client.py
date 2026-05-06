import os

from supabase import create_client, Client

_client: Client | None = None


def get_supabase() -> Client:
    """Return a singleton Supabase service-role client.

    Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY via os.environ.get + an
    explicit empty-check so a missing or blank Railway service variable
    surfaces a clear, named error instead of a bare KeyError. The previous
    `os.environ["SUPABASE_URL"]` form raised KeyError on first call — which
    bubbled up to FastAPI as a generic 500 with no hint that a *specific*
    secret was missing. Same misconfiguration, much harder to diagnose.

    Startup validation lives in app.main via _validate_required_secrets so
    Railway picks up the misconfiguration at deploy time, not at first
    student request.
    """
    global _client
    if _client is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        missing = [
            name
            for name, val in (
                ("SUPABASE_URL", url),
                ("SUPABASE_SERVICE_ROLE_KEY", key),
            )
            if not val
        ]
        if missing:
            raise RuntimeError(
                "Cloud API misconfigured — required env vars missing or empty: "
                f"{', '.join(missing)}. Set them in the Railway service "
                "variables and redeploy."
            )
        _client = create_client(url, key)
    return _client
