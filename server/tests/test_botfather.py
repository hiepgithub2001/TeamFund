"""The @BotFather dialogue, against a scripted conversation (no network)."""
import asyncio

import pytest

from app.application.ports import BotFactoryError
from app.infrastructure.botfather import converse

TOKEN = "7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class Msg:
    def __init__(self, text):
        self.raw_text = text


class Conv:
    """Answers each message from a script keyed by what was sent."""

    def __init__(self, replies):
        self.replies, self.sent, self._next = replies, [], None

    async def send_message(self, text):
        self.sent.append(text)
        r = self.replies.get(text)
        self._next = r(text) if callable(r) else r or "OK"

    async def get_response(self):
        return Msg(self._next)


def script(**extra):
    base = {"/cancel": "No active command to cancel.",
            "/newbot": "Alright, a new bot. How are we going to call it? Please choose a name for your bot.",
            "FC · Team Fund": "Good. Now let's choose a username for your bot. It must end in `bot`."}
    return {**base, **extra}


def run(conv, usernames=("fc_fund_bot", "fc_tf_bot")):
    return asyncio.run(converse(conv, "FC · Team Fund", list(usernames)))


def test_tries_usernames_until_one_is_free():
    conv = Conv(script(fc_fund_bot="Sorry, this username is already taken. Please try something different.",
                       fc_tf_bot=f"Done! Congratulations on your new bot.\nUse this token to access the HTTP API:\n{TOKEN}\nKeep it secure"))
    bot = run(conv)
    assert (bot.token, bot.username) == (TOKEN, "fc_tf_bot")
    assert conv.sent == ["/cancel", "/newbot", "FC · Team Fund", "fc_fund_bot", "fc_tf_bot"]


def test_all_taken():
    conv = Conv(script(fc_fund_bot="Sorry, this username is already taken.", fc_tf_bot="Sorry, this username is invalid."))
    with pytest.raises(BotFactoryError) as e:
        run(conv)
    assert e.value.kind == "taken" and conv.sent[-1] == "/cancel"


@pytest.mark.parametrize("reply,kind", [
    ("That I cannot do. You come to me asking for more than 20 bots.", "limit"),
    ("Sorry, too many attempts. Please try again in 8 seconds.", "flood"),
    ("Something unexpected", "unavailable"),
])
def test_newbot_refused(reply, kind):
    with pytest.raises(BotFactoryError) as e:
        run(Conv(script(**{"/newbot": reply})))
    assert e.value.kind == kind
