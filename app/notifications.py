import json
import urllib.error
import urllib.parse
import urllib.request
from html import escape
from http.client import HTTPException
from typing import Protocol


class NotificationChannel(Protocol):
    def send(self, recipient_email: str, message: str, action_url: str = "") -> None:
        """Deliver a notification to the recipient or raise on failure."""
        ...


class SlackBotChannel:
    def __init__(self, token: str) -> None:
        self._token = token

    def send(self, recipient_email: str, message: str, action_url: str = "") -> None:
        user = self._call("users.lookupByEmail", query={"email": recipient_email})["user"]
        dm = self._call("conversations.open", payload={"users": user["id"]})["channel"]

        text = escape(message, quote=False)
        if action_url:
            text += f"\n<{escape(action_url, quote=False)}|打开审批后台>"
        self._call("chat.postMessage", payload={
            "channel": dm["id"],
            "text": text,
            "parse": "none",
            "unfurl_links": False,
            "unfurl_media": False,
        })

    def _call(self, method, *, query=None, payload=None):
        url = f"https://slack.com/api/{method}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Authorization": f"Bearer {self._token}"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            error.close()
            raise RuntimeError(f"Slack {method}: HTTP {error.code}") from error
        except (OSError, HTTPException, ValueError) as error:
            raise RuntimeError(f"Slack {method}: {type(error).__name__}") from error
        if not result.get("ok"):
            raise RuntimeError(f"Slack {method}: {result.get('error', 'unknown_error')}")
        return result
