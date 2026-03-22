#!/usr/bin/env python3
"""
Receipt to Ledger
Watches a local Dropbox folder for new PDF receipts, converts them to
ledger-cli entries via Claude, and appends them to a ledger file.

Configuration: ~/.receipt-to-ledger.conf (see receipt_to_ledger.conf.example)
"""

import os
import sys
import time
import base64
import logging
import hashlib
import configparser
from pathlib import Path

import queue
import threading

import anthropic

# ── CONFIG ────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path.home() / ".receipt-to-ledger.conf"

def load_config() -> configparser.ConfigParser:
    if not CONFIG_FILE.exists():
        print(f"ERROR: Config file not found: {CONFIG_FILE}")
        print(f"Copy receipt_to_ledger.conf.example to {CONFIG_FILE} and fill in your values.")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    return cfg

def expand(path_str: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path_str)))

cfg = load_config()

ANTHROPIC_API_KEY       = cfg.get("anthropic", "api_key")
WATCH_FOLDER            = expand(cfg.get("paths", "watch_folder"))
LEDGER_FILE             = expand(cfg.get("paths", "ledger_file"))
PROCESSED_FOLDER        = expand(cfg.get("paths", "processed_folder"))
PROCESSED_LOG           = expand(cfg.get("paths", "processed_log",
                                         fallback="~/.receipt-to-ledger-processed"))
LOG_FILE                = expand(cfg.get("paths", "log_file",
                                         fallback="~/logs/receipt_to_ledger.log"))

# Invoice mappings file is optional
_invoice_mappings_raw   = cfg.get("paths", "invoice_mappings_file", fallback="")
INVOICE_MAPPINGS_FILE   = expand(_invoice_mappings_raw) if _invoice_mappings_raw else None

# Accounts file is optional but recommended
_accounts_file_raw      = cfg.get("paths", "accounts_file", fallback="")
ACCOUNTS_FILE           = expand(_accounts_file_raw) if _accounts_file_raw else None

DEFAULT_ACCOUNT         = cfg.get("accounts", "default_account",
                                   fallback="Assets:Checking")

# ── LOGGING ──────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("receipt-to-ledger")

# ── PROMPT ────────────────────────────────────────────────────────────────────

def load_liability_accounts() -> list[str]:
    """
    Parse the accounts file and return all Liabilities:* account names.
    Returns an empty list if no file is configured or the file doesn't exist.
    """
    if not ACCOUNTS_FILE:
        return []
    if not ACCOUNTS_FILE.exists():
        log.warning(f"Accounts file not found: {ACCOUNTS_FILE}")
        return []
    accounts = []
    for line in ACCOUNTS_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("account Liabilities:"):
            accounts.append(line[len("account "):])
    return accounts


def build_system_prompt() -> str:
    """Build the system prompt, incorporating liability accounts and default account from config."""
    liability_accounts = load_liability_accounts()

    if liability_accounts:
        accounts_list = "\n".join(f"  {a}" for a in liability_accounts)
        accounts_section = (
            f"Credit the appropriate liability account based on the vendor name.\n"
            f"Match the vendor name to the most likely account from this list:\n"
            f"{accounts_list}\n"
            f"If no liability account is a reasonable match for the vendor, "
            f"debit {DEFAULT_ACCOUNT} instead."
        )
    else:
        accounts_section = f"If no liability account can be determined, debit {DEFAULT_ACCOUNT}."

    return f"""\
You are a bookkeeping assistant that converts retail receipts into Ledger-CLI journal entries. Follow these rules exactly:

Format:
Date format: YYYY-MM-DD
Use a ! flag: 2026-03-14 ! Vendor Name
No transaction-level comments
Two spaces minimum between account and amount (align amounts)
Inline comments for each line item using ;;
Always include a dollar sign ($) for amounts.
If an amount is negative, format it with the minus after the dollar sign (e.g., $-10.00).
For returns, use negative amounts on Expenses:Materials and a positive amount on the liability account

Accounts:
All purchased items go to Expenses:Materials
{accounts_section}

Tax:
Maine sales tax (5.5%) is folded into each line item amount — do not record it as a separate posting
Multiply each pre-tax line item amount by 1.055 and round to the nearest cent
Adjust one item if necessary so all postings sum exactly to the receipt total

Line item comments:
Describe the item concisely
For multiples, note quantity and unit price: (3 @ $4.60)
The receipt PDF filename is formatted as "YYYY-MM-DD [VENDOR] - [CLIENT].pdf". The Invoice Mappings data is formatted as "[CLIENT]: [INVOICE DESCRIPTION]". If the receipt PDF contains a [CLIENT] that matches a line in the invoice mappings, add the [INVOICE DESCRIPTION] information to the start of the line item comment. For example, if the receipt filename is "2026-03-16 Lowes - Birch.pdf", and the Invoice Mappings contains "Birch: Invoice 0000348 - 43 Birch Rd", the resulting line item comments for the receipt should start with ";; Invoice 0000348 - 43 Birch Rd - " followed by the line item info from the receipt.

Validation:
The sum of all Expenses:Materials postings must equal the receipt total exactly
Output only the Ledger-CLI entry, nothing else.

If the PDF is not a receipt, output exactly: NOT_A_RECEIPT\
"""

def build_user_message(pdf_path: Path) -> list:
    """Build the user message content blocks for a receipt PDF."""
    blocks = [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": pdf_to_base64(pdf_path),
            },
        },
        {
            "type": "text",
            "text": f"Receipt filename: {pdf_path.name}",
        },
    ]

    mappings = load_invoice_mappings()
    if mappings:
        blocks.append({
            "type": "text",
            "text": f"Invoice Mappings:\n{mappings}",
        })

    return blocks

