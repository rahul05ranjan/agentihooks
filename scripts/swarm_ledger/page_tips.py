def values() -> dict:
    from hooks.context import profile_chain
    from scripts.gates import claims, quiet, reruns
    from scripts.swarm_ledger import ledger_artifacts, ledger_bin, ledger_phases

    return {
        "send_backs": ledger_phases.ROUND_CAP,
        "claims": claims.CAP,
        "reruns": reruns.RERUN_CAP,
        "quiet_minutes": quiet.QUIET_MINUTES,
        "overlays": profile_chain.OVERLAY_CAP,
        "bin_days": ledger_bin.KEEP_DAYS,
        "trash_days": ledger_artifacts.KEEP_DAYS,
    }
