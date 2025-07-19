import math
import requests
from typing import List, Dict, Any
from dataclasses import dataclass

from . import ArrService
from ..config.queue import WIDTH, PAGE_SIZE
from ..tg_handler import command, callback, handler, escape_markdownv2_chars
from ..tg_handler.keyboard import keyboard
from ..tg_handler.message import Response, repaint, clear
from ..tg_handler.auth import authorized, AuthLevels
from ..tg_handler.session_state import sessionState, default_session_state_key_fn
from ..tg_handler.keyboard import Button


@dataclass(frozen=True)
class QueueState:
    items: Dict[str, Any]
    page: int
    page_size: int


@handler
class Sabnzbd(ArrService):
    def __init__(
        self,
        commands: List[str],
        api_host: str,
        api_key: str,
    ):
        self.commands = commands
        self.api_key = api_key
        self.api_url = api_host.rstrip("/")

        # SABnzbd doesn't need version detection like Sonarr/Radarr
        self.api_version = "1"

        # Set default properties for command help
        self.default_pattern = ""
        self.default_description = "Show SABnzbd download queue"
        self.sub_commands = [
            ("queue", "", "Show the download queue", None),
            ("help", "", "Show this help page", None)
        ]

    def get_queue(self, page: int = None, page_size: int = None):
        """Get queue from SABnzbd API"""
        params = {
            "mode": "queue",
            "output": "json",
            "apikey": self.api_key
        }

        if page is not None and page_size is not None:
            params["start"] = page * page_size
            params["limit"] = page_size
        elif page_size is not None:
            params["limit"] = page_size

        try:
            response = self.request("api", params=params, fallback={"queue": {"slots": [], "noofslots_total": 0}})

            # Transform SABnzbd response to match expected format
            queue_data = response.get("queue", {})
            slots = queue_data.get("slots", [])

            # Calculate progress for each item and transform data
            records = []
            for slot in slots:
                # Calculate percentage completed
                size_mb = float(slot.get("mb", 0))
                size_left_mb = float(slot.get("mbleft", 0))

                if size_mb > 0:
                    percent_complete = ((size_mb - size_left_mb) / size_mb) * 100
                else:
                    percent_complete = 0

                # Transform to expected format
                record = {
                    "title": slot.get("filename", "Unknown"),
                    "status": slot.get("status", "Unknown"),
                    "size": size_mb * 1024 * 1024,  # Convert MB to bytes
                    "sizeleft": size_left_mb * 1024 * 1024,  # Convert MB to bytes
                    "timeleft": slot.get("timeleft", "N/A"),
                    "percentage": int(percent_complete),
                    "trackedDownloadState": slot.get("priority", ""),
                    "nzo_id": slot.get("nzo_id", ""),
                    # Additional SABnzbd specific fields
                    "speed": queue_data.get("speed", "0 B/s"),
                    "priority": slot.get("priority", "Normal"),
                    "category": slot.get("cat", ""),
                    "labels": slot.get("labels", [])
                }
                records.append(record)

            return {
                "records": records,
                "totalRecords": queue_data.get("noofslots_total", len(records)),
                "speed": queue_data.get("speed", "0 B/s"),
                "paused": queue_data.get("paused", False),
                "timeleft": queue_data.get("timeleft", "0:00:00")
            }

        except Exception as e:
            # Return empty queue on error
            return {"records": [], "totalRecords": 0, "speed": "0 B/s", "paused": False, "timeleft": "0:00:00"}

    def pause_queue(self):
        """Pause the SABnzbd queue"""
        params = {
            "mode": "pause",
            "apikey": self.api_key
        }
        try:
            return self.request("api", params=params, fallback={"status": False})
        except Exception:
            return {"status": False}

    def resume_queue(self):
        """Resume the SABnzbd queue"""
        params = {
            "mode": "resume",
            "apikey": self.api_key
        }
        try:
            return self.request("api", params=params, fallback={"status": False})
        except Exception:
            return {"status": False}

    def pause_item(self, nzo_id: str):
        """Pause a specific download item"""
        params = {
            "mode": "queue",
            "name": "pause",
            "value": nzo_id,
            "apikey": self.api_key
        }
        try:
            return self.request("api", params=params, fallback={"status": False})
        except Exception:
            return {"status": False}

    def resume_item(self, nzo_id: str):
        """Resume a specific download item"""
        params = {
            "mode": "queue",
            "name": "resume",
            "value": nzo_id,
            "apikey": self.api_key
        }
        try:
            return self.request("api", params=params, fallback={"status": False})
        except Exception:
            return {"status": False}

    @keyboard
    def create_queue_keyboard(self, state: QueueState):
        total_pages = max(1, (int(state.items["totalRecords"]) + state.page_size - 1) // state.page_size)

        # Main queue pause/resume button
        is_paused = state.items.get("paused", False)
        main_button_text = "▶️ Resume Queue" if is_paused else "⏸️ Pause Queue"
        main_action = "resume_queue" if is_paused else "pause_queue"

        buttons = [
            [Button(main_button_text, self.get_clbk(main_action))],
        ]

        # Individual item pause/resume buttons
        offset = state.page * state.page_size
        for idx, item in enumerate(state.items["records"]):
            nzo_id = item.get("nzo_id", "")
            if nzo_id:
                item_status = item.get("status", "").lower()
                # SABnzbd uses "Paused" status for paused items
                is_item_paused = item_status == "paused"
                item_number = offset + idx + 1

                if is_item_paused:
                    button_text = f"▶️ Resume #{item_number}"
                    buttons.append([Button(button_text, self.get_clbk("resume_item", nzo_id))])
                else:
                    button_text = f"⏸️ Pause #{item_number}"
                    buttons.append([Button(button_text, self.get_clbk("pause_item", nzo_id))])

        # Navigation buttons
        nav_buttons = []
        if state.page > 0:
            nav_buttons.append(Button("⬅️ Prev", self.get_clbk("queue", state.page - 1)))
        else:
            nav_buttons.append(Button())

        if state.page < total_pages - 1:
            nav_buttons.append(Button("Next ➡️", self.get_clbk("queue", state.page + 1)))
        else:
            nav_buttons.append(Button())

        if len(nav_buttons) > 0 and any(btn.title for btn in nav_buttons):
            buttons.append(nav_buttons)

        return buttons

    def create_queue_message(self, state: QueueState, full_redraw=False):
        # Queue header with overall status
        speed = state.items.get("speed", "0 B/s")
        paused = state.items.get("paused", False)
        total_timeleft = state.items.get("timeleft", "0:00:00")

        status_emoji = "⏸️" if paused else "📥"
        lines = [f"*{status_emoji} SABnzbd Queue*"]

        # Only show speed and time left if queue is not paused
        if not paused:
            lines.append(f"_Speed: {escape_markdownv2_chars(speed)} • Time left: {escape_markdownv2_chars(total_timeleft)}_")
        else:
            lines.append(f"_Queue is paused_")

        lines.append("")

        offset = state.page * state.page_size + 1

        for idx, item in enumerate(state.items["records"]):
            # Calculate progress bar
            size_total = float(item.get("size", 0))
            size_left = float(item.get("sizeleft", 0))

            if size_total > 0:
                percent = 1.0 - (size_left / size_total)
            else:
                percent = 0

            progress = math.floor(percent * WIDTH)
            remaining = WIDTH - progress

            title = escape_markdownv2_chars(item.get("title", "Unknown")[0:60])  # Limit title length

            # Add pause indicator to title for paused items
            item_status = item.get("status", "").lower()
            is_item_paused = item_status == "paused"
            status_indicator = "⏸️ " if is_item_paused else ""

            title_ln = f"{offset + idx}\\. {status_indicator}*{title}*"

            # Create progress bar with percentage
            progress_bar = f"{'█' * progress}{'░' * remaining}"
            progress_ln = f">`{progress_bar}` {round(percent*100)}%"

            # Format size information
            size_mb = size_total / (1024 * 1024) if size_total > 0 else 0
            left_mb = size_left / (1024 * 1024) if size_left > 0 else 0

            status = escape_markdownv2_chars(item.get("status", "N/A"))
            timeleft = escape_markdownv2_chars(item.get("timeleft", "N/A"))
            priority = escape_markdownv2_chars(item.get("priority", "Normal"))
            category = escape_markdownv2_chars(item.get("category", ""))

            # Status line with multiple pieces of information
            status_parts = [f"Status: _{status}_"]
            if size_mb > 0:
                size_str = escape_markdownv2_chars(f"{size_mb:.1f}")
                status_parts.append(f"Size: _{size_str} MB_")
            if left_mb > 0:
                left_str = escape_markdownv2_chars(f"{left_mb:.1f}")
                status_parts.append(f"Left: _{left_str} MB_")
            if timeleft != "N/A":
                status_parts.append(f"Time: _{timeleft}_")
            if priority and priority != "Normal":
                status_parts.append(f"Priority: _{priority}_")
            if category:
                status_parts.append(f"Cat: _{category}_")

            status_ln = ">" + "   ".join(status_parts)

            lines += [title_ln, progress_ln, status_ln, ""]

        if not len(state.items["records"]):
            n = PAGE_SIZE // 4
            lines += [n * "\n", "\t_No Downloads_", n * "\n"]
        elif len(lines) < state.page_size:
            lines += [(state.page_size - len(lines)) * "\n"]

        total_pages = max(1, (int(state.items["totalRecords"]) + state.page_size - 1) // state.page_size)
        lines.append(f"\t\tPage _{state.page + 1}_ of _{total_pages}_")

        reply_message = "\n".join(lines)
        keyboard_markup = self.create_queue_keyboard(state)

        return Response(
            caption=reply_message,
            reply_markup=keyboard_markup,
            state=state,
            parse_mode="MarkdownV2",
        )

    @repaint
    @command(
        default=True,
        default_pattern="",
        default_description="Show SABnzbd download queue",
        cmds=[("queue", "", "Show the download queue")]
    )
    @sessionState(init=True)
    @authorized(min_auth_level=AuthLevels.USER)
    async def cmd_default(self, update, context, args):
        # Handle both direct command and 'queue' subcommand
        items = self.get_queue(page=0, page_size=PAGE_SIZE)

        state = QueueState(
            items=items,
            page=0,
            page_size=PAGE_SIZE,
        )

        self.session_db.add_session_entry(
            default_session_state_key_fn(self, update), state
        )

        return self.create_queue_message(state, full_redraw=True)

    @repaint
    @command(cmds=[("queue", "", "Show the download queue")])
    @sessionState(init=True)
    @authorized(min_auth_level=AuthLevels.USER)
    async def cmd_queue(self, update, context, args):
        # This is the same implementation as cmd_default
        return await self.cmd_default(update, context, args)

    @repaint
    @callback(cmds=["queue"])
    @sessionState()
    @authorized(min_auth_level=AuthLevels.USER)
    async def clbk_queue(self, update, context, args, state):
        new_page = int(args[1])
        items = self.get_queue(page=new_page, page_size=PAGE_SIZE)

        new_state = QueueState(
            items=items,
            page=new_page,
            page_size=PAGE_SIZE,
        )

        return self.create_queue_message(new_state)

    @repaint
    @callback(cmds=["pause_queue"])
    @sessionState()
    @authorized(min_auth_level=AuthLevels.USER)
    async def clbk_pause_queue(self, update, context, args, state):
        # Pause the queue
        self.pause_queue()

        # Refresh the queue data to get updated status
        items = self.get_queue(page=state.page, page_size=state.page_size)

        new_state = QueueState(
            items=items,
            page=state.page,
            page_size=state.page_size,
        )

        return self.create_queue_message(new_state)

    @repaint
    @callback(cmds=["resume_queue"])
    @sessionState()
    @authorized(min_auth_level=AuthLevels.USER)
    async def clbk_resume_queue(self, update, context, args, state):
        # Resume the queue
        self.resume_queue()

        # Refresh the queue data to get updated status
        items = self.get_queue(page=state.page, page_size=state.page_size)

        new_state = QueueState(
            items=items,
            page=state.page,
            page_size=state.page_size,
        )

        return self.create_queue_message(new_state)

    @repaint
    @callback(cmds=["pause_item"])
    @sessionState()
    @authorized(min_auth_level=AuthLevels.USER)
    async def clbk_pause_item(self, update, context, args, state):
        # Extract nzo_id from the callback args
        nzo_id = args[1]  # args[0] is "pause_item", args[1] is the nzo_id

        # Pause the specific item
        self.pause_item(nzo_id)

        # Refresh the queue data to get updated status
        items = self.get_queue(page=state.page, page_size=state.page_size)

        new_state = QueueState(
            items=items,
            page=state.page,
            page_size=state.page_size,
        )

        return self.create_queue_message(new_state)

    @repaint
    @callback(cmds=["resume_item"])
    @sessionState()
    @authorized(min_auth_level=AuthLevels.USER)
    async def clbk_resume_item(self, update, context, args, state):
        # Extract nzo_id from the callback args
        nzo_id = args[1]  # args[0] is "resume_item", args[1] is the nzo_id

        # Resume the specific item
        self.resume_item(nzo_id)

        # Refresh the queue data to get updated status
        items = self.get_queue(page=state.page, page_size=state.page_size)

        new_state = QueueState(
            items=items,
            page=state.page,
            page_size=state.page_size,
        )

        return self.create_queue_message(new_state)

    @command(cmds=[("help", "", "Shows the SABnzbd help page")])
    async def cmd_help(self, update, context, args):
        response_message = f"""
*butlarr* \\- Help page for SABnzbd service\\.

Following commands are available:
        """
        for cmd in self.commands:
            response_message += f"\n \\- `/{cmd}` \\t _Show SABnzbd download queue_\n"
            response_message += f"\n \\- `/{cmd} queue` \\t _Show the download queue_\n"
            response_message += f"\n \\- `/{cmd} help` \\t _Show this help page_\n"

        return await update.message.reply_text(response_message, parse_mode="MarkdownV2")

    # Override the default request method to handle SABnzbd's different API structure
    def request(self, endpoint: str, *, action=None, params={}, fallback=None):
        url = f"{self.api_url}/{endpoint}"

        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            if fallback is not None:
                return fallback
            raise e
