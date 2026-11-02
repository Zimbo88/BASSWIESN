from dataclasses import dataclass
from xml.etree import ElementTree as ET


URL_TAGS = ("margeServerUrl", "statsServerUrl", "swUpdateUrl", "bmxRegistryUrl")
HOSTS_DOMAINS = (
    "content.api.bose.io",
    "streaming.bose.com",
    "bmx.bose.com",
    "api.bosesoundtouch.com",
    "events.api.bosecm.com",
    "worldwide.bose.com",
    "update.bose.com",
    "analytics.bose.com",
    "telemetry.bose.com",
    "bose.vtuner.com",
    "bose2.vtuner.com",
    "primary5.vtuner.com",
    "primary6.vtuner.com",
    "streamingefeint.bose.com",
    "streamingintoauth.bose.com",
    "streamingefeintoauth.bose.com",
    "eventsdev.bosecm.com",
    "bose-test.apigee.net",
    "device-tuner.pandora.com",
    "device-tuner-beta.savagebeast.com",
    "tuner-beta.savagebeast.com",
    "invalid.pandora.com",
)
HOSTS_BEGIN = "# BASSWIESN BEGIN"
HOSTS_END = "# BASSWIESN END"


@dataclass(frozen=True)
class RewritePlan:
    target_base_url: str
    changes: dict[str, str]
    strategy: str
    warnings: list[str]


def plan_sdk_config_rewrite(xml_text: str, target_base_url: str) -> RewritePlan:
    root = ET.fromstring(xml_text)
    changes: dict[str, str] = {}
    values = {
        "margeServerUrl": target_base_url,
        "statsServerUrl": target_base_url,
        "swUpdateUrl": f"{target_base_url}/updates/soundtouch",
        "bmxRegistryUrl": f"{target_base_url}/bmx/registry/v1/services",
    }
    for tag in URL_TAGS:
        elem = root.find(tag)
        if elem is not None and elem.text != values[tag]:
            changes[tag] = values[tag]
    return RewritePlan(
        target_base_url=target_base_url,
        changes=changes,
        strategy="config-rewrite-first, DNS/hosts fallback, reverse-proxy optional",
        warnings=[
            "Backup /mnt/nv and all SoundTouchSdkPrivateCfg variants before writing.",
            "Use HTTP URLs unless a trusted Bose-compatible certificate path is proven.",
            "Treat /mnt/nv/OverrideSdkPrivateCfg.xml as firmware-dependent optional.",
            "Re-apply envswitch boseurls after XML migration or reboot to sync runtime state.",
        ],
    )


def rewrite_sdk_config(xml_text: str, target_base_url: str) -> str:
    """Rewrite the four cloud route fields in an existing SDK config."""
    root = ET.fromstring(xml_text)
    values = {
        "margeServerUrl": target_base_url,
        "statsServerUrl": target_base_url,
        "swUpdateUrl": f"{target_base_url}/updates/soundtouch",
        "bmxRegistryUrl": f"{target_base_url}/bmx/registry/v1/services",
    }
    for tag, value in values.items():
        element = root.find(tag)
        if element is None:
            element = ET.SubElement(root, tag)
        element.text = value
    return ET.tostring(root, encoding="unicode")


def rewrite_hosts(hosts_text: str, target_host: str) -> str:
    """Replace the managed redirect block and remove stale mappings."""
    kept: list[str] = []
    in_managed_block = False
    domains = set(HOSTS_DOMAINS)
    for raw_line in hosts_text.splitlines():
        stripped = raw_line.strip()
        if stripped == HOSTS_BEGIN:
            in_managed_block = True
            continue
        if stripped == HOSTS_END:
            in_managed_block = False
            continue
        if in_managed_block:
            continue
        body, separator, comment = raw_line.partition("#")
        fields = body.split()
        if len(fields) >= 2:
            remaining = [name for name in fields[1:] if name not in domains]
            if not remaining:
                continue
            raw_line = " ".join([fields[0], *remaining])
            if separator:
                raw_line += f"  # {comment.strip()}"
        kept.append(raw_line.rstrip())
    while kept and not kept[-1]:
        kept.pop()
    block = [HOSTS_BEGIN, f"{target_host} {' '.join(HOSTS_DOMAINS)}", HOSTS_END]
    return "\n".join([*kept, "", *block]) + "\n"


def verify_hosts_redirect(hosts_text: str, target_host: str) -> dict:
    found: dict[str, set[str]] = {domain: set() for domain in HOSTS_DOMAINS}
    for raw_line in hosts_text.splitlines():
        fields = raw_line.split("#", 1)[0].split()
        if len(fields) < 2:
            continue
        for domain in fields[1:]:
            if domain in found:
                found[domain].add(fields[0])
    missing = [domain for domain, hosts in found.items() if target_host not in hosts]
    return {"ok": not missing, "target_host": target_host, "missing_domains": missing}
