"""Exception hierarchy for the Intervals.icu client.

4xx responses map to distinct exception types so callers can react
specifically (e.g. re-auth vs. skip activity); 5xx and 429 are retried
with exponential backoff and only raised once retries are exhausted.
"""


class IntervalsError(Exception):
    """Base class for all Intervals.icu client errors."""


class IntervalsConfigError(IntervalsError):
    """Raised when required client configuration (e.g. the API key) is missing."""


class IntervalsHTTPError(IntervalsError):
    """Base class for HTTP-level errors, carrying the response status code."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Intervals.icu returned HTTP {status_code}: {message}")
        self.status_code = status_code


class IntervalsAuthError(IntervalsHTTPError):
    """401/403: the personal API key was rejected or lacks access."""


class IntervalsNotFoundError(IntervalsHTTPError):
    """404: the requested activity/resource does not exist."""


class IntervalsRateLimitError(IntervalsHTTPError):
    """429: rate limit still exceeded after retries with exponential backoff."""


class IntervalsClientError(IntervalsHTTPError):
    """Any other 4xx client error."""


class IntervalsServerError(IntervalsHTTPError):
    """5xx server error, raised after retries with exponential backoff."""
