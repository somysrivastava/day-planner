"""Central logging setup for the whole app. Imported once via
app/__init__.py (which every app.* module transitively imports), so every
module gets a consistently formatted logger via `logging.getLogger(__name__)`
without each one having to configure logging itself.

Deliberately minimal for a solo prototype (Week 2 Day 4 - "logging &
observability"): stdlib logging to stderr, one consistent format, no
external log aggregation/alerting/dashboard - the goal is that failures
are visible in whatever's running the process, not that they page anyone.
"""

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
