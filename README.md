# Personal Knowledge Library

A personal Telegram bot for saving structured documents, browsing them by category,
setting reminders, and exporting notes to clean, professional PDFs.

Built with **python-telegram-bot (v21+, async)**, **SQLite**, and **APScheduler**.
Requires **Python 3.10+**.

## Features

1. **Document saving** — send a symbol-structured document, pick a category, and keep
   extending it with `@`, `#`, and `*` blocks
2. **Photo attachments** — attach photos to the current section (or the document);
   they appear inline in the exported PDF at their exact position
3. **`/library`** — browse documents by category (7 categories, 4+3 layout)
4. **`/remind`** — interactive date + time selection for one-time reminders that
   survive bot restarts
5. **`/export`** — export any document to a clean, professional PDF
6. **Individual document deletion** — with preview and confirmation
7. **Private access control** — only configured Telegram user IDs may use the bot

## Categories (7)

| Left column | Right column |
|-------------|--------------|
| Tech        | Finance      |
| DEV         | Ideas        |
| Hardware    | Personal Development |
| Logistics   |              |

## Document Structure

Documents use exactly three symbols. There is no `Title:` / `Subtitle:` /
`Paragraph:` text format.

| Symbol | Meaning |
|--------|---------|
| `@` | Title — starts a new document |
| `#` | Subtitle / section heading |
| `*` | Paragraph / body text |

Everything after a symbol, up to the next symbol or the end of the message, is that
block's content. Line breaks, URLs, pipes, commands, backslashes and special
characters are preserved.

**One line:**

```
@Windows #Uninstall stubborn apps *Qwen3.5-9B-Abliterated
```

**Multiple lines:**

```
@Computer Architecture

#CPU

*The CPU executes instructions and processes data.

#RAM

*RAM temporarily stores information currently being used by the computer.
```

Both forms produce the same structure: one title, two subtitles, two paragraphs.

Behaviour:

- A new `@` starts a new document. The previous document stays saved in the library.
- `#` and `*` blocks append to the document currently being built, including across
  separate Telegram messages.
- Every `@` must start a line (leading spaces are fine). Inline `@` remains ordinary
  text, so `user@example.com` and `ask @alice` do not split a document.
- Inline `#` and `*` count as markers only when they follow a space and are attached to
  text (`#Uninstall`, `*Paragraph`), or when they are followed by a space and open a
  block after a title/subtitle (`@Doc # Section * Paragraph`). `C#`, `2 * 3`, `# 5`
  and URL fragments such as `https://example.com/#frag` stay ordinary content.
- Inside a paragraph, an apostrophe used as a manual line-break marker becomes a new
  line: `*First line'Second line` renders as two lines of the same paragraph. Normal
  apostrophes (`don't`, `O'Brien`, `students' work`) are preserved.
- A backslash escapes a marker (`\@`, `\#`, `\*`) and is kept verbatim.
- Messages without a marker are not added to a document; the bot replies with the
  symbol instructions instead.

### Starting a document

Send the document text first, then choose a category. If the text spans several
messages, send them before choosing the category — they are buffered into the same
document. Alternatively use `/new`, choose a category, and start writing immediately.

Photos sent during document building are associated with the current section (or the
document if no section is active).

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message + symbol instructions |
| `/new` | Start a new document (choose a category) |
| `@` / `#` / `*` blocks | Build the document structure |
| `/library` | Browse documents by category |
| `/remind` | Set a reminder (interactive date + time) |
| `/export` | Export a document to PDF |
| `/done` | Finish building the current document |
| `/cancel` | Cancel the current document |

## Configuration

