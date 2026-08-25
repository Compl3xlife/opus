from __future__ import annotations

import traceback


if __name__ == "__main__":
    try:
        from opus.runtime import set_runtime

        set_runtime("phone")
        from opus.phone.app import main

        main()
    except SystemExit:
        raise
    except Exception as exc:
        try:
            from opus.settings import appdata_dir

            log_path = appdata_dir() / "opus.log"
            with log_path.open("a", encoding="utf-8") as f:
                f.write("\n[PHONE STARTUP FATAL]\n")
                f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
                f.write("\n")
        except Exception:
            pass
        raise
