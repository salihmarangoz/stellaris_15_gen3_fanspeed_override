import argparse
import json
import threading

import paho.mqtt.client as mqtt


HOST = "::1"
PORT = 13688
CLIENT_ID = "MyDynamicDesktop"
USERNAME = "MyDynamicDesktopUser"
PASSWORD = "MyDynamicDesktopPwd888881772688"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--curve", help="Read a named fan curve, for example M2T1")
    args = parser.parse_args()
    received = threading.Event()
    expected_topic = "Fan/Table" if args.curve else "Fan/Status"

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            raise RuntimeError(f"MQTT connection failed: {reason_code}")
        client.subscribe("Fan/#")
        if args.curve:
            request = {
                "Action": "GET_FAN_SPEED_CURVE_SETTING",
                "Name": args.curve,
            }
        else:
            request = {"Action": "GETSTATUS"}
        client.publish("Fan/Control", json.dumps(request))

    def on_message(client, userdata, message):
        payload = message.payload.decode("utf-8", errors="replace")
        try:
            payload = json.dumps(json.loads(payload), indent=2)
        except json.JSONDecodeError:
            pass
        print(f"{message.topic}\n{payload}")
        if message.topic == expected_topic:
            received.set()

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=CLIENT_ID,
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set(USERNAME, PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(HOST, PORT, keepalive=30)
    client.loop_start()
    try:
        if not received.wait(timeout=10):
            raise TimeoutError("No fan status response received within 10 seconds")
    finally:
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    main()
