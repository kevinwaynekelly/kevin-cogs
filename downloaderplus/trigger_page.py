"""Private fragment credentials stay out of HTTP URLs, history and referrers."""

import hashlib
import hmac
from base64 import b64encode
from urllib.parse import urlsplit


def trigger_token(secret):
    return hmac.new(
        secret.encode(), b"DownloaderPlus manual update trigger v1", hashlib.sha256
    ).hexdigest()


def trigger_url(base, port, secret):
    base = base or f"http://10.10.1.200:{port}"
    try:
        parsed = urlsplit(base)
        valid = (
            len(base) <= 500
            and not any(char.isspace() or ord(char) < 32 for char in base)
            and parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Use a root HTTP/HTTPS address without a path, login, query or fragment.")
    return base.rstrip("/") + "/update#token=" + trigger_token(secret)


SCRIPT = """
(async () => {
  'use strict';
  const state = document.getElementById('state');
  const token = new URLSearchParams(location.hash.slice(1)).get('token') || '';
  history.replaceState(null, '', location.pathname);
  if (!/^[0-9a-f]{64}$/.test(token)) {
    state.textContent = 'Open the private link sent by the bot. This page alone does not start an update.';
    return;
  }
  if (document.prerendering) {
    await new Promise(resolve => document.addEventListener('prerenderingchange', resolve, {once: true}));
  }
  const headers = {Authorization: 'Bearer ' + token};
  try {
    const response = await fetch('/update', {method: 'POST', headers, cache: 'no-store', credentials: 'omit'});
    if (!response.ok) throw new Error('The update request was refused (' + response.status + '). Check the webhook status in Discord.');
    state.textContent = 'Update queued. Results will also appear in the configured Discord channel.';
    for (let tries = 0; tries < 60; tries++) {
      await new Promise(resolve => setTimeout(resolve, 2000));
      const check = await fetch('/update/status', {headers, cache: 'no-store', credentials: 'omit'});
      if (!check.ok) throw new Error('Status is unavailable. Check the result channel in Discord.');
      const data = await check.json();
      if (data.pending) { state.textContent = 'Update queued. Waiting for the next update pass.'; continue; }
      if (data.result.status) state.textContent = data.result.status + ': ' + data.result.detail;
      if (['complete', 'failed'].includes(data.result.status)) return;
    }
    state.textContent = 'The update is taking longer. Follow its progress in the Discord result channel.';
  } catch (error) {
    state.textContent = error.message + ' No automatic retry was sent.';
  }
})();
"""

PAGE = (
    """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kevin's Cogs · Update</title>
<style>body{margin:0;background:#111320;color:#edf0ff;font:17px system-ui;display:grid;min-height:100vh;place-items:center}
main{margin:24px;padding:30px;max-width:560px;background:#1b1e30;border-radius:18px;border-top:4px solid #818cf8}
h1{font-size:26px}p{line-height:1.6}.muted{color:#b9c0d8;font-size:14px}</style>
<main><p class="muted">KEVIN'S COGS</p><h1>Update repositories and cogs</h1>
<p id="state" role="status">Preparing the update request…</p>
<p class="muted">Pinned cogs stay pinned. Changed loaded cogs reload automatically.</p>
<noscript>JavaScript is required for this private link. Use !updateall in Discord instead.</noscript></main>
<script>"""
    + SCRIPT
    + "</script></html>"
)

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'none'; connect-src 'self'; style-src 'unsafe-inline'; script-src 'sha256-"
    + b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
    + "'",
}
