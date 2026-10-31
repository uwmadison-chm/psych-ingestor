"""Settings for the property tests, which `just fuzz` runs, and CI doesn't.

Each test runs a hundred examples by default. For a longer look, which is worth doing
after a change to how Pig stores anything:

    just fuzz --hypothesis-profile=long
"""

from hypothesis import settings

# No time limit per example. Pig fsyncs everything it writes, and how long that takes
# depends on the disk, not on the code, so a limit would fail on a slow disk for
# nothing.
settings.register_profile("default", deadline=None)
settings.register_profile("long", deadline=None, max_examples=2000)
settings.load_profile("default")
