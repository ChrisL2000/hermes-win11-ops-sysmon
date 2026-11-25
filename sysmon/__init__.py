"""hermes-win11-ops:sysmon -- Windows run-state worker.

Publishes a *head* (a small JSON record on a shared filesystem + optional
HTTP registration) that a supervisory node reads to discover this worker, then
operates autonomously: sample the host in real time, enforce run-states,
verify read-back, ship logs.
"""

__version__ = "0.1.0"
AGENT_KIND = "hermes-win11-ops:sysmon"
