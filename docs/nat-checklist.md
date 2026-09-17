# NAT and transport checklist (Milestone 2 gate)

Run on the user's own network, with the daemon registered to a real carrier line. Fill the Result column with the date and what you saw. The 24 h soak (`scripts/soak.sh`) covers the registered-time and recovery numbers; these are the per-transport and per-router checks around it.

| Check | How to run | Expected | Result (date) |
|---|---|---|---|
| UDP registration held 1 h | line `transport: udp`, `scripts/soak.sh 1` | `SOAK PASS`, 0 unexpected drops | |
| TCP registration held 1 h | line `transport: tcp`, restart, `scripts/soak.sh 1` | `SOAK PASS`; CRLF keepalives every `sip.tcp_keepalive_sec` (30 s) in a `tcpdump -i <iface> port 5060` (pick the interface carrying the registration: `tcpdump -D` lists them; `en0` on a typical Mac, often `eth0`/`wlan0` on Linux) | |
| TLS registration held 1 h | line `transport: tls` (registrar `sip:…;transport=tls`, port 5061), restart, `scripts/soak.sh 1` | `SOAK PASS`; certificate verified against the certifi bundle (no `verifyServer` warnings in the log) | |
| SIP ALG on the home router | register over UDP; inspect the daemon log for a Contact rewritten by the router (`contactRewriteUse` events) or one-way audio | if the router rewrites SIP, switch the line to `transport: tcp` or `tls` and re-check | |
| IP change (Wi-Fi → Ethernet, router reboot) | switch networks while registered | the line shows `degraded`; a restart re-registers (automatic handling is later work) | |
| DNS SRV resolution of the registrar | once with `sip.nameservers: []` (system resolvers) and once with explicit servers, e.g. `sip.nameservers: ["1.1.1.1", "8.8.8.8"]`; check the start-up log line for the registrar's resolved addresses | registration succeeds through the SRV-selected proxy in both runs | |
| One-way audio | after a call, `ai-phone status --last-call` and the call's turn records (`stt_text` non-empty proves inbound frames; `first_audible_ms` proves outbound frames reached PJSIP) | inbound and outbound both non-zero: every turn has a transcript and a first-audible time; rx overruns 0, tx underruns 0; both directions audible on the phone | |
| Inbound RTP behind this NAT | a call from a mobile; listen for the greeting and speak | audio both ways within the first second; if not, note the router model (CGNAT / symmetric NAT) and use `sip.public_addr` + port forwarding or a public host | |
| Induced transport drop | Wi-Fi off 30 s during the soak (twice) | each drop `degraded` → `registered` within 60 s in `scripts/soak_report.py` | |
