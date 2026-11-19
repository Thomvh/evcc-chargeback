import smtplib

import pytest

from evcc_chargeback import mailer
from evcc_chargeback.mailer import NotSent, PartialDelivery


@pytest.fixture
def server(monkeypatch):
    """Replace smtplib.SMTP and SMTP_SSL with a fake server; `fail` sets the step that raises and its error."""
    state = {"fail": {}, "refused": {}, "calls": []}

    class FakeSMTP:
        def __init__(self, host, port, context=None, timeout=None):
            state["calls"].append(type(self).__name__)
            self.step("connect")

        def step(self, name):
            state["calls"].append(name)
            if name in state["fail"]:
                raise state["fail"][name]

        def starttls(self, context=None):
            self.step("starttls")

        def login(self, user, password):
            self.step("login")

        def send_message(self, message):
            self.step("send_message")
            return state["refused"]

        def quit(self):
            self.step("quit")

    class FakeSMTP_SSL(FakeSMTP):
        pass

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP_SSL)
    return state


def send(port=587):
    mailer.send("subject", "text", host="smtp.invalid", port=port, user="u", password="p", sender="a@example.org",
                recipients=["b@example.org", "c@example.org"])


def test_success_with_starttls_on_port_587(server):
    send()
    assert server["calls"] == ["FakeSMTP", "connect", "starttls", "login", "send_message", "quit"]


def test_port_465_uses_tls_from_the_start(server):
    send(465)
    assert server["calls"][0] == "FakeSMTP_SSL" and "starttls" not in server["calls"]


@pytest.mark.parametrize("step, error", [
    ("connect", ConnectionRefusedError("refused")),
    ("starttls", smtplib.SMTPNotSupportedError("no STARTTLS")),
    ("login", smtplib.SMTPAuthenticationError(535, b"bad login")),
])
def test_failure_before_the_send_is_not_sent(server, step, error):
    server["fail"][step] = error
    with pytest.raises(NotSent):
        send()
    assert "send_message" not in server["calls"]


@pytest.mark.parametrize("error", [
    smtplib.SMTPRecipientsRefused({"b@example.org": (550, b"no such user")}),
    smtplib.SMTPSenderRefused(553, b"not your address", "a@example.org"),
    smtplib.SMTPDataError(554, b"message refused"),
])
def test_refusal_of_the_message_is_not_sent(server, error):
    server["fail"]["send_message"] = error
    with pytest.raises(NotSent):
        send()


def test_refused_recipients_are_a_partial_delivery(server):
    server["refused"] = {"c@example.org": (550, b"no such user")}
    with pytest.raises(PartialDelivery, match="c@example.org"):
        send()


def test_timeout_during_the_send_is_not_classified(server):
    server["fail"]["send_message"] = TimeoutError("no answer after DATA")
    with pytest.raises(TimeoutError):
        send()


def test_failed_quit_after_a_send_is_a_success(server):
    server["fail"]["quit"] = smtplib.SMTPServerDisconnected("gone")
    send()
    assert server["calls"][-2:] == ["send_message", "quit"]