def load_invoice_mappings() -> str:
    """
    Load invoice mappings from the configured file, stripping comment lines
    (lines starting with #) and blank lines. Returns an empty string if no
    file is configured or the file doesn't exist.
    """
    if not INVOICE_MAPPINGS_FILE:
        return ""
    if not INVOICE_MAPPINGS_FILE.exists():
        log.warning(f"Invoice mappings file not found: {INVOICE_MAPPINGS_FILE}")
        return ""
    lines = INVOICE_MAPPINGS_FILE.read_text().splitlines()
    active = [l for l in lines if l.strip() and not l.strip().startswith("#")]
    return "\n".join(active)

# ── HELPERS ───────────────────────────────────────────────────────────────────

def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_processed() -> set:
    p = Path(PROCESSED_LOG)
    if not p.exists():
        return set()
    return set(p.read_text().splitlines())


def mark_processed(identifier: str):
    with open(PROCESSED_LOG, "a") as f:
        f.write(identifier + "\n")


def wait_for_stable(path: Path, stable_seconds: float = 0.5, timeout: float = 30.0):
    """Wait until a file stops growing (Dropbox may still be syncing)."""
    deadline = time.time() + timeout
    last_size = -1
    while time.time() < deadline:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            time.sleep(0.5)
            continue
        if size == last_size and size > 0:
            return True
        last_size = size
        time.sleep(stable_seconds)
    return False


def pdf_to_base64(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode("utf-8")


def move_to_processed(pdf_path: Path):
    """Move a PDF to the processed folder, avoiding filename collisions."""
    PROCESSED_FOLDER.mkdir(parents=True, exist_ok=True)
    dest = PROCESSED_FOLDER / pdf_path.name
    if dest.exists():
        # Append a suffix to avoid overwriting an existing file
        stem, suffix = pdf_path.stem, pdf_path.suffix
        dest = PROCESSED_FOLDER / f"{stem}-{int(time.time())}{suffix}"
    pdf_path.rename(dest)
    log.info(f"  Moved to {dest}")


def process_receipt(pdf_path: Path):
    log.info(f"Processing: {pdf_path.name}")

    if not wait_for_stable(pdf_path):
        log.warning(f"  File never stabilised, skipping: {pdf_path.name}")
        return

    fhash = file_hash(pdf_path)
    if fhash in load_processed():
        log.info(f"  Already processed (duplicate content), skipping.")
        move_to_processed(pdf_path)
        return

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=build_system_prompt(),
            messages=[
                {
                    "role": "user",
                    "content": build_user_message(pdf_path),
                }
            ],
        )
    except Exception as e:
        log.error(f"  Claude API error: {e}")
        return

    entry = response.content[0].text.strip()
    # Strip markdown code fences if the model includes them despite instructions
    if entry.startswith("```"):
        entry = "\n".join(entry.splitlines()[1:])
    if entry.endswith("```"):
        entry = "\n".join(entry.splitlines()[:-1])
    entry = entry.strip()

    if entry == "NOT_A_RECEIPT":
        log.warning(f"  Not a receipt, skipping: {pdf_path.name}")
        mark_processed(fhash)
        move_to_processed(pdf_path)
        return

    ledger_path = Path(LEDGER_FILE)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a") as f:
        f.write("\n" + entry + "\n")

    mark_processed(fhash)
    move_to_processed(pdf_path)
    log.info(f"  ✓ Appended to {ledger_path.name}")
    log.info(f"  Entry:\n{entry}")


# ── POLLING LOOP ──────────────────────────────────────────────────────────────

# Watchdog relies on FSEvents, which Dropbox's sync mechanism doesn't always
# trigger reliably on macOS. A simple polling loop is more robust for this use case.

POLL_INTERVAL = 3  # seconds between scans

# Processing happens on a worker thread so the polling loop is never blocked
# waiting for a file to stabilise or for the Claude API to respond.
_receipt_queue: queue.Queue = queue.Queue()

def _worker():
    """Background thread: pull paths from the queue and process them."""
    while True:
        pdf_path = _receipt_queue.get()
        if pdf_path is None:           # None is the shutdown sentinel
            break
        try:
            process_receipt(pdf_path)
        except Exception as e:
            log.error(f"Unhandled error processing {pdf_path.name}: {e}")
        finally:
            _receipt_queue.task_done()


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    if not WATCH_FOLDER.exists():
        log.error(f"Watch folder does not exist: {WATCH_FOLDER}")
        sys.exit(1)

    worker_thread = threading.Thread(target=_worker, daemon=True)
    worker_thread.start()

    log.info(f"Watching {WATCH_FOLDER} for new PDFs (polling every {POLL_INTERVAL}s) ...")

    # Track which files have been queued this session to avoid re-queuing
    # a file that's still sitting in the folder while its worker job runs.
    queued: set = set()

    try:
        while True:
            processed = load_processed()
            for pdf in sorted(WATCH_FOLDER.glob("*.pdf")):
                fhash = file_hash(pdf)
                if fhash not in processed and fhash not in queued:
                    log.info(f"  Found unprocessed file: {pdf.name}")
                    queued.add(fhash)
                    _receipt_queue.put(pdf)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        _receipt_queue.put(None)       # signal worker to exit cleanly
        worker_thread.join()


if __name__ == "__main__":
    main()
