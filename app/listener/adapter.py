"""
The ONLY file in this project allowed to import faster_whisper or sounddevice.
Captures microphone audio and turns it into a Transcript (docs/step4 Section 5, Phase 2).

Not built yet - Feature 1 defined the boundary, the config and the data shapes only.

When it is built it must: return app.listener.models.Transcript or VoiceFailure and nothing else;
keep raw audio in memory only, never on disk; open the microphone only for an explicit, bounded
capture; and never call the Executor, the grammar or app.console. Pure decisions (language policy,
normalization, error policy) belong in app/listener/logic.py, which must stay free of I/O and must
not import this file.
"""
