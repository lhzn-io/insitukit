"""Fetch, summarise and publish in situ ocean observations.

- ``insitukit.coops``: NOAA CO-OPS (Tides and Currents) observations, tide predictions and station metadata.
- ``insitukit.erddap``: a paced, budgeted, thread-safe ERDDAP tabledap client for any server, for batch jobs and
  long-running services.
- ``insitukit.ndbc``: NDBC buoy standard meteorology and ADCP currents, from the monthly, yearly and real-time files.
- ``insitukit.stats``: daily means with a coverage rule, exceedance, joint histograms, marine heatwave climatology.
- ``insitukit.series``: the compact JSON form of a regular time series, and back.
- ``insitukit.store``: where the JSON goes (a directory, Cloudflare Workers KV or Google Cloud Storage), behind a
  guard on object names and sizes.

Call ``identify`` once at start-up so every request names your application and a contact.
"""

__version__ = "0.5.0"


def identify(user_agent: str, application: str | None = None) -> None:
    """Name the caller on every request: ``user_agent`` as the HTTP User-Agent (include a URL or an address a data
    provider can reach you at), and ``application`` as CO-OPS's ``application`` parameter (default: unchanged)."""
    from . import coops

    coops.USER_AGENT = user_agent
    coops._session.headers["User-Agent"] = user_agent
    if application is not None:
        coops.APPLICATION = application
