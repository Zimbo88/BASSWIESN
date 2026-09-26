"""Pure, deterministic LAB examples. Never import a radio adapter or transport."""
from copy import deepcopy
from basswiesn.app.services.lab_workbench import diagnose
from basswiesn.app.services.station_metadata import DISPLAY_FIELDS, DisplayPreference, display_fields

SCENARIOS = {
    "network_loss": ("playing", "offline", "stale", "returned"),
    "stale_ip": ("playing", "offline", "identity_required", "returned"),
    "metadata_missing": ("playing", "metadata_absent", "metadata_stale", "returned"),
    "reporting_failure": ("playing", "report_backoff", "report_repeated", "returned"),
    "provider_unavailable": ("playing", "provider_failed", "invalid", "returned"),
    "invalid_source": ("playing", "invalid", "stopped", "returned"),
    "partial_zone": ("playing", "zone_partial", "zone_unknown", "returned"),
}


def simulate(scenario, step):
    phase = SCENARIOS[scenario][step]
    observation = {"freshness": "RECENT", "observed_at": "2000-01-01T12:00:00+00:00"}
    snapshot = {
        "device": {"cached_reachable": True, **observation},
        "health": {"source_valid": True, "stream_alive": True, "state": "PLAYING", **observation},
        "playback": {"status": "PLAYING", **observation},
        "providers": [], "reporting": [], "restrictions": [],
        "metadata": {"state": "RECENT"},
    }
    if phase in {"offline", "stale", "identity_required"}:
        snapshot["device"].update(cached_reachable=False)
        snapshot["health"].update(stream_alive=None, state="UNKNOWN")
        snapshot["playback"].update(status="UNKNOWN")
        if phase == "stale": snapshot["device"]["freshness"] = "STALE"
    if phase.startswith("metadata_"):
        snapshot["metadata"]["state"] = "STALE" if phase.endswith("stale") else "NOT_OBSERVED"
    if phase.startswith("report_"):
        snapshot["reporting"] = [{"retry_count": 1 if phase.endswith("backoff") else 3, "state": "BACKOFF", **observation}]
    if phase == "provider_failed":
        snapshot["providers"] = [{"state": "UNAVAILABLE", **observation}]
    if phase in {"invalid", "stopped"}:
        snapshot["health"].update(source_valid=False, state="INVALID_SOURCE", stream_alive=None)
        snapshot["playback"]["status"] = "STOPPED" if phase == "stopped" else "UNKNOWN"
    zone = None
    if phase.startswith("zone_"):
        zone = {"master": "SIMULATED_MASTER", "members": [
            {"name": "SIMULATED_MEMBER_A", "observed": "JOINED"},
            {"name": "SIMULATED_MEMBER_B", "observed": "UNREACHABLE" if phase.endswith("partial") else "UNKNOWN"}],
            "result": "PARTIAL_NOT_VERIFIED"}
    return {"synthetic": True, "radio_contacted": False, "state_saved": False,
            "scenario": scenario, "step": step, "steps": len(SCENARIOS[scenario]),
            "phase": phase, "snapshot": deepcopy(snapshot), "diagnosis": diagnose(snapshot),
            "zone": zone, "recovery_executed": False, "audible_output_verified": False}


def preview_profile(fields, field_order, scenario):
    # Fixed fictional examples only; no submitted URLs, IDs, metadata or logs.
    station = "Example Station"
    metadata = {"artist": "Example Artist", "track": "Example Song", "other_info": "Example programme"}
    if scenario == "missing": metadata = {}
    if scenario == "long":
        station *= 15
        metadata = {key: value * 20 for key, value in metadata.items()}
    preference = DisplayPreference("CUSTOM", "other" in fields, tuple(fields), tuple(field_order))
    output = display_fields(preference, station, metadata, clock_text="20:15")
    return {"synthetic": True, "radio_contacted": False, "native_layout_verified": False,
            "title": output["track"], "limit": 256,
            "missing_fields": [f for f in fields if f in {"artist", "title", "other"} and not metadata]}
