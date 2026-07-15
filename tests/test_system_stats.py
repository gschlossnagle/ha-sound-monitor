"""Tests for system_stats's pure parsing functions -- no psutil or
subprocess calls exercised here, matching the hardware-independent test
philosophy already used for event_detection.py."""

from system_stats import parse_throttled, parse_vcgencmd_value


class TestParseThrottled:
    def test_clean_state_is_not_under_voltage(self):
        assert parse_throttled("throttled=0x0\n") == {"under_voltage_now": False}

    def test_under_voltage_bit_set(self):
        assert parse_throttled("throttled=0x1\n") == {"under_voltage_now": True}

    def test_under_voltage_bit_set_among_others(self):
        # bit 0 (now) and bit 16 (since boot) both set
        assert parse_throttled("throttled=0x50001\n") == {"under_voltage_now": True}

    def test_other_bits_alone_are_not_under_voltage(self):
        # bit 2 (currently throttled) set, bit 0 clear
        assert parse_throttled("throttled=0x4\n") == {"under_voltage_now": False}

    def test_malformed_input_defaults_to_false(self):
        assert parse_throttled("garbage") == {"under_voltage_now": False}

    def test_empty_input_defaults_to_false(self):
        assert parse_throttled("") == {"under_voltage_now": False}


class TestParseVcgencmdValue:
    def test_parses_volts(self):
        assert parse_vcgencmd_value("volt=1.3500V\n") == 1.35

    def test_parses_temp(self):
        assert parse_vcgencmd_value("temp=43.3'C\n") == 43.3

    def test_malformed_input_returns_none(self):
        assert parse_vcgencmd_value("garbage") is None

    def test_empty_input_returns_none(self):
        assert parse_vcgencmd_value("") is None
