"""Exercise the live output pump with ElevenLabs-style delayed playback."""
from types import SimpleNamespace

from hal.drivers.voice.voice_service import VoiceService
from hal.drivers.voice._internal.live_reply import LiveReplyGuard
from hal.realtime.models.output import TextOutput, InterruptedOutput
from hal.test.test_live_voice_metrics import _pump
from hal.test.test_voice_metrics import kpi  # noqa: F401


def test_no_speech_marker_split_across_events_never_reaches_tts(monkeypatch, kpi):
    spoken = _pump(monkeypatch, kpi, [
        ([TextOutput(text='<no ', user_turn_id='old'),
          TextOutput(text='speech>', user_turn_id='old')], 'old', True),
        ([TextOutput(text='Bạn cần mình giúp gì?', user_turn_id='new')], 'new', True),
    ], strip_markers=VoiceService.strip_rt_markers)
    words = [text for text, _ in spoken]
    assert not any('no speech' in text for text in words)
    assert 'Bạn cần mình giúp gì?' in words


def enable_aec_live(monkeypatch):
    current = {}
    original = VoiceService._live_out_pump
    def run(service, *args, **kwargs):
        current['service'] = service
        service._live_gate = SimpleNamespace()
        service._aec_live_replies = LiveReplyGuard()
        service._tts.stop_realtime_reply = service._tts.stop
        return original(service, *args, **kwargs)
    monkeypatch.setattr(VoiceService, '_live_out_pump', run)
    return current


def test_local_energy_candidate_preserves_reply_without_server_interrupt(monkeypatch, kpi):
    current = enable_aec_live(monkeypatch)
    ducked = []
    monkeypatch.setattr('hal.drivers.voice.voice_service.live_playback.duck', ducked.append)
    spoken = _pump(monkeypatch, kpi, [
        ([TextOutput(text='First reply.', user_turn_id='old')], 'old', True),
        ([lambda: current['service']._aec_live_interrupt_reply('local_speech'),
          TextOutput(text='Old delayed text.', user_turn_id='old'),
          TextOutput(text='New reply.', user_turn_id='new')], 'new', True),
    ])
    words = [text for text, _ in spoken]
    assert 'First reply.' in words
    assert '__stop__' not in words
    assert 'Old delayed text.' in words
    assert 'New reply.' in words
    assert ducked == [True]


def test_provider_interrupt_drops_buffered_sentence_tail(monkeypatch, kpi):
    enable_aec_live(monkeypatch)
    spoken = _pump(monkeypatch, kpi, [
        ([TextOutput(text='unfinished', user_turn_id='old'),
          InterruptedOutput(reason='server_interrupt', user_turn_id='old'),
          TextOutput(text=' stale tail', user_turn_id='old')], 'old', False),
        ([TextOutput(text='Fresh answer.', user_turn_id='new')], 'new', True),
    ])
    words = [text for text, _ in spoken]
    assert '__stop__' in words
    assert not any('unfinished' in text or 'stale' in text for text in words)
    assert 'Fresh answer.' in words


def test_late_old_interrupt_preserves_new_partial_sentence(monkeypatch, kpi):
    enable_aec_live(monkeypatch)
    spoken = _pump(monkeypatch, kpi, [
        ([TextOutput(text='Old.', user_turn_id='old')], 'old', True),
        ([TextOutput(text='Fresh', user_turn_id='new'),
          InterruptedOutput(reason='server_interrupt', user_turn_id='old'),
          TextOutput(text=' answer.', user_turn_id='new')], 'new', True),
    ])
    words = [text for text, _ in spoken]
    assert 'Fresh answer.' in words
    assert '__stop__' not in words


def test_unkeyed_reply_after_cancel_gets_new_fallback_identity(monkeypatch, kpi):
    current = enable_aec_live(monkeypatch)
    spoken = _pump(monkeypatch, kpi, [
        ([TextOutput(text='Old.'),
          lambda: current['service']._aec_live_interrupt_reply('provider'),
          TextOutput(text='Stale.')], '', False),
        ([TextOutput(text='Fresh.')], '', True),
    ])
    words = [text for text, _ in spoken]
    assert 'Stale.' not in words
    assert 'Fresh.' in words
