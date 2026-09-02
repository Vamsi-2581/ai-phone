"""Prove PJSIP starts headless with the null audio device and only G.711 enabled."""
import sys
import time

sys.path.insert(0, "native/pjsua2/build")
import pjsua2 as pj  # noqa: E402

ep = pj.Endpoint()
ep.libCreate()
cfg = pj.EpConfig()
cfg.uaConfig.threadCnt = 1
cfg.medConfig.clockRate = 16000
cfg.medConfig.sndClockRate = 16000
cfg.medConfig.audioFramePtime = 20
cfg.medConfig.ecTailLen = 0
cfg.logConfig.level = 3
ep.libInit(cfg)
ep.audDevManager().setNullDev()
tcfg = pj.TransportConfig()
tcfg.port = 5060
ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tcfg)
ep.libStart()
for ci in ep.codecEnum2():
    pri = 255 if ci.codecId.startswith(("PCMU/8000", "PCMA/8000")) else 0
    ep.codecSetPriority(ci.codecId, pri)
print("codecs:", [c.codecId for c in ep.codecEnum2() if c.priority > 0])
print("ByteVector from bytes:", len(pj.ByteVector(b"ab")))
time.sleep(1.0)
ep.libDestroy()
print("OK: headless PJSIP started and stopped with null audio device")
