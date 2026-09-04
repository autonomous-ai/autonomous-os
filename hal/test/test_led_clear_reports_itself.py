"""A clear that does not take must say so — it is invisible from outside."""

import logging
from unittest import mock

from hal.drivers.rgb.rgb_service import RGBService


class _Driver:
    """Minimal driver stand-in. `sticky` refuses to be cleared."""

    def __init__(self, color=(0, 2, 2), sticky=False):
        self._color = color
        self._sticky = sticky
        self.shows = 0

    def fill(self, color, count):
        if not self._sticky:
            self._color = color

    def show(self):
        self.shows += 1

    def getPixelColor(self, index):
        return self._color


def _service(driver):
    svc = RGBService.__new__(RGBService)
    svc._driver = driver
    svc.led_count = 32
    svc.logger = mock.Mock()
    return svc


def test_a_clear_that_takes_is_logged_as_such():
    svc = _service(_Driver(color=(0, 2, 2)))
    svc.clear()
    svc.logger.error.assert_not_called()
    assert svc.logger.info.called


# The 03/09/2026 fault: /led/off returned ok four times while the ring stayed
# lit, with nothing in the log to show for it.
def test_a_clear_that_does_not_take_is_an_error():
    svc = _service(_Driver(color=(0, 2, 2), sticky=True))
    svc.clear()
    svc.logger.error.assert_called_once()
    assert "did NOT take" in svc.logger.error.call_args[0][0]


def test_clearing_an_already_dark_strip_says_nothing():
    svc = _service(_Driver(color=(0, 0, 0)))
    svc.clear()
    svc.logger.error.assert_not_called()
    svc.logger.info.assert_not_called()


# Diagnostics must never be the reason a clear raises.
# The check must survive the pattern it exists for: pixel 0 dark, rest lit.
def test_a_dark_first_pixel_does_not_silence_the_check():
    class _Ring(_Driver):
        def __init__(self):
            super().__init__(sticky=True)
            self._pixels = [(0, 0, 0)] + [(0, 2, 2)] * 31

        def getPixelColor(self, index):
            return self._pixels[index]

    svc = _service(_Ring())
    svc.clear()
    svc.logger.error.assert_called_once()


def test_a_driver_without_read_back_still_clears():
    class _NoReadBack(_Driver):
        def getPixelColor(self, index):
            raise RuntimeError("no read-back")

    driver = _NoReadBack()
    svc = _service(driver)
    svc.clear()
    assert driver.shows == 2
    svc.logger.error.assert_not_called()