All secrets and deployment paths are read from environment variables or the
project's `.env` file. Nothing is hardcoded.

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `BOT_TOKEN` | Yes | — | Bot token from [@BotFather](https://t.me/BotFather) |
| `ALLOWED_USER_IDS` | Recommended | empty (open) | Comma-separated numeric Telegram user IDs allowed to use the bot |
| `TIMEZONE` | No | `Africa/Addis_Ababa` | IANA timezone for reminder dates/times |
| `DATA_DIR` | On a VPS | project directory | Persistent root for SQLite, photos and PDFs |
| `DB_PATH` | No | `$DATA_DIR/knowledge_library.db` | SQLite file override |
| `PHOTOS_DIR` | No | `$DATA_DIR/document_photos` | Uploaded photos override |
| `EXPORTS_DIR` | No | `$DATA_DIR/exports` | Generated PDFs override |

Example `.env`:

```ini
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
ALLOWED_USER_IDS=123456789
TIMEZONE=Africa/Addis_Ababa
DATA_DIR=/var/lib/knowledge-library
```

> `DATA_DIR` is optional for local development. When it is unset, the project
> directory is used, which preserves the existing local database and photos.

Protect the file:

```bash
chmod 600 .env
```

## Local setup

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## Deploying to a Linux VPS

The example below uses:

- application: `/opt/knowledge-library`
- persistent data: `/var/lib/knowledge-library`
- service user: `telegrambot`

Adjust paths and the user if your server uses different ones.

### 1. Create the user and directories

```bash
sudo useradd --system --shell /usr/sbin/nologin --home /opt/knowledge-library telegrambot
sudo mkdir -p /opt/knowledge-library /var/lib/knowledge-library /opt/knowledge-library/backups
sudo chown -R telegrambot:telegrambot /var/lib/knowledge-library
```

### 2. Copy the project

From your workstation, stage the project in `/tmp` and let `root` place it in
`/opt` (add `--exclude` entries as shown so local secrets and data are not
uploaded):

```bash
rsync -av \
  --exclude '.env' \
  --exclude 'knowledge_library.db' \
  --exclude 'document_photos' \
  --exclude 'exports' \
  --exclude 'backups' \
  --exclude '__pycache__' \
  ./ user@your-vps:/tmp/knowledge-library/

ssh user@your-vps '
  sudo cp -a /tmp/knowledge-library/. /opt/knowledge-library/ &&
  sudo chown -R telegrambot:telegrambot /opt/knowledge-library &&
  rm -rf /tmp/knowledge-library
'
```

### 3. Install dependencies

```bash
sudo -u telegrambot python3 -m venv /opt/knowledge-library/venv
sudo -u telegrambot /opt/knowledge-library/venv/bin/pip install --upgrade pip
sudo -u telegrambot /opt/knowledge-library/venv/bin/pip install \
  -r /opt/knowledge-library/requirements.txt
```

### 4. Configure `.env`

Create `/opt/knowledge-library/.env` as `telegrambot`:

```ini
BOT_TOKEN=your-real-token
ALLOWED_USER_IDS=your-numeric-telegram-id
TIMEZONE=Africa/Addis_Ababa
DATA_DIR=/var/lib/knowledge-library
```

```bash
sudo -u telegrambot chmod 600 /opt/knowledge-library/.env
```

`ALLOWED_USER_IDS` is the private-bot gate. Any other Telegram user is rejected
before a handler runs. For several accounts, separate IDs with commas.

### 5. Copy existing notes, photos and exports

The local development database is in the project directory. Copy it and the
uploaded files to the persistent directory:

```bash
scp knowledge_library.db user@your-vps:/tmp/
scp -r document_photos exports user@your-vps:/tmp/       # if they exist

ssh user@your-vps
sudo mv /tmp/knowledge_library.db /var/lib/knowledge-library/
sudo mv /tmp/document_photos /tmp/exports /var/lib/knowledge-library/ 2>/dev/null || true
sudo chown -R telegrambot:telegrambot /var/lib/knowledge-library
```

The schema is upgraded in place on first start. Existing documents, photos and
reminders are preserved.

### 6. Install the systemd service

```bash
sudo cp /opt/knowledge-library/deploy/knowledge-library.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now knowledge-library
sudo systemctl status knowledge-library
```

Follow the logs:

```bash
sudo journalctl -u knowledge-library -f
```

The unit runs the bot with `python main.py`, restarts automatically after a crash,
and keeps `/opt/knowledge-library` read-only while allowing writes only to
`/var/lib/knowledge-library`. If you use different paths or a different service
user, edit the unit before installing it.

### Updating the deployment

```bash
sudo systemctl stop knowledge-library
# copy the new source, then:
sudo -u telegrambot /opt/knowledge-library/venv/bin/pip install \
  -r /opt/knowledge-library/requirements.txt
sudo systemctl start knowledge-library
```

## Persistent storage and reminders

- SQLite lives at `$DATA_DIR/knowledge_library.db`.
- Uploaded photos live at `$DATA_DIR/document_photos/`.
- Generated PDFs live at `$DATA_DIR/exports/`.
- Reminders are stored in the `reminders` table in SQLite. On startup the bot
  restores every undelivered reminder from the database. A reminder whose time
  passed while the bot was offline is delivered shortly after startup instead of
  being lost.
- Reminder dates and times use `TIMEZONE` (`Africa/Addis_Ababa` by default), not
  the VPS clock timezone. The server may run on UTC.

## Backups

`deploy/backup.sh` creates a consistent SQLite backup with Python's online backup
API and archives photos/exports:

```bash
sudo -u telegrambot chmod +x /opt/knowledge-library/deploy/backup.sh
sudo -u telegrambot /opt/knowledge-library/deploy/backup.sh
```

Default layout:

- `/opt/knowledge-library/backups/knowledge_library-YYYYMMDD-HHMMSS.db`
- `/opt/knowledge-library/backups/files-YYYYMMDD-HHMMSS.tar.gz`

Schedule a daily run as `telegrambot`:

```bash
sudo crontab -u telegrambot -e
```

```cron
0 3 * * * /opt/knowledge-library/deploy/backup.sh >> /opt/knowledge-library/backups/backup.log 2>&1
```

Backups older than `RETENTION_DAYS` (default 14) are removed automatically.

If your `DATA_DIR` or `APP_DIR` differs from the defaults, pass it explicitly to
the script (also in the cron line), for example:

```bash
DATA_DIR=/srv/knowledge-library /opt/knowledge-library/deploy/backup.sh
```

### Restore

Stop the bot, restore the database and files, then start it:

```bash
sudo systemctl stop knowledge-library

sudo cp /opt/knowledge-library/backups/knowledge_library-YYYYMMDD-HHMMSS.db \
  /var/lib/knowledge-library/knowledge_library.db
sudo tar -xzf /opt/knowledge-library/backups/files-YYYYMMDD-HHMMSS.tar.gz \
  -C /var/lib/knowledge-library

sudo chown -R telegrambot:telegrambot /var/lib/knowledge-library
sudo systemctl start knowledge-library
```

Test a restore on a copy before relying on it. Keep at least one backup outside
the VPS (for example, download it periodically).

## Security

- The bot token lives only in `.env` or the service environment. Never commit it.
- `.gitignore` excludes `.env`, the database, photos, exports and backups.
- `ALLOWED_USER_IDS` rejects every other Telegram account with
  “This bot is private.”
- Keep `.env` at mode `600`, owned by `telegrambot`.
- If a token is ever exposed, revoke it with [@BotFather](https://t.me/BotFather)
  and update `.env`.
- Restrict SSH access to the VPS and keep the operating system updated.

## Testing

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

The tests drive realistic Telegram update payloads through the same
`Application`/handler routing used by `main.py`, with a recording bot (no network),
a temporary database, and no production data. `pypdf` is test-only.

## Error handling

- Handler exceptions are caught by the application error handler and logged with
  tracebacks; the bot keeps running.
- Reminder delivery errors are logged from the scheduler thread and remain
  undelivered in SQLite, so they are retried after a restart.
- PDF generation failures are logged and reported to the user without terminating
  the bot.
- `systemd` restarts the process automatically if it exits unexpectedly.

## Project structure

```
main.py                     # Bot setup, /start, handler registration, error logging
config.py                   # Environment, persistent paths, allow-list, timezone
security.py                 # Private-bot access control decorator
requirements.txt            # Runtime dependencies
requirements-dev.txt        # Test dependencies
README.md                   # This file
.env                        # Secrets (not committed)
.env.example                # Configuration template
database.py                 # SQLite layer (documents, sections, photos, reminders)
document_parser.py          # The only document-input parser (@ / # / *)
pdf_generator.py            # Reportlab PDF generation
handlers/__init__.py        # Handler package
handlers/notes.py           # Document builder, library, view, edit, delete, photos
handlers/reminders.py       # /remind, persistent scheduler, timezone handling
handlers/export.py          # /export document PDF
deploy/knowledge-library.service  # systemd unit
deploy/backup.sh            # database + files backup script
tests/                      # Parser unit tests + end-to-end handler tests
```

## Data migration

On first startup, legacy notes from the previous 12-category schema are
automatically migrated into the new 7-category document structure. No data is
deleted during migration.

Documents written while the symbol parser was still incomplete are repaired in
place on startup: a raw `@...` title is re-parsed into real title/subtitle/
paragraph blocks, and a paragraph marker left inside a subtitle (`* Qwen...`)
is split back out. Section ids and photo attachments are preserved, and the
repair is idempotent.
