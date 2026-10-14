"""Read only the dedicated Gmail label; never mark, delete or send mail."""
import email
import hmac
import imaplib
import json
import re
import secrets
import ssl
from email import policy
from email.utils import parseaddr

from bs4 import BeautifulSoup
from sqlalchemy import select

from .config import AgentError, fingerprint
from .db import Cursor, now


MAX_MAIL_BYTES = 128 * 1024


def parse_ifttt_mail(raw, token):
    if len(raw) > MAX_MAIL_BYTES:
        raise AgentError("IFTTT_MAIL_TOO_LARGE")
    message = email.message_from_bytes(raw, policy=policy.default)
    sender = parseaddr(str(message.get("From", "")))[1].casefold()
    expected = "[MAX-REDDIT " + token + "]"
    if sender != "action@ifttt.com" or not hmac.compare_digest(
            str(message.get("Subject", "")).strip().encode("utf-8"), expected.encode("utf-8")):
        raise AgentError("IFTTT_MAIL_UNTRUSTED")
    body = message.get_body(preferencelist=("plain", "html"))
    if not body or body.get_content_disposition() == "attachment":
        raise AgentError("IFTTT_MAIL_FORMAT_INVALID")
    try:
        text = body.get_content()
    except (LookupError, UnicodeError, ValueError) as exc:
        raise AgentError("IFTTT_MAIL_FORMAT_INVALID") from exc
    if body.get_content_type() == "text/html":
        text = BeautifulSoup(text, "html.parser").get_text("\n")
    # IFTTT can wrap the template in its own footer and HTML block elements.
    match = re.search(r"URL=([^\r\n]+)\s*\nTITLE=([^\r\n]+)\s*\nTEXT_BEGIN\s*\n(.*?)\nTEXT_END(?:\s|$)",
                      text.replace("\r\n", "\n"), re.DOTALL)
    if not match:
        raise AgentError("IFTTT_MAIL_FORMAT_INVALID")
    url, title, original = (x.strip() for x in match.groups())
    if any("{{" in value or "}}" in value for value in (url, title, original)):
        raise AgentError("IFTTT_INGREDIENT_NOT_EXPANDED")
    return {"url": url, "title": title, "original": original}


