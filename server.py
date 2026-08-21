"""DCSS MCP Server — play Dungeon Crawl Stone Soup through MCP.

The server intentionally runs one live Crawl session at a time, while
PostgreSQL can hold any number of independent profiles and checkpoints.

A profile (``instance_id``) is a long-lived character/world line.
A checkpoint (``slot``) is a named save inside that profile.
"""

import atexit
import asyncio
import logging
import os
import signal
import sys
import time

from mcp.server.fastmcp import FastMCP

from dcss_engine import DcssEngine
from pg_store import PgStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
)
logger = logging.getLogger("server")

SERVER_VERSION = "2026-08-21-multi-profile-v1"
DEPLOY_COMMIT = (
    os.environ.get("RENDER_GIT_COMMIT")
    or os.environ.get("RENDER_COMMIT")
    or os.environ.get("GIT_COMMIT")
    or "local"
)

MCP_INSTRUCTIONS = """
This MCP server lets the assistant play Dungeon Crawl Stone Soup through tools.
Only one game is active at a time, but PostgreSQL can store multiple independent
profiles and multiple checkpoints per profile.

Use list_instances() or list_saves() before loading when the desired profile is
unclear. A fresh game should have an instance_id (for example "Asterion" or
"wizard-test"). save_game() defaults to checkpoint "latest" for the active
profile. load_game(instance_id="Asterion", slot="latest") switches the live
session to that profile without deleting any other database checkpoints.

If start_game() or load_game() returns INITIALIZING, do not restart it; call
read_screen() on the same session until the terminal appears.
"""

mcp = FastMCP("DCSS Game Server", instructions=MCP_INSTRUCTIONS)
engine = DcssEngine()
pg = PgStore()

_pending_autoplay: dict[str, str] | None = None
_active_instance: str | None = None

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


