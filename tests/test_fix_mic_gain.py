"""Tests for fix_mic_gain's card-resolution logic — pure text parsing, no hardware."""

import pytest

from fix_mic_gain import find_card_index

CARDS_TEXT = """\
 0 [ATR4697USB     ]: USB-Audio - ATR4697-USB
                      Conference USB microphone ATR4697-USB at usb-20980000.usb-1, full speed
 1 [vc4hdmi        ]: vc4-hdmi - vc4-hdmi
                      vc4-hdmi
"""

AMBIGUOUS_CARDS_TEXT = """\
 0 [USBMic0        ]: USB-Audio - Generic USB Audio
                      USB Audio Device at usb-0000:00:00.0-1, full speed
 1 [USBMic1        ]: USB-Audio - Generic USB Audio
                      USB Audio Device at usb-0000:00:00.0-2, full speed
"""


class TestFindCardIndex:
    def test_matches_short_id(self):
        assert find_card_index(CARDS_TEXT, "ATR4697") == 0

    def test_matches_long_name_on_continuation_line(self):
        assert find_card_index(CARDS_TEXT, "Conference USB microphone") == 0

    def test_is_case_insensitive(self):
        assert find_card_index(CARDS_TEXT, "atr4697") == 0

    def test_matches_other_card(self):
        assert find_card_index(CARDS_TEXT, "vc4-hdmi") == 1

    def test_no_match_raises(self):
        with pytest.raises(ValueError, match="No ALSA card matched"):
            find_card_index(CARDS_TEXT, "nonexistent")

    def test_ambiguous_match_raises(self):
        with pytest.raises(ValueError, match="matched multiple"):
            find_card_index(AMBIGUOUS_CARDS_TEXT, "USB Audio")
