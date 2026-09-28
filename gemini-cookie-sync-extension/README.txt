Gemini Cookie Sync v1.3

Purpose:
- Read cookies for the current Google/Gemini session.
- Extract the XSRF token named SNlM0e from the Gemini page.
- Extract gemini_bl from cfb2h or from page requests when available.
- Push the auth payload straight to your local Gemini-API server (server.py),
  which saves `gemini-auth.json` and re-authenticates automatically.
- This is now the ONLY cookie channel: server.py no longer reads cookies from
  disk (Chrome App-Bound Encryption makes direct reading impossible).

Installation (ONE TIME per profile):
1. Open `chrome://extensions`
2. Enable Developer mode
3. Click Load unpacked
4. Select this folder
5. Open `https://gemini.google.com/app`, sign in, and refresh the page
6. Close Chrome and start the server - that is all

There is nothing to configure: the extension asks the server for its URL and
API key via `GET /v1/auth/bootstrap`, so a freshly cloned profile syncs with no
popup interaction at all. The popup is only needed to inspect the session, force
an instant sync, or export `gemini-auth.json` by hand.

Auto-sync:
- On install and on every Chrome start the extension fetches the server URL and
  API key, so rotating the key in config.json needs no storage cleanup.
- While Chrome is running, the extension keeps the server authenticated:
  - an alarm checks the server /health every ~1 minute and pushes fresh cookies
    when the server is unauthenticated or the last push is older than 3 minutes;
  - it also pushes promptly (debounced ~15-20s) right after Google rotates the
    cookies on google.com or a gemini.google.com tab finishes loading;
  - if no Gemini tab is open, it opens a hidden inactive one, collects the
    session, pushes it, and closes the tab again. No visible window or clicking
    required (Chrome itself must be running, in the background if you like).
  - a 401 is not counted as a hard failure: the extension re-reads the API key
    from the server and retries once.
- With Chrome-only cookies this is the reliable automation path: modern Chrome
  encrypts its cookie database (App-Bound Encryption), so only Chrome itself -
  through this extension - can decrypt them. Nobody else, including the local
  server, can read them without admin rights.
- Consecutive failures (e.g. server stopped) pause auto-sync; a popup Sync
  resets it.

Server counterpart:
- `GET /v1/auth/bootstrap` returns `{base_url, api_key, auto_sync}` and is
  answered only to requests whose `Origin` is `chrome-extension://...`. Disable
  it in config.json with `"extension": {"bootstrap": false}` if you prefer to
  type the key in the popup by hand.

Security:
The auth payload represents the real Google session and must be treated as
secret. Do not send it to any remote server (only 127.0.0.1/localhost is
supported), print it, or commit it to Git.