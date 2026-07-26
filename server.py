"""DCSS MCP Server — expose Dungeon Crawl Stone Soup as MCP tools.

claude.ai / ChatGPT  <->  Streamable HTTP or SSE  <->  pty  <->  crawl

Tools:
  start_game()     - fresh game, ready to play
  read_screen()    - current 80x24 terminal content
  send_keys(str)   - keystrokes to game
  start_new_game() - compatibility alias
  save_game(slot)  - checkpoint to PostgreSQL
  load_game(slot)  - restore checkpoint
  list_saves()     - available checkpoints
  delete_save()    - remove a checkpoint
  game_status()    - is DCSS alive?
"""

import os
import sys
import asyncio
import signal
import atexit
import time
import logging
from mcp.server.fastmcp import FastMCP

from dcss_engine import DcssEngine
from pg_store import PgStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
)
logger = logging.getLogger("server")

SERVER_VERSION = "2026-07-26-pty-debug-v1"
DEPLOY_COMMIT = (
    os.environ.get("RENDER_GIT_COMMIT")
    or os.environ.get("RENDER_COMMIT")
    or os.environ.get("GIT_COMMIT")
    or "local"
)

MCP_INSTRUCTIONS = """
This MCP server lets the assistant play Dungeon Crawl Stone Soup through tools.
To begin a run, call start_game() first. After that, repeatedly call
read_screen() and send_keys(keys) to inspect the terminal and act in game.
Do not merely explain the controls when the user asks to play; use the tools.
"""

mcp = FastMCP("DCSS Game Server", instructions=MCP_INSTRUCTIONS)
engine = DcssEngine()
pg = PgStore()

# ── Tools ─────────────────────────────────────────────────────────────

SPECIAL_KEY_TOKENS = {
    "Backspace": "\x7f",
    "Escape": "\x1b",
    "Return": "\r",
    "Enter": "\r",
    "Space": " ",
    "Esc": "\x1b",
    "Tab": "\t",
    "\\t": "\t",
    "\\n": "\r",
}


def _normalize_keys(keys: str) -> str:
    """Allow model-friendly key names such as TabTab, Space, and Enter."""
    if not keys:
        return ""

    tokens = sorted(SPECIAL_KEY_TOKENS, key=len, reverse=True)
    out: list[str] = []
    i = 0
    while i < len(keys):
        matched = False
        for token in tokens:
            wrapped = f"<{token}>"
            if keys.startswith(wrapped, i):
                out.append(SPECIAL_KEY_TOKENS[token])
                i += len(wrapped)
                matched = True
                break
            if keys.startswith(token, i):
                out.append(SPECIAL_KEY_TOKENS[token])
                i += len(token)
                matched = True
                break
        if not matched:
            out.append(keys[i])
            i += 1
    return "".join(out)


async def _start_game_impl(
    auto_play: bool = True,
    name: str = "MCP",
    species: str = "Mi",
    background: str = "Be",
    weapon: str = "c",
) -> str:
    """Start crawl and optionally enter the preselected default game."""
    engine.stop()
    engine.clean_save_files()
    extra_args: list[str] = []
    if auto_play:
        extra_args = ["-name", name, "-species", species, "-background", background]

    engine.start(*extra_args)
    screen = engine.wait_for_stable(timeout=6.0)

    if auto_play:
        engine.send_keys("\r")
        screen = engine.wait_for_stable(timeout=8.0)
        if weapon and "choice of weapons" in screen.lower():
            engine.send_keys(weapon[:1])
            screen = engine.wait_for_stable(timeout=8.0)

    return screen


def _screen_or_blank_diagnostic(screen: str) -> str:
    if screen.strip():
        return screen
    diag = engine.diagnostics()
    lines = [
        "SCREEN BLANK - DCSS process is running but pyte rendered no visible terminal cells.",
        f"server_version={SERVER_VERSION} commit={DEPLOY_COMMIT}",
        f"pid={diag['child_pid']} fd={diag['master_fd']} TERM={diag['term']} size={diag['cols']}x{diag['rows']}",
        f"binary={diag['binary']} realpath={diag['binary_realpath']}",
        f"raw_tail_chars={diag['raw_tail_chars']}",
    ]
    if diag["raw_tail_preview"]:
        lines.append("raw_tail_preview:")
        lines.append(diag["raw_tail_preview"])
    return "\n".join(lines)


