"""Typed loader for ~/.ai-phone/config.yaml (the Milestone 1 subset of architecture.md §8)."""
from __future__ import annotations

import os
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Literal

import yaml


class ConfigError(ValueError):
    """A config file problem; the message names the offending key and the fix."""


@dataclass
class BindConfig:
    addr: str = "0.0.0.0"
    port: int = 5060


@dataclass
class SipConfig:
    user: str = "ai"
    domain: str | None = None
    password: str | None = None
    password_env: str | None = None
    register_expiry_sec: int = 300
    bind: BindConfig = field(default_factory=BindConfig)
    public_addr: str | None = None
    trusted_peers: list[str] = field(default_factory=list)
    accept_invites_from: Literal["registered_trunk", "lan", "any"] = "registered_trunk"


@dataclass
class RtpConfig:
    port_min: int = 10000
    port_max: int = 10100


@dataclass
class ModelsConfig:
    tier: Literal["auto", "t9", "t4"] = "auto"
    home: str = "~/.ai-phone/models"


@dataclass
class ConversationConfig:
    greeting: str = "Hi, this is the house. What can I do for you?"
    system_prompt: str = "You are ai-phone, a local phone assistant. Answer in one short sentence."
    endpoint_silence_ms: int = 300
    bargein_min_speech_ms: int = 250
    bargein_playback_gain: float = 3.0
    history_turns: int = 6
    max_tokens: int = 120
    silence_prompt_sec: int = 8
    silence_hangup_sec: int = 8
    max_call_sec: int = 600
    rtp_timeout_sec: int = 10


@dataclass
class LogConfig:
    level: str = "info"
    pjsip_level: int = 3
    dir: str = "~/Library/Logs/ai-phone"


@dataclass
class Config:
    sip: SipConfig = field(default_factory=SipConfig)
    rtp: RtpConfig = field(default_factory=RtpConfig)
    codecs: list[str] = field(default_factory=lambda: ["PCMU", "PCMA"])
    models: ModelsConfig = field(default_factory=ModelsConfig)
    conversation: ConversationConfig = field(default_factory=ConversationConfig)
    log: LogConfig = field(default_factory=LogConfig)


def _check_type(value: Any, hint: Any, path: str) -> None:
    """Reject values that do not match the field's annotation, so a bad config fails at startup, not mid-call."""
    origin = typing.get_origin(hint)
    if origin is Literal:
        return  # validated separately with the allowed values
    if origin is typing.Union or (origin is not None and origin.__name__ == "UnionType"):
        errors = []
        for member in typing.get_args(hint):
            try:
                _check_type(value, member, path)
                return
            except ConfigError as e:
                errors.append(str(e))
        raise ConfigError(f"{path}: {errors[-1]}")
    if origin is list:
        if not isinstance(value, list):
            raise ConfigError(f"{path}: expected a list")
        (elem,) = typing.get_args(hint) or (Any,)
        for i, v in enumerate(value):
            _check_type(v, elem, f"{path}[{i}]")
        return
    if hint is type(None):
        if value is not None:
            raise ConfigError(f"{path}: expected null")
        return
    if hint is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{path}: expected true or false")
        return
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{path}: expected an integer, got {value!r}")
        return
    if hint is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{path}: expected a number, got {value!r}")
        return
    if hint is str:
        if not isinstance(value, str):
            raise ConfigError(f"{path}: expected a string, got {value!r}")
        return


def _build(cls: type, data: Any, path: str) -> Any:
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping")
    hints = typing.get_type_hints(cls)
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"{path}: unknown key(s): {', '.join(sorted(unknown))}")
    kwargs = {}
    for name in known:
        if name not in data:
            continue
        val = data[name]
        ftype = hints[name]
        if isinstance(ftype, type) and is_dataclass(ftype):
            val = _build(ftype, val, f"{path}.{name}")
        else:
            _check_type(val, ftype, f"{path}.{name}")
        kwargs[name] = val
    obj = cls(**kwargs)
    _validate(obj, path)
    return obj


def _validate(obj: Any, path: str) -> None:
    hints = typing.get_type_hints(type(obj))
    for f in fields(obj):
        ftype = hints[f.name]
        if typing.get_origin(ftype) is Literal:
            allowed = typing.get_args(ftype)
            if getattr(obj, f.name) not in allowed:
                raise ConfigError(f"{path}.{f.name}: must be one of {list(allowed)}")


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(Path(path).expanduser().read_text()) or {}
    cfg = _build(Config, raw, "config")
    if cfg.rtp.port_min >= cfg.rtp.port_max:
        raise ConfigError("config.rtp: port_min must be less than port_max")
    for c in cfg.codecs:
        if c not in ("PCMU", "PCMA"):
            raise ConfigError(f"config.codecs: only PCMU and PCMA are allowed, got {c}")
    if cfg.sip.password is None and cfg.sip.password_env and not os.environ.get(cfg.sip.password_env):
        raise ConfigError(f"config.sip.password_env: environment variable {cfg.sip.password_env} is not set")
    return cfg
