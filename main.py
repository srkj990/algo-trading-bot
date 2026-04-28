import sys

from cli.configuration import collect_session_configuration
from logger import finalize_session_logger, log_event, setup_session_logger
from orchestration.context import build_trading_context
from orchestration.session import handle_keyboard_interrupt, run_trading_session, summarize_session

sys.stdout.reconfigure(encoding="utf-8")


def main():
    logger = setup_session_logger()
    session_log_path = None
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


if __name__ == "__main__":
    main()