@mcp.tool(
    description=(
        "Read the current DCSS game screen as ASCII text (80x24 terminal). "
        "Shows status lines (HP, MP, depth, turns), the map view, "
        "message lines, and prompts (--more--, y/n, etc.)."
    )
)
async def read_screen() -> str:
    if not engine.is_running:
        return "NO GAME RUNNING — use start_game(), start_new_game(), or load_game() first."
    return _screen_or_blank_diagnostic(engine.read_screen())


@mcp.tool(
    description=(
        "Send keystrokes to DCSS and return the updated screen.\n\n"
        "Common keys:  o=auto-explore  Tab=attack  g=pick-up  >=down-stairs  "
        "<=up-stairs  5=wait  a=ability  S=save&quit  Space=more  y/n=confirm  "
        ".=wait-one  q=quaff  d=drop item\n\n"
        "Movement:  h/j/k/l = left/down/up/right  y/u/b/n = diagonals\n\n"
        "Send multiple keys as a single string, e.g. '>>' or 'gg' or 'TabTab'."
    )
)
async def send_keys(keys: str = "") -> str:
    if not engine.is_running:
        return "NO GAME RUNNING — use start_game(), start_new_game(), or load_game() first."
    if not keys:
        return _screen_or_blank_diagnostic(engine.read_screen())
    try:
        engine.send_keys(_normalize_keys(keys))
        screen = engine.wait_for_stable(timeout=2.0)
        return _screen_or_blank_diagnostic(screen)
    except Exception as exc:
        logger.exception("send_keys failed")
        return f"ERROR: {exc}"


@mcp.tool(
    description=(
        "Start a fresh DCSS game. Any current game is terminated first.\n\n"
        "By default auto_play=true, which preselects a simple Minotaur "
        "Berserker using name/species/background and presses Enter on the "
        "initial Dungeon Crawl menu. Pass custom name/species/background to "
        "preselect a different character. If DCSS asks for a starting weapon, "
        "the weapon argument sends that one-letter choice; pass weapon='' to "
        "stop at weapon selection. Set auto_play=false to stop at the menu."
    )
)
async def start_game(
    auto_play: bool = True,
    name: str = "MCP",
    species: str = "Mi",
    background: str = "Be",
    weapon: str = "c",
) -> str:
    """Start a fresh game, defaulting to the quickest playable character."""
    try:
        screen = await _start_game_impl(
            auto_play=auto_play,
            name=name,
            species=species,
            background=background,
            weapon=weapon,
        )
        return _screen_or_blank_diagnostic(screen)
    except Exception as exc:
        logger.exception("start_game failed")
        return f"ERROR starting game: {exc}"


@mcp.tool(
    description=(
        "Compatibility alias for start_game(). Starts a fresh DCSS game. "
        "By default, it preselects a simple Minotaur Berserker, but you can "
        "pass custom name/species/background/weapon. Use weapon='' to stop at "
        "weapon selection, or auto_play=false to stop at the menu."
    )
)
async def start_new_game(
    auto_play: bool = True,
    name: str = "MCP",
    species: str = "Mi",
    background: str = "Be",
    weapon: str = "c",
) -> str:
    return await start_game(
        auto_play=auto_play,
        name=name,
        species=species,
        background=background,
        weapon=weapon,
    )


