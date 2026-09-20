"""
The ONLY file in this project allowed to import edge_tts or pyttsx3.
Speaks text aloud: edge-tts online, pyttsx3 offline (docs/step4 Section 5, Phase 2).

Not built yet - Feature 1 defined the boundary and the config only.

When it is built it must: speak text it is given and nothing else; never read the microphone; never
call the Executor, the grammar or app.console; and leave the choice between the online and offline
engines to app/speaker/logic.py, which stays free of those libraries.
"""
