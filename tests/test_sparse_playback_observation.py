"""Incomplete post-command XML is not permission to invent a selected station."""
import pytest
from basswiesn.app.services.direct_selection import Selection


@pytest.mark.parametrize("item", ["", "<ContentItem/>"])
@pytest.mark.parametrize("reported", ["", "<playStatus>PLAY_STATE</playStatus>", "<playStatus>BUFFERING_STATE</playStatus>"])
def test_sparse_post_command_never_claims_playing_or_requested_identity(item, reported):
    xml = f'<nowPlaying source="LOCAL_INTERNET_RADIO">{item}{reported}</nowPlaying>'
    observed = Selection.read(xml, "AA11BB22CC33", allow_pending=True)
    assert observed == Selection("LOCAL_INTERNET_RADIO", "", "", "UNCONFIRMED_STATE")
    assert not observed.same_content(Selection("LOCAL_INTERNET_RADIO", "", "http://example.invalid/station"))


@pytest.mark.parametrize("item", ["", "<ContentItem/>"])
def test_pre_write_read_still_rejects_sparse_xml(item):
    with pytest.raises(ValueError):
        Selection.read(f'<nowPlaying source="LOCAL_INTERNET_RADIO">{item}</nowPlaying>', "AA11BB22CC33")


@pytest.mark.parametrize("xml", [
    '<nowPlaying source="LOCAL_INTERNET_RADIO"><ContentItem source="AUX"/></nowPlaying>',
    '<nowPlaying source="LOCAL_INTERNET_RADIO"><ContentItem/><ContentItem/></nowPlaying>',
    '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus/></nowPlaying>',
    '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus><playStatus>STOP_STATE</playStatus></nowPlaying>',
    '<nowPlaying source="AUX"/>', '<nowPlaying/>',
])
def test_pending_does_not_hide_conflicting_or_unknown_evidence(xml):
    with pytest.raises(ValueError):
        Selection.read(xml, "AA11BB22CC33", allow_pending=True)
