import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import sys as _sys
_sys.stdout.reconfigure(encoding="utf-8")

from logger import enable_web_mode, finalize_session_logger, log_event, setup_session_logger

# Each runner folder uses a unique port so multiple web UIs can run side-by-side.
# Ports: intraday_equity=8081  delivery_equity=8082  futures_equity=8083
#        options_equity=8084   intraday_futures=8085  intraday_options=8086
PORT = 8083

if __name__ == "__main__":
    import web.server as _web_server
    from pathlib import Path
    from web import state as web_state
    from web.server import start_web_server_in_thread

    _web_server._STATIC_DIR = Path(__file__).parent.parent.parent / "web" / "static"

    enable_web_mode()
    setup_session_logger()

    try:
        start_web_server_in_thread(port=PORT, open_browser=True)
        log_event(f"[WEB] Futures Equity UI → http://localhost:{PORT}")
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
