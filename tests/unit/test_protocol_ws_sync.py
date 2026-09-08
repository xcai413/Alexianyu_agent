"""Regression tests for the M5 WebSocket initial sync builder split."""

from xianyu_agent.protocol import ws_auth
from xianyu_agent.protocol.ws import sync


def test_build_sync_frame_preserves_wire_contract(monkeypatch) -> None:
    monkeypatch.setattr(sync, "_generate_mid", lambda: "mid-1")

    frame = sync.build_sync_frame(now_ms=1_700_000_000_123)

    assert frame == {
        "lwp": "/r/SyncStatus/ackDiff",
        "headers": {"mid": "mid-1"},
        "body": [
            {
                "pipeline": "sync",
                "tooLong2Tag": "PNM,1",
                "channel": "sync",
                "topic": "sync",
                "highPts": 0,
                "pts": 1_700_000_000_123_000,
                "seq": 0,
                "timestamp": 1_700_000_000_123,
            }
        ],
    }


def test_ws_auth_keeps_sync_builder_compatibility_alias() -> None:
    assert ws_auth.ws_sync is sync
    assert ws_auth.build_sync_frame is sync.build_sync_frame
