#!/bin/bash
set -e

Xvfb :1 -screen 0 1280x800x24 &
DISPLAY=:1 fluxbox &
sleep 1

exec x11vnc -display :1 -listen 0.0.0.0 -port 5900 -forever -nopw