@mcp.tool(
    description=(
        "Save & quit the current game, then upload the save files to PostgreSQL "
        "and restart the session so you can keep playing.\n\n"
        "Use different 'slot' names for branching saves (e.g. 'before_lair', "
        "'before_grinder').  Default slot is 'latest'."
    )
)
async def save_game(slot: str = "latest") -> str:
    if not engine.is_running:
        return "No game running. Nothing to save."
    try:
        logger.info("Saving game to slot '%s' ...", slot)

        # Ask DCSS to save & quit
        engine.send_keys("S")
        await asyncio.sleep(1.5)
        engine.send_keys("y")  # confirm "Save and quit? (y/N)"
        await asyncio.sleep(1.0)

        # If still running, quit to exit crawl completely
        if engine.is_running:
            screen = engine.read_screen()
            if "menu" in screen.lower() or "play" in screen.lower() or "quit" in screen.lower():
                engine.send_keys("Q")
                await asyncio.sleep(0.5)

        engine.wait_for_exit(timeout=5.0)
        files = engine.collect_save_files()

        if files:
            pg.store(slot, files)
            logger.info("Saved %d files to slot '%s'.", len(files), slot)

            engine.restore_save_files(files)
            engine.start()
            screen = engine.wait_for_stable(timeout=6.0)
            return (
                f"✓ Saved game to slot '{slot}' ({len(files)} files).\n"
                f"Game restarted — you can continue playing.\n"
                f"{screen}"
            )
        else:
            engine.start()
            screen = engine.wait_for_stable(timeout=6.0)
            return f"No save files found. Starting new game.\n{screen}"

    except Exception as exc:
        logger.exception("save_game failed")
        engine.stop()
        return f"ERROR saving game: {exc}"


@mcp.tool(
    description=(
        "Load a saved-game checkpoint from PostgreSQL.\n\n"
        "The save files are downloaded, written to the game directory, "
        "and DCSS starts from that checkpoint."
    )
)
async def load_game(slot: str = "latest") -> str:
    try:
        files = pg.load(slot)
        if not files:
            saves = pg.list_saves()
            msg = f"No save found in slot '{slot}'."
            if saves:
                names = "\n".join(f"  [{s['slot']}] {s['character']} — {s['depth']} — {s['turns']}turns"
                                  for s in saves)
                msg += f"\nAvailable saves:\n{names}"
            return msg

        engine.stop()
        engine.clean_save_files()
        engine.restore_save_files(files)
        engine.start()
        screen = engine.wait_for_stable(timeout=6.0)
        return screen
    except Exception as exc:
        logger.exception("load_game failed")
        return f"ERROR loading game: {exc}"


