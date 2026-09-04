"""Ports the integration tests bind. AI_PHONE_TEST_SIP_PORT moves everything so the suite can run beside a live daemon
(the daemon under test listens on SIP_PORT, the pjsua peer on PEER_PORT, RTP in its own range)."""
import os

SIP_PORT = int(os.environ.get("AI_PHONE_TEST_SIP_PORT", "5060"))
PEER_PORT = SIP_PORT + 2
RTP_MIN = 10000 + (SIP_PORT - 5060) * 100  # stride = the range width, so two runs never share RTP ports
RTP_MAX = RTP_MIN + 100
SIP_CONFIG = f"sip: {{bind: {{udp_port: {SIP_PORT}, tcp_port: {SIP_PORT}, tls_port: {SIP_PORT + 1}}}}}\nrtp: {{port_min: {RTP_MIN}, port_max: {RTP_MAX}}}\n"


def config():
    """A Config with this run's ports applied (for tests that build Telephony directly)."""
    from ai_phone.config import Config

    c = Config()
    c.sip.bind.udp_port, c.sip.bind.tcp_port, c.sip.bind.tls_port = SIP_PORT, SIP_PORT, SIP_PORT + 1
    c.rtp.port_min, c.rtp.port_max = RTP_MIN, RTP_MAX
    return c
