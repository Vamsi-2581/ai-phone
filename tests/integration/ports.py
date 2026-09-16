"""Ports the integration tests bind. AI_PHONE_TEST_SIP_PORT (P below) moves everything, so the suite can run beside a
live daemon or beside another run of itself.

One run uses:
- SIP ports P to P+6:
  - P: the daemon under test, UDP and TCP; P+1: its TLS;
  - P+2: the pjsua peer (PEER_PORT);
  - P+4 and P+5: a second daemon and its TLS (SECOND_SIP_PORT, test_agent_modes);
  - P+4 and P+6: extra pjsua callers (test_busy_and_forbidden).
- RTP: the range rtp_range(P) for the daemon on P, and rtp_range(P + 4) for the second daemon. Each range is
  10000 + (port - 5060) * 100 up to 100 ports above that. The pjsua peers use pjsua's own RTP ports.

Runs at the same time must use values of P at least 10 apart (for example 5090 and 5100); closer runs share SIP or RTP
ports. P is never 5060, where a real daemon may listen."""
import os

SIP_PORT = int(os.environ.get("AI_PHONE_TEST_SIP_PORT", "5060"))
PEER_PORT = SIP_PORT + 2
SECOND_SIP_PORT = SIP_PORT + 4  # a second daemon (TLS on + 5); the pjsua peer uses SIP_PORT + 2


def rtp_range(port: int) -> tuple[int, int]:
    """The RTP ports of a daemon whose SIP port is `port`: 100 wide, one range per SIP port."""
    low = 10000 + (port - 5060) * 100
    return low, low + 100


RTP_MIN, RTP_MAX = rtp_range(SIP_PORT)


def sip_config(port: int) -> str:
    """Bind block for a daemon on `port` (UDP and TCP), TLS on port + 1, and the RTP range rtp_range(port)."""
    low, high = rtp_range(port)
    return (f"sip: {{bind: {{udp_port: {port}, tcp_port: {port}, tls_port: {port + 1}}}}}\n"
            f"rtp: {{port_min: {low}, port_max: {high}}}\n")


SIP_CONFIG = sip_config(SIP_PORT)


def config():
    """A Config with this run's ports applied (for tests that build Telephony directly)."""
    from ai_phone.config import Config

    c = Config()
    c.sip.bind.udp_port, c.sip.bind.tcp_port, c.sip.bind.tls_port = SIP_PORT, SIP_PORT, SIP_PORT + 1
    c.rtp.port_min, c.rtp.port_max = RTP_MIN, RTP_MAX
    return c
