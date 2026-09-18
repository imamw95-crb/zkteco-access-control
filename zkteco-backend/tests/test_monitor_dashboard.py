"""Wiring of the Monitoring tab in the dashboard page.

The feed is deliberately fed from the database, never from a panel: a C3 panel
accepts one connection at a time, so a page that dialled panels would fight the
scheduler and the push agent and could make healthy panels look offline. The other
consequence is that the feed stands still while `SCHEDULER_ENABLED=false` (the dev
launcher's default), so the page has to say that rather than look broken.
"""

from __future__ import annotations


def test_dashboard_page_has_a_monitoring_tab(client):
    html = client.get("/").text

    assert 'data-tab="monitor"' in html
    assert 'id="panel-monitor" hidden' in html  # its own tab, not mixed into Device


def test_dashboard_page_streams_the_monitor_feed(client):
    html = client.get("/").text

    # Same NDJSON shape the push and scan streams already use.
    assert "/api/monitor/stream" in html
    assert "/api/monitor/snapshot" in html
    assert "function readMonitorStream" in html
    assert "application/x-ndjson" in html
    assert 'id="mon-feed"' in html
    assert 'id="mon-devices"' in html


def test_dashboard_page_stops_the_monitor_feed_when_leaving_the_tab(client):
    """An auto-refreshing page nobody stopped would poll in a forgotten tab forever.

    The guard lives in `selectTab()`, which is also the boot path — the page opens
    on Personel, so boot has to go through the same switch for the feed to be
    stopped there too.
    """
    html = client.get("/").text

    assert "function selectTab(name)" in html
    assert "if (name !== 'monitor') stopMonitor();" in html
    assert "selectTab('personnel');" in html  # boot uses the same switch
    assert 'id="btn-mon-stop"' in html
    assert "if (monAbort) return;  // never two feeds at once" in html


def test_dashboard_page_says_when_nothing_is_polling_the_panels(client):
    """A still feed must be explained, or it reads as a broken feature."""
    html = client.get("/").text

    assert "SCHEDULER_ENABLED=false" in html
    assert 'id="mon-poll-warning"' in html
    # The manual pull is the way out of that state.
    assert "/api/logs/pull" in html
    assert "Tarik log sekarang" in html
    assert "askConfirm" in html  # never the browser's own confirm()
    # A bare "22 GAGAL" would send the operator to the server log for something the
    # per-panel error already explains (the agent buffer).
    assert "failures[0].error" in html


def test_dashboard_page_filters_the_feed_and_the_pull_by_date(client):
    """One date drives both, so "today" means the same thing to the feed and the pull.

    A panel always answers with its whole transaction buffer, so the date cannot
    narrow the read — it narrows what is stored. The page has to say that, or an
    operator expects the pull to be smaller on the wire than it is.
    """
    html = client.get("/").text

    assert 'id="mon-date"' in html
    assert 'id="btn-mon-today"' in html
    assert 'id="btn-mon-all"' in html
    assert "function monWindowQuery" in html
    assert "since=" in html and "until=" in html
    assert "/api/logs/pull" in html and "monWindowQuery()" in html
    # No toISOString(): in WIB it would name yesterday until 07:00.
    assert "function localDay" in html
    # The "today" default must apply once only, or pressing "Semua tanggal" is undone
    # by the very reload it triggers.
    assert "monDateInitialised" in html
    assert "berlaku untuk keduanya" in html
    assert "dibuang" in html  # rows outside the day are read and then dropped


def test_dashboard_page_keeps_the_monitor_feed_alive_looking_when_idle(client):
    html = client.get("/").text

    # An idle tick still has to prove the stream is alive, and a denial has to be
    # distinguishable from an accepted card without reading the code number.
    assert "pemeriksaan terakhir" in html
    assert "EventType 27" in html
    assert "benar-benar dibaca reader" in html
    assert 'id="mon-only-denied"' in html
