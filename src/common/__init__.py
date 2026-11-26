"""Platform-neutral relay engine shared by the Linux CLI and the Windows GUI.

Modules here never import from ``linux`` or ``win11``: the dependency direction
is always client -> common.
"""
