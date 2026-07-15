"""Tests for calibration — pure arithmetic, no hardware."""

from calibration import apply_offset


class TestApplyOffset:
    def test_zero_offset_is_a_no_op_besides_rounding(self):
        assert apply_offset(-51.34, 0.0) == -51.3

    def test_positive_offset_shifts_value_up(self):
        assert apply_offset(-51.3, 12.5) == -38.8

    def test_negative_offset_shifts_value_down(self):
        assert apply_offset(-40.0, -5.0) == -45.0

    def test_rounds_to_one_decimal_place(self):
        assert apply_offset(-51.26, 0.04) == -51.2
