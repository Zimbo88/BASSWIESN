# Phase 4A API-Vertragsmatrix

Automatisch aus montierten FastAPI-Routen und manuellen Cloud-Montagen
erzeugt. Zweck, Hardwarewirkung und Authbedarf sind statische
Auditklassifikationen; bestehende API-Vertraege wurden nicht veraendert.

Routenmethoden: 622

| Anwendung | Methode | Pfad | Handler | Antwort | Status | Write | Hardware | DB | Policy | Tests |
|---|---|---|---|---|---|---:|---:|---:|---:|---|
| cloud | GET | / | cloud_root | Response | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /about | cloud_about | HTMLResponse | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| cloud | GET | /api/cloud/stations | cloud_stations | list[dict] | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /bmx/orion/now-playing | now_playing | JSONResponse | 200 | nein | moeglich | nein | ja | tests/test_connectivity_recovery_250.py, tests/test_device_settings.py, tests/test_high_impact_architecture.py, tests/test_keepalive_circuit_breaker.py, tests/test_live_device_tools.py, tests/test_multiroom_router.py, tests/test_phase3_soundbig_validation.py, tests/test_phase3b_protected_access.py, tests/test_play_history_robustness.py, tests/test_playback_keepalive_research_160.py, tests/test_portable_safe_mode.py, tests/test_preset_cloud.py, tests/test_release_polish.py, tests/test_research_contracts_160.py, tests/test_research_runtime_integration_160.py, tests/test_research_state_api_160.py, tests/test_safety_fixes_160.py, tests/test_st20_readiness_retry.py |
| cloud | GET | /bmx/orion/now-playing/station/{station_id} | now_playing | JSONResponse | 200 | nein | moeglich | nein | ja | tests/test_connectivity_recovery_250.py, tests/test_device_settings.py, tests/test_high_impact_architecture.py, tests/test_keepalive_circuit_breaker.py, tests/test_live_device_tools.py, tests/test_multiroom_router.py, tests/test_phase3_soundbig_validation.py, tests/test_phase3b_protected_access.py, tests/test_play_history_robustness.py, tests/test_playback_keepalive_research_160.py, tests/test_portable_safe_mode.py, tests/test_preset_cloud.py, tests/test_release_polish.py, tests/test_research_contracts_160.py, tests/test_research_runtime_integration_160.py, tests/test_research_state_api_160.py, tests/test_safety_fixes_160.py, tests/test_st20_readiness_retry.py |
| cloud | POST | /bmx/orion/reporting | reporting | JSONResponse | 200 | ja | nein | nein | nein | tests/test_database_package.py, tests/test_human_ui_160.py, tests/test_playback_keepalive_research_160.py, tests/test_preset_cloud.py, tests/test_release_polish.py, tests/test_research_contracts_160.py, tests/test_research_domain_persistence.py, tests/test_research_runtime_integration_160.py, tests/test_research_state_api_160.py, tests/test_safety_fixes_160.py, tests/test_synthetic_24h_simulation.py |
| cloud | POST | /bmx/orion/reporting/station/{station_id} | reporting | JSONResponse | 200 | ja | nein | nein | nein | tests/test_database_package.py, tests/test_human_ui_160.py, tests/test_playback_keepalive_research_160.py, tests/test_preset_cloud.py, tests/test_release_polish.py, tests/test_research_contracts_160.py, tests/test_research_domain_persistence.py, tests/test_research_runtime_integration_160.py, tests/test_research_state_api_160.py, tests/test_safety_fixes_160.py, tests/test_synthetic_24h_simulation.py |
| cloud | GET | /bmx/radiobrowser/v1/now-playing/station/{uuid} | bmx_radiobrowser_station | JSONResponse | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /bmx/radiobrowser/v1/playback/station/{uuid} | bmx_radiobrowser_station | JSONResponse | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | POST | /bmx/radiobrowser/v1/reporting/station/{uuid} | bmx_radiobrowser_station | JSONResponse | 200 | ja | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /bmx/registry/servicesAvailability | bmx_services_availability | JSONResponse | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /bmx/registry/v1/introspect | provider_discovery | JSONResponse | 200 | nein | nein | nein | nein | tests/test_high_impact_architecture.py |
| cloud | GET | /bmx/registry/v1/services | bmx_registry | JSONResponse | 200 | nein | nein | nein | nein | tests/test_config_rewrite.py, tests/test_high_impact_architecture.py, tests/test_preset_cloud.py, tests/test_recovery_profiles_setup.py, tests/test_setup_batch_jobs.py |
| cloud | GET | /bmx/registry/v1/servicesAvailability | bmx_services_availability | JSONResponse | 200 | nein | nein | nein | nein | tests/test_high_impact_architecture.py |
| cloud | GET | /bmx/resolve | bmx_resolve | JSONResponse | 200 | nein | nein | nein | nein | tests/test_preset_cloud.py |
| cloud | POST | /bmx/resolve | bmx_resolve | JSONResponse | 200 | ja | nein | nein | nein | tests/test_preset_cloud.py |
| cloud | GET | /bmx/tunein/v1/favorite/{station_id} | bmx_tunein_station | JSONResponse | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | POST | /bmx/tunein/v1/favorite/{station_id} | bmx_tunein_station | JSONResponse | 200 | ja | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /bmx/tunein/v1/navigate | bmx_tunein_navigate | JSONResponse | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /bmx/tunein/v1/navigate/{path:path} | bmx_tunein_navigate | JSONResponse | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /bmx/tunein/v1/now-playing/station/{station_id} | bmx_tunein_station | JSONResponse | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /bmx/tunein/v1/playback/station/{station_id} | bmx_tunein_station | JSONResponse | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | POST | /bmx/tunein/v1/reporting/station/{station_id} | bmx_tunein_station | JSONResponse | 200 | ja | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /bmx/tunein/v1/token | bmx_tunein_token | JSONResponse | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | POST | /bmx/tunein/v1/token | bmx_tunein_token | JSONResponse | 200 | ja | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /core02/svc-bmx-adapter-orion/prod/orion | orion_service | JSONResponse | 200 | nein | nein | nein | nein | tests/test_orion.py, tests/test_phase3b_preset_integrity.py, tests/test_preset_cloud.py, tests/test_research_state_api_160.py |
| cloud | GET | /core02/svc-bmx-adapter-orion/prod/orion/station | orion_station | JSONResponse | 200,400,503 | nein | moeglich | nein | ja | tests/test_orion.py, tests/test_phase3b_preset_integrity.py, tests/test_preset_cloud.py, tests/test_research_state_api_160.py |
| cloud | POST | /core02/svc-bmx-adapter-orion/prod/orion/token | orion_token | JSONResponse | 200 | ja | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /getServiceSettings | service_settings | JSONResponse | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | POST | /getServiceSettings | service_settings | JSONResponse | 200 | ja | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /group | group_state | JSONResponse | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| cloud | POST | /group/create | group_create | JSONResponse | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| cloud | POST | /group/delete | group_delete | JSONResponse | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| cloud | POST | /group/update | group_update | JSONResponse | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| cloud | GET | /serviceSettings | service_settings | JSONResponse | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| cloud | POST | /serviceSettings | service_settings | JSONResponse | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| cloud | POST | /setMusicServiceAccount | set_music_service_account | JSONResponse | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| cloud | POST | /setMusicServiceOAuthAccount | set_music_service_oauth_account | JSONResponse | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| cloud | GET | /stationInfo | station_info | JSONResponse | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| cloud | POST | /streaming/account/{account_id}/device/ | streaming_add_device | Response | 201,400 | ja | nein | moeglich | nein | kein statischer Treffer |
| cloud | DELETE | /streaming/account/{account_id}/device/{device_id} | streaming_delete_device | JSONResponse | 200 | ja | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /streaming/account/{account_id}/device/{device_id} | streaming_get_device | Response | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | PUT | /streaming/account/{account_id}/device/{device_id} | streaming_put_device | JSONResponse | 200 | ja | nein | nein | nein | kein statischer Treffer |
| cloud | POST | /streaming/account/{account_id}/device/{device_id}/heartbeat | streaming_device_keepalive | JSONResponse | 200 | ja | nein | nein | nein | kein statischer Treffer |
| cloud | POST | /streaming/account/{account_id}/device/{device_id}/keepalive | streaming_device_keepalive | JSONResponse | 200 | ja | nein | nein | nein | kein statischer Treffer |
| cloud | DELETE | /streaming/account/{account_id}/device/{device_id}/preset/{button} | remove_preset | Response | 200,400 | ja | nein | moeglich | nein | kein statischer Treffer |
| cloud | PUT | /streaming/account/{account_id}/device/{device_id}/preset/{button} | put_preset | Response | 200,400 | ja | nein | moeglich | nein | kein statischer Treffer |
| cloud | GET | /streaming/account/{account_id}/device/{device_id}/presets | streaming_device_presets | Response | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | DELETE | /streaming/account/{account_id}/device/{device_id}/presets/{button} | remove_preset | Response | 200,400 | ja | nein | moeglich | nein | kein statischer Treffer |
| cloud | POST | /streaming/account/{account_id}/device/{device_id}/presets/{button} | put_preset | Response | 200,400 | ja | nein | moeglich | nein | kein statischer Treffer |
| cloud | PUT | /streaming/account/{account_id}/device/{device_id}/presets/{button} | put_preset | Response | 200,400 | ja | nein | moeglich | nein | kein statischer Treffer |
| cloud | POST | /streaming/account/{account_id}/device/{device_id}/recent | streaming_device_recent | Response | 200 | ja | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /streaming/account/{account_id}/device/{device_id}/recents | streaming_device_recents | Response | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /streaming/account/{account_id}/full | streaming_account_full | Response | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /streaming/account/{account_id}/presets/all | streaming_account_presets | Response | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /streaming/account/{account_id}/provider_settings | streaming_provider_settings | Response | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /streaming/account/{account_id}/sources | streaming_account_sources | Response | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /streaming/device/{device_id}/streaming_token | streaming_token | Response | 200 | nein | nein | nein | nein | tests/test_preset_cloud.py |
| cloud | GET | /streaming/provider-discovery | provider_discovery | JSONResponse | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /streaming/sourceproviders | streaming_sourceproviders | Response | 200 | nein | nein | nein | nein | tests/test_preset_cloud.py, tests/test_release_polish.py |
| cloud | POST | /streaming/support/power_on | streaming_power_on | Response | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| cloud | GET | /v1/blacklist/{device_id} | device_blacklist | JSONResponse | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | POST | /v1/blacklist/{device_id} | device_blacklist | JSONResponse | 200 | ja | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /v1/systems/devices/{device_id} | marge_full | Response | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | GET | /v1/systems/devices/{device_id}/presets | marge_presets | Response | 200 | nein | nein | nein | nein | tests/test_preset_cloud.py |
| cloud | GET | /v1/systems/devices/{device_id}/sources | marge_sources | Response | 200 | nein | nein | nein | nein | kein statischer Treffer |
| cloud | DELETE | /{path:path} | cloud_catch_all | Response | 200,204 | ja | nein | moeglich | nein | kein statischer Treffer |
| cloud | GET | /{path:path} | cloud_catch_all | Response | 200,204 | nein | nein | moeglich | nein | kein statischer Treffer |
| cloud | HEAD | /{path:path} | cloud_catch_all | Response | 200,204 | nein | nein | moeglich | nein | kein statischer Treffer |
| cloud | OPTIONS | /{path:path} | cloud_catch_all | Response | 200,204 | nein | nein | moeglich | nein | kein statischer Treffer |
| cloud | POST | /{path:path} | cloud_catch_all | Response | 200,204 | ja | nein | moeglich | nein | kein statischer Treffer |
| cloud | PUT | /{path:path} | cloud_catch_all | Response | 200,204 | ja | nein | moeglich | nein | kein statischer Treffer |
| diagnostics | GET | / | debug_home | HTMLResponse | 200 | nein | nein | nein | nein | kein statischer Treffer |
| diagnostics | GET | /diagnostics.json | diagnostics | dict | 200 | nein | nein | nein | nein | tests/test_contract_quality_audit_160.py, tests/test_database_package.py, tests/test_high_impact_architecture.py, tests/test_human_ui_160.py, tests/test_phase4a_contracts.py, tests/test_playback_keepalive_research_160.py, tests/test_release_polish.py, tests/test_research_state_api_160.py, tests/test_webui_smoke.py |
| diagnostics | GET | /health | health | dict[str, str] | 200 | nein | nein | nein | nein | tests/test_contract_quality_audit_160.py, tests/test_database_package.py, tests/test_high_impact_architecture.py, tests/test_human_ui_160.py, tests/test_local_test_expansion.py, tests/test_maintenance_reboot.py, tests/test_masterlog_setup_mode.py, tests/test_mobile_release.py, tests/test_phase3b_protected_access.py, tests/test_phase4a_contracts.py, tests/test_playback_keepalive_research_160.py, tests/test_protected_webui_200.py, tests/test_release_candidate.py, tests/test_release_hardening.py, tests/test_release_polish.py, tests/test_research_contracts_160.py, tests/test_research_domain_persistence.py, tests/test_research_runtime_integration_160.py, tests/test_research_state_api_160.py, tests/test_setup_rebuild_browser_160.py, tests/test_setup_wizard_playwright.py, tests/test_synthetic_24h_simulation.py, tests/test_version_metadata.py, tests/test_webui_smoke.py |
| diagnostics | GET | /requests | requests | list[dict] | 200 | nein | nein | nein | nein | tests/test_human_ui_160.py, tests/test_protected_webui_200.py, tests/test_release_polish.py, tests/test_research_state_api_160.py |
| https-webgui | POST | /api/announcements/jobs | announcement_job_create | dict | 200,400 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | POST | /api/announcements/preview | announcement_preview | dict | 200,400 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/announcements/status | get_announcements_status | dict | 200 | nein | nein | nein | nein | tests/test_local_test_expansion.py |
| https-webgui | POST | /api/backup/create | system_backup_create | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/backup/preview | system_backup_preview | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/backup/restore | system_backup_restore | dict | 200,409 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/backups | list_backups | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/backups/create | create_backup | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | GET | /api/battery/latest | latest_battery_states | list[dict] | 200,410 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/battery/patch/{device_id}/apply | battery_patch_apply | dict | 200,409,502 | ja | moeglich | nein | ja | tests/test_battery_diagnosis.py |
| https-webgui | POST | /api/battery/patch/{device_id}/dry-run | battery_patch_dry_run | dict | 200 | ja | nein | nein | ja | tests/test_battery_diagnosis.py |
| https-webgui | POST | /api/battery/patch/{device_id}/rollback | battery_patch_rollback | dict | 200,409,502 | ja | moeglich | nein | ja | tests/test_battery_diagnosis.py |
| https-webgui | GET | /api/battery/status/{device_id} | battery_patch_status | dict | 200 | nein | nein | nein | ja | tests/test_battery_diagnosis.py |
| https-webgui | GET | /api/device-capabilities/{device_id} | get_device_capabilities | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | DELETE | /api/device-capabilities/{device_id}/overrides/{capability_key} | delete_device_capability_override | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | POST | /api/device-capabilities/{device_id}/overrides/{capability_key} | set_device_capability_override | dict | 200,400 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | GET | /api/device-interactions | list_device_interactions | dict | 200 | nein | nein | nein | ja | kein statischer Treffer |
| https-webgui | GET | /api/device-models | get_device_models | dict | 200 | nein | nein | nein | nein | tests/test_local_test_expansion.py |
| https-webgui | GET | /api/devices | list_devices | list[dict] | 200 | nein | moeglich | nein | ja | tests/test_artwork_webui_160.py, tests/test_contract_quality_audit_160.py, tests/test_device_crud_api.py, tests/test_device_repository_service.py, tests/test_device_scan_api.py, tests/test_device_settings_200.py, tests/test_final_hardware_gate.py, tests/test_high_impact_architecture.py, tests/test_maintenance_reboot.py, tests/test_multiroom_router.py, tests/test_network_action_preflight.py, tests/test_phase3_soundbig_validation.py, tests/test_phase3b_protected_access.py, tests/test_play_history_robustness.py, tests/test_portable_safe_mode.py, tests/test_preset_cloud.py, tests/test_preset_transactions_200.py, tests/test_recovery_profiles_setup.py, tests/test_research_state_api_160.py, tests/test_safety_fixes_160.py, tests/test_webui_smoke.py |
| https-webgui | POST | /api/devices | add_device | dict | 200,400 | ja | moeglich | nein | ja | tests/test_artwork_webui_160.py, tests/test_contract_quality_audit_160.py, tests/test_device_crud_api.py, tests/test_device_scan_api.py, tests/test_device_settings_200.py, tests/test_final_hardware_gate.py, tests/test_high_impact_architecture.py, tests/test_maintenance_reboot.py, tests/test_multiroom_router.py, tests/test_network_action_preflight.py, tests/test_phase3_soundbig_validation.py, tests/test_phase3b_protected_access.py, tests/test_play_history_robustness.py, tests/test_portable_safe_mode.py, tests/test_preset_cloud.py, tests/test_preset_transactions_200.py, tests/test_recovery_profiles_setup.py, tests/test_research_state_api_160.py, tests/test_safety_fixes_160.py, tests/test_setup_batch_jobs.py, tests/test_webui_smoke.py |
| https-webgui | GET | /api/devices/health | devices_health | list[dict] | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/devices/live-comparison | device_live_comparison | dict | 200 | nein | moeglich | nein | ja | tests/test_webui_smoke.py |
| https-webgui | POST | /api/devices/radio-log/capture-batch | capture_radio_logs_batch | dict | 200,400,409 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/scan | scan_devices | dict | 200,400 | ja | nein | nein | nein | tests/test_device_scan_api.py, tests/test_webui_smoke.py |
| https-webgui | GET | /api/devices/status-badges | device_status_badges | list[dict] | 200 | nein | moeglich | nein | ja | tests/test_safety_fixes_160.py |
| https-webgui | GET | /api/devices/ui-capabilities | device_ui_capabilities | list[dict] | 200 | nein | nein | nein | nein | tests/test_high_impact_architecture.py |
| https-webgui | DELETE | /api/devices/{device_id} | remove_device | dict | 200,400,404 | ja | nein | nein | nein | tests/test_phase3_soundbig_validation.py, tests/test_preset_transactions_200.py |
| https-webgui | GET | /api/devices/{device_id}/action-journal | device_action_journal | list[dict] | 200 | nein | nein | nein | nein | tests/test_database_package.py |
| https-webgui | POST | /api/devices/{device_id}/backup/plan | backup_plan | dict | 200 | ja | moeglich | nein | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/bass-capabilities | bass_capabilities_probe | dict | 200 | ja | moeglich | nein | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/battery/diagnose | battery_diagnose | dict | 200 | ja | nein | nein | ja | kein statischer Treffer |
| https-webgui | GET | /api/devices/{device_id}/battery/patch-plan | battery_patch_plan | dict | 200 | nein | nein | nein | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/battery/probe | battery_probe | dict | 200,410 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/display-recovery/plan | display_recovery_plan | dict | 200,400 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/display/direct-select | display_direct_select | dict | 200,400 | ja | moeglich | nein | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/display/metadata-preview | display_metadata_preview | dict | 200,404 | ja | moeglich | nein | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/display/settings | save_device_display_settings | dict | 200,400 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/factory-fix | factory_fix | dict | 200,409,502 | ja | moeglich | nein | ja | kein statischer Treffer |
| https-webgui | GET | /api/devices/{device_id}/host-config | device_host_config | dict | 200 | nein | moeglich | nein | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/key | send_key_command | dict | 200,400,409,502 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | GET | /api/devices/{device_id}/live-summary | device_live_summary | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | PUT | /api/devices/{device_id}/maintenance-reboot | configure_maintenance_reboot | dict | 200,409,422 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/maintenance-reboot/run | run_safe_maintenance_reboot | dict | 200,403,409 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/media/list-servers | list_media_servers | dict | 200 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/migrate-id | migrate_device_id | dict | 200,400,409 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | GET | /api/devices/{device_id}/policy | get_device_policy | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | PUT | /api/devices/{device_id}/policy | update_device_policy | dict | 200 | ja | nein | moeglich | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/power/{action} | device_power_action | dict | 200,400,409,410 | ja | moeglich | nein | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/presets/download | download_device_presets | dict | 200 | ja | moeglich | moeglich | ja | tests/test_preset_transactions_200.py |
| https-webgui | POST | /api/devices/{device_id}/probe-info | probe_device_info | dict | 200 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | GET | /api/devices/{device_id}/provider-status | device_provider_status | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/radio-log/capture | capture_radio_logs | dict | 200 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | GET | /api/devices/{device_id}/radio-log/sources | radio_log_sources | dict | 200 | nein | moeglich | nein | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/recovery/{action} | device_recovery_action | dict | 200,400,410 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/rename | rename_device | dict | 200 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | GET | /api/devices/{device_id}/settings | device_settings | dict | 200 | nein | moeglich | moeglich | ja | tests/test_mobile_release.py |
| https-webgui | POST | /api/devices/{device_id}/settings-apply | apply_changed_device_settings | dict | 200,400 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/settings/{setting} | set_device_setting | dict | 200 | ja | moeglich | nein | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/setup/live-test | setup_live_test | dict | 200,409 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/sources/name-plan | source_name_plan | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/ssh-log/capture | capture_ssh_logs | dict | 200,400,409 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | GET | /api/devices/{device_id}/standby-clock/jobs/{job_id} | device_standby_clock_job | dict | 200,404 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/standby-clock/restore | device_standby_clock_restore | dict | 200,409 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | GET | /api/devices/{device_id}/standby-clock/status | device_standby_clock_status | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/devices/{device_id}/state | device_state | dict | 200 | nein | nein | nein | nein | tests/test_connectivity_recovery_250.py, tests/test_database_package.py, tests/test_high_impact_architecture.py, tests/test_playback_keepalive_research_160.py, tests/test_portable_safe_mode.py, tests/test_release_polish.py, tests/test_research_runtime_integration_160.py |
| https-webgui | POST | /api/devices/{device_id}/station/add-native | native_station_add | dict | 200 | ja | moeglich | nein | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/station/search-native | native_station_search | dict | 200 | ja | moeglich | nein | ja | tests/test_device_settings.py |
| https-webgui | POST | /api/devices/{device_id}/stations/{station_id}/play | play_station_on_device | dict | 200,400,404,409,502 | ja | moeglich | moeglich | ja | tests/test_multiroom_router.py, tests/test_phase3_soundbig_validation.py, tests/test_setup_batch_jobs.py |
| https-webgui | GET | /api/devices/{device_id}/support-bundle | support_bundle | StreamingResponse | 200,413 | nein | nein | nein | nein | tests/test_high_impact_architecture.py, tests/test_phase4b_hardening.py, tests/test_release_hardening.py |
| https-webgui | POST | /api/devices/{device_id}/telemetry/probe | probe_device_telemetry | dict | 200,400 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | GET | /api/devices/{device_id}/telnet/capabilities | device_telnet_capabilities | dict | 200 | nein | moeglich | nein | ja | kein statischer Treffer |
| https-webgui | GET | /api/devices/{device_id}/telnet/jobs/{job_id} | device_telnet_job | dict | 200,404 | nein | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/telnet/plan | device_telnet_plan | dict | 200,400 | ja | moeglich | nein | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/telnet/reboot | device_telnet_reboot_job | dict | 200,409 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | GET | /api/devices/{device_id}/wireless-profiles | wireless_profiles | dict | 200 | nein | moeglich | nein | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/wireless-profiles | add_wireless_profile | dict | 200,400,409 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | POST | /api/devices/{device_id}/zone/status | zone_status_probe | dict | 200 | ja | moeglich | nein | ja | kein statischer Treffer |
| https-webgui | GET | /api/diagnostics/emulation-gaps | diagnostics_emulation_gaps | dict | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| https-webgui | GET | /api/diagnostics/support-bundle | diagnostics_support_bundle | StreamingResponse | 200,404 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/diagnostics/system/export | diagnostics_system_export | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/diagnostics/system/preview | diagnostics_system_preview | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/diagnostics/telemetry/export | diagnostics_telemetry_export | Response | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| https-webgui | GET | /api/diagnostics/telemetry/report | diagnostics_telemetry_report | HTMLResponse | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| https-webgui | GET | /api/diagnostics/telemetry/summary | diagnostics_telemetry_summary | dict | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| https-webgui | POST | /api/discovery/ssdp | discovery_ssdp | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | GET | /api/discovery/test | discovery_test | dict | 200 | nein | nein | moeglich | nein | tests/test_setup_rebuild_160.py, tests/test_setup_rebuild_browser_160.py |
| https-webgui | GET | /api/display/metadata-modes | display_metadata_modes | list[dict] | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| https-webgui | POST | /api/dlna/discover | dlna_discover | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/dlna/status | get_dlna_status | dict | 200 | nein | nein | nein | nein | tests/test_local_test_expansion.py |
| https-webgui | GET | /api/events | get_events | dict | 200 | nein | nein | nein | nein | tests/test_local_test_expansion.py |
| https-webgui | POST | /api/events/test | create_test_event | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | GET | /api/features/docs/{document_id} | feature_documentation | Response | 200,404 | nein | nein | nein | nein | tests/test_phase2_activation.py, tests/test_release_candidate.py |
| https-webgui | GET | /api/features/status | feature_status | dict | 200 | nein | nein | nein | nein | tests/test_phase2_activation.py |
| https-webgui | GET | /api/health | health | dict[str, str | bool] | 200 | nein | nein | nein | nein | tests/test_contract_quality_audit_160.py, tests/test_database_package.py, tests/test_high_impact_architecture.py, tests/test_human_ui_160.py, tests/test_local_test_expansion.py, tests/test_maintenance_reboot.py, tests/test_masterlog_setup_mode.py, tests/test_mobile_release.py, tests/test_phase3b_protected_access.py, tests/test_phase4a_contracts.py, tests/test_playback_keepalive_research_160.py, tests/test_protected_webui_200.py, tests/test_release_candidate.py, tests/test_release_hardening.py, tests/test_release_polish.py, tests/test_research_contracts_160.py, tests/test_research_domain_persistence.py, tests/test_research_runtime_integration_160.py, tests/test_research_state_api_160.py, tests/test_setup_rebuild_browser_160.py, tests/test_setup_wizard_playwright.py, tests/test_synthetic_24h_simulation.py, tests/test_version_metadata.py, tests/test_webui_smoke.py |
| https-webgui | POST | /api/health/center | run_health_center | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | GET | /api/health/center/latest | health_center_latest | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/keys | key_commands | list[dict] | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| https-webgui | GET | /api/lab/port-probe | lab_port_probe | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/lab/status | get_lab_status | dict | 200 | nein | nein | nein | nein | tests/test_local_test_expansion.py |
| https-webgui | GET | /api/languages | languages | list[dict] | 200 | nein | nein | nein | nein | tests/test_high_impact_architecture.py, tests/test_mobile_release.py, tests/test_release_polish.py, tests/test_webui_smoke.py |
| https-webgui | GET | /api/maintenance/cleanup-preview | cleanup_preview | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/maintenance/cleanup/dry-run | maintenance_cleanup_dry_run | dict | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| https-webgui | POST | /api/maintenance/cleanup/run | maintenance_cleanup_run | dict | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| https-webgui | POST | /api/maintenance/clear-logs | clear_logs | dict | 200,409 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | POST | /api/maintenance/clear-test-devices | clear_test_devices | dict | 200,409 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | POST | /api/maintenance/reconcile-devices | reconcile_devices | dict | 200,409 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | GET | /api/maintenance/storage | maintenance_storage | dict | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| https-webgui | GET | /api/media-library/capabilities | media_library_capabilities | dict | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| https-webgui | DELETE | /api/media-playlists | clear_media_playlists | dict | 200 | ja | nein | moeglich | nein | tests/test_webui_smoke.py |
| https-webgui | GET | /api/media-playlists | media_playlists | list[dict] | 200 | nein | nein | nein | nein | tests/test_database_package.py, tests/test_webui_smoke.py |
| https-webgui | POST | /api/media-playlists | save_media_playlist | dict | 200,400 | ja | nein | moeglich | nein | tests/test_webui_smoke.py |
| https-webgui | GET | /api/media-types | media_types | list[dict] | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| https-webgui | POST | /api/media/library/roots | media_root_create | dict | 200,400 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | POST | /api/media/library/roots/{root_id}/scan | media_root_scan | dict | 200,400 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | GET | /api/media/library/search | media_search | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/media/library/status | media_library_status | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/media/library/validate-root | media_root_validate | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/multiroom/clear | multiroom_clear | dict | 200,404 | ja | moeglich | moeglich | ja | tests/test_multiroom_router.py, tests/test_webui_smoke.py |
| https-webgui | POST | /api/multiroom/clear-all | multiroom_clear_all | dict | 200 | ja | moeglich | nein | ja | kein statischer Treffer |
| https-webgui | POST | /api/multiroom/latency | multiroom_latency | dict | 200,400 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | GET | /api/multiroom/methods | multiroom_methods | list[dict] | 200 | nein | nein | nein | nein | tests/test_play_history_robustness.py |
| https-webgui | POST | /api/multiroom/preview | multiroom_preview | dict | 200,400,404 | ja | nein | nein | ja | tests/test_multiroom_router.py, tests/test_phase2_activation.py, tests/test_safety_fixes_160.py, tests/test_webui_smoke.py |
| https-webgui | GET | /api/multiroom/recent-stations | multiroom_recent_stations | list[dict] | 200 | nein | nein | nein | nein | tests/test_play_history_robustness.py |
| https-webgui | POST | /api/multiroom/remove-device | multiroom_remove_device | dict | 200,409,502,503 | ja | moeglich | moeglich | ja | tests/test_multiroom_router.py |
| https-webgui | GET | /api/multiroom/scenarios | multiroom_scenarios | list[dict] | 200 | nein | nein | nein | nein | tests/test_database_package.py, tests/test_final_hardware_gate.py, tests/test_multiroom_router.py, tests/test_webui_smoke.py |
| https-webgui | POST | /api/multiroom/scenarios | save_multiroom_scenario | dict | 200,400,404 | ja | nein | moeglich | nein | tests/test_final_hardware_gate.py, tests/test_multiroom_router.py, tests/test_webui_smoke.py |
| https-webgui | GET | /api/multiroom/scenarios-safe | multiroom_scenarios_safe | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/multiroom/scenarios-safe | multiroom_scenario_safe_create | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | DELETE | /api/multiroom/scenarios/{scenario_id} | delete_multiroom_scenario | dict | 200,404 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | POST | /api/multiroom/scenarios/{scenario_id}/activate | activate_multiroom_scenario | dict | 200,404 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/multiroom/scenarios/{scenario_id}/preview | preview_multiroom_scenario | dict | 200,404 | ja | nein | nein | ja | kein statischer Treffer |
| https-webgui | POST | /api/multiroom/set | multiroom_set | dict | 200,400,404,502 | ja | moeglich | moeglich | ja | tests/test_final_hardware_gate.py, tests/test_multiroom_router.py, tests/test_safety_fixes_160.py, tests/test_webui_smoke.py |
| https-webgui | GET | /api/multiroom/status/{device_id} | multiroom_status | dict | 200,404 | nein | moeglich | nein | ja | tests/test_safety_fixes_160.py |
| https-webgui | POST | /api/offline/preflight | offline_preflight | dict | 200,404 | ja | nein | nein | nein | tests/test_outbound_protected_targets_160.py, tests/test_phase2_activation.py |
| https-webgui | GET | /api/offline/status | get_offline_status | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/play-history | play_history | list[dict] | 200 | nein | nein | nein | nein | tests/test_confirmed_playback_state.py, tests/test_database_migrations.py, tests/test_database_package.py, tests/test_play_history_robustness.py, tests/test_release_polish.py, tests/test_webui_smoke.py |
| https-webgui | POST | /api/play-history/event | record_play_history_event | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | POST | /api/play-history/start | start_play_history | dict | 200,400 | ja | nein | moeglich | nein | tests/test_play_history_robustness.py |
| https-webgui | POST | /api/play-history/{history_id}/stop | stop_play_history | dict | 200,404 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | GET | /api/preset-profiles | preset_profiles | list[dict] | 200 | nein | nein | nein | nein | tests/test_database_package.py, tests/test_webui_smoke.py |
| https-webgui | POST | /api/preset-profiles | create_preset_profile | dict | 200,400 | ja | nein | moeglich | nein | tests/test_webui_smoke.py |
| https-webgui | POST | /api/preset-profiles/{profile_id}/apply/{device_id} | apply_preset_profile | dict | 200,404 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | POST | /api/presets/clone | clone_presets | dict | 200,400,404 | ja | nein | moeglich | nein | tests/test_webui_smoke.py |
| https-webgui | GET | /api/presets/{device_id} | get_presets | list[dict] | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/presets/{device_id}/plan | preset_plan | dict | 200,400 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/presets/{device_id}/status | preset_status | dict | 200 | nein | moeglich | moeglich | ja | tests/test_preset_cloud.py |
| https-webgui | POST | /api/presets/{device_id}/sync | sync_local_presets | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | DELETE | /api/presets/{device_id}/{button} | delete_preset | dict | 200,404,502 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | POST | /api/presets/{device_id}/{button} | set_preset | dict | 200,400,404,502 | ja | moeglich | moeglich | ja | tests/test_preset_cloud.py |
| https-webgui | GET | /api/quick-fixes | quick_fixes | dict | 200 | nein | nein | nein | nein | tests/test_local_test_expansion.py |
| https-webgui | POST | /api/quick-fixes/{quick_fix_id}/execute | quick_fix_execute | dict | 200,400,409 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | POST | /api/quick-fixes/{quick_fix_id}/preview | quick_fix_preview | dict | 200,400 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/readiness | readiness | dict[str, str | bool | dict] | 200 | nein | nein | nein | nein | tests/test_contract_quality_audit_160.py, tests/test_database_package.py, tests/test_phase4b_hardening.py, tests/test_release_candidate.py, tests/test_release_polish.py, tests/test_research_contracts_160.py, tests/test_research_domain_persistence.py, tests/test_research_runtime_integration_160.py, tests/test_research_state_api_160.py, tests/test_setup_batch_jobs.py, tests/test_st20_readiness_retry.py |
| https-webgui | GET | /api/reference-setups | reference_setups | list[dict] | 200 | nein | nein | nein | nein | tests/test_database_package.py, tests/test_webui_smoke.py |
| https-webgui | POST | /api/reference-setups/from-device/{device_id} | create_reference_setup | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | POST | /api/reference-setups/{setup_id}/apply/{device_id} | apply_reference_setup | dict | 200,404 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/restore/prepare | restore_prepare | dict | 200,400 | ja | nein | moeglich | nein | tests/test_system_backup.py |
| https-webgui | POST | /api/restore/preview | restore_preview | dict | 200,400 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | DELETE | /api/schedules | clear_schedules | dict | 200 | ja | nein | moeglich | nein | tests/test_final_hardware_gate.py, tests/test_multiroom_router.py, tests/test_play_history_robustness.py, tests/test_webui_smoke.py |
| https-webgui | GET | /api/schedules | schedules | list[dict] | 200 | nein | nein | nein | nein | tests/test_final_hardware_gate.py, tests/test_human_ui_160.py, tests/test_multiroom_router.py, tests/test_play_history_robustness.py, tests/test_webui_smoke.py |
| https-webgui | POST | /api/schedules | create_schedule | dict | 200 | ja | nein | moeglich | nein | tests/test_final_hardware_gate.py, tests/test_multiroom_router.py, tests/test_play_history_robustness.py, tests/test_webui_smoke.py |
| https-webgui | DELETE | /api/schedules/{schedule_id} | delete_schedule | dict | 200 | ja | nein | moeglich | nein | tests/test_multiroom_router.py |
| https-webgui | POST | /api/schedules/{schedule_id} | update_schedule | dict | 200 | ja | nein | moeglich | nein | tests/test_multiroom_router.py |
| https-webgui | POST | /api/schedules/{schedule_id}/enable | enable_schedule | dict | 200 | ja | nein | moeglich | nein | tests/test_multiroom_router.py |
| https-webgui | POST | /api/schedules/{schedule_id}/trigger | trigger_schedule_now | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/services/catalog | services_catalog | list[dict] | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| https-webgui | GET | /api/settings/catalog | settings_catalog | list[dict] | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| https-webgui | GET | /api/setup-jobs | setup_jobs | dict | 200 | nein | nein | nein | nein | tests/test_database_package.py, tests/test_local_test_expansion.py, tests/test_release_hardening.py |
| https-webgui | POST | /api/setup-jobs | setup_job_create | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | POST | /api/setup-jobs/{job_id}/status | setup_job_status | dict | 200,400 | ja | nein | moeglich | nein | tests/test_local_test_expansion.py |
| https-webgui | POST | /api/setup/account/{device_id} | setup_local_account | dict | 200,400,409 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/setup/activation-playback/{device_id} | retry_setup_activation_playback | dict | 200,502 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/setup/cloud-route/{device_id} | setup_cloud_route | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/setup/cloud-route/{device_id}/apply | apply_setup_cloud_route | dict | 200,400,409,502 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | POST | /api/setup/cloud-route/{device_id}/rollback | rollback_setup_cloud_route | dict | 200,404,409 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | POST | /api/setup/cloud-route/{device_id}/verify | verify_setup_cloud_route | dict | 200 | ja | moeglich | nein | ja | kein statischer Treffer |
| https-webgui | GET | /api/setup/devices | setup_devices | list[dict] | 200 | nein | nein | nein | nein | tests/test_phase4b_hardening.py, tests/test_setup_batch_jobs.py |
| https-webgui | GET | /api/setup/jobs/latest | latest_setup_job | dict | 200,404 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| https-webgui | POST | /api/setup/jobs/start | start_setup_job | dict | 200,400 | ja | nein | nein | nein | tests/test_setup_batch_jobs.py |
| https-webgui | GET | /api/setup/jobs/{job_id} | get_setup_job | dict | 200,404 | nein | nein | nein | nein | tests/test_setup_batch_jobs.py |
| https-webgui | POST | /api/setup/jobs/{job_id}/cancel | cancel_setup_job | dict | 200,404 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/setup/plans/{device_id} | setup_plan | dict | 200 | nein | nein | nein | nein | tests/test_database_package.py |
| https-webgui | POST | /api/setup/plans/{device_id} | save_setup_plan | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | POST | /api/setup/wizard/apply/{device_id} | setup_wizard_apply | dict | 200,409 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/setup/wizard/preflight/{device_id} | setup_wizard_preflight | dict | 200 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| https-webgui | GET | /api/setup/wizard/server-info | setup_wizard_server_info | dict | 200 | nein | moeglich | nein | ja | tests/test_recovery_profiles_setup.py, tests/test_webui_smoke.py |
| https-webgui | POST | /api/setup/wizard/service-status | record_browser_service_status | dict | 200,400 | ja | nein | nein | nein | tests/test_recovery_profiles_setup.py |
| https-webgui | GET | /api/ssh/remote-services-file | download_remote_services_file | Response | 200 | nein | moeglich | nein | ja | tests/test_ssh_remote_services.py |
| https-webgui | GET | /api/stations | stations | list[dict] | 200 | nein | nein | nein | nein | tests/test_artwork_webui_160.py, tests/test_database_package.py, tests/test_device_settings.py, tests/test_high_impact_architecture.py, tests/test_human_ui_160.py, tests/test_internal_activation_streams.py, tests/test_multiroom_router.py, tests/test_offline_mode.py, tests/test_phase2_activation.py, tests/test_phase3_soundbig_validation.py, tests/test_phase3b_notification_order.py, tests/test_phase3b_preset_integrity.py, tests/test_phase4b_hardening.py, tests/test_play_history_robustness.py, tests/test_preset_cloud.py, tests/test_preset_transactions_200.py, tests/test_webui_smoke.py |
| https-webgui | POST | /api/stations | add_station | dict | 200,400 | ja | nein | moeglich | nein | tests/test_artwork_webui_160.py, tests/test_device_settings.py, tests/test_human_ui_160.py, tests/test_internal_activation_streams.py, tests/test_multiroom_router.py, tests/test_offline_mode.py, tests/test_phase4b_hardening.py, tests/test_preset_cloud.py, tests/test_webui_smoke.py |
| https-webgui | GET | /api/stations/search-online | search_online_stations | list[dict] | 200,409,503 | nein | moeglich | nein | ja | tests/test_offline_mode.py |
| https-webgui | POST | /api/stations/upload | upload_station_file | dict | 200,400,413 | ja | nein | moeglich | nein | tests/test_phase4b_hardening.py |
| https-webgui | GET | /api/stations/{station_id}/logo-status | station_logo_status | dict | 200,404 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/stats/playback | playback_stats | dict | 200 | nein | nein | nein | nein | tests/test_multiroom_router.py, tests/test_play_history_robustness.py, tests/test_stats_internal_playback_exclusion.py, tests/test_webui_smoke.py |
| https-webgui | GET | /api/stereo-pairing/research | stereo_pairing_research | dict | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| https-webgui | GET | /api/support-bundle | global_support_bundle | StreamingResponse | 200,413 | nein | nein | nein | nein | tests/test_release_hardening.py |
| https-webgui | GET | /api/system/healthcheck | system_healthcheck | dict | 200 | nein | nein | nein | nein | tests/test_release_hardening.py, tests/test_release_polish.py |
| https-webgui | GET | /api/system/service-health | system_service_health | dict | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| https-webgui | GET | /api/system/settings | system_settings | dict | 200 | nein | nein | nein | ja | tests/test_final_hardware_gate.py, tests/test_high_impact_architecture.py, tests/test_mobile_release.py, tests/test_setup_wizard_playwright.py, tests/test_version_metadata.py, tests/test_webui_smoke.py |
| https-webgui | POST | /api/system/settings | save_system_settings | dict | 200,400 | ja | nein | moeglich | ja | tests/test_final_hardware_gate.py, tests/test_high_impact_architecture.py, tests/test_mobile_release.py, tests/test_setup_wizard_playwright.py, tests/test_version_metadata.py, tests/test_webui_smoke.py |
| https-webgui | POST | /api/system/warnings/ack | acknowledge_first_run_warning | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | GET | /api/telemetry | telemetry | list[dict] | 200 | nein | nein | nein | nein | tests/test_database_package.py, tests/test_human_ui_160.py, tests/test_mobile_release.py, tests/test_playback_keepalive_research_160.py, tests/test_release_polish.py, tests/test_webui_smoke.py |
| https-webgui | POST | /api/telemetry | ingest_telemetry | dict | 200 | ja | nein | moeglich | nein | tests/test_webui_smoke.py |
| https-webgui | GET | /api/telemetry/summary | telemetry_summary | dict | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| https-webgui | GET | /api/telnet/commands | telnet_commands | list[dict] | 200 | nein | moeglich | nein | ja | tests/test_webui_smoke.py |
| https-webgui | POST | /api/update/check | run_update_check | dict | 200 | ja | nein | nein | nein | tests/test_offline_mode.py, tests/test_update_check.py |
| https-webgui | GET | /api/update/status | update_status | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /api/updates/local/prepare | update_local_prepare | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | POST | /api/updates/local/preview | update_local_preview | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /api/version | api_version | dict[str, str] | 200 | nein | nein | nein | nein | tests/test_phase4b_hardening.py, tests/test_release_candidate.py, tests/test_release_polish.py |
| https-webgui | GET | /api/webhooks | list_webhooks | dict | 200 | nein | nein | nein | nein | tests/test_local_test_expansion.py |
| https-webgui | POST | /api/webhooks | create_webhook | dict | 200,400 | ja | nein | moeglich | nein | tests/test_local_test_expansion.py |
| https-webgui | GET | /api/webhooks/validate | webhook_validate | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | DELETE | /api/webhooks/{endpoint_id} | delete_webhook | dict | 200,404 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | PUT | /api/webhooks/{endpoint_id} | update_webhook | dict | 200,400 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | POST | /api/webhooks/{endpoint_id}/test | test_webhook | dict | 200,404 | ja | nein | moeglich | nein | tests/test_local_test_expansion.py |
| https-webgui | GET | /bmx/orion/now-playing | now_playing | handler-defined JSON | 200 | nein | moeglich | nein | ja | tests/test_connectivity_recovery_250.py, tests/test_device_settings.py, tests/test_high_impact_architecture.py, tests/test_keepalive_circuit_breaker.py, tests/test_live_device_tools.py, tests/test_multiroom_router.py, tests/test_phase3_soundbig_validation.py, tests/test_phase3b_protected_access.py, tests/test_play_history_robustness.py, tests/test_playback_keepalive_research_160.py, tests/test_portable_safe_mode.py, tests/test_preset_cloud.py, tests/test_release_polish.py, tests/test_research_contracts_160.py, tests/test_research_runtime_integration_160.py, tests/test_research_state_api_160.py, tests/test_safety_fixes_160.py, tests/test_st20_readiness_retry.py |
| https-webgui | GET | /bmx/orion/now-playing/station/{station_id} | now_playing | handler-defined JSON | 200 | nein | moeglich | nein | ja | tests/test_connectivity_recovery_250.py, tests/test_device_settings.py, tests/test_high_impact_architecture.py, tests/test_keepalive_circuit_breaker.py, tests/test_live_device_tools.py, tests/test_multiroom_router.py, tests/test_phase3_soundbig_validation.py, tests/test_phase3b_protected_access.py, tests/test_play_history_robustness.py, tests/test_playback_keepalive_research_160.py, tests/test_portable_safe_mode.py, tests/test_preset_cloud.py, tests/test_release_polish.py, tests/test_research_contracts_160.py, tests/test_research_runtime_integration_160.py, tests/test_research_state_api_160.py, tests/test_safety_fixes_160.py, tests/test_st20_readiness_retry.py |
| https-webgui | POST | /bmx/orion/reporting | reporting | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_database_package.py, tests/test_human_ui_160.py, tests/test_playback_keepalive_research_160.py, tests/test_preset_cloud.py, tests/test_release_polish.py, tests/test_research_contracts_160.py, tests/test_research_domain_persistence.py, tests/test_research_runtime_integration_160.py, tests/test_research_state_api_160.py, tests/test_safety_fixes_160.py, tests/test_synthetic_24h_simulation.py |
| https-webgui | POST | /bmx/orion/reporting/station/{station_id} | reporting | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_database_package.py, tests/test_human_ui_160.py, tests/test_playback_keepalive_research_160.py, tests/test_preset_cloud.py, tests/test_release_polish.py, tests/test_research_contracts_160.py, tests/test_research_domain_persistence.py, tests/test_research_runtime_integration_160.py, tests/test_research_state_api_160.py, tests/test_safety_fixes_160.py, tests/test_synthetic_24h_simulation.py |
| https-webgui | GET | /bmx/radiobrowser/v1/now-playing/station/{uuid} | bmx_radiobrowser_station | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /bmx/radiobrowser/v1/playback/station/{uuid} | bmx_radiobrowser_station | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /bmx/radiobrowser/v1/reporting/station/{uuid} | bmx_radiobrowser_station | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /bmx/registry/servicesAvailability | bmx_services_availability | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /bmx/registry/v1/introspect | provider_discovery | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_high_impact_architecture.py |
| https-webgui | GET | /bmx/registry/v1/services | bmx_registry | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_config_rewrite.py, tests/test_high_impact_architecture.py, tests/test_preset_cloud.py, tests/test_recovery_profiles_setup.py, tests/test_setup_batch_jobs.py |
| https-webgui | GET | /bmx/registry/v1/servicesAvailability | bmx_services_availability | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_high_impact_architecture.py |
| https-webgui | GET | /bmx/resolve | bmx_resolve | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_preset_cloud.py |
| https-webgui | POST | /bmx/resolve | bmx_resolve | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_preset_cloud.py |
| https-webgui | GET | /bmx/tunein/v1/favorite/{station_id} | bmx_tunein_station | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /bmx/tunein/v1/favorite/{station_id} | bmx_tunein_station | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /bmx/tunein/v1/now-playing/station/{station_id} | bmx_tunein_station | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /bmx/tunein/v1/playback/station/{station_id} | bmx_tunein_station | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /bmx/tunein/v1/reporting/station/{station_id} | bmx_tunein_station | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /core02/svc-bmx-adapter-orion/prod/orion/station | orion_station | handler-defined JSON | 200 | nein | moeglich | nein | ja | tests/test_orion.py, tests/test_phase3b_preset_integrity.py, tests/test_preset_cloud.py, tests/test_research_state_api_160.py |
| https-webgui | POST | /core02/svc-bmx-adapter-orion/prod/orion/token | orion_token | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /getServiceSettings | service_settings | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /getServiceSettings | service_settings | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /group | group_state | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| https-webgui | POST | /group/create | group_create | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| https-webgui | POST | /group/delete | group_delete | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| https-webgui | POST | /group/update | group_update | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| https-webgui | GET | /remote/{device_id} | remote | HTMLResponse | 200 | nein | moeglich | nein | ja | tests/test_artwork_cache_160.py, tests/test_connectivity_recovery_250.py, tests/test_local_test_expansion.py, tests/test_outbound_protected_targets_160.py, tests/test_phase3b_protected_access.py, tests/test_recovery_profiles_setup.py, tests/test_safety_fixes_160.py, tests/test_ssh_preflight.py, tests/test_ssh_remote_services.py, tests/test_version_metadata.py, tests/test_webui_smoke.py |
| https-webgui | GET | /serviceSettings | service_settings | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| https-webgui | POST | /serviceSettings | service_settings | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| https-webgui | POST | /setMusicServiceAccount | set_music_service_account | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| https-webgui | POST | /setMusicServiceOAuthAccount | set_music_service_oauth_account | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| https-webgui | GET | /stationInfo | station_info | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| https-webgui | POST | /streaming/account/{account_id}/device/ | streaming_add_device | handler-defined JSON | 201 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | DELETE | /streaming/account/{account_id}/device/{device_id} | streaming_delete_device | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /streaming/account/{account_id}/device/{device_id} | streaming_get_device | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | PUT | /streaming/account/{account_id}/device/{device_id} | streaming_put_device | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /streaming/account/{account_id}/device/{device_id}/heartbeat | streaming_device_keepalive | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /streaming/account/{account_id}/device/{device_id}/keepalive | streaming_device_keepalive | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | PUT | /streaming/account/{account_id}/device/{device_id}/preset/{button} | put_preset | handler-defined JSON | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | GET | /streaming/account/{account_id}/device/{device_id}/presets | streaming_device_presets | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /streaming/account/{account_id}/device/{device_id}/presets/{button} | put_preset | handler-defined JSON | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | PUT | /streaming/account/{account_id}/device/{device_id}/presets/{button} | put_preset | handler-defined JSON | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| https-webgui | POST | /streaming/account/{account_id}/device/{device_id}/recent | streaming_device_recent | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /streaming/account/{account_id}/device/{device_id}/recents | streaming_device_recents | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /streaming/account/{account_id}/full | streaming_account_full | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /streaming/account/{account_id}/presets/all | streaming_account_presets | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /streaming/account/{account_id}/provider_settings | streaming_provider_settings | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /streaming/account/{account_id}/sources | streaming_account_sources | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /streaming/device/{device_id}/streaming_token | streaming_token | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_preset_cloud.py |
| https-webgui | GET | /streaming/provider-discovery | provider_discovery | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /streaming/sourceproviders | streaming_sourceproviders | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_preset_cloud.py, tests/test_release_polish.py |
| https-webgui | POST | /streaming/support/power_on | streaming_power_on | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| https-webgui | GET | /v1/blacklist/{device_id} | device_blacklist | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | POST | /v1/blacklist/{device_id} | device_blacklist | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /v1/systems/devices/{device_id} | marge_full | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| https-webgui | GET | /v1/systems/devices/{device_id}/presets | marge_presets | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_preset_cloud.py |
| https-webgui | GET | /v1/systems/devices/{device_id}/sources | marge_sources | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/announcements/jobs | announcement_job_create | dict | 200,400 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | POST | /api/announcements/preview | announcement_preview | dict | 200,400 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/announcements/status | get_announcements_status | dict | 200 | nein | nein | nein | nein | tests/test_local_test_expansion.py |
| webgui | POST | /api/backup/create | system_backup_create | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/backup/preview | system_backup_preview | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/backup/restore | system_backup_restore | dict | 200,409 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/backups | list_backups | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/backups/create | create_backup | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | GET | /api/battery/latest | latest_battery_states | list[dict] | 200,410 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/battery/patch/{device_id}/apply | battery_patch_apply | dict | 200,409,502 | ja | moeglich | nein | ja | tests/test_battery_diagnosis.py |
| webgui | POST | /api/battery/patch/{device_id}/dry-run | battery_patch_dry_run | dict | 200 | ja | nein | nein | ja | tests/test_battery_diagnosis.py |
| webgui | POST | /api/battery/patch/{device_id}/rollback | battery_patch_rollback | dict | 200,409,502 | ja | moeglich | nein | ja | tests/test_battery_diagnosis.py |
| webgui | GET | /api/battery/status/{device_id} | battery_patch_status | dict | 200 | nein | nein | nein | ja | tests/test_battery_diagnosis.py |
| webgui | GET | /api/device-capabilities/{device_id} | get_device_capabilities | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | DELETE | /api/device-capabilities/{device_id}/overrides/{capability_key} | delete_device_capability_override | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | POST | /api/device-capabilities/{device_id}/overrides/{capability_key} | set_device_capability_override | dict | 200,400 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | GET | /api/device-interactions | list_device_interactions | dict | 200 | nein | nein | nein | ja | kein statischer Treffer |
| webgui | GET | /api/device-models | get_device_models | dict | 200 | nein | nein | nein | nein | tests/test_local_test_expansion.py |
| webgui | GET | /api/devices | list_devices | list[dict] | 200 | nein | moeglich | nein | ja | tests/test_artwork_webui_160.py, tests/test_contract_quality_audit_160.py, tests/test_device_crud_api.py, tests/test_device_repository_service.py, tests/test_device_scan_api.py, tests/test_device_settings_200.py, tests/test_final_hardware_gate.py, tests/test_high_impact_architecture.py, tests/test_maintenance_reboot.py, tests/test_multiroom_router.py, tests/test_network_action_preflight.py, tests/test_phase3_soundbig_validation.py, tests/test_phase3b_protected_access.py, tests/test_play_history_robustness.py, tests/test_portable_safe_mode.py, tests/test_preset_cloud.py, tests/test_preset_transactions_200.py, tests/test_recovery_profiles_setup.py, tests/test_research_state_api_160.py, tests/test_safety_fixes_160.py, tests/test_webui_smoke.py |
| webgui | POST | /api/devices | add_device | dict | 200,400 | ja | moeglich | nein | ja | tests/test_artwork_webui_160.py, tests/test_contract_quality_audit_160.py, tests/test_device_crud_api.py, tests/test_device_scan_api.py, tests/test_device_settings_200.py, tests/test_final_hardware_gate.py, tests/test_high_impact_architecture.py, tests/test_maintenance_reboot.py, tests/test_multiroom_router.py, tests/test_network_action_preflight.py, tests/test_phase3_soundbig_validation.py, tests/test_phase3b_protected_access.py, tests/test_play_history_robustness.py, tests/test_portable_safe_mode.py, tests/test_preset_cloud.py, tests/test_preset_transactions_200.py, tests/test_recovery_profiles_setup.py, tests/test_research_state_api_160.py, tests/test_safety_fixes_160.py, tests/test_setup_batch_jobs.py, tests/test_webui_smoke.py |
| webgui | GET | /api/devices/health | devices_health | list[dict] | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/devices/live-comparison | device_live_comparison | dict | 200 | nein | moeglich | nein | ja | tests/test_webui_smoke.py |
| webgui | POST | /api/devices/radio-log/capture-batch | capture_radio_logs_batch | dict | 200,400,409 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | POST | /api/devices/scan | scan_devices | dict | 200,400 | ja | nein | nein | nein | tests/test_device_scan_api.py, tests/test_webui_smoke.py |
| webgui | GET | /api/devices/status-badges | device_status_badges | list[dict] | 200 | nein | moeglich | nein | ja | tests/test_safety_fixes_160.py |
| webgui | GET | /api/devices/ui-capabilities | device_ui_capabilities | list[dict] | 200 | nein | nein | nein | nein | tests/test_high_impact_architecture.py |
| webgui | DELETE | /api/devices/{device_id} | remove_device | dict | 200,400,404 | ja | nein | nein | nein | tests/test_phase3_soundbig_validation.py, tests/test_preset_transactions_200.py |
| webgui | GET | /api/devices/{device_id}/action-journal | device_action_journal | list[dict] | 200 | nein | nein | nein | nein | tests/test_database_package.py |
| webgui | POST | /api/devices/{device_id}/backup/plan | backup_plan | dict | 200 | ja | moeglich | nein | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/bass-capabilities | bass_capabilities_probe | dict | 200 | ja | moeglich | nein | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/battery/diagnose | battery_diagnose | dict | 200 | ja | nein | nein | ja | kein statischer Treffer |
| webgui | GET | /api/devices/{device_id}/battery/patch-plan | battery_patch_plan | dict | 200 | nein | nein | nein | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/battery/probe | battery_probe | dict | 200,410 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/display-recovery/plan | display_recovery_plan | dict | 200,400 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/display/direct-select | display_direct_select | dict | 200,400 | ja | moeglich | nein | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/display/metadata-preview | display_metadata_preview | dict | 200,404 | ja | moeglich | nein | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/display/settings | save_device_display_settings | dict | 200,400 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/factory-fix | factory_fix | dict | 200,409,502 | ja | moeglich | nein | ja | kein statischer Treffer |
| webgui | GET | /api/devices/{device_id}/host-config | device_host_config | dict | 200 | nein | moeglich | nein | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/key | send_key_command | dict | 200,400,409,502 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | GET | /api/devices/{device_id}/live-summary | device_live_summary | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | PUT | /api/devices/{device_id}/maintenance-reboot | configure_maintenance_reboot | dict | 200,409,422 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/maintenance-reboot/run | run_safe_maintenance_reboot | dict | 200,403,409 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/media/list-servers | list_media_servers | dict | 200 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/migrate-id | migrate_device_id | dict | 200,400,409 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | GET | /api/devices/{device_id}/policy | get_device_policy | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | PUT | /api/devices/{device_id}/policy | update_device_policy | dict | 200 | ja | nein | moeglich | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/power/{action} | device_power_action | dict | 200,400,409,410 | ja | moeglich | nein | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/presets/download | download_device_presets | dict | 200 | ja | moeglich | moeglich | ja | tests/test_preset_transactions_200.py |
| webgui | POST | /api/devices/{device_id}/probe-info | probe_device_info | dict | 200 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | GET | /api/devices/{device_id}/provider-status | device_provider_status | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/radio-log/capture | capture_radio_logs | dict | 200 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | GET | /api/devices/{device_id}/radio-log/sources | radio_log_sources | dict | 200 | nein | moeglich | nein | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/recovery/{action} | device_recovery_action | dict | 200,400,410 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/rename | rename_device | dict | 200 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | GET | /api/devices/{device_id}/settings | device_settings | dict | 200 | nein | moeglich | moeglich | ja | tests/test_mobile_release.py |
| webgui | POST | /api/devices/{device_id}/settings-apply | apply_changed_device_settings | dict | 200,400 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/settings/{setting} | set_device_setting | dict | 200 | ja | moeglich | nein | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/setup/live-test | setup_live_test | dict | 200,409 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/sources/name-plan | source_name_plan | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/ssh-log/capture | capture_ssh_logs | dict | 200,400,409 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | GET | /api/devices/{device_id}/standby-clock/jobs/{job_id} | device_standby_clock_job | dict | 200,404 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/standby-clock/restore | device_standby_clock_restore | dict | 200,409 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | GET | /api/devices/{device_id}/standby-clock/status | device_standby_clock_status | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/devices/{device_id}/state | device_state | dict | 200 | nein | nein | nein | nein | tests/test_connectivity_recovery_250.py, tests/test_database_package.py, tests/test_high_impact_architecture.py, tests/test_playback_keepalive_research_160.py, tests/test_portable_safe_mode.py, tests/test_release_polish.py, tests/test_research_runtime_integration_160.py |
| webgui | POST | /api/devices/{device_id}/station/add-native | native_station_add | dict | 200 | ja | moeglich | nein | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/station/search-native | native_station_search | dict | 200 | ja | moeglich | nein | ja | tests/test_device_settings.py |
| webgui | POST | /api/devices/{device_id}/stations/{station_id}/play | play_station_on_device | dict | 200,400,404,409,502 | ja | moeglich | moeglich | ja | tests/test_multiroom_router.py, tests/test_phase3_soundbig_validation.py, tests/test_setup_batch_jobs.py |
| webgui | GET | /api/devices/{device_id}/support-bundle | support_bundle | StreamingResponse | 200,413 | nein | nein | nein | nein | tests/test_high_impact_architecture.py, tests/test_phase4b_hardening.py, tests/test_release_hardening.py |
| webgui | POST | /api/devices/{device_id}/telemetry/probe | probe_device_telemetry | dict | 200,400 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | GET | /api/devices/{device_id}/telnet/capabilities | device_telnet_capabilities | dict | 200 | nein | moeglich | nein | ja | kein statischer Treffer |
| webgui | GET | /api/devices/{device_id}/telnet/jobs/{job_id} | device_telnet_job | dict | 200,404 | nein | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/telnet/plan | device_telnet_plan | dict | 200,400 | ja | moeglich | nein | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/telnet/reboot | device_telnet_reboot_job | dict | 200,409 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | GET | /api/devices/{device_id}/wireless-profiles | wireless_profiles | dict | 200 | nein | moeglich | nein | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/wireless-profiles | add_wireless_profile | dict | 200,400,409 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | POST | /api/devices/{device_id}/zone/status | zone_status_probe | dict | 200 | ja | moeglich | nein | ja | kein statischer Treffer |
| webgui | GET | /api/diagnostics/emulation-gaps | diagnostics_emulation_gaps | dict | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| webgui | GET | /api/diagnostics/support-bundle | diagnostics_support_bundle | StreamingResponse | 200,404 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/diagnostics/system/export | diagnostics_system_export | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/diagnostics/system/preview | diagnostics_system_preview | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/diagnostics/telemetry/export | diagnostics_telemetry_export | Response | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| webgui | GET | /api/diagnostics/telemetry/report | diagnostics_telemetry_report | HTMLResponse | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| webgui | GET | /api/diagnostics/telemetry/summary | diagnostics_telemetry_summary | dict | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| webgui | POST | /api/discovery/ssdp | discovery_ssdp | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | GET | /api/discovery/test | discovery_test | dict | 200 | nein | nein | moeglich | nein | tests/test_setup_rebuild_160.py, tests/test_setup_rebuild_browser_160.py |
| webgui | GET | /api/display/metadata-modes | display_metadata_modes | list[dict] | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| webgui | POST | /api/dlna/discover | dlna_discover | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/dlna/status | get_dlna_status | dict | 200 | nein | nein | nein | nein | tests/test_local_test_expansion.py |
| webgui | GET | /api/events | get_events | dict | 200 | nein | nein | nein | nein | tests/test_local_test_expansion.py |
| webgui | POST | /api/events/test | create_test_event | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | GET | /api/features/docs/{document_id} | feature_documentation | Response | 200,404 | nein | nein | nein | nein | tests/test_phase2_activation.py, tests/test_release_candidate.py |
| webgui | GET | /api/features/status | feature_status | dict | 200 | nein | nein | nein | nein | tests/test_phase2_activation.py |
| webgui | GET | /api/health | health | dict[str, str | bool] | 200 | nein | nein | nein | nein | tests/test_contract_quality_audit_160.py, tests/test_database_package.py, tests/test_high_impact_architecture.py, tests/test_human_ui_160.py, tests/test_local_test_expansion.py, tests/test_maintenance_reboot.py, tests/test_masterlog_setup_mode.py, tests/test_mobile_release.py, tests/test_phase3b_protected_access.py, tests/test_phase4a_contracts.py, tests/test_playback_keepalive_research_160.py, tests/test_protected_webui_200.py, tests/test_release_candidate.py, tests/test_release_hardening.py, tests/test_release_polish.py, tests/test_research_contracts_160.py, tests/test_research_domain_persistence.py, tests/test_research_runtime_integration_160.py, tests/test_research_state_api_160.py, tests/test_setup_rebuild_browser_160.py, tests/test_setup_wizard_playwright.py, tests/test_synthetic_24h_simulation.py, tests/test_version_metadata.py, tests/test_webui_smoke.py |
| webgui | POST | /api/health/center | run_health_center | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | GET | /api/health/center/latest | health_center_latest | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/keys | key_commands | list[dict] | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| webgui | GET | /api/lab/port-probe | lab_port_probe | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/lab/status | get_lab_status | dict | 200 | nein | nein | nein | nein | tests/test_local_test_expansion.py |
| webgui | GET | /api/languages | languages | list[dict] | 200 | nein | nein | nein | nein | tests/test_high_impact_architecture.py, tests/test_mobile_release.py, tests/test_release_polish.py, tests/test_webui_smoke.py |
| webgui | GET | /api/maintenance/cleanup-preview | cleanup_preview | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/maintenance/cleanup/dry-run | maintenance_cleanup_dry_run | dict | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| webgui | POST | /api/maintenance/cleanup/run | maintenance_cleanup_run | dict | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| webgui | POST | /api/maintenance/clear-logs | clear_logs | dict | 200,409 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | POST | /api/maintenance/clear-test-devices | clear_test_devices | dict | 200,409 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | POST | /api/maintenance/reconcile-devices | reconcile_devices | dict | 200,409 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | GET | /api/maintenance/storage | maintenance_storage | dict | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| webgui | GET | /api/media-library/capabilities | media_library_capabilities | dict | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| webgui | DELETE | /api/media-playlists | clear_media_playlists | dict | 200 | ja | nein | moeglich | nein | tests/test_webui_smoke.py |
| webgui | GET | /api/media-playlists | media_playlists | list[dict] | 200 | nein | nein | nein | nein | tests/test_database_package.py, tests/test_webui_smoke.py |
| webgui | POST | /api/media-playlists | save_media_playlist | dict | 200,400 | ja | nein | moeglich | nein | tests/test_webui_smoke.py |
| webgui | GET | /api/media-types | media_types | list[dict] | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| webgui | POST | /api/media/library/roots | media_root_create | dict | 200,400 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | POST | /api/media/library/roots/{root_id}/scan | media_root_scan | dict | 200,400 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | GET | /api/media/library/search | media_search | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/media/library/status | media_library_status | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/media/library/validate-root | media_root_validate | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/multiroom/clear | multiroom_clear | dict | 200,404 | ja | moeglich | moeglich | ja | tests/test_multiroom_router.py, tests/test_webui_smoke.py |
| webgui | POST | /api/multiroom/clear-all | multiroom_clear_all | dict | 200 | ja | moeglich | nein | ja | kein statischer Treffer |
| webgui | POST | /api/multiroom/latency | multiroom_latency | dict | 200,400 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | GET | /api/multiroom/methods | multiroom_methods | list[dict] | 200 | nein | nein | nein | nein | tests/test_play_history_robustness.py |
| webgui | POST | /api/multiroom/preview | multiroom_preview | dict | 200,400,404 | ja | nein | nein | ja | tests/test_multiroom_router.py, tests/test_phase2_activation.py, tests/test_safety_fixes_160.py, tests/test_webui_smoke.py |
| webgui | GET | /api/multiroom/recent-stations | multiroom_recent_stations | list[dict] | 200 | nein | nein | nein | nein | tests/test_play_history_robustness.py |
| webgui | POST | /api/multiroom/remove-device | multiroom_remove_device | dict | 200,409,502,503 | ja | moeglich | moeglich | ja | tests/test_multiroom_router.py |
| webgui | GET | /api/multiroom/scenarios | multiroom_scenarios | list[dict] | 200 | nein | nein | nein | nein | tests/test_database_package.py, tests/test_final_hardware_gate.py, tests/test_multiroom_router.py, tests/test_webui_smoke.py |
| webgui | POST | /api/multiroom/scenarios | save_multiroom_scenario | dict | 200,400,404 | ja | nein | moeglich | nein | tests/test_final_hardware_gate.py, tests/test_multiroom_router.py, tests/test_webui_smoke.py |
| webgui | GET | /api/multiroom/scenarios-safe | multiroom_scenarios_safe | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/multiroom/scenarios-safe | multiroom_scenario_safe_create | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | DELETE | /api/multiroom/scenarios/{scenario_id} | delete_multiroom_scenario | dict | 200,404 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | POST | /api/multiroom/scenarios/{scenario_id}/activate | activate_multiroom_scenario | dict | 200,404 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/multiroom/scenarios/{scenario_id}/preview | preview_multiroom_scenario | dict | 200,404 | ja | nein | nein | ja | kein statischer Treffer |
| webgui | POST | /api/multiroom/set | multiroom_set | dict | 200,400,404,502 | ja | moeglich | moeglich | ja | tests/test_final_hardware_gate.py, tests/test_multiroom_router.py, tests/test_safety_fixes_160.py, tests/test_webui_smoke.py |
| webgui | GET | /api/multiroom/status/{device_id} | multiroom_status | dict | 200,404 | nein | moeglich | nein | ja | tests/test_safety_fixes_160.py |
| webgui | POST | /api/offline/preflight | offline_preflight | dict | 200,404 | ja | nein | nein | nein | tests/test_outbound_protected_targets_160.py, tests/test_phase2_activation.py |
| webgui | GET | /api/offline/status | get_offline_status | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/play-history | play_history | list[dict] | 200 | nein | nein | nein | nein | tests/test_confirmed_playback_state.py, tests/test_database_migrations.py, tests/test_database_package.py, tests/test_play_history_robustness.py, tests/test_release_polish.py, tests/test_webui_smoke.py |
| webgui | POST | /api/play-history/event | record_play_history_event | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | POST | /api/play-history/start | start_play_history | dict | 200,400 | ja | nein | moeglich | nein | tests/test_play_history_robustness.py |
| webgui | POST | /api/play-history/{history_id}/stop | stop_play_history | dict | 200,404 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | GET | /api/preset-profiles | preset_profiles | list[dict] | 200 | nein | nein | nein | nein | tests/test_database_package.py, tests/test_webui_smoke.py |
| webgui | POST | /api/preset-profiles | create_preset_profile | dict | 200,400 | ja | nein | moeglich | nein | tests/test_webui_smoke.py |
| webgui | POST | /api/preset-profiles/{profile_id}/apply/{device_id} | apply_preset_profile | dict | 200,404 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | POST | /api/presets/clone | clone_presets | dict | 200,400,404 | ja | nein | moeglich | nein | tests/test_webui_smoke.py |
| webgui | GET | /api/presets/{device_id} | get_presets | list[dict] | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/presets/{device_id}/plan | preset_plan | dict | 200,400 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/presets/{device_id}/status | preset_status | dict | 200 | nein | moeglich | moeglich | ja | tests/test_preset_cloud.py |
| webgui | POST | /api/presets/{device_id}/sync | sync_local_presets | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | DELETE | /api/presets/{device_id}/{button} | delete_preset | dict | 200,404,502 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | POST | /api/presets/{device_id}/{button} | set_preset | dict | 200,400,404,502 | ja | moeglich | moeglich | ja | tests/test_preset_cloud.py |
| webgui | GET | /api/quick-fixes | quick_fixes | dict | 200 | nein | nein | nein | nein | tests/test_local_test_expansion.py |
| webgui | POST | /api/quick-fixes/{quick_fix_id}/execute | quick_fix_execute | dict | 200,400,409 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | POST | /api/quick-fixes/{quick_fix_id}/preview | quick_fix_preview | dict | 200,400 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/readiness | readiness | dict[str, str | bool | dict] | 200 | nein | nein | nein | nein | tests/test_contract_quality_audit_160.py, tests/test_database_package.py, tests/test_phase4b_hardening.py, tests/test_release_candidate.py, tests/test_release_polish.py, tests/test_research_contracts_160.py, tests/test_research_domain_persistence.py, tests/test_research_runtime_integration_160.py, tests/test_research_state_api_160.py, tests/test_setup_batch_jobs.py, tests/test_st20_readiness_retry.py |
| webgui | GET | /api/reference-setups | reference_setups | list[dict] | 200 | nein | nein | nein | nein | tests/test_database_package.py, tests/test_webui_smoke.py |
| webgui | POST | /api/reference-setups/from-device/{device_id} | create_reference_setup | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | POST | /api/reference-setups/{setup_id}/apply/{device_id} | apply_reference_setup | dict | 200,404 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/restore/prepare | restore_prepare | dict | 200,400 | ja | nein | moeglich | nein | tests/test_system_backup.py |
| webgui | POST | /api/restore/preview | restore_preview | dict | 200,400 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | DELETE | /api/schedules | clear_schedules | dict | 200 | ja | nein | moeglich | nein | tests/test_final_hardware_gate.py, tests/test_multiroom_router.py, tests/test_play_history_robustness.py, tests/test_webui_smoke.py |
| webgui | GET | /api/schedules | schedules | list[dict] | 200 | nein | nein | nein | nein | tests/test_final_hardware_gate.py, tests/test_human_ui_160.py, tests/test_multiroom_router.py, tests/test_play_history_robustness.py, tests/test_webui_smoke.py |
| webgui | POST | /api/schedules | create_schedule | dict | 200 | ja | nein | moeglich | nein | tests/test_final_hardware_gate.py, tests/test_multiroom_router.py, tests/test_play_history_robustness.py, tests/test_webui_smoke.py |
| webgui | DELETE | /api/schedules/{schedule_id} | delete_schedule | dict | 200 | ja | nein | moeglich | nein | tests/test_multiroom_router.py |
| webgui | POST | /api/schedules/{schedule_id} | update_schedule | dict | 200 | ja | nein | moeglich | nein | tests/test_multiroom_router.py |
| webgui | POST | /api/schedules/{schedule_id}/enable | enable_schedule | dict | 200 | ja | nein | moeglich | nein | tests/test_multiroom_router.py |
| webgui | POST | /api/schedules/{schedule_id}/trigger | trigger_schedule_now | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/services/catalog | services_catalog | list[dict] | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| webgui | GET | /api/settings/catalog | settings_catalog | list[dict] | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| webgui | GET | /api/setup-jobs | setup_jobs | dict | 200 | nein | nein | nein | nein | tests/test_database_package.py, tests/test_local_test_expansion.py, tests/test_release_hardening.py |
| webgui | POST | /api/setup-jobs | setup_job_create | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | POST | /api/setup-jobs/{job_id}/status | setup_job_status | dict | 200,400 | ja | nein | moeglich | nein | tests/test_local_test_expansion.py |
| webgui | POST | /api/setup/account/{device_id} | setup_local_account | dict | 200,400,409 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/setup/activation-playback/{device_id} | retry_setup_activation_playback | dict | 200,502 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/setup/cloud-route/{device_id} | setup_cloud_route | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/setup/cloud-route/{device_id}/apply | apply_setup_cloud_route | dict | 200,400,409,502 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | POST | /api/setup/cloud-route/{device_id}/rollback | rollback_setup_cloud_route | dict | 200,404,409 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | POST | /api/setup/cloud-route/{device_id}/verify | verify_setup_cloud_route | dict | 200 | ja | moeglich | nein | ja | kein statischer Treffer |
| webgui | GET | /api/setup/devices | setup_devices | list[dict] | 200 | nein | nein | nein | nein | tests/test_phase4b_hardening.py, tests/test_setup_batch_jobs.py |
| webgui | GET | /api/setup/jobs/latest | latest_setup_job | dict | 200,404 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| webgui | POST | /api/setup/jobs/start | start_setup_job | dict | 200,400 | ja | nein | nein | nein | tests/test_setup_batch_jobs.py |
| webgui | GET | /api/setup/jobs/{job_id} | get_setup_job | dict | 200,404 | nein | nein | nein | nein | tests/test_setup_batch_jobs.py |
| webgui | POST | /api/setup/jobs/{job_id}/cancel | cancel_setup_job | dict | 200,404 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/setup/plans/{device_id} | setup_plan | dict | 200 | nein | nein | nein | nein | tests/test_database_package.py |
| webgui | POST | /api/setup/plans/{device_id} | save_setup_plan | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | POST | /api/setup/wizard/apply/{device_id} | setup_wizard_apply | dict | 200,409 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/setup/wizard/preflight/{device_id} | setup_wizard_preflight | dict | 200 | ja | moeglich | moeglich | ja | kein statischer Treffer |
| webgui | GET | /api/setup/wizard/server-info | setup_wizard_server_info | dict | 200 | nein | moeglich | nein | ja | tests/test_recovery_profiles_setup.py, tests/test_webui_smoke.py |
| webgui | POST | /api/setup/wizard/service-status | record_browser_service_status | dict | 200,400 | ja | nein | nein | nein | tests/test_recovery_profiles_setup.py |
| webgui | GET | /api/ssh/remote-services-file | download_remote_services_file | Response | 200 | nein | moeglich | nein | ja | tests/test_ssh_remote_services.py |
| webgui | GET | /api/stations | stations | list[dict] | 200 | nein | nein | nein | nein | tests/test_artwork_webui_160.py, tests/test_database_package.py, tests/test_device_settings.py, tests/test_high_impact_architecture.py, tests/test_human_ui_160.py, tests/test_internal_activation_streams.py, tests/test_multiroom_router.py, tests/test_offline_mode.py, tests/test_phase2_activation.py, tests/test_phase3_soundbig_validation.py, tests/test_phase3b_notification_order.py, tests/test_phase3b_preset_integrity.py, tests/test_phase4b_hardening.py, tests/test_play_history_robustness.py, tests/test_preset_cloud.py, tests/test_preset_transactions_200.py, tests/test_webui_smoke.py |
| webgui | POST | /api/stations | add_station | dict | 200,400 | ja | nein | moeglich | nein | tests/test_artwork_webui_160.py, tests/test_device_settings.py, tests/test_human_ui_160.py, tests/test_internal_activation_streams.py, tests/test_multiroom_router.py, tests/test_offline_mode.py, tests/test_phase4b_hardening.py, tests/test_preset_cloud.py, tests/test_webui_smoke.py |
| webgui | GET | /api/stations/search-online | search_online_stations | list[dict] | 200,409,503 | nein | moeglich | nein | ja | tests/test_offline_mode.py |
| webgui | POST | /api/stations/upload | upload_station_file | dict | 200,400,413 | ja | nein | moeglich | nein | tests/test_phase4b_hardening.py |
| webgui | GET | /api/stations/{station_id}/logo-status | station_logo_status | dict | 200,404 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/stats/playback | playback_stats | dict | 200 | nein | nein | nein | nein | tests/test_multiroom_router.py, tests/test_play_history_robustness.py, tests/test_stats_internal_playback_exclusion.py, tests/test_webui_smoke.py |
| webgui | GET | /api/stereo-pairing/research | stereo_pairing_research | dict | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| webgui | GET | /api/support-bundle | global_support_bundle | StreamingResponse | 200,413 | nein | nein | nein | nein | tests/test_release_hardening.py |
| webgui | GET | /api/system/healthcheck | system_healthcheck | dict | 200 | nein | nein | nein | nein | tests/test_release_hardening.py, tests/test_release_polish.py |
| webgui | GET | /api/system/service-health | system_service_health | dict | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| webgui | GET | /api/system/settings | system_settings | dict | 200 | nein | nein | nein | ja | tests/test_final_hardware_gate.py, tests/test_high_impact_architecture.py, tests/test_mobile_release.py, tests/test_setup_wizard_playwright.py, tests/test_version_metadata.py, tests/test_webui_smoke.py |
| webgui | POST | /api/system/settings | save_system_settings | dict | 200,400 | ja | nein | moeglich | ja | tests/test_final_hardware_gate.py, tests/test_high_impact_architecture.py, tests/test_mobile_release.py, tests/test_setup_wizard_playwright.py, tests/test_version_metadata.py, tests/test_webui_smoke.py |
| webgui | POST | /api/system/warnings/ack | acknowledge_first_run_warning | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | GET | /api/telemetry | telemetry | list[dict] | 200 | nein | nein | nein | nein | tests/test_database_package.py, tests/test_human_ui_160.py, tests/test_mobile_release.py, tests/test_playback_keepalive_research_160.py, tests/test_release_polish.py, tests/test_webui_smoke.py |
| webgui | POST | /api/telemetry | ingest_telemetry | dict | 200 | ja | nein | moeglich | nein | tests/test_webui_smoke.py |
| webgui | GET | /api/telemetry/summary | telemetry_summary | dict | 200 | nein | nein | nein | nein | tests/test_webui_smoke.py |
| webgui | GET | /api/telnet/commands | telnet_commands | list[dict] | 200 | nein | moeglich | nein | ja | tests/test_webui_smoke.py |
| webgui | POST | /api/update/check | run_update_check | dict | 200 | ja | nein | nein | nein | tests/test_offline_mode.py, tests/test_update_check.py |
| webgui | GET | /api/update/status | update_status | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /api/updates/local/prepare | update_local_prepare | dict | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | POST | /api/updates/local/preview | update_local_preview | dict | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /api/version | api_version | dict[str, str] | 200 | nein | nein | nein | nein | tests/test_phase4b_hardening.py, tests/test_release_candidate.py, tests/test_release_polish.py |
| webgui | GET | /api/webhooks | list_webhooks | dict | 200 | nein | nein | nein | nein | tests/test_local_test_expansion.py |
| webgui | POST | /api/webhooks | create_webhook | dict | 200,400 | ja | nein | moeglich | nein | tests/test_local_test_expansion.py |
| webgui | GET | /api/webhooks/validate | webhook_validate | dict | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | DELETE | /api/webhooks/{endpoint_id} | delete_webhook | dict | 200,404 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | PUT | /api/webhooks/{endpoint_id} | update_webhook | dict | 200,400 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | POST | /api/webhooks/{endpoint_id}/test | test_webhook | dict | 200,404 | ja | nein | moeglich | nein | tests/test_local_test_expansion.py |
| webgui | GET | /bmx/orion/now-playing | now_playing | handler-defined JSON | 200 | nein | moeglich | nein | ja | tests/test_connectivity_recovery_250.py, tests/test_device_settings.py, tests/test_high_impact_architecture.py, tests/test_keepalive_circuit_breaker.py, tests/test_live_device_tools.py, tests/test_multiroom_router.py, tests/test_phase3_soundbig_validation.py, tests/test_phase3b_protected_access.py, tests/test_play_history_robustness.py, tests/test_playback_keepalive_research_160.py, tests/test_portable_safe_mode.py, tests/test_preset_cloud.py, tests/test_release_polish.py, tests/test_research_contracts_160.py, tests/test_research_runtime_integration_160.py, tests/test_research_state_api_160.py, tests/test_safety_fixes_160.py, tests/test_st20_readiness_retry.py |
| webgui | GET | /bmx/orion/now-playing/station/{station_id} | now_playing | handler-defined JSON | 200 | nein | moeglich | nein | ja | tests/test_connectivity_recovery_250.py, tests/test_device_settings.py, tests/test_high_impact_architecture.py, tests/test_keepalive_circuit_breaker.py, tests/test_live_device_tools.py, tests/test_multiroom_router.py, tests/test_phase3_soundbig_validation.py, tests/test_phase3b_protected_access.py, tests/test_play_history_robustness.py, tests/test_playback_keepalive_research_160.py, tests/test_portable_safe_mode.py, tests/test_preset_cloud.py, tests/test_release_polish.py, tests/test_research_contracts_160.py, tests/test_research_runtime_integration_160.py, tests/test_research_state_api_160.py, tests/test_safety_fixes_160.py, tests/test_st20_readiness_retry.py |
| webgui | POST | /bmx/orion/reporting | reporting | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_database_package.py, tests/test_human_ui_160.py, tests/test_playback_keepalive_research_160.py, tests/test_preset_cloud.py, tests/test_release_polish.py, tests/test_research_contracts_160.py, tests/test_research_domain_persistence.py, tests/test_research_runtime_integration_160.py, tests/test_research_state_api_160.py, tests/test_safety_fixes_160.py, tests/test_synthetic_24h_simulation.py |
| webgui | POST | /bmx/orion/reporting/station/{station_id} | reporting | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_database_package.py, tests/test_human_ui_160.py, tests/test_playback_keepalive_research_160.py, tests/test_preset_cloud.py, tests/test_release_polish.py, tests/test_research_contracts_160.py, tests/test_research_domain_persistence.py, tests/test_research_runtime_integration_160.py, tests/test_research_state_api_160.py, tests/test_safety_fixes_160.py, tests/test_synthetic_24h_simulation.py |
| webgui | GET | /bmx/radiobrowser/v1/now-playing/station/{uuid} | bmx_radiobrowser_station | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /bmx/radiobrowser/v1/playback/station/{uuid} | bmx_radiobrowser_station | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /bmx/radiobrowser/v1/reporting/station/{uuid} | bmx_radiobrowser_station | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /bmx/registry/servicesAvailability | bmx_services_availability | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /bmx/registry/v1/introspect | provider_discovery | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_high_impact_architecture.py |
| webgui | GET | /bmx/registry/v1/services | bmx_registry | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_config_rewrite.py, tests/test_high_impact_architecture.py, tests/test_preset_cloud.py, tests/test_recovery_profiles_setup.py, tests/test_setup_batch_jobs.py |
| webgui | GET | /bmx/registry/v1/servicesAvailability | bmx_services_availability | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_high_impact_architecture.py |
| webgui | GET | /bmx/resolve | bmx_resolve | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_preset_cloud.py |
| webgui | POST | /bmx/resolve | bmx_resolve | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_preset_cloud.py |
| webgui | GET | /bmx/tunein/v1/favorite/{station_id} | bmx_tunein_station | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /bmx/tunein/v1/favorite/{station_id} | bmx_tunein_station | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /bmx/tunein/v1/now-playing/station/{station_id} | bmx_tunein_station | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /bmx/tunein/v1/playback/station/{station_id} | bmx_tunein_station | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /bmx/tunein/v1/reporting/station/{station_id} | bmx_tunein_station | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /core02/svc-bmx-adapter-orion/prod/orion/station | orion_station | handler-defined JSON | 200 | nein | moeglich | nein | ja | tests/test_orion.py, tests/test_phase3b_preset_integrity.py, tests/test_preset_cloud.py, tests/test_research_state_api_160.py |
| webgui | POST | /core02/svc-bmx-adapter-orion/prod/orion/token | orion_token | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /getServiceSettings | service_settings | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /getServiceSettings | service_settings | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /group | group_state | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| webgui | POST | /group/create | group_create | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| webgui | POST | /group/delete | group_delete | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| webgui | POST | /group/update | group_update | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| webgui | GET | /remote/{device_id} | remote | HTMLResponse | 200 | nein | moeglich | nein | ja | tests/test_artwork_cache_160.py, tests/test_connectivity_recovery_250.py, tests/test_local_test_expansion.py, tests/test_outbound_protected_targets_160.py, tests/test_phase3b_protected_access.py, tests/test_recovery_profiles_setup.py, tests/test_safety_fixes_160.py, tests/test_ssh_preflight.py, tests/test_ssh_remote_services.py, tests/test_version_metadata.py, tests/test_webui_smoke.py |
| webgui | GET | /serviceSettings | service_settings | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| webgui | POST | /serviceSettings | service_settings | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| webgui | POST | /setMusicServiceAccount | set_music_service_account | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| webgui | POST | /setMusicServiceOAuthAccount | set_music_service_oauth_account | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| webgui | GET | /stationInfo | station_info | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_release_polish.py |
| webgui | POST | /streaming/account/{account_id}/device/ | streaming_add_device | handler-defined JSON | 201 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | DELETE | /streaming/account/{account_id}/device/{device_id} | streaming_delete_device | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /streaming/account/{account_id}/device/{device_id} | streaming_get_device | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | PUT | /streaming/account/{account_id}/device/{device_id} | streaming_put_device | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /streaming/account/{account_id}/device/{device_id}/heartbeat | streaming_device_keepalive | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /streaming/account/{account_id}/device/{device_id}/keepalive | streaming_device_keepalive | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | PUT | /streaming/account/{account_id}/device/{device_id}/preset/{button} | put_preset | handler-defined JSON | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | GET | /streaming/account/{account_id}/device/{device_id}/presets | streaming_device_presets | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /streaming/account/{account_id}/device/{device_id}/presets/{button} | put_preset | handler-defined JSON | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | PUT | /streaming/account/{account_id}/device/{device_id}/presets/{button} | put_preset | handler-defined JSON | 200 | ja | nein | moeglich | nein | kein statischer Treffer |
| webgui | POST | /streaming/account/{account_id}/device/{device_id}/recent | streaming_device_recent | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /streaming/account/{account_id}/device/{device_id}/recents | streaming_device_recents | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /streaming/account/{account_id}/full | streaming_account_full | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /streaming/account/{account_id}/presets/all | streaming_account_presets | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /streaming/account/{account_id}/provider_settings | streaming_provider_settings | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /streaming/account/{account_id}/sources | streaming_account_sources | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /streaming/device/{device_id}/streaming_token | streaming_token | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_preset_cloud.py |
| webgui | GET | /streaming/provider-discovery | provider_discovery | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /streaming/sourceproviders | streaming_sourceproviders | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_preset_cloud.py, tests/test_release_polish.py |
| webgui | POST | /streaming/support/power_on | streaming_power_on | handler-defined JSON | 200 | ja | nein | nein | nein | tests/test_release_polish.py |
| webgui | GET | /v1/blacklist/{device_id} | device_blacklist | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | POST | /v1/blacklist/{device_id} | device_blacklist | handler-defined JSON | 200 | ja | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /v1/systems/devices/{device_id} | marge_full | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |
| webgui | GET | /v1/systems/devices/{device_id}/presets | marge_presets | handler-defined JSON | 200 | nein | nein | nein | nein | tests/test_preset_cloud.py |
| webgui | GET | /v1/systems/devices/{device_id}/sources | marge_sources | handler-defined JSON | 200 | nein | nein | nein | nein | kein statischer Treffer |

## Sonderfaelle

- Der Cloud-Catch-all /{path:path} ist kompatibilitaetsorientiert und
  kann unbekannte Pfade mit 200/204 beantworten.
- /serviceSettings und /getServiceSettings sind bestehende Aliasse.
- POST /api/devices/{device_id}/telnet/reboot ist aktuell doppelt in
  api.py und fulltest.py montiert; dies ist als Uebergangspfad markiert.
- Die optionale HTTPS-App nutzt dieselbe WebGUI-Routenmenge und ist
  nur bei BASSWIESN_ENABLE_HTTPS=true aktiv.
- Die Phase 4A implementiert keine Authentifizierung.
