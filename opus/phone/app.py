from __future__ import annotations

import uvicorn

from opus.hub import hub
from opus.logutil import get_logger
from opus.net_policy import configure_policy
from opus.phone.controller import PhoneController
from opus.phone.server import create_app
from opus.phone.util import ensure_phone_icons, lan_addresses
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

    addrs = lan_addresses()
    print()
    print("Opus phone page is being served for local preview.")
    print(f"  Local:  http://127.0.0.1:{port}")
    for addr in addrs:
        print(f"  Preview: http://{addr}:{port}")
    if not addrs:
        print("  Preview: use this PC's LAN IP on port", port)
    print("  The iPhone home-screen app does not need this PC after GitHub Pages is published.")
    print("  Settings on the published app are stored on the phone.")
    print("  Ctrl+C to stop.")
    print()
    log.info("phone listening on %s:%s lan=%s", host, port, addrs)

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