def _clean_instance_id(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("instance_id must not be empty")
    if "::" in value:
        raise ValueError("instance_id must not contain '::'")
    if len(value) > 60:
        raise ValueError("instance_id is too long (max 60 characters)")
    return value


def _clean_slot(value: str) -> str:
    value = (value or "latest").strip() or "latest"
    if "::" in value:
        raise ValueError("slot must not contain '::'")
    if len(value) > 60:
        raise ValueError("slot is too long (max 60 characters)")
    return value


def _normalize_keys(keys: str) -> str:
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


def _infer_character(files: dict[str, bytes]) -> str | None:
    cs_names = sorted(name for name in files if name.endswith(".cs"))
    if not cs_names:
        return None
    return cs_names[0][:-3]


def _save_meta(files: dict[str, bytes], instance_id: str) -> dict:
    return {
        "instance_id": instance_id,
        "character": _infer_character(files) or instance_id,
    }


def _resolve_active_instance(
    requested: str = "",
    files: dict[str, bytes] | None = None,
) -> str:
    if requested.strip():
        return _clean_instance_id(requested)
    if _active_instance:
        return _active_instance
    if files:
        inferred = _infer_character(files)
        if inferred:
            return _clean_instance_id(inferred)
    raise ValueError(
        "No active profile is known. Pass instance_id explicitly or load/start a profile first."
    )


def _format_saves(saves: list[dict]) -> str:
    if not saves:
        return "No saves found in database."
    lines = ["Available DCSS saves:"]
    for save in saves:
        legacy = " [legacy]" if save.get("legacy") else ""
        lines.append(
            f"  [{save['instance_id']}/{save['slot']}] "
            f"{save['character']} — {save['depth']} — {save['turns']} turns — "
            f"{save['updated_at']}{legacy}"
        )
    return "\n".join(lines)


def _finish_pending_autoplay(screen: str) -> str:
    global _pending_autoplay
    if not _pending_autoplay or not screen.strip():
        return screen

    weapon = _pending_autoplay.get("weapon", "")
    engine.send_keys("\r")
    screen = engine.wait_for_stable(timeout=3.0)
    if weapon and "choice of weapons" in screen.lower():
        engine.send_keys(weapon[:1])
        screen = engine.wait_for_stable(timeout=3.0)
    _pending_autoplay = None
    return screen


async def _start_game_impl(
    auto_play: bool = True,
    name: str = "MCP",
    species: str = "Mi",
    background: str = "Be",
    weapon: str = "c",
    instance_id: str = "",
) -> str:
    global _pending_autoplay, _active_instance

    new_instance = _clean_instance_id(instance_id or name or "default")
    engine.stop()
    engine.clean_save_files()
    _active_instance = new_instance

    extra_args: list[str] = []
    if auto_play:
        extra_args = ["-name", name, "-species", species, "-background", background]
        _pending_autoplay = {"weapon": weapon}
    else:
        _pending_autoplay = None

    engine.start(*extra_args)
    screen = engine.read_screen()
    if auto_play and screen.strip():
        screen = _finish_pending_autoplay(screen)
    return screen


def _screen_or_blank_diagnostic(screen: str) -> str:
    if screen.strip():
        return screen

    diag = engine.diagnostics()
    elapsed = diag.get("startup_elapsed_sec")
    elapsed_text = f"{elapsed:.1f}s" if isinstance(elapsed, (int, float)) else "unknown"

    if isinstance(elapsed, (int, float)) and elapsed < 120:
        return (
            "INITIALIZING - DCSS process is alive but has not painted the terminal yet "
            f"(elapsed={elapsed_text}).\n"
            "Do NOT call start_game() again; call read_screen() on this same session."
        )

    lines = [
        "STARTUP STILL BLANK - DCSS process is alive but has not painted the terminal.",
        f"startup_elapsed={elapsed_text}",
        f"server_version={SERVER_VERSION} commit={DEPLOY_COMMIT}",
        f"active_instance={_active_instance or '?'}",
        f"TERM={diag['term']} size={diag['cols']}x{diag['rows']} terminfo={diag['terminfo']}",
        f"binary={diag['binary']} realpath={diag['binary_realpath']}",
    ]
    if diag.get("proc"):
        lines.append(f"proc: {diag['proc']}")
    if diag.get("process_table"):
        lines.append("process_table:")
        lines.append(diag["process_table"])
    return "\n".join(lines)


@mcp.tool(
    description=(
        "Read the current DCSS screen as ASCII text. If startup is still blank, "
        "keep calling read_screen() instead of restarting the game."
    )
)
async def read_screen() -> str:
    if not engine.is_running:
        return "NO GAME RUNNING — use start_game(), start_new_game(), or load_game() first."
    screen = engine.read_screen()
    if screen.strip() and _pending_autoplay:
        screen = _finish_pending_autoplay(screen)
    return _screen_or_blank_diagnostic(screen)


@mcp.tool(
    description=(
        "Send keystrokes to the active DCSS profile and return the updated screen. "
        "Common keys: o=auto-explore, Tab=attack, g=pick-up, >=down-stairs, "
        "<=up-stairs, 5=wait, a=ability, Space=more, y/n=confirm. "
        "Movement: h/j/k/l and y/u/b/n diagonals."
    )
)
async def send_keys(keys: str = "") -> str:
    global _pending_autoplay
    if not engine.is_running:
        return "NO GAME RUNNING — use start_game(), start_new_game(), or load_game() first."
    if not keys:
        return await read_screen()
    try:
        _pending_autoplay = None
        engine.send_keys(_normalize_keys(keys))
        return _screen_or_blank_diagnostic(engine.wait_for_stable(timeout=2.0))
    except Exception as exc:
        logger.exception("send_keys failed")
        return f"ERROR: {exc}"


@mcp.tool(
    description=(
        "Start a fresh DCSS profile. The live session is single-instance, so any "
        "currently running game is stopped first, but database checkpoints for all "
        "other profiles remain untouched. Give each long-lived character a distinct "
        "instance_id. Set auto_play=false to stop at the main menu."
    )
)
async def start_game(
    auto_play: bool = True,
    name: str = "MCP",
    species: str = "Mi",
    background: str = "Be",
    weapon: str = "c",
    instance_id: str = "",
) -> str:
    try:
        screen = await _start_game_impl(
            auto_play=auto_play,
            name=name,
            species=species,
            background=background,
            weapon=weapon,
            instance_id=instance_id,
        )
        prefix = f"Active profile: {_active_instance}\n"
        return prefix + _screen_or_blank_diagnostic(screen)
    except Exception as exc:
        logger.exception("start_game failed")
        return f"ERROR starting game: {exc}"


@mcp.tool(
    description=(
        "Compatibility alias for start_game(). Give different long-lived characters "
        "different instance_id values."
    )
)
async def start_new_game(
    auto_play: bool = True,
    name: str = "MCP",
    species: str = "Mi",
    background: str = "Be",
    weapon: str = "c",
    instance_id: str = "",
) -> str:
    return await start_game(
        auto_play=auto_play,
        name=name,
        species=species,
        background=background,
        weapon=weapon,
        instance_id=instance_id,
    )


@mcp.tool(
    description=(
        "Save and quit the active game to PostgreSQL, then restart Crawl so play can "
        "continue. instance_id identifies the long-lived profile; slot identifies a "
        "checkpoint inside that profile. If instance_id is omitted, the active "
        "profile is used. Examples: Asterion/latest, Asterion/before_lair."
    )
)
async def save_game(slot: str = "latest", instance_id: str = "") -> str:
    global _active_instance
    if not engine.is_running:
        return "No game running. Nothing to save."

    try:
        checkpoint = _clean_slot(slot)
        logger.info("Saving active profile=%s slot=%s", _active_instance, checkpoint)

        engine.send_keys("S")
        await asyncio.sleep(1.5)
        engine.send_keys("y")
        await asyncio.sleep(1.0)

        if engine.is_running:
            screen = engine.read_screen()
            if "menu" in screen.lower() or "play" in screen.lower() or "quit" in screen.lower():
                engine.send_keys("Q")
                await asyncio.sleep(0.5)

        engine.wait_for_exit(timeout=5.0)
        files = engine.collect_save_files()
        if not files:
            engine.start()
            return "No save files found. Crawl was restarted."

        profile = _resolve_active_instance(instance_id, files)
        _active_instance = profile
        pg.store(
            checkpoint,
            files,
            _save_meta(files, profile),
            instance_id=profile,
        )
        logger.info("Saved %d files to profile=%s slot=%s", len(files), profile, checkpoint)

        engine.restore_save_files(files)
        engine.start()
        screen = engine.read_screen()
        return (
            f"✓ Saved [{profile}/{checkpoint}] ({len(files)} files).\n"
            "Game process restarted on the same profile.\n"
            f"{_screen_or_blank_diagnostic(screen)}"
        )
    except Exception as exc:
        logger.exception("save_game failed")
        engine.stop()
        return f"ERROR saving game: {exc}"


@mcp.tool(
    description=(
        "Load one database checkpoint and make it the active live profile. Prefer "
        "load_game(instance_id='Asterion', slot='latest'). For backward compatibility "
        "an old single-slot save can still be loaded with load_game(slot='Asterion')."
    )
)
async def load_game(slot: str = "latest", instance_id: str = "") -> str:
    global _active_instance, _pending_autoplay
    try:
        checkpoint = _clean_slot(slot)
        record = None

        if instance_id.strip():
            profile = _clean_instance_id(instance_id)
            record = pg.load_record(slot=checkpoint, instance_id=profile)
        elif _active_instance and checkpoint == "latest":
            record = pg.load_record(slot=checkpoint, instance_id=_active_instance)

        if record is None and not instance_id.strip():
            record = pg.load_record(slot=checkpoint)

        if not record:
            saves = pg.list_saves(instance_id=instance_id or None)
            return (
                f"No save found for instance={instance_id or '?'} slot={checkpoint}.\n"
                + _format_saves(saves)
            )

        engine.stop()
        engine.clean_save_files()
        engine.restore_save_files(record["files"])
        _active_instance = record["instance_id"]
        _pending_autoplay = None
        engine.start()

        return (
            f"✓ Loaded [{record['instance_id']}/{record['slot']}]"
            + (" from legacy save." if record.get("legacy") else ".")
            + "\n"
            + _screen_or_blank_diagnostic(engine.read_screen())
        )
    except Exception as exc:
        logger.exception("load_game failed")
        return f"ERROR loading game: {exc}"


@mcp.tool(
    description=(
        "List saved checkpoints. Optionally pass instance_id to show only one profile."
    )
)
async def list_saves(instance_id: str = "") -> str:
    try:
        saves = pg.list_saves(instance_id=instance_id or None)
        return _format_saves(saves)
    except Exception as exc:
        logger.exception("list_saves failed")
        return f"ERROR listing saves: {exc}"


@mcp.tool(
    description="List long-lived DCSS profiles and how many checkpoints each has."
)
async def list_instances() -> str:
    try:
        instances = pg.list_instances()
        if not instances:
            return "No DCSS profiles found in database."
        lines = ["DCSS profiles:"]
        for item in instances:
            active = " ← ACTIVE" if (
                _active_instance
                and item["instance_id"].lower() == _active_instance.lower()
            ) else ""
            lines.append(
                f"  {item['instance_id']} — {item['character']} — "
                f"{item['checkpoint_count']} checkpoint(s) — "
                f"newest {item['latest_checkpoint']} @ {item['updated_at']}{active}"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.exception("list_instances failed")
        return f"ERROR listing profiles: {exc}"


@mcp.tool(
    description=(
        "Delete one database checkpoint. Pass both instance_id and slot for new "
        "multi-profile saves. Legacy raw slots remain supported."
    )
)
async def delete_save(slot: str = "latest", instance_id: str = "") -> str:
    try:
        deleted = pg.delete(
            _clean_slot(slot),
            instance_id=_clean_instance_id(instance_id) if instance_id.strip() else None,
        )
        if deleted:
            label = f"{instance_id}/{slot}" if instance_id else slot
            return f"Deleted save '{label}'."
        return "No matching save was found."
    except Exception as exc:
        logger.exception("delete_save failed")
        return f"ERROR deleting save: {exc}"


@mcp.tool(
    description=(
        "Return server version, active profile, transport/runtime settings, "
        "and DCSS engine diagnostics."
    )
)
async def server_info() -> str:
    diag = engine.diagnostics()
    return "\n".join([
        f"DCSS MCP server version: {SERVER_VERSION}",
        f"Deploy commit: {DEPLOY_COMMIT}",
        f"Active profile: {_active_instance or '?'}",
        f"Backend: {diag.get('backend', '?')}",
        f"DCSS binary: {diag['binary']}",
        f"DCSS data dir: {diag.get('crawl_dir', '?')} exists={diag.get('crawl_dir_exists', '?')}",
        f"tmux: {diag.get('tmux_binary', '?')} exists={diag.get('tmux_exists', '?')} session={diag.get('session', '?')}",
        f"TERM: {diag['term']} size={diag['cols']}x{diag['rows']} terminfo={diag['terminfo']}",
        f"Running: {diag['is_running']} startup_elapsed={diag.get('startup_elapsed_sec')}s",
        f"Screen nonblank chars: {diag['screen_nonblank_chars']}",
    ])


@mcp.tool(
    description="Quick check whether DCSS is running and which profile is active."
)
async def game_status() -> str:
    prefix = (
        f"DCSS MCP {SERVER_VERSION} commit={DEPLOY_COMMIT}\n"
        f"active_profile={_active_instance or '?'}\n"
    )
    diag = engine.diagnostics()
    if not engine.is_running:
        return prefix + "DCSS is NOT running."

    try:
        screen = engine.read_screen()
        if not screen.strip():
            return prefix + _screen_or_blank_diagnostic(screen)
        if len(screen) > 300:
            return prefix + "DCSS is READY/RUNNING.\n(Preview)\n" + screen[:300] + "..."
        return prefix + "DCSS is READY/RUNNING.\n" + screen
    except Exception as exc:
        return prefix + f"DCSS is RUNNING but screen read error: {exc}"


@atexit.register
def _save_on_exit():
    """Best-effort latest checkpoint for the active profile."""
    global _active_instance
    if not engine.is_running:
        pg.close()
        return

    try:
        if not engine.read_screen().strip():
            logger.info("Shutdown during Crawl initialization; stopping without save.")
            return

        logger.info("Shutdown: saving active profile...")
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
            profile = _resolve_active_instance(files=files)
            _active_instance = profile
            pg.store(
                "latest",
                files,
                _save_meta(files, profile),
                instance_id=profile,
            )
            logger.info("Shutdown checkpoint complete: %s/latest", profile)
    except Exception as exc:
        logger.error("Shutdown save failed: %s", exc)
    finally:
        engine.stop()
        pg.close()


def _sigterm(signum, frame):
    logger.warning("SIGTERM — saving and shutting down ...")
    sys.exit(0)


signal.signal(signal.SIGTERM, _sigterm)


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
            "active_instance": _active_instance,
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
                "list_instances",
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
