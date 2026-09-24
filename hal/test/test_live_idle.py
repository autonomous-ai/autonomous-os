"""Provider progress must prevent idle hangup before ElevenLabs playback."""
from hal.drivers.voice.voice_service import VoiceService
from hal.realtime.models.output import TextOutput
from hal.test.test_live_voice_metrics import _pump
from hal.test.test_voice_metrics import kpi  # noqa: F401


def test_provider_text_renews_idle_window_before_first_sentence(monkeypatch, kpi):
    services = []
    original = VoiceService._live_out_pump
    def run(service, *args, **kwargs):
        services.append(service)
        return original(service, *args, **kwargs)
    monkeypatch.setattr(VoiceService, '_live_out_pump', run)
    _pump(monkeypatch, kpi, [([TextOutput(text='A still unfinished response', user_turn_id='u')], 'u', False)])
    service = services[0]
    progress = service._live_last_model_output
    assert progress > 0
    assert service._live_quiet_for(progress + 1, progress - 20, progress - 20) == 1
    assert service._live_quiet_for(progress + 16, progress - 20, progress - 20) == 16
    assert service._live_quiet_for(progress + 16, progress - 20, progress + 15) == 1
