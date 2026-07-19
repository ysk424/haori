# SPDX-License-Identifier: GPL-3.0-or-later
"""Haori pattern loading, automatic sewing, Update, and GRAVITY tools."""

from __future__ import annotations

from . import ui


def register():
    ui.register()


def unregister():
    ui.unregister()