@mcp.tool(
    description="List all available save checkpoints stored in PostgreSQL."
)
async def list_saves() -> str:
    try:
        saves = pg.list_saves()
        if not saves:
            return "No saves found in database."
        lines = ["Available saves:"]
        for s in saves:
            lines.append(
                f"  [{s['slot']}] {s['character']} — "
                f"{s['depth']} — {s['turns']} turns — {s['updated_at']}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"ERROR listing saves: {exc}"


@mcp.tool(
    description="Delete a saved-game checkpoint from PostgreSQL."
)
async def delete_save(slot: str) -> str:
    try:
        pg.delete(slot)
        return f"Deleted save '{slot}'."
    except Exception as exc:
        return f"ERROR deleting save: {exc}"


@mcp.tool(
    description=(
        "Return server version, deployed commit, transport/runtime settings, "
        "and DCSS engine diagnostics. Use this to verify which Render deploy "
        "the MCP client is actually connected to."
    )
)
async def server_info() -> str:
    diag = engine.diagnostics()
    lines = [
        f"DCSS MCP server version: {SERVER_VERSION}",
        f"Deploy commit: {DEPLOY_COMMIT}",
        f"Render service: {os.environ.get('RENDER_SERVICE_NAME', '?')}",
        f"Render instance: {os.environ.get('RENDER_INSTANCE_ID', '?')}",
        f"DCSS binary: {diag['binary']}",
        f"DCSS binary realpath: {diag['binary_realpath']}",
        f"DCSS binary exists/executable: {diag['binary_exists']}/{diag['binary_executable']}",
        f"TERM: {diag['term']}  size: {diag['cols']}x{diag['rows']}",
        f"Running: {diag['is_running']}  pid: {diag['child_pid']}  fd: {diag['master_fd']}",
        f"Screen nonblank chars: {diag['screen_nonblank_chars']}  raw tail chars: {diag['raw_tail_chars']}",
    ]
    if diag["raw_tail_preview"]:
        lines.append("Raw tail preview:")
        lines.append(diag["raw_tail_preview"])
    return "\n".join(lines)


@mcp.tool(
    description="Quick check whether DCSS is running and responding."
)
async def game_status() -> str:
    prefix = f"DCSS MCP {SERVER_VERSION} commit={DEPLOY_COMMIT}\n"
    diag = engine.diagnostics()
    if not engine.is_running:
        return (
            prefix
            + "DCSS is NOT running.\n"
            + f"binary={diag['binary']} realpath={diag['binary_realpath']} "
            + f"exists/executable={diag['binary_exists']}/{diag['binary_executable']} "
            + f"TERM={diag['term']} size={diag['cols']}x{diag['rows']}"
        )
    try:
        screen = engine.read_screen()
        diag = engine.diagnostics()
        if len(screen) > 300:
            return (
                prefix
                + f"DCSS is RUNNING. pid={diag['child_pid']} "
                + f"screen_nonblank={diag['screen_nonblank_chars']} raw_tail={diag['raw_tail_chars']}\n"
                + f"(Preview)\n{screen[:300]}..."
            )
        return (
            prefix
            + f"DCSS is RUNNING. pid={diag['child_pid']} "
            + f"screen_nonblank={diag['screen_nonblank_chars']} raw_tail={diag['raw_tail_chars']}\n"
            + screen
        )
    except Exception as exc:
        return prefix + f"DCSS is RUNNING but screen read error: {exc}"

# ── Graceful shutdown — save game on SIGTERM ─────────────────────────


@atexit.register
def _save_on_exit():
    """Best-effort save when the server stops."""
    if not engine.is_running:
        return
    try:
        logger.info("Shutdown: saving game...")
        engine.send_keys("S")
        time.sleep(1.5)
        engine.send_keys("y")
        time.sleep(1.0)
        if engine.is_running:
            engine.send_keys("Q")
            time.sleep(1.0)
        engine.wait_for_exit(timeout=4.0)
        files = engine.collect_save_files()
        if files:
            pg.store("latest", files)
            logger.info("Shutdown save complete.")
    except Exception as exc:
        logger.error("Shutdown save failed: %s", exc)
    finally:
        engine.stop()
        pg.close()


def _sigterm(signum, frame):
    """SIGTERM → sys.exit → atexit handler runs above."""
    logger.warning("SIGTERM — saving and shutting down ...")
    sys.exit(0)


signal.signal(signal.SIGTERM, _sigterm)

# ── Entry point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("===== DCSS MCP Server =====")

    pg_dsn = os.environ.get("DATABASE_URL")
    if pg_dsn:
        pg.connect(pg_dsn)
    else:
        logger.warning("DATABASE_URL not set — saves disabled.")

    port = int(os.environ.get("PORT", 8000))
    logger.info("Listening on 0.0.0.0:%d", port)

    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    mcp.settings.transport_security = security
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = port
    mcp.settings.streamable_http_path = "/mcp"
    mcp.settings.sse_path = "/sse"
    mcp.settings.message_path = "/messages/"

    async def health(_request):
        return JSONResponse({
            "ok": True,
            "name": "DCSS Game Server",
            "version": SERVER_VERSION,
            "commit": DEPLOY_COMMIT,
            "mcp": "/mcp",
            "sse": "/sse",
            "tools": [
                "start_game",
                "read_screen",
                "send_keys",
                "start_new_game",
                "save_game",
                "load_game",
                "list_saves",
                "delete_save",
                "server_info",
                "game_status",
            ],
        })

    streamable_app = mcp.streamable_http_app()
    sse_app = mcp.sse_app()
    app = Starlette(
        routes=[
            Route("/", endpoint=health, methods=["GET"]),
            Route("/health", endpoint=health, methods=["GET"]),
            *streamable_app.routes,
            *sse_app.routes,
        ],
        lifespan=lambda app: mcp.session_manager.run(),
    )

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