class RedditMail:
    def __init__(self, core):
        self.core = core
        self.path = core.settings.root / "data/reddit-mail.local.json"

    def read(self):
        if not self.path.exists():
            return {"enabled": False, "username": "", "app_password": "", "token": "",
                    "mailbox": "Reddit-IFTTT"}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (set(data) != {"enabled", "username", "app_password", "token", "mailbox"}
                    or type(data["enabled"]) is not bool
                    or any(not isinstance(data[k], str) for k in
                           ("username", "app_password", "token", "mailbox"))
                    or not re.fullmatch(r"Reddit-IFTTT", data["mailbox"])
                    or not re.fullmatch(r"[A-Za-z0-9_-]{32,100}", data["token"])):
                raise ValueError
            return data
        except (ValueError, TypeError, OSError) as exc:
            raise AgentError("IFTTT_MAIL_CONFIG_INVALID") from exc

    def status(self):
        config = self.read()
        with self.core.db.session() as s:
            cursor = s.get(Cursor, "reddit_ifttt_mail")
            return {"enabled": config["enabled"], "configured": bool(config["app_password"]),
                    "mailbox": config["mailbox"], "state": cursor.status if cursor else "not_configured",
                    "last_success": cursor.last_success if cursor else None,
                    "next_due": cursor.next_due if cursor else None}

    def configure(self, username, app_password, enabled):
        if not re.fullmatch(r"[A-Za-z0-9._%+\-]+@gmail\.com", username):
            raise AgentError("GMAIL_ADDRESS_REQUIRED")
        config = self.read()
        password = app_password.replace(" ", "")
        if password and not re.fullmatch(r"[A-Za-z]{16}", password):
            raise AgentError("GMAIL_APP_PASSWORD_REQUIRED")
        if username != config["username"] and not password:
            config["app_password"] = ""
        config.update(username=username, enabled=enabled,
                      token=config["token"] or secrets.token_urlsafe(32))
        if password:
            config["app_password"] = password
        if enabled and not config["app_password"]:
            raise AgentError("GMAIL_APP_PASSWORD_REQUIRED")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".next")
        temporary.touch(mode=0o600, exist_ok=True)
        temporary.chmod(0o600)
        temporary.write_text(json.dumps(config), encoding="utf-8")
        temporary.replace(self.path)
        with self.core.db.session.begin() as s:
            cursor = s.get(Cursor, "reddit_ifttt_mail")
            if cursor:
                cursor.next_due, cursor.status = 0, "configured" if enabled else "disabled"
            self.core.db.audit(s, "owner", "reddit_mail_configured", "gmail", enabled=enabled)
        return self.status()

    def template(self):
        config = self.read()
        if not config["token"]:
            raise AgentError("IFTTT_MAIL_SETUP_REQUIRED", 409)
        return {"subject": "[MAX-REDDIT " + config["token"] + "]",
                "body": "URL={{PostURL}}\nTITLE={{Title}}\nTEXT_BEGIN\n{{Content}}\nTEXT_END",
                "mailbox": config["mailbox"], "recipient": config["username"]}

    def poll(self, connector=None):
        config = self.read()
        if not config["enabled"]:
            return 0
        target_id = self.core.reddit_intake.options()["mail_target_id"]
        if not target_id:
            raise AgentError("IFTTT_TARGET_REQUIRED")
        with self.core.db.session() as s:
            self.core.reddit_intake._target(s, target_id)
        # A shared cursor lease prevents simultaneous API and worker polling.
        with self.core.db.session.begin() as s:
            cursor = s.scalar(select(Cursor).where(Cursor.id == "reddit_ifttt_mail").with_for_update())
            if not cursor:
                cursor = Cursor(id="reddit_ifttt_mail", data={})
                s.add(cursor)
                s.flush()
            if cursor.next_due > now():
                return 0
            cursor.next_due, cursor.status = now() + 600, "polling"
            saved = dict(cursor.data)
        accepted, client = 0, None
        try:
            client = connector() if connector else imaplib.IMAP4_SSL(
                "imap.gmail.com", 993, ssl_context=ssl.create_default_context(), timeout=20)
            client.login(config["username"], config["app_password"])
            status, _ = client.select('"' + config["mailbox"] + '"', readonly=True)
            if status != "OK":
                raise AgentError("GMAIL_LABEL_NOT_FOUND")
            validity = client.response("UIDVALIDITY")[1]
            if not validity or not validity[0]:
                raise AgentError("GMAIL_UIDVALIDITY_MISSING")
            binding = fingerprint({"username": config["username"], "mailbox": config["mailbox"],
                                   "validity": validity[0].decode("ascii")})
            last = saved.get("last_uid", 0) if saved.get("binding") == binding else 0
            status, result = client.uid("search", None, "UID", f"{last + 1}:*")
            if status != "OK":
                raise AgentError("GMAIL_READ_FAILED")
            ids = sorted(int(x) for x in (result[0] or b"").split() if int(x) > last)[:25]
            rejected = 0
            for ident in ids:
                # Limit the literal before parsing MIME; no attachments or remote images are fetched.
                status, values = client.uid("fetch", str(ident), f"(BODY.PEEK[]<0.{MAX_MAIL_BYTES + 1}>)")
                if status != "OK":
                    raise AgentError("GMAIL_READ_FAILED")
                raw = next((x[1] for x in values if isinstance(x, tuple) and isinstance(x[1], bytes)), None)
                if raw is None:
                    raise AgentError("GMAIL_READ_FAILED")
                try:
                    value = parse_ifttt_mail(raw, config["token"])
                    receipt = self.core.reddit_intake.submit(**value, target_id=target_id,
                                                            origin="ifttt", actor="ifttt_mail")
                    accepted += not receipt["duplicate"]
                except AgentError:
                    # Quarantine a malformed message by advancing this private cursor only.
                    # The original message remains untouched for owner inspection/reimport.
                    rejected += 1
                with self.core.db.session.begin() as s:
                    row = s.get(Cursor, "reddit_ifttt_mail", with_for_update=True)
                    row.data = {"binding": binding, "last_uid": ident}
            with self.core.db.session.begin() as s:
                row = s.get(Cursor, "reddit_ifttt_mail", with_for_update=True)
                row.status, row.last_success, row.next_due = (
                    "mail_needs_review" if rejected else "ok", now(), now() + 120)
                self.core.db.audit(s, "ifttt_mail", "reddit_mail_poll", "gmail",
                                   accepted=accepted, rejected=rejected)
            return accepted
        except Exception as exc:
            code = exc.code if isinstance(exc, AgentError) else "GMAIL_CONNECTION_OR_AUTH_FAILED"
            with self.core.db.session.begin() as s:
                row = s.get(Cursor, "reddit_ifttt_mail", with_for_update=True)
                # An auth failure requires owner intervention, not repeated credential attempts.
                row.status, row.next_due = code, now() + 365 * 86400
            raise AgentError(code, 503) from None
        finally:
            if client:
                try:
                    client.logout()
                except Exception:
                    client = None  # Logout failures must not expose transport credentials.
