"""The Twitch VOD retention window used by storage and search."""

from datetime import datetime, timedelta, timezone


# Twitch recordings expire, so keeping their links longer only creates dead cards.
# Fifty days leaves a margin before Twitch removes the recordings themselves.
TWITCH_RETENTION_DAYS = 50


def retention_cutoff(now=None):
    """Return the oldest upload date (YYYYMMDD) still retained, inclusive."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    oldest = current.astimezone(timezone.utc) - timedelta(days=TWITCH_RETENTION_DAYS)
    return oldest.strftime("%Y%m%d")


def is_recent_upload(upload_date, cutoff=None):
    """Whether a normalized YYYYMMDD upload date is inside the retention window."""
    value = str(upload_date or "").replace("-", "")
    if len(value) < 8:
        return False
    return value[:8] >= (cutoff or retention_cutoff())
