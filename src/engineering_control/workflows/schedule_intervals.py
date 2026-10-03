"""Shared periodic-refresh intervals.

Kept separate from scheduler.py (which needs the heavier temporalio.client
Schedule types) so that consumers of these numbers -- like a staleness check
that needs to know how often a snapshot is refreshed -- don't have to pull
those in too. Single source of truth: nothing else should hardcode these
periods. The provider preflight trusts an observation for 300 s
(operations/connection_status.py, PROVIDER_FRESHNESS_SECONDS), so the provider
refresh must run, including its execution timeout, inside that window
(NEW-16, 2026-09-22: a 5-minute threshold against an undocumented 30-minute
schedule made solve_backlog/solve_issue's preflight fail ~83% of the time).
"""

from __future__ import annotations

from datetime import timedelta

from engineering_control.operations.connection_status import PROVIDER_REFRESH_INTERVAL_SECONDS

# 2b678d9 (fd90545/33a1404): provider readiness is refreshed inside its 300 s freshness
# window, so the quota/provider refresh runs every PROVIDER_REFRESH_INTERVAL_SECONDS.
QUOTA_REFRESH_INTERVAL = timedelta(seconds=PROVIDER_REFRESH_INTERVAL_SECONDS)
CONNECTIONS_REFRESH_INTERVAL = timedelta(minutes=15)
