"""Self-contained, server-rendered HTML for the `/oauth/authorize` consent
screen -- docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §3.4.

Deliberately plain string templates, not a templating engine (Jinja2 isn't
a dependency here) -- every caller-influenced value (client_name,
redirect_uri, error messages) is run through `html.escape` before
interpolation, since this page renders values that ultimately originate
from a DCR registration request (`OAuthClient.client_name`) or query
string, both attacker-reachable inputs.
"""

from __future__ import annotations

import html
from urllib.parse import urlencode

_PAGE_STYLE = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #0b0d12; color: #e6e8eb; display: flex; align-items: center;
  justify-content: center; min-height: 100vh; margin: 0; }
.card { background: #14171f; border: 1px solid #262b36; border-radius: 12px;
  padding: 2rem; max-width: 400px; width: 90%; }
h1 { font-size: 1.1rem; margin: 0 0 1rem; }
p.detail { color: #9aa2b1; font-size: 0.9rem; margin: 0 0 1.5rem; }
input { width: 100%; box-sizing: border-box; padding: 0.6rem 0.75rem; margin-bottom: 0.75rem;
  border-radius: 6px; border: 1px solid #2e3441; background: #0b0d12; color: #e6e8eb; }
button, a.btn { display: block; width: 100%; box-sizing: border-box; padding: 0.6rem 0.75rem;
  border-radius: 6px; border: none; font-size: 0.95rem; cursor: pointer; margin-bottom: 0.6rem;
  text-align: center; text-decoration: none; }
button.primary, a.btn.primary { background: #4c7cf0; color: white; }
button.secondary { background: #262b36; color: #e6e8eb; }
.error { color: #f36a6a; font-size: 0.85rem; margin-bottom: 1rem; }
.divider { text-align: center; color: #565e6e; font-size: 0.8rem; margin: 1rem 0; }
"""


def render_page(*, title: str, body: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>{_PAGE_STYLE}</style></head>
<body><div class="card">{body}</div></body></html>"""


def render_error_page(*, message: str) -> str:
    return render_page(
        title="boxkite -- authorization error",
        body=f'<h1>Can\'t continue</h1><p class="error">{html.escape(message)}</p>',
    )


def render_login_page(
    *,
    client_name: str,
    authorize_query: str,
    github_enabled: bool,
    google_enabled: bool,
    error: str | None = None,
) -> str:
    """Login form (email+password) plus GitHub/Google buttons, shown when
    the browser has no valid `/oauth/*`-scoped session cookie yet. Submits
    to `POST /oauth/authorize/login`, carrying the original authorize
    query string through a hidden field so the flow can resume at
    `GET /oauth/authorize` once logged in."""
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    social_buttons = ""
    next_qs = urlencode({"next": f"/oauth/authorize?{authorize_query}"})
    if github_enabled:
        social_buttons += f'<a class="btn secondary" href="/v1/auth/github/start?{next_qs}">Sign in with GitHub</a>'
    if google_enabled:
        social_buttons += f'<a class="btn secondary" href="/v1/auth/google/start?{next_qs}">Sign in with Google</a>'
    divider = '<div class="divider">or</div>' if social_buttons else ""
    return render_page(
        title=f"Sign in to allow {client_name}",
        body=f"""
<h1>Sign in to continue</h1>
<p class="detail">{html.escape(client_name)} wants to connect to your boxkite account.</p>
{error_html}
<form method="post" action="/oauth/authorize/login">
  <input type="hidden" name="authorize_query" value="{html.escape(authorize_query)}">
  <input type="email" name="email" placeholder="Email" required>
  <input type="password" name="password" placeholder="Password" required>
  <button class="primary" type="submit">Sign in</button>
</form>
{divider}
{social_buttons}
""",
    )


def render_consent_page(*, client_name: str, account_email: str, authorize_query: str) -> str:
    """Allow/Deny screen, shown once the caller has a valid `/oauth/*`
    session (either just logged in, or already had a cookie from a prior
    approval in the same browser)."""
    return render_page(
        title=f"Allow {client_name}?",
        body=f"""
<h1>{html.escape(client_name)} wants to access your boxkite account</h1>
<p class="detail">Signed in as {html.escape(account_email)}. This grants the same access an API key already has: creating, using, and destroying sandboxes on your account.</p>
<form method="post" action="/oauth/authorize/decide">
  <input type="hidden" name="authorize_query" value="{html.escape(authorize_query)}">
  <button class="primary" type="submit" name="decision" value="allow">Allow</button>
  <button class="secondary" type="submit" name="decision" value="deny">Deny</button>
</form>
""",
    )
