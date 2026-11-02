from basswiesn.app.services.config_rewrite import HOSTS_DOMAINS, plan_sdk_config_rewrite, rewrite_hosts, rewrite_sdk_config, verify_hosts_redirect


def test_plan_sdk_config_rewrite():
    xml = """<SoundTouchSdkPrivateCfg>
    <margeServerUrl>https://streaming.bose.com</margeServerUrl>
    <statsServerUrl>https://events.api.bosecm.com</statsServerUrl>
    <swUpdateUrl>https://worldwide.bose.com/updates/soundtouch</swUpdateUrl>
    <bmxRegistryUrl>https://content.api.bose.io/bmx/registry/v1/services</bmxRegistryUrl>
    </SoundTouchSdkPrivateCfg>"""
    plan = plan_sdk_config_rewrite(xml, "http://content.api.bose.io:1516")
    assert plan.changes["margeServerUrl"] == "http://content.api.bose.io:1516"
    assert plan.changes["bmxRegistryUrl"].endswith("/bmx/registry/v1/services")


def test_rewrite_sdk_config_sets_all_route_urls():
    rewritten = rewrite_sdk_config(
        "<SoundTouchSdkPrivateCfg><margeServerUrl>http://old</margeServerUrl></SoundTouchSdkPrivateCfg>",
        "http://192.168.50.77:1516",
    )

    assert "<margeServerUrl>http://192.168.50.77:1516</margeServerUrl>" in rewritten
    assert "<statsServerUrl>http://192.168.50.77:1516</statsServerUrl>" in rewritten
    assert "<swUpdateUrl>http://192.168.50.77:1516/updates/soundtouch</swUpdateUrl>" in rewritten
    assert "<bmxRegistryUrl>http://192.168.50.77:1516/bmx/registry/v1/services</bmxRegistryUrl>" in rewritten


def test_hosts_rewrite_is_idempotent_and_complete():
    original = "127.0.0.1 localhost\n192.0.2.1 content.api.bose.io unrelated.local\n"
    once = rewrite_hosts(original, "192.0.2.20")
    twice = rewrite_hosts(once, "192.0.2.20")

    assert once == twice
    assert verify_hosts_redirect(once, "192.0.2.20")["ok"] is True
    assert once.count("# BASSWIESN BEGIN") == 1
    assert "192.0.2.1 unrelated.local" in once


def test_hosts_verify_reports_missing_domain():
    result = verify_hosts_redirect("192.0.2.20 content.api.bose.io\n", "192.0.2.20")

    assert result["ok"] is False
    assert set(result["missing_domains"]) == set(HOSTS_DOMAINS) - {"content.api.bose.io"}
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.unit
