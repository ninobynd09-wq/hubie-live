#!/usr/bin/env python3
"""One-time setup, read-only chat: subscribes the Kick app to chat messages for a channel, using Kick's
documented app token flow. Needs KICK_CLIENT_ID and KICK_CLIENT_SECRET in the environment.
The webhook address itself is set on the app's page on Kick. Not run yet: there is no app."""
import json
import os
import urllib.parse
import urllib.request

BROADCASTER_USER_ID = int(os.environ.get("KICK_BROADCASTER_USER_ID", "1000926592"))   # hubielive
form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": os.environ["KICK_CLIENT_ID"],
                               "client_secret": os.environ["KICK_CLIENT_SECRET"]}).encode()
with urllib.request.urlopen(urllib.request.Request("https://id.kick.com/oauth/token", data=form), timeout=15) as r:
    token = json.loads(r.read())["access_token"]
body = json.dumps({"events": [{"name": "chat.message.sent", "version": 1}], "method": "webhook",
                   "broadcaster_user_id": BROADCASTER_USER_ID}).encode()
req = urllib.request.Request("https://api.kick.com/public/v1/events/subscriptions", data=body, method="POST",
                             headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=15) as r:
    print(r.status, r.read().decode())
