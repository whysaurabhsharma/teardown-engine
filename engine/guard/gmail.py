"""Guard's Gmail client: drafts only (docs/PLAN.md Steps 51 and 54;
protocol/CONDUCT.md Part 3, "A human gate at send").

    from engine.guard import gmail
    draft_id = gmail.create_draft(to, subject, body_text)

The one public function puts a plain-text message in the user's Gmail drafts
folder through `users.drafts.create` and returns the draft id. There is no way
here to send, read, list, change or delete mail: no code path in the engine
sends email, and a person reads and sends every draft by hand. The OAuth scope
is exactly `gmail.compose` (SCOPE), the narrowest scope that allows
drafts.create.

Credentials:
  client  the OAuth client of type Desktop app saved from Google Cloud Console,
          data/gmail_client.json ($TE_GMAIL_CLIENT or the `client_file` argument)
  token   the authorised-user token cached after the first consent,
          data/gmail_token.json ($TE_GMAIL_TOKEN or `token_file`); refreshed when
          expired. A token that does not list exactly SCOPE is refused: delete
          it and authorise again.
The first run opens the browser for consent (google-auth-oauthlib's local
server flow). Both files live under data/ and are gitignored; nothing here
prints or logs their contents, the recipient, the subject or the body, and no
error message carries them.

The message is one text/plain part: one recipient, no attachment, no HTML (so
no tracking pixel). Only engine/guard/ touches the network, so the drafter in
engine/outreach/ calls this and never the Gmail API itself.
"""

from __future__ import annotations

import base64
import json
import os
import re
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from typing import Any

SCOPE = "https://www.googleapis.com/auth/gmail.compose"
_SCOPES = [SCOPE]
CLIENT_ENV_VAR = "TE_GMAIL_CLIENT"
TOKEN_ENV_VAR = "TE_GMAIL_TOKEN"
_DEFAULT_CLIENT = Path("data") / "gmail_client.json"
_DEFAULT_TOKEN = Path("data") / "gmail_token.json"

_ADDRESS = re.compile(r"^[^\s@,;<>\"]+@[^\s@,;<>\"]+\.[^\s@,;<>\"]+$")

__all__ = ["SCOPE", "GmailError", "create_draft"]


class GmailError(RuntimeError):
    """A draft could not be made. The message never carries the recipient, the
    subject, the body or a credential."""


def _client_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get(CLIENT_ENV_VAR)
    return Path(env) if env else _DEFAULT_CLIENT


def _token_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get(TOKEN_ENV_VAR)
    return Path(env) if env else _DEFAULT_TOKEN


def _token_scopes_ok(path: Path) -> bool:
    """The cached token lists SCOPE and nothing else."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    scopes = data.get("scopes") if isinstance(data, dict) else None
    if isinstance(scopes, str):
        scopes = scopes.split()
    return isinstance(scopes, list) and sorted(set(scopes)) == _SCOPES


def _credentials(client: Path, token: Path) -> Any:
    """Cached credentials, refreshed when expired; the consent flow on first use."""
    from google.auth.exceptions import GoogleAuthError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token.exists():
        if not _token_scopes_ok(token):
            raise GmailError(f"{token} is not a token for exactly the gmail.compose scope; "
                             "delete it and authorise again")
        creds = Credentials.from_authorized_user_file(str(token), _SCOPES)
    if creds is not None and creds.valid:
        return creds
    try:
        if creds is not None and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not client.exists():
                raise GmailError(f"no OAuth client file at {client}; create a Desktop app "
                                 "client in Google Cloud Console and save its JSON there "
                                 "(docs/PLAN.md Step 51)")
            flow = InstalledAppFlow.from_client_secrets_file(str(client), _SCOPES)
            creds = flow.run_local_server(port=0)
    except GoogleAuthError as e:
        raise GmailError(f"Gmail authorisation failed ({type(e).__name__}); delete {token} "
                         "and authorise again") from None
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _build_service(creds: Any) -> Any:
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _mime(to: str, subject: str, body_text: str) -> EmailMessage:
    """One text/plain part, one recipient, no attachment."""
    if not _ADDRESS.match(to.strip()):
        raise GmailError("the recipient is not one plain address")
    if "\n" in subject or "\r" in subject or not subject.strip():
        raise GmailError("the subject must be one non-blank line")
    if not body_text.strip():
        raise GmailError("the body is blank")
    msg = EmailMessage(policy=SMTP)
    msg["To"] = to.strip()
    msg["Subject"] = subject.strip()
    msg.set_content(body_text, subtype="plain", charset="utf-8")
    return msg


def create_draft(to: str, subject: str, body_text: str, *,
                 client_file: str | os.PathLike[str] | None = None,
                 token_file: str | os.PathLike[str] | None = None) -> str:
    """Put one plain-text message in the Gmail drafts folder; return the draft id.
    Nothing is sent."""
    msg = _mime(to, subject, body_text)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    service = _build_service(_credentials(_client_path(client_file), _token_path(token_file)))
    try:
        resp = (service.users().drafts()
                .create(userId="me", body={"message": {"raw": raw}}).execute())
    except Exception as e:  # the client raises HttpError and transport errors
        status = getattr(getattr(e, "resp", None), "status", None)
        raise GmailError(f"users.drafts.create failed ({type(e).__name__}"
                         f"{f', HTTP {status}' if status else ''})") from None
    draft_id = resp.get("id") if isinstance(resp, dict) else None
    if not isinstance(draft_id, str) or not draft_id:
        raise GmailError("users.drafts.create returned no draft id")
    return draft_id
