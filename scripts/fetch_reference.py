"""Download the reference datasets that have no usable API.

    python scripts/fetch_reference.py

Two inputs cannot be collected through ``FinanceDataReader`` and are not served
by a stable JSON endpoint, so they arrive as files under ``data/reference/``
(see that directory's README):

``CNN Fear & Greed history``
    CNN's own endpoint serves roughly one year. The long history is a
    *reconstructed secondary dataset* maintained in public repositories, and it
    starts in 2011 — after both the dot-com crash and the financial crisis. It
    is therefore stored REVIEW-grade and stays an optional indicator.

``AAII weekly sentiment``
    Published by AAII as a spreadsheet. It reaches back to 1987, which makes it
    the only sentiment series that covers the periods this strategy cares most
    about.

Both are written in the plain ``date,...`` CSV shape the file providers expect,
so nothing downstream needs to know where they came from beyond the provenance
already recorded on each observation.
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd

from fear_ladder import paths
from fear_ladder.monitoring.logging import configure_logging

logger = logging.getLogger("fetch_reference")

#: A public mirror of CNN's index. Reconstructed, not published by CNN.
CNN_HISTORY_URLS = (
    "https://raw.githubusercontent.com/whit3rabbit/fear-greed-data/main/fear-greed.csv",
    "https://raw.githubusercontent.com/whit3rabbit/fear-greed-data/master/fear-greed.csv",
)
#: AAII's own published spreadsheet.
AAII_URL = "https://www.aaii.com/files/surveys/sentiment.xls"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

BROWSER_HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}

#: aaii.com serves a 6KB HTML interstitial instead of the spreadsheet unless the
#: request looks like a real navigation. Verified: a bare User-Agent gets HTML,
#: the full set below gets the 1.3MB workbook.
AAII_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.aaii.com/sentimentsurvey",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}

#: OLE2 compound-file magic. An HTML body means we were served the interstitial.
XLS_MAGIC = bytes((0xD0, 0xCF, 0x11, 0xE0))


class FetchError(RuntimeError):
    """Raised when a reference dataset cannot be downloaded."""


def _download(
    url: str, *, timeout: float = 60.0, headers: dict[str, str] | None = None
) -> bytes:
    request = urllib.request.Request(url, headers=headers or BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{url} -> HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FetchError(f"{url} -> {exc}") from exc


# ------------------------------------------------------------------ CNN F&G


def fetch_cnn_history() -> pd.DataFrame:
    """Reconstructed CNN Fear & Greed history as ``date,value``."""
    last_error: str | None = None
    for url in CNN_HISTORY_URLS:
        try:
            raw = _download(url)
        except FetchError as exc:
            last_error = str(exc)
            logger.debug("%s", exc)
            continue

        frame = pd.read_csv(io.BytesIO(raw))
        columns = {str(column).strip().lower(): column for column in frame.columns}
        date_key = columns.get("date")
        value_key = next(
            (columns[name] for name in ("fear greed", "fear_greed", "value", "index")
             if name in columns),
            None,
        )
        if date_key is None or value_key is None:
            last_error = f"{url}: unexpected columns {list(frame.columns)}"
            continue

        result = pd.DataFrame(
            {
                "date": pd.to_datetime(frame[date_key], errors="coerce"),
                "value": pd.to_numeric(frame[value_key], errors="coerce"),
            }
        ).dropna()
        result = result[result["value"].between(0, 100)]
        if result.empty:
            last_error = f"{url}: no usable rows"
            continue
        return result.drop_duplicates("date").sort_values("date")

    raise FetchError(f"no CNN history source responded ({last_error})")


# --------------------------------------------------------------------- AAII


def fetch_aaii() -> pd.DataFrame:
    """AAII weekly sentiment as ``date,bullish,bearish,neutral``.

    ``date`` is the *survey* week, not the publication date. The publication lag
    is applied later, from ``availability_lag_days`` in data_sources.yaml, so a
    backtest cannot read the survey before it existed.
    """
    raw = _download(AAII_URL, headers=AAII_HEADERS)
    if not raw.startswith(XLS_MAGIC):
        raise FetchError(
            f"aaii.com returned {len(raw)} bytes that are not a spreadsheet "
            "(most likely its anti-bot interstitial). Download "
            f"{AAII_URL} in a browser and save it as "
            "data/reference/aaii/sentiment.csv — see data/reference/README.md."
        )
    # The sheet carries four rows of titles before the real header.
    frame = pd.read_excel(io.BytesIO(raw), engine="xlrd", header=3)
    frame.columns = [str(column).strip() for column in frame.columns]

    date_column = frame.columns[0]
    for required in ("Bullish", "Neutral", "Bearish"):
        if required not in frame.columns:
            raise FetchError(f"AAII sheet is missing a {required!r} column")

    result = pd.DataFrame(
        {
            "date": pd.to_datetime(frame[date_column], errors="coerce"),
            "bullish": pd.to_numeric(frame["Bullish"], errors="coerce"),
            "neutral": pd.to_numeric(frame["Neutral"], errors="coerce"),
            "bearish": pd.to_numeric(frame["Bearish"], errors="coerce"),
        }
    ).dropna(subset=["date", "bullish", "bearish"])

    # The sheet stores fractions (0.36) in some vintages and percents (36.0) in
    # others. Normalise to fractions so the indicator is comparable either way.
    if result["bullish"].max() > 1.5:
        result[["bullish", "neutral", "bearish"]] /= 100.0

    if result.empty:
        raise FetchError("AAII sheet produced no usable rows")
    return result.drop_duplicates("date").sort_values("date")


# --------------------------------------------------------------------- main


def _write(frame: pd.DataFrame, path: Path, *, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, date_format="%Y-%m-%d", encoding="utf-8")
    logger.info(
        "%s: %d rows %s .. %s -> %s",
        label,
        len(frame),
        frame["date"].min().date(),
        frame["date"].max().date(),
        path.relative_to(paths.PROJECT_ROOT),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cnn", action="store_true", help="fetch only CNN Fear & Greed")
    parser.add_argument("--aaii", action="store_true", help="fetch only AAII sentiment")
    parser.add_argument("--out", type=Path, default=paths.REFERENCE_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()
    everything = not (args.cnn or args.aaii)

    failures: list[str] = []

    if everything or args.cnn:
        try:
            frame = fetch_cnn_history()
            _write(frame, args.out / "cnn" / "fear_greed_history.csv", label="CNN Fear & Greed")
            if frame["date"].min().date() > date(2009, 1, 1):
                logger.warning(
                    "CNN history starts %s, after the dot-com crash and the financial "
                    "crisis. It stays an optional indicator and its weight is "
                    "redistributed on days it does not cover.",
                    frame["date"].min().date(),
                )
        except FetchError as exc:
            logger.error("CNN Fear & Greed: %s", exc)
            failures.append("cnn")

    if everything or args.aaii:
        try:
            _write(fetch_aaii(), args.out / "aaii" / "sentiment.csv", label="AAII sentiment")
        except FetchError as exc:
            logger.error("AAII sentiment: %s", exc)
            failures.append("aaii")

    if failures:
        logger.error(
            "%s could not be fetched. The pipeline still runs; those indicators "
            "are simply excluded.",
            ", ".join(failures),
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
