"""
ui/theme.py — Theme manager for light/dark mode with custom colors.
"""

import customtkinter as ctk


class ThemeManager:
    """Manages light and dark color schemes for the application."""

    _current_theme = "dark"

    LIGHT = {
        "bg":            "#f5f5f5",
        "bg_dark":       "#e8e8e8",
        "bg_input":      "#ffffff",
        "bg_panel":      "#ffffff",
        "bg_bar":        "#e0e0e0",
        "bg_sash":       "#d0d0d0",
        "bg_accent":     "#6a7a8c",
        "bg_accent_hover": "#5a6a7c",
        "bg_message_user": "#6a7a8c",
        "bg_message_klara": "#e8e8e8",
        "bg_sidebar_collapsed": "#d8d8d8",
        "accent":        "#6a7a8c",
        "accent_red":    "#c0392b",
        "fg":            "#1a1a1a",
        "fg_dim":        "#888888",
        "fg_subtle":     "#555555",
        "fg_user":       "#ffffff",
        "fg_clara":      "#1a1a1a",
        "fg_tool":       "#888888",
        "mono_bg":       "#f0f0f0",
        "mono_fg":       "#333333",
        "border":        "#d0d0d0",
        "border_active": "#6a7a8c",
        "shadow":        "#cccccc",
    }

    DARK = {
        "bg":            "#1e1e2e",
        "bg_dark":       "#161622",
        "bg_input":      "#252535",
        "bg_panel":      "#1e1e2e",
        "bg_bar":        "#2a2a3a",
        "bg_sash":       "#3a3a4a",
        "bg_accent":     "#4a5a6c",
        "bg_accent_hover": "#5a6a7c",
        "bg_message_user": "#4a5a6c",
        "bg_message_klara": "#252535",
        "bg_sidebar_collapsed": "#161622",
        "accent":        "#6a7a8c",
        "accent_red":    "#e74c3c",
        "fg":            "#e0e0e0",
        "fg_dim":        "#666677",
        "fg_subtle":     "#aaaaaa",
        "fg_user":       "#ffffff",
        "fg_clara":      "#e0e0e0",
        "fg_tool":       "#666677",
        "mono_bg":       "#161622",
        "mono_fg":       "#cccccc",
        "border":        "#3a3a4a",
        "border_active": "#6a7a8c",
        "shadow":        "#0a0a12",
    }

    @staticmethod
    def apply_theme(theme_name: str) -> None:
        """Apply the specified theme to customtkinter."""
        ThemeManager._current_theme = theme_name
        ctk.set_appearance_mode(theme_name)

    @staticmethod
    def get_current() -> dict:
        """Get the color palette for the currently active theme."""
        if ThemeManager._current_theme == "light":
            return ThemeManager.LIGHT
        return ThemeManager.DARK

    @staticmethod
    def get_colors(theme_name: str) -> dict:
        """Get the color palette for the specified theme."""
        if theme_name == "light":
            return ThemeManager.LIGHT
        return ThemeManager.DARK
