### Focus Tomato State Band

This GNOME Shell 42 extension draws a three-logical-pixel state band over the
bottom edge of the existing top panel. It does not reserve screen space and is
not part of the pointer input region. GNOME hides it when the primary monitor
has a fullscreen window.

Colors:

- muted tomato: focus
- muted green: short break
- muted blue: long break
- amber: paused
- hidden: idle or application unavailable

The extension receives state over the session D-Bus from Focus Tomato. It does
not read the timer database and cannot control the timer.
