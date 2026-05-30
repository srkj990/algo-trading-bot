import sys

from cli.configuration import collect_session_configuration
from logger import enable_web_mode, finalize_session_logger, log_event, setup_session_logger
from orchestration.context import build_trading_context
from orchestration.session import handle_keyboard_interrupt, run_trading_session, summarize_session

sys.stdout.reconfigure(encoding="utf-8")

_WEB_PORT = 8080


def _run_console_session():
    """Standard console flow — unchanged from original."""
    logger = setup_session_logger()
    context = None
    try:
        log_event("Starting Algo Bot...\n")
        session_config = collect_session_configuration()
        context = build_trading_context(session_config)
        run_trading_session(context)
    except KeyboardInterrupt:
        if context is not None:
            handle_keyboard_interrupt(context)
    except Exception as exc:
        log_event(f"\n[ERROR] {exc}", "error")
        logger.exception("[MAIN] Unhandled exception")
        raise
    finally:
        if context is not None:
            try:
                summarize_session(context)
            except Exception as exc:
                log_event(f"[STATS] Failed to generate summary: {exc}", "warning")

        session_log_path = finalize_session_logger()
        if session_log_path:
            print(f"[LOG] Session log saved to {session_log_path}")


def _run_web_session():
    """
    Default mode — browser UI:
      - Start uvicorn in a daemon thread; return immediately once the socket binds.
      - Open the browser at localhost:8080 — the user configures and starts the
        session entirely from the browser form (no terminal prompts needed).
      - The trading loop is spawned by POST /api/start from the browser.
      - Main thread blocks on web_state.get_stop_event() so the process stays
        alive for as long as the server runs.
      - Run with --console to use the old terminal prompt flow instead.
    """
    from web import state as web_state
    from web.server import start_web_server_in_thread

    enable_web_mode()
    setup_session_logger()

    try:
        start_web_server_in_thread(port=_WEB_PORT, open_browser=True)
        log_event(f"[WEB] Browser UI running at http://localhost:{_WEB_PORT}")
        log_event(f"[WEB] Configure and start your session from the browser.")
        log_event(f"[WEB] API docs at http://localhost:{_WEB_PORT}/docs")
        log_event("[WEB] Press Ctrl+C to stop the server.")

        # block main thread until Ctrl+C or server shutdown
        web_state.get_stop_event().wait()

    except KeyboardInterrupt:
        log_event("\n[WEB] Shutting down...")
        web_state.request_stop()
    finally:
        web_state.set_status("stopped")
        session_log_path = finalize_session_logger()
        if session_log_path:
            print(f"[LOG] Session log saved to {session_log_path}")


def main():
    if "--console" in sys.argv:
        _run_console_session()
    else:
        _run_web_session()


if __name__ == "__main__":
    main()
