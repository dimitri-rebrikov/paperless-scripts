# paperless-scripts

Helper scripts for a paperless-ngx instance that feeds [paperless-gpt](https://github.com/icereed/paperless-gpt).

## `post_consume.py`

A post-consume hook for `PAPERLESS_POST_CONSUME_SCRIPT`. For every consumed document it

- always adds the metadata trigger tag (`paperless-gpt-auto`), and
- adds the OCR trigger tag (`paperless-gpt-ocr-auto`) **only when OCR is safe**.

Everything else is paperless-gpt's job: the metadata run replaces the trigger tag with
`paperless-gpt-auto-complete`, the OCR run with `paperless-gpt-ocr-complete`, and repeated OCR
failures end in `paperless-gpt-failed`.

### Why there is a page gate

paperless-gpt's `OCR_LIMIT_PAGES` does **not** protect your document text. Its page-limit check
only guards the searchable-PDF step; the OCR result is assigned to the document's `content`
regardless. With `OCR_LIMIT_PAGES=5`, a 20-page scan gets its content replaced by pages 1-5, the
trigger tag is removed and `paperless-gpt-ocr-complete` is added: the text is gone and nothing
retries it.

So the gate lives here instead. The rule (`ocr_gate()`, unit-tested):

| document | pages used | OCR queued |
| --- | --- | --- |
| `application/pdf` | reported `page_count` | only if known and `<= OCR_LIMIT_PAGES` |
| `image/*` | 1 | yes - a single image cannot exceed a limit of 1 or more |
| anything else | - | no (`text/plain`, office formats, ...) |
| any, when `OCR_LIMIT_PAGES=0` | - | yes - paperless-gpt has no limit, so nothing can be truncated |

Multi-page images (TIFF) are treated as one page; if you ingest those, set `OCR_LIMIT_PAGES=0` or
handle them another way.

### Requirements

- paperless-ngx 3.x (REST API v10)
- [`uv`](https://docs.astral.sh/uv/) available to the user running the consumer service - the
  shebang is `uv run --quiet --script`, so no virtualenv is needed
- an API token

### Install

```sh
sudo install -d -m 755 /opt/paperless-scripts
sudo install -m 755 post_consume.py /opt/paperless-scripts/
sudo install -m 644 post_consume.py.lock /opt/paperless-scripts/
sudo install -m 600 .env.example /opt/paperless-scripts/.env
sudo "$EDITOR" /opt/paperless-scripts/.env     # set PAPERLESS_API_TOKEN
```

`post_consume.py.lock` is optional but recommended: `uv run --script` reuses it, which keeps the
dependency resolution reproducible.

Deploy from a clone rather than by copying a Windows working file: the script is started through its
shebang, and a `\r` on that line makes it unrunnable (`env: 'uv': No such file or directory`). Line
endings are pinned to LF by `.gitattributes`; if a copy looks suspicious, check it with
`file post_consume.py` and strip the CRs with `dos2unix post_consume.py`.

### Token

My Profile -> circular arrow button (or `POST /api/token/`). The token's user needs permission to
change the document - an admin token always works.

### Configuration

All of it lives in `/opt/paperless-scripts/.env`. See `.env.example` for the annotated list:

| variable | default | notes |
| --- | --- | --- |
| `PAPERLESS_API_URL` | `http://localhost:8000` | scheme is mandatory; use `localhost`, not `127.0.0.1` |
| `PAPERLESS_API_TOKEN` | - (required) | |
| `API_TIMEOUT` | `15` | seconds; bounds a hung API so it cannot stall the consumer |
| `AUTO_TAG` | `paperless-gpt-auto` | must match paperless-gpt's `AUTO_TAG` |
| `OCR_AUTO_TAG` | `paperless-gpt-ocr-auto` | must match paperless-gpt's `AUTO_OCR_TAG` |
| `OCR_LIMIT_PAGES` | - (required) | paperless-gpt's `OCR_LIMIT_PAGES`; `0` = no limit |

Only `PAPERLESS_API_TOKEN` and `OCR_LIMIT_PAGES` are required. The other keys can be left out of
`.env` entirely - the defaults above are defined in `post_consume.py`, and `.env.example` lists them
commented out so that the script stays the single source of truth for them.

`DOCUMENT_ID` comes from paperless-ngx itself, so it is not set here.

### Wiring it up

`/opt/paperless/paperless.conf` gets exactly one line:

```ini
PAPERLESS_POST_CONSUME_SCRIPT=/opt/paperless-scripts/post_consume.py
```

then:

```sh
sudo systemctl restart paperless-consumer
```

Both trigger tags are created automatically when missing (olive green `#6B8E23`, no matching rules).
Tags that already exist are left untouched, including their colour - change it in the UI if you want
the newer colour on a tag you created earlier.

### Keep in sync with paperless-gpt

These are paperless-gpt's settings, mirrored here because the two services cannot see each other:

- **`OCR_LIMIT_PAGES`** must never be *higher* than paperless-gpt's. Lower is safe (some documents
  simply are not queued for OCR); higher means truncated text.
- If you ever switch paperless-gpt to `OCR_PROCESS_MODE=whole_pdf`, its limit no longer applies -
  set this one to `0` as well.
- **`AUTO_TAG` / `OCR_AUTO_TAG`** must match paperless-gpt's tag names. A mismatch is silent: the
  tags are added, but nothing consumes them.

### Environment precedence

`.env` is read by pydantic-settings, whose priority is **process environment -> `.env` -> defaults**.
paperless-ngx passes the consumer's *entire* environment to the hook (`script_env = os.environ.copy()`),
which includes every variable from `paperless.conf` and the systemd unit, so keeping the script's
variables out of those two files avoids a stale value silently shadowing `.env`. To check the
environment the hook actually gets:

```sh
tr '\0' '\n' < /proc/$(pgrep -f document_consumer | head -1)/environ | grep -iE 'ocr_limit|auto_tag|paperless_api'
```

A key that is present but empty (`AUTO_TAG=`) is an error, not a fallback to the default - delete the
key to get the default. The hook logs the values it used (`ocr_limit=...`, `tags_added=...`, `endpoint=...`)
on every run, so `journalctl` shows the configuration in force without guesswork.

### First run (pre-warm)

The first execution resolves and caches `pypaperless`, `httpx` and `pydantic` for the user that
runs it. Do that once as the service user, so no consume task pays for the download:

```sh
sudo -u <consumer-user> DOCUMENT_ID=<some doc id> /opt/paperless-scripts/post_consume.py --dry-run
```

`--dry-run` reads the document and reports the decision without creating tags or writing anything.

### Manual run

```sh
DOCUMENT_ID=42 /opt/paperless-scripts/post_consume.py --dry-run
```

### Output and failure behaviour

paperless-ngx captures both streams and relays them through its own logger **after the script has exited**:
stdout lines at INFO and stderr lines at WARNING, each block introduced by `post_consume.py stdout:` /
`post_consume.py stderr:`, followed by a `post_consume.py exited <code>` line. The script therefore prints
plain lines without log-level labels - the level and the stream come from the relay, not from us.

On success there is one line, for example:

```
post_consume.py stdout:
post_consume: document=42 endpoint=http://localhost:8000 mime=application/pdf pages=2 ocr_limit=5 ocr_queued=yes reason='2 pages fit OCR_LIMIT_PAGES=5' tags_added=['paperless-gpt-auto', 'paperless-gpt-ocr-auto'] paperless=3.0.1 api=10 dry_run=no
```

On failure there is a `post_consume: failed: ...` line on stderr, a traceback when the error was unexpected,
and a non-zero exit code. Because the relayed stderr is logged at WARNING, a failed hook appears as warnings
plus `post_consume.py exited 1` - the task status in the UI is the authoritative signal. Look at:

```sh
journalctl -u paperless-consumer -n 50
```

### Troubleshooting

| symptom | cause |
| --- | --- |
| task FAILED, `401`/`403` in the log | wrong token, or the token's user cannot change the document |
| task FAILED, `environment file ... not found` | `.env` missing next to the script |
| task FAILED, `uv: command not found` | `uv` not on the service user's `PATH` |
| `ocr_queued=no reason='page count unknown'` | paperless-ngx reported no page count for a PDF, so it is not queued - safe by design |
| `ocr_queued=no reason='... exceed OCR_LIMIT_PAGES=5'` | working as intended; raise the limit in paperless-gpt and here if you accept the cost |
| tags appear, paperless-gpt does nothing | tag names no longer match paperless-gpt's configuration |
| first consume very slow | pre-warm was not done for the service user |

### Development

```sh
# the module imports pydantic-settings, hence the second --with
uv run --python 3.13 --with pytest --with 'pydantic-settings>=2.15' pytest -q test_post_consume.py
uv lock --script post_consume.py                      # after dependency changes
```

## License

MIT - see `LICENSE`.
