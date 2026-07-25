"""DCSS MCP Server — expose Dungeon Crawl Stone Soup as SSE-based MCP tools.

claude.ai / GPT Chat  ←→  SSE  ←→  this server  ←→  pty  ←→  crawl

Tools:
  read_screen()    — current 80×24 terminal content
  send_keys(str)   — keystrokes to game
  start_new_game() — fresh character
  save_game(slot)  — checkpoint to PostgreSQL
  load_game(slot)  — restore checkpoint
  list_saves()     — available checkpoints
  delete_save()    — remove a checkpoint
  game_status()    — is DCSS alive?
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

mcp = FastMCP("DCSS Game Server")
engine = DcssEngine()
pg = PgStore()

# ── Tools ─────────────────────────────────────────────────────────────


@mcp.tool(
    description=(
        "Read the current DCSS game screen as ASCII text (80x24 terminal). "
        "Shows status lines (HP, MP, depth, turns), the map view, "
        "message lines, and prompts (--more--, y/n, etc.)."
    )
)
async def read_screen() -> str:
    if not engine.is_running:
        return "NO GAME RUNNING — use start_new_game() to begin, or load_game() to restore a save."
    return engine.read_screen()


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
        return "NO GAME RUNNING — use start_new_game() or load_game() first."
    if not keys:
        return engine.read_screen()
    try:
        engine.send_keys(keys)
        screen = engine.wait_for_stable(timeout=2.0)
        return screen
    except Exception as exc:
        logger.exception("send_keys failed")
        return f"ERROR: {exc}"


@mcp.tool(
    description=(
        "Start a fresh DCSS game. Any current game is terminated first.\n\n"
        "After calling this, the main menu appears. For a quick start, "
        "send_keys('P') to play the default character, or send_keys('C') to customise."
    )
)
async def start_new_game() -> str:
    try:
        engine.stop()
        engine.clean_save_files()
        engine.start()
        screen = engine.wait_for_stable(timeout=6.0)
        return screen
    except Exception as exc:
        logger.exception("start_game failed")
        return f"ERROR starting game: {exc}"


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
    description="Quick check whether DCSS is running and responding."
)
async def game_status() -> str:
    if not engine.is_running:
        return "DCSS is NOT running."
    try:
        screen = engine.read_screen()
        if len(screen) > 300:
            return f"DCSS is RUNNING.\n(Preview)\n{screen[:300]}..."
        return f"DCSS is RUNNING.\n{screen}"
    except Exception as exc:
        return f"DCSS is RUNNING but screen read error: {exc}"

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

    # Use uvicorn directly (mcp.run() binds to 127.0.0.1:8000 which fails on Render)
    import uvicorn
    app = mcp.sse_app()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
