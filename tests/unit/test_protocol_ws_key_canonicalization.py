import base64

import msgpack

from xianyu_agent.protocol.events import MessageReceived, WsFrame
from xianyu_agent.protocol.parser import parse_frame
from xianyu_agent.protocol.ws.normalizer import decode_sync_data


def test_decode_sync_data_canonicalizes_numeric_msgpack_keys_recursively():
    raw = msgpack.packb(
        {
            1: {
                2: "chat",
                10: {
                    "reminderContent": "x",
                    "senderUserId": "u",
                },
            }
        },
        use_bin_type=True,
    )

    decoded = decode_sync_data(base64.b64encode(raw).decode())

    assert decoded == {
        "1": {
            "2": "chat",
            "10": {
                "reminderContent": "x",
                "senderUserId": "u",
            },
        }
    }


def test_decode_sync_data_rejects_numeric_string_key_collision():
    raw = msgpack.packb(
        {
            1: "numeric",
            "1": "string",
        },
        use_bin_type=True,
    )

    decoded = decode_sync_data(base64.b64encode(raw).decode())

    assert decoded is None



def test_msgpack_numeric_keys_parse_into_live_message_event():
    payload = {
        1: {
            2: "chat-live@goofish",
            5: 1700000000000,
            10: {
                "senderUserId": "buyer-live",
                "senderNick": "buyer",
                "reminderContent": "hello",
                "extJson": '{"messageId":"MSG-LIVE"}',
            },
        }
    }
    raw = msgpack.packb(payload, use_bin_type=True)
    encoded = base64.b64encode(raw).decode("ascii")
    frame = WsFrame(
        body={
            "syncPushPackage": {
                "data": [
                    {
                        "data": encoded,
                    }
                ]
            }
        }
    )

    events = parse_frame(frame, "account-live", account_user_id="seller-live")

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, MessageReceived)
    assert event.chat_id == "chat-live"
    assert event.sender_id == "buyer-live"
    assert event.content == "hello"
    assert event.message_id == "MSG-LIVE"


def test_decode_sync_data_canonicalizes_numeric_keys_inside_lists():
    raw = msgpack.packb(
        {
            "items": [
                {
                    10: "nested",
                }
            ]
        },
        use_bin_type=True,
    )

    decoded = decode_sync_data(base64.b64encode(raw).decode())

    assert decoded == {
        "items": [
            {
                "10": "nested",
            }
        ]
    }
