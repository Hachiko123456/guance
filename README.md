# 掼策（Guance）

Windows 10/11 desktop assistant for local Daguandan state reconstruction and FableDan recommendations.

> 当前仓库处于开发阶段。默认只显示推荐，不自动操作游戏。

## Status

The project skeleton and implementation plan are being built in small, testable layers:

1. deterministic domain/state core;
2. live capture and proxy lifecycle;
3. simple modern desktop UI;
4. FableDan model integration;
5. portable Windows packaging and diagnostics.

See `docs/` for the architecture, protocol, security, UX, and acceptance documents.

## Development

Python 3.12 x64 is the target runtime. The release build will bundle the runtime, the capture helper, and the configured model profile so end users do not need Python or mitmproxy installed.

## Safety

Guance never clicks the game UI or submits a move. If state is incomplete, stale, or inconsistent, it pauses recommendations instead of guessing.
