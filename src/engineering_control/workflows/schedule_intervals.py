"""Shared periodic-refresh intervals.

Kept separate from scheduler.py (which needs the heavier temporalio.client
Schedule types) so that consumers of these numbers -- like a staleness check
that needs to know how often a snapshot is refreshed -- don't have to pull
those in too. Single source of truth: nothing else should hardcode these
periods, and nothing that depends on their cadence (e.g. a staleness
threshold) should hardcode a number derived from them either -- see
operations/repository.py's provider-health staleness check, which computes
its threshold from QUOTA_REFRESH_INTERVAL instead of an independent literal
(NEW-16, 2026-09-22: a hardcoded 5-minute staleness threshold against this
schedule's old, undocumented 30-minute interval caused solve_backlog/
solve_issue's preflight to fail ~83% of the time).
"""

from __future__ import annotations

from datetime import timedelta

QUOTA_REFRESH_INTERVAL = timedelta(minutes=30)
CONNECTIONS_REFRESH_INTERVAL = timedelta(minutes=15)
