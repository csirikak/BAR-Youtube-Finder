"""Rich-backed logging and summary tables for the update pipeline.

One console keeps stage logs, yt-dlp warnings, and summary tables visually
consistent. Rich degrades to plain text when output is redirected to a file.
"""

import logging
import time

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

console = Console()
_ROOT = "bar"
_LEVEL_STYLES = {
    logging.DEBUG: "dim",
    logging.WARNING: "yellow",
    logging.ERROR: "bold red",
    logging.CRITICAL: "bold red",
}


class _Handler(logging.Handler):
    """Print one line per record: a timestamp plus the rich-markup message."""

    def __init__(self):
        super().__init__()
        self.last_stamp = None

    def emit(self, record):
        try:
            stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
            prefix = f"[dim]{stamp}[/dim] " if stamp != self.last_stamp else " " * 9
            self.last_stamp = stamp
            text = Text.from_markup(prefix + record.getMessage())
            console.print(text, style=_LEVEL_STYLES.get(record.levelno), highlight=False)
        except Exception:
            self.handleError(record)


def setup(verbose=False):
    """Install one handler on the shared logger and apply the verbosity level."""
    logger = logging.getLogger(_ROOT)
    if not logger.handlers:
        logger.addHandler(_Handler())
        logger.propagate = False
    if verbose:
        logger.setLevel(logging.DEBUG)
    elif not logger.level:
        logger.setLevel(logging.INFO)
    return logger


def get(name):
    """Return the module logger; safe to call before the CLI sets --verbose."""
    setup()
    return logging.getLogger(f"{_ROOT}.{name}")


def duration(seconds):
    """Format an elapsed time compactly: 41.2s, 3m 12s, 1h 04m."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, seconds = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def banner(title, subtitle=None):
    console.rule(f"[bold]{title}[/bold]", style="cyan")
    if subtitle:
        console.print(f"[dim]{escape(subtitle)}[/dim]")


def stats_table(title, rows):
    """Two-column label/value table for run counters."""
    table = Table(title=title, title_justify="left", title_style="bold",
                  show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim", no_wrap=True)
    table.add_column(justify="right")
    for label, value in rows:
        table.add_row(str(label), str(value))
    console.print(table)


def failures_table(rows):
    """Video ID plus the reason it could not be completed."""
    table = Table(title="Failed videos", title_justify="left", title_style="bold",
                  box=None, pad_edge=False)
    table.add_column("Video", no_wrap=True)
    table.add_column("Reason", overflow="fold")
    for video_id, reason in rows:
        table.add_row(video_id, escape(reason))
    console.print(table)
