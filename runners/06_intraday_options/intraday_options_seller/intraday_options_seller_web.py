import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import sys as _sys
_sys.stdout.reconfigure(encoding="utf-8")

from logger import enable_web_mode, finalize_session_logger, log_event, setup_session_logger

# Each runner folder uses a unique port so multiple web UIs can run side-by-side.
# Ports: intraday_equity=8081  delivery_equity=8082  futures_equity=8083
#        options_equity=8084   intraday_futures=8085  intraday_options=8086
#        intraday_options_buyer=8087  intraday_options_seller=8088
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8088

# Seller Only (writes PE). Pre-selects the Engine dropdown in the web UI.
# Override via the second CLI arg if needed, e.g. `main_web.py 8088 6`.
ENGINE_CHOICE = sys.argv[2] if len(sys.argv) > 2 else "8"

if __name__ == "__main__":
    import web.server as _web_server
    from pathlib import Path
    from web import state as web_state
    from web.server import start_web_server_in_thread

    _web_server._STATIC_DIR = Path(__file__).parent.parent.parent.parent / "web" / "static"

    os.environ["IOPTS_ENGINE_CHOICE"] = ENGINE_CHOICE
    enable_web_mode()
    setup_session_logger(tag=f"iopts{ENGINE_CHOICE}")

    try:
        start_web_server_in_thread(port=PORT, open_browser=True)
        log_event(f"[WEB] Intraday Options UI (engine_choice={ENGINE_CHOICE}) → http://localhost:{PORT}")
        log_event("[WEB] Select engine, configure, and start from the browser.")
        log_event("[WEB] Press Ctrl+C to shut down.")
        web_state.get_server_exit_event().wait()
    except KeyboardInterrupt:
        log_event("\n[WEB] Shutting down...")
        web_state.request_stop()
        web_state.request_server_exit()
    finally:
        web_state.set_status("stopped")
        session_log_path = finalize_session_logger()
        if session_log_path:
            print(f"[LOG] Session log saved to {session_log_path}")
