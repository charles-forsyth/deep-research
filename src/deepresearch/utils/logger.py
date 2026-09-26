import logging
import os
import sys
from datetime import datetime
from rich.console import Console

console = Console(width=120)


def setup_logger(quiet: bool = False):
    logger = logging.getLogger("deepresearch")
    logger.propagate = False

    # Avoid duplicate handlers
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    logger.setLevel(logging.ERROR if quiet else logging.INFO)
    return logger


def log_message(
    logger: logging.Logger,
    message: str,
    level: int = logging.INFO,
    end: str = "\n",
    **kwargs,
):
    if logger.level > level:
        return

    if os.getenv("DR_LOG_TIMESTAMPS") and any(
        t in message for t in ("[INFO]", "[THOUGHT]", "[ERROR]", "[WARN]")
    ):
        stripped = message.lstrip("\n")
        lead = message[: len(message) - len(stripped)]
        message = f"{lead}[{datetime.now():%H:%M:%S}] {stripped}"

    msg = message
    if "[INFO]" in message:
        msg = message.replace("[INFO]", "[bold cyan][INFO][/]")
    elif "[THOUGHT]" in message:
        msg = message.replace("[THOUGHT]", "[bold magenta][THOUGHT][/]")
    elif "[ERROR]" in message:
        msg = message.replace("[ERROR]", "[bold red][ERROR][/]")
    elif "[WARN]" in message:
        msg = message.replace("[WARN]", "[bold yellow][WARN][/]")
    elif "[DB]" in message:
        msg = message.replace("[DB]", "[bold green][DB][/]")

    kwargs.pop("flush", None)

    if len(msg) > 10000:
        print(msg, end=end, flush=True)
    else:
        console.print(msg, end=end, highlight=False, **kwargs)
