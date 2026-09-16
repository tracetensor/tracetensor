# TraceTensor Capability Base Images

These two Dockerfiles provide the base environments for TraceTensor's capability system — the infrastructure that allows agent tasks to interact with browser and desktop environments.

## Images

### `Dockerfile.browser` — CDP Browser

**Port:** 9222  
**Protocol:** Chrome DevTools Protocol (CDP)

Runs Chromium in headless mode with the remote debugging port exposed. Agents connect over CDP to drive the browser programmatically: navigate pages, click elements, capture screenshots, intercept network requests, and execute JavaScript.

Build:
```
docker build -f Dockerfile.browser -t tracetensor/capability-browser .
```

Run:
```
docker run -p 9222:9222 tracetensor/capability-browser
```

Connect via CDP at `http://localhost:9222`.

---

### `Dockerfile.desktop` — VNC Desktop

**Port:** 5900  
**Protocol:** RFB (Remote Framebuffer / VNC)

Runs a minimal X11 desktop (Xvfb + Fluxbox) with x11vnc serving the display. Agents connect over RFB to observe and control a full graphical desktop — useful for GUI applications, screen recording, and tasks that require a real windowed environment rather than a headless browser.

Build:
```
docker build -f Dockerfile.desktop -t tracetensor/capability-desktop .
```

Run:
```
docker run -p 5900:5900 tracetensor/capability-desktop
```

Connect with any VNC client at `localhost:5900` (no password).

---

## Relation to the Capability System

TraceTensor's capability system maps task requirements to execution environments. Each capability type has a corresponding transport:

| Capability | Image | Transport |
|---|---|---|
| `browser` | `Dockerfile.browser` | CDP over HTTP/WebSocket |
| `desktop` | `Dockerfile.desktop` | RFB over TCP |

The TraceTensor scheduler pulls the appropriate base image when a task declares a capability requirement, injects the agent code, and exposes the control port for the evaluator to connect to.
