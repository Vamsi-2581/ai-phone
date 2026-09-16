"""Who writes the replies on a call: ai-phone's own model (phone agent mode) or the calling AI (relay mode)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:
    from ai_phone.pipeline.turn import TurnController


class ReplySource(Protocol):
    relay: bool

    def on_heard(self, tc: "TurnController", text: str) -> None: ...


class ModelReplies:
    """Phone agent mode: the model answers every finished utterance."""
    relay = False

    def on_heard(self, tc: "TurnController", text: str) -> None:
        tc.generate()


class RelayReplies:
    """Relay mode: finished utterances go to the calling AI (`listen`); its `say` text is spoken."""
    relay = True

    def __init__(self, deliver: Callable[[str], None]):
        self.deliver = deliver

    def on_heard(self, tc: "TurnController", text: str) -> None:
        self.deliver(text)


@dataclass(frozen=True)
class Phrase:
    text: str
    frames: tuple[bytes, ...]


@dataclass(frozen=True)
class RelayPhrases:
    filler: Phrase
    still_here: Phrase
    goodbye: Phrase
