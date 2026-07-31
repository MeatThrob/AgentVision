"""
AgentVision sitecustomize — auto-loaded by Python at interpreter startup
when this directory is on PYTHONPATH.

Does ONE thing: install all in-process diagnostic hooks. Bails silently
if AGENTVISION_PROJECT isn't set (so this dir on PYTHONPATH never breaks
unrelated Python invocations).
"""
from __future__ import annotations

try:
    from av_runtime import install_all_hooks
    install_all_hooks()
except Exception:
    # Never break the host interpreter because instrumentation failed.
    pass
