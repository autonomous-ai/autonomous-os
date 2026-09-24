"""Known provider error apologies must not leak through streamed TTS."""

import unittest

from hal.drivers.voice._internal.realtime_turn import (
    _filter_system_error_tts,
    _pending_system_error_tts,
)


class ProviderErrorTTSTests(unittest.TestCase):
    SENTENCES = (
        "I'm sorry, there was a system error.",
        "Rất tiếc, đã có lỗi hệ thống xảy ra.",
        "Rất tiếc, đã xảy ra lỗi hệ thống.",
        "Rất tiếc, đã xảy ra lỗi hệ thống trong quá trình xử lý yêu cầu của bạn.",
    )

    def test_exact_known_apologies_are_suppressed(self):
        for sentence in self.SENTENCES:
            with self.subTest(sentence=sentence):
                self.assertEqual(_filter_system_error_tts(sentence, log=False), '')
                self.assertEqual(_filter_system_error_tts(sentence[:-1], log=False), '')

    def test_every_partial_prefix_is_held_until_sentence_can_be_filtered(self):
        for sentence in self.SENTENCES:
            for length in range(1, len(sentence)):
                prefix = sentence[:length]
                with self.subTest(prefix=prefix):
                    self.assertTrue(_pending_system_error_tts(prefix))
            self.assertFalse(_pending_system_error_tts(sentence))

    def test_normal_apology_releases_when_it_diverges(self):
        for text in (
            "Rất tiếc, tôi chưa biết câu trả lời.",
            "Rất tiếc, đã hết vé cho chuyến này.",
            "I'm sorry, I don't know the answer.",
        ):
            self.assertFalse(_pending_system_error_tts(text))
            self.assertEqual(_filter_system_error_tts(text, log=False), text)

    def test_explanations_and_extended_sentences_are_not_blanket_filtered(self):
        for text in (
            'Lỗi hệ thống xảy ra khi máy chủ không phản hồi.',
            'Rất tiếc, đã có lỗi hệ thống xảy ra khi bạn cập nhật hôm qua.',
            "I'm sorry, there was a system error in yesterday's deployment.",
            'Thông báo "Rất tiếc, đã có lỗi hệ thống xảy ra." nghĩa là yêu cầu thất bại.',
        ):
            self.assertEqual(_filter_system_error_tts(text, log=False), text)

    def test_whitespace_case_and_curly_apostrophe(self):
        for text in (
            'RẤT TIẾC,\n đã có lỗi hệ thống xảy ra!',
            'Rất tiếc, đã xảy ra lỗi hệ thống trong quá trình xử lý yêu cầu của bạn?',
            'I’m sorry,  there was a system error.',
        ):
            self.assertEqual(_filter_system_error_tts(text, log=False), '')
        self.assertTrue(_pending_system_error_tts('RẤT TIẾC,\n đã xảy ra'))

    def test_only_matched_sentence_removed_and_normal_followup_preserved(self):
        for sentence in self.SENTENCES:
            text = sentence + ' Bạn thử hỏi lại nhé.'
            self.assertEqual(_filter_system_error_tts(text, log=False), 'Bạn thử hỏi lại nhé.')
        self.assertFalse(_pending_system_error_tts(''))


if __name__ == '__main__':
    unittest.main()
