import logging
from datetime import datetime
from pathlib import Path

from config import LOG_LEVEL


LOGGER_NAME = "algo_bot"
LOG_DIR = Path("logs")
_session_state = {
    "start_time": None,
    "running_path": None,
}


def setup_session_logger():
    logger = logging.getLogger(LOGGER_NAME)
    level = getattr(logging, LOG_LEVEL)
    logger.setLevel(level)
    logger.propagate = False

    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    LOG_DIR.mkdir(exist_ok=True)

    start_time = datetime.now()
    running_name = f"algo_{start_time.strftime('%Y%m%d_%H%M%S')}_running.log"
    running_path = LOG_DIR / running_name

    file_handler = logging.FileHandler(running_path, encoding="utf-8")
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    _session_state["start_time"] = start_time
    _session_state["running_path"] = running_path

    return logger


def get_logger():
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        return setup_session_logger()
    return logger


def finalize_session_logger():
    logger = logging.getLogger(LOGGER_NAME)
    running_path = _session_state["running_path"]
    start_time = _session_state["start_time"]

    if not running_path or not start_time:
        return None

    end_time = datetime.now()
    final_name = (
        f"algo_{start_time.strftime('%Y%m%d_%H%M%S')}"
        f"_to_{end_time.strftime('%Y%m%d_%H%M%S')}.log"
    )
    final_path = LOG_DIR / final_name

    for handler in list(logger.handlers):
        handler.flush()
        handler.close()
        logger.removeHandler(handler)

    if running_path.exists():
        running_path.rename(final_path)

    _session_state["start_time"] = None
    _session_state["running_path"] = None
    return final_path


def log_event(message, level="info"):
    print(message)
    logger = get_logger()
    getattr(logger, level.lower())(message)
