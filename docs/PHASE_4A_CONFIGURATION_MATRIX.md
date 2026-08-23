# Phase 4A Konfigurationsmatrix

Statisch aus config.py, den Python-Quellen und .env.example erzeugt.
Environment-Settings werden ueber die gecachte get_settings()-Instanz
gelesen; fuer eine sichere Wirksamkeit ist ein Prozessneustart anzunehmen.

| Variable | Typ | Standardwert | Erforderlich | Dienst | Sicherheit | .env | Neustart | Erlaubte Werte | Hinweis |
|---|---|---|---:|---|---|---:|---:|---|---|
| BASSWIESN_BACKUP_RETENTION_COUNT | int | 10 | nein | Persistence/Diagnostics | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_CERT_DAYS | int | 3650 | nein | WebGUI/HTTPS | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_CERT_MODE | string | "selfsigned" | nein | WebGUI/HTTPS | niedrig | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_CONFIG_BACKUP_RETENTION_COUNT | int | 100 | nein | Persistence/Diagnostics | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_DEBUG_BASE_URL | string | "f'http://{lan_host}:1860' if lan_host else ''" | nein | Global Runtime | niedrig | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_DEVICE_INTERACTION_MAX_CONCURRENCY | int | 4 | nein | WebGUI/Device Runtime | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_DIAGNOSTIC_MAX_SIZE_MB | int | 50 | nein | Persistence/Diagnostics | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_DISABLE_SETUP_CONFIRMATIONS | bool | false | nein | Global Runtime | niedrig | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_ENABLE_HTTPS | bool | false | nein | WebGUI/HTTPS | mittel | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_EVENT_RETENTION_DAYS | int | 30 | nein | Persistence/Diagnostics | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_EXPERIMENTAL_ANNOUNCEMENTS | bool | false | nein | Experimental/LAB | niedrig | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_EXPERIMENTAL_DLNA | bool | false | nein | Experimental/LAB | niedrig | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_HTTPS_PORT | int | 1329 | nein | WebGUI/HTTPS | mittel | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_INTERACTION_RETENTION_DAYS | int | 14 | nein | Persistence/Diagnostics | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_IP_SCAN_FALLBACK | bool | true | nein | Global Runtime | niedrig | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_LAB_MODE | bool | false | nein | Global Runtime | niedrig | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_LAN_HOST | string | "" | nein | Global Runtime | niedrig | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_LAN_HOST_CANDIDATES | string | "" | nein | Global Runtime | niedrig | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_LOCAL_BASE_URL | string | "" | nein | Global Runtime | niedrig | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_MAINTENANCE_REBOOT_DEFAULT_INTERVAL_HOURS | int | 24 | nein | Global Runtime | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_MAINTENANCE_REBOOT_ENABLED_BY_DEFAULT | bool | false | nein | Global Runtime | niedrig | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_MAINTENANCE_REBOOT_MAX_INTERVAL_HOURS | int | 168 | nein | Global Runtime | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_MAINTENANCE_REBOOT_MIN_INTERVAL_HOURS | int | 6 | nein | Global Runtime | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_MAINTENANCE_REBOOT_RETURN_TIMEOUT_SECONDS | int | 600 | nein | Global Runtime | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_MAINTENANCE_REBOOT_SCHEDULER_ENABLED | bool | false | nein | Global Runtime | niedrig | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_MAINTENANCE_REBOOT_SCHEDULER_INTERVAL_SECONDS | int | 300 | nein | Global Runtime | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_MARGE_AUTH_TOKEN_FILE | string | "" | nein | Global Runtime | hoch | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_MASTERLOG_BACKUP_COUNT | int | 5 | nein | Persistence/Diagnostics | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_MASTERLOG_ENABLED | bool | true | nein | Persistence/Diagnostics | niedrig | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_MASTERLOG_MAX_MB | int | 50 | nein | Persistence/Diagnostics | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_MEDIA_ENABLED | bool | false | nein | Experimental/LAB | niedrig | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_MEDIA_MAX_FILE_SIZE_MB | int | 500 | nein | Experimental/LAB | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_MEDIA_ROOTS | csv | "" | nein | Experimental/LAB | niedrig | ja | ja | Kommagetrennte Werte |  |
| BASSWIESN_OFFLINE_ALLOWED_STREAM_HOSTS | csv | "" | nein | WebGUI/External Services | mittel | ja | ja | Kommagetrennte Werte |  |
| BASSWIESN_OFFLINE_MODE | enum | "auto" | nein | WebGUI/External Services | mittel | ja | ja | off | auto | strict |  |
| BASSWIESN_PLAYBACK_KEEPALIVE_ENABLED | bool | true | nein | WebGUI/Device Runtime | niedrig | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_PLAYBACK_KEEPALIVE_INTERVAL_SECONDS | int | 300 | nein | WebGUI/Device Runtime | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_PLAYBACK_KEEPALIVE_LOG_EVERY_SECONDS | int | 1800 | nein | WebGUI/Device Runtime | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_PLAYBACK_STATE_STALE_AFTER_SECONDS | int | 360 | nein | WebGUI/Device Runtime | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_PORTABLE_SAFE_LOW_RISK_INTERVAL_SECONDS | int | 3600 | nein | Global Runtime | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_PROTECTED_DEVICE_IDS | csv | "" | nein | WebGUI/Device Runtime | mittel | ja | ja | Kommagetrennte Werte |  |
| BASSWIESN_PROTECTED_DEVICE_IPS | csv | "" | nein | WebGUI/Device Runtime | mittel | ja | ja | Kommagetrennte Werte | Alias/Quelle fuer geschuetzte Geraete; fail-closed relevant |
| BASSWIESN_RELEASE_MANIFEST_REQUIRED | bool | false | nein | Global Runtime | niedrig | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_REQUEST_LOG_RETENTION_DAYS | int | 14 | nein | Persistence/Diagnostics | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_RETENTION_DAYS | int | 30 | nein | Persistence/Diagnostics | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_RUNTIME_USER | string | "" | nein | Global Runtime | niedrig | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_SETUP_WRITE_RADIO_IPS | csv | "" | nein | Global Runtime | niedrig | ja | ja | Kommagetrennte Werte |  |
| BASSWIESN_SSDP_ENABLED | bool | true | nein | WebGUI/Device Runtime | niedrig | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_SSDP_INTERVAL_SECONDS | int | 300 | nein | WebGUI/Device Runtime | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_SSDP_TIMEOUT_SECONDS | int | 4 | nein | WebGUI/Device Runtime | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_SSH_ALLOWED_DEVICE_IDS | csv | "" | nein | WebGUI/Device Runtime | mittel | nein | ja | Kommagetrennte Werte |  |
| BASSWIESN_SSH_HOST_KEY_POLICY | string | "strict" | nein | LAB/Recovery | hoch | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_SSH_KNOWN_HOSTS_FILE | string | "" | nein | LAB/Recovery | mittel | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_SSH_PASSWORD_FILE | string | "" | nein | LAB/Recovery | hoch | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_SSH_PORT | int | 22 | nein | LAB/Recovery | mittel | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_SSH_PRIVATE_KEY_FILE | string | "" | nein | LAB/Recovery | hoch | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_SSH_RETRY_COUNT | int | 2 | nein | LAB/Recovery | mittel | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_SSH_TIMEOUT_SECONDS | int | 8 | nein | LAB/Recovery | mittel | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_SSH_USERNAME | string | "" | nein | LAB/Recovery | mittel | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_STANDBY_CLOCK_RECOVERY_ENABLED | bool | false | nein | LAB/Recovery | niedrig | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_STATIONARY_LOW_RISK_INTERVAL_SECONDS | int | 300 | nein | Global Runtime | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_STATION_UPLOAD_MAX_MB | int | 50 | nein | Global Runtime | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_STATION_UPLOAD_QUOTA_MB | int | 500 | nein | Global Runtime | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_SUPPORT_BUNDLE_MAX_MB | int | 50 | nein | Global Runtime | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_TELEMETRY_RETENTION_DAYS | int | 30 | nein | Persistence/Diagnostics | niedrig | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_TELNET_ALLOWED_DEVICE_IDS | csv | "" | nein | WebGUI/Device Runtime | mittel | ja | ja | Kommagetrennte Werte |  |
| BASSWIESN_TELNET_ENABLED | bool | false | nein | LAB/Recovery | mittel | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_TELNET_PASSWORD_FILE | string | "" | nein | LAB/Recovery | hoch | ja | ja | freier Text gemaess Code | Secret-Datei; Inhalt nie in UI/Reports ausgeben |
| BASSWIESN_TELNET_PORT | int | 23 | nein | LAB/Recovery | mittel | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_TELNET_REBOOT_WAIT_SECONDS | int | 180 | nein | LAB/Recovery | mittel | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_TELNET_TIMEOUT_SECONDS | int | 8 | nein | LAB/Recovery | mittel | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_TELNET_USERNAME | string | "" | nein | LAB/Recovery | mittel | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_TEST_MODE | bool | false | nein | Global Runtime | niedrig | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_TLS_CERT_FILE | string | "" | nein | WebGUI/HTTPS | mittel | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_TLS_KEY_FILE | string | "" | nein | WebGUI/HTTPS | hoch | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_UPDATE_ALLOW_LOCAL_ARCHIVE | bool | true | nein | WebGUI/External Services | mittel | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_UPDATE_CHANNEL | enum | "manual" | nein | WebGUI/External Services | mittel | ja | ja | manual | stable | beta |  |
| BASSWIESN_UPDATE_CHECK_ENABLED | bool | false | nein | WebGUI/External Services | mittel | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_UPDATE_MANIFEST_URL | string | "" | nein | WebGUI/External Services | mittel | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_UPDATE_REPO_URL | string | "" | nein | WebGUI/External Services | mittel | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_VERSION | string | "__version__" | nein | Global Runtime | niedrig | ja | ja | freier Text gemaess Code |  |
| BASSWIESN_WEBHOOKS_ENABLED | bool | false | nein | WebGUI/External Services | mittel | ja | ja | true/false, 1/0, yes/no, on/off |  |
| BASSWIESN_WEBHOOK_ALLOWED_HOSTS | csv | "" | nein | WebGUI/External Services | mittel | ja | ja | Kommagetrennte Werte |  |
| BASSWIESN_WEBHOOK_MAX_RETRIES | int | 5 | nein | WebGUI/External Services | mittel | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_WEBHOOK_TIMEOUT_SECONDS | int | 5 | nein | WebGUI/External Services | mittel | ja | ja | Ganzzahl; _env_int verwendet Mindestwert 1 |  |
| BASSWIESN_WEB_BASE_URL | string | "f'http://{lan_host}:1328' if lan_host else ''" | nein | Global Runtime | niedrig | ja | ja | freier Text gemaess Code |  |
| LANG | string | "" | nein | Global Runtime | niedrig | nein | ja | freier Text gemaess Code |  |
| LC_ALL | string | "" | nein | Global Runtime | niedrig | nein | ja | freier Text gemaess Code |  |
| LC_MESSAGES | string | "" | nein | Global Runtime | niedrig | nein | ja | freier Text gemaess Code |  |
| PROTECTED_DEVICE_IPS | csv | "" | nein | WebGUI/Device Runtime | mittel | ja | ja | Kommagetrennte Werte | Alias/Quelle fuer geschuetzte Geraete; fail-closed relevant |
