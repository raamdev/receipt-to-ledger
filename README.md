# Receipt to Ledger

Watches a local Dropbox folder for new PDF receipts, converts them to [Ledger-CLI](https://ledger-cli.org/) journal entries using Claude, and appends them to a ledger file — automatically, in the background.

Drop a receipt PDF into your Dropbox folder. Within a few seconds, a formatted Ledger-CLI transaction is appended to your ledger file and the PDF is moved to a processed folder.

## How it works

1. A polling loop scans a Dropbox folder every 3 seconds for new PDF files
2. New PDFs are sent to Claude along with the filename, your liability accounts, and your invoice mappings
3. Claude reads the receipt, matches the vendor to a liability account, and returns a formatted Ledger-CLI transaction
4. The transaction is appended to your ledger file
5. The PDF is moved to a configured processed folder

Processing runs on a background thread, so multiple receipts dropped in quick succession are handled in order without any being missed.

## Requirements

- macOS
- Python 3.8+
- An [Anthropic API key](https://console.anthropic.com/)
- [Dropbox](https://www.dropbox.com/) with a local sync folder

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/receipt-to-ledger.git
cd receipt-to-ledger
```

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv ~/.venvs/receipt-to-ledger
~/.venvs/receipt-to-ledger/bin/pip install anthropic
```

### 3. Set up your config file

```bash
cp receipt_to_ledger.conf.example ~/.receipt-to-ledger.conf
```

Edit `~/.receipt-to-ledger.conf` and fill in your values:

```ini
[anthropic]
api_key = sk-ant-YOUR_KEY_HERE

[paths]
watch_folder     = ~/Dropbox/Receipts
processed_folder = ~/Dropbox/Receipts/Processed
ledger_file      = ~/Dropbox/finances.ledger
accounts_file    = ~/Dropbox/accounts.ledger

[accounts]
default_account  = Assets:Checking
```

See `receipt_to_ledger.conf.example` for all available options.

### 4. Set up your accounts file (recommended)

Receipt to Ledger reads your ledger-cli accounts file and passes all `Liabilities:*` accounts to Claude, which matches them against vendor names on each receipt. If you already have an accounts file for your ledger setup, just point `accounts_file` in your config to it — the script ignores everything except `Liabilities:*` lines.

If you don't have one yet, copy the example to get started:

```bash
cp accounts.example ~/Dropbox/accounts.ledger
```

Edit it to reflect your actual liability accounts. See [Accounts](#accounts) below for details.

### 5. Set up your invoice mappings file (optional)

If you want line items tagged with invoice information, copy the example and fill it in:

```bash
cp invoice_mappings.example ~/Dropbox/invoice_mappings.txt
```

Point `invoice_mappings_file` in your config to wherever you put it. See [Invoice Mappings](#invoice-mappings) below for details.

### 6. Create any folders that don't exist yet

```bash
mkdir -p ~/Dropbox/Receipts/Processed
mkdir -p ~/logs
```

### 7. Test it manually

```bash
~/.venvs/receipt-to-ledger/bin/python receipt_to_ledger.py
```

Drop a receipt PDF into your watch folder and confirm the ledger entry looks correct and the PDF moves to the processed folder. Press Ctrl-C to stop.

### 8. Install the script somewhere permanent

```bash
mkdir -p ~/scripts
cp receipt_to_ledger.py ~/scripts/
```

### 9. Install the launchd agent

Edit `com.raam.receipt-to-ledger.plist` and update the two paths to match your username and venv location:

```xml
<string>/Users/YOUR_USERNAME/.venvs/receipt-to-ledger/bin/python</string>
<string>/Users/YOUR_USERNAME/scripts/receipt_to_ledger.py</string>
```

Then install and load it:

```bash
cp com.raam.receipt-to-ledger.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.raam.receipt-to-ledger.plist
```

The script will start immediately and relaunch automatically on login or if it crashes.

### 10. Verify it's running

```bash
launchctl list | grep receipt-to-ledger
```

A PID in the first column confirms it's running. Tail the log to watch it work:

```bash
tail -f ~/logs/receipt_to_ledger.log
```

## Managing the background agent

```bash
# Stop
launchctl unload ~/Library/LaunchAgents/com.raam.receipt-to-ledger.plist

# Start
launchctl load ~/Library/LaunchAgents/com.raam.receipt-to-ledger.plist

# Restart (e.g. after updating the script)
launchctl unload ~/Library/LaunchAgents/com.raam.receipt-to-ledger.plist
launchctl load ~/Library/LaunchAgents/com.raam.receipt-to-ledger.plist
```

## Receipt filename format

Claude uses the PDF filename to extract vendor and client information, so naming your receipts consistently gets the best results:

```
YYYY-MM-DD Vendor Name - Client.pdf
```

Examples:
```
2026-03-16 Lowes - Birch.pdf
2026-03-20 Home Depot - ClientA.pdf
2026-03-21 Hammond Lumber - ClientB.pdf
```

If no client is present in the filename, invoice mapping is simply skipped.

## Invoice mappings

The invoice mappings file associates client names (as they appear in receipt filenames) with invoice descriptions. When a match is found, the invoice description is prepended to each line item comment in the ledger entry.

**Format:** one entry per line, `Client: Invoice description`. Lines starting with `#` are comments and are ignored.

```
# invoice_mappings.txt

Birch: Invoice 0000348 - 43 Birch Rd
ClientA: Invoice 0000351 - 123 Some Rd
ClientB: Invoice 0000355 - 456 Another Rd

# Completed jobs (kept for reference):
# Harbor: Invoice 0000312 - 14 Harbor View Ln
```

The file is re-read on every receipt, so you can update it between receipts without restarting the watcher. Comment out completed jobs rather than deleting them to keep a running record.

## Configuration reference

All configuration lives in `~/.receipt-to-ledger.conf`.

| Key | Section | Required | Description |
|---|---|---|---|
| `api_key` | `[anthropic]` | Yes | Your Anthropic API key |
| `watch_folder` | `[paths]` | Yes | Folder to watch for new PDFs |
| `processed_folder` | `[paths]` | Yes | Where PDFs are moved after processing |
| `ledger_file` | `[paths]` | Yes | Ledger file to append entries to |
| `accounts_file` | `[paths]` | No | Path to your ledger-cli accounts file; `Liabilities:*` accounts are passed to Claude for vendor matching |
| `invoice_mappings_file` | `[paths]` | No | Path to your invoice mappings file |
| `processed_log` | `[paths]` | No | Tracks processed file hashes (default: `~/.receipt-to-ledger-processed`) |
| `log_file` | `[paths]` | No | Log file path (default: `~/logs/receipt_to_ledger.log`) |
| `default_account` | `[accounts]` | No | Fallback account when no liability account matches the vendor (default: `Assets:Checking`) |

## Accounts

Liability account matching is driven by your accounts file rather than hardcoded mappings. All `Liabilities:*` accounts found in the file are passed to Claude, which matches them against the vendor name on each receipt. Claude handles fuzzy matching — e.g. "Lowe's" will match `Liabilities:Lowes` without needing an explicit mapping.

If no liability account is a reasonable match for a vendor, the `default_account` from your config is used instead.

All purchased items are posted to `Expenses:Materials`. Maine sales tax (5.5%) is folded into each line item — not recorded as a separate posting.

See `accounts.example` for the expected file format.

## Logs

| File | Contents |
|---|---|
| `~/logs/receipt_to_ledger.log` | Main application log — processing events, errors, ledger entries |
| `~/logs/receipt_to_ledger.stdout.log` | launchd stdout capture |
| `~/logs/receipt_to_ledger.stderr.log` | launchd stderr capture |
| `~/.receipt-to-ledger-processed` | SHA-256 hashes of all processed PDFs (used for deduplication) |

## Deduplication

Each processed PDF is tracked by a SHA-256 hash of its contents in `~/.receipt-to-ledger-processed`. If the same file is dropped into the watch folder again — or a duplicate appears with a different filename — it will be moved to the processed folder without creating a duplicate ledger entry.

## Troubleshooting

**Script starts but doesn't pick up new files**
Confirm the `watch_folder` path in your config is correct and contains no backslash-escaped spaces — spaces in paths are fine as-is in the config file.

**`ModuleNotFoundError: No module found 'anthropic'`**
Use the venv's pip directly to install, rather than relying on an activated environment:
```bash
~/.venvs/receipt-to-ledger/bin/pip install anthropic
```

**Ledger entry has ` ```ledger ` / ` ``` ` fences around it**
This is handled automatically by the script. If you see this in entries that were already appended, remove the fence lines manually from your ledger file.

**`Watch folder does not exist` error**
Don't escape spaces in the config file. Use:
```
watch_folder = ~/Dropbox/My Receipts
```
Not:
```
watch_folder = ~/Dropbox/My\ Receipts
```

## Security

`~/.receipt-to-ledger.conf` contains your Anthropic API key and is excluded from the repo via `.gitignore`. Never commit it. The `com.raam.receipt-to-ledger.plist` launchd agent reads config from that file at runtime and contains no secrets of its own.
