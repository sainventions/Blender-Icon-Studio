"""Blender Icon Studio: Blender worker package.

Runs INSIDE Blender 5.0 (Python 3.11, bpy + numpy only). Entry points:

* ``worker.py``: persistent TCP JSON-lines render server (drafts / previews), PLAN §7.
* ``oneshot.py``: single job (finals, exports, animations), streams ``BIS_EVENT`` lines.

Modules: gpu · scene · geometry · materials · lighting · appearance · render · swatches, plus the
plain-dict contract helpers (``defaults``), preset access (``presets``) and the shared command
dispatcher (``commands``).
"""

VERSION = "1.0.0"
