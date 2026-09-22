from __future__ import annotations

import uvicorn

from opus.hub import hub
from opus.logutil import get_logger
from opus.net_policy import configure_policy
from opus.phone.controller import PhoneController
from opus.phone.server import create_app
from opus.phone.util import ensure_phone_icons, lan_addresses, nic_ipv4s
from opus.settings import Settings

log = get_logger()


def main() -> None:
    ensure_phone_icons()
    settings = Settings()
    configure_policy(settings)
    controller = PhoneController(settings)
    app = create_app(settings, controller)

    host = str(settings.get("host") or "0.0.0.0")
    port = int(settings.get("port") or 5841)
    hub.set_status(listening=True, speaking=False, mode="idle", message="Open this page on your phone.")

    addrs = nic_ipv4s()
    print()
    print("Opus phone page is being served for local preview.")
    print(f"  Local:  http://127.0.0.1:{port}")
    for kind, ip in addrs:
        label = {"usb": "USB phone", "wifi": "Wi-Fi", "lan": "LAN"}.get(kind, kind)
        print(f"  {label}: http://{ip}:{port}")
    if not addrs:
        print("  Preview: use this PC's LAN or USB IP on port", port)
    if any(kind == "usb" for kind, _ip in addrs):
        print("  Plug in this iPhone, tap Trust, turn on Personal Hotspot, then open the USB phone URL in Safari.")
    else:
        print("  For a private cable link: plug in the iPhone, Trust this PC, turn on Personal Hotspot, then restart this.")
    print("  The iPhone home-screen app at https://compl3xlife.github.io/opus/ does not need this PC.")
    print("  Ctrl+C to stop.")
    print()
    log.info("phone listening on %s:%s lan=%s", host, port, lan_addresses())

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("phone stopped")


if __name__ == "__main__":
    from opus.runtime import set_runtime

    set_runtime("phone")
    main()
