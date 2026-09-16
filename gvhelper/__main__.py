# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""`python -m gvhelper`, for a machine where the console script is not on PATH.

Which is most of them, in the shape this ships: the launcher installs into a
private root and never touches the user's PATH, so the login item invokes the
interpreter and this module by absolute path.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
