from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import pathlib
import random
import re
import typing
from zoneinfo import ZoneInfo

import aiohttp
from discord.ext.commands import Context, errors
from dotenv import load_dotenv

import discord
from discord import app_commands
from discord.ext import commands, tasks

if typing.TYPE_CHECKING:
    # noinspection PyProtectedMember
    from discord.ext.commands._types import BotT

load_dotenv()
discord.utils.setup_logging()
logger = logging.getLogger()

if "TIMEZONE" in os.environ:
    tzinfo = ZoneInfo(os.environ["TIMEZONE"])
else:
    tzinfo = datetime.datetime.now(datetime.timezone.utc).astimezone().tzinfo

# Set to True to sync the commands tree with the Discord API.
SYNCING_TREE = False

MAIN_COOKIE_DUMP_PATH = pathlib.Path(__file__).absolute().parent / "data" / ".main.cookies"
AUTO_COOKIE_DUMP_PATH = pathlib.Path(__file__).absolute().parent / "data" / ".auto.cookies"
MAIN_COOKIE_DUMP_PATH.parent.mkdir(exist_ok=True)

CONFIG_PATH = pathlib.Path(__file__).absolute().parent / "data" / "config.json"
CONFIG_PATH.parent.mkdir(exist_ok=True)
if not CONFIG_PATH.exists():
    with CONFIG_PATH.open("w") as f_:
        json.dump({}, f_, indent=4)


class GeoGuessr(commands.Cog):
    """
    A cog for GeoGuessr-related commands.
    """

    def __init__(self, bot: Bot):
        """
        Initialize the GeoGuessr cog.

        :param bot: The bot instance.
        """
        self.bot = bot
        self.config_lock = asyncio.Lock()

        # Load saved cookies.
        self.main_saved_cookies: dict[str, str] = {}
        if MAIN_COOKIE_DUMP_PATH.exists():
            with MAIN_COOKIE_DUMP_PATH.open("r") as f:
                self.main_saved_cookies = json.load(f)

        self.auto_saved_cookies: dict[str, str] = {}
        if AUTO_COOKIE_DUMP_PATH.exists():
            with AUTO_COOKIE_DUMP_PATH.open("r") as f:
                self.auto_saved_cookies = json.load(f)

        self.all_maps_data: dict[str, dict[str, str]] = {}  # Format: {slug: {name: name, countryCode: countryCode}}

    async def cog_load(self) -> None:
        """
        Callback for when the GeoGuessr cog loads.
        """

        # Error GeoGuessr cookies if not already saved.
        if not self.main_saved_cookies:
            logger.critical("Cookies are out of date. Please set new cookies (main).")

        if not self.auto_saved_cookies:
            logger.critical("Cookies are out of date. Please set new cookies (auto).")

        await self._load_map_data()
        self.load_map_data.start()
        self.daily_challenge_task.start()

        # noinspection PyAsyncCall
        asyncio.create_task(self.send_missing_daily_challenges())

    async def cog_unload(self) -> None:
        """
        Callback for when the GeoGuessr cog unloads.
        """
        self.load_map_data.cancel()
        self.load_map_data.stop()
        self.daily_challenge_task.cancel()
        self.daily_challenge_task.stop()

    async def _load_map_data(self) -> None:
        """
        Load the data for all maps available on GeoGuessr.
        """
        # Load all maps data.
        # noinspection PyBroadException
        try:
            async with aiohttp.ClientSession(cookies=self.main_saved_cookies) as session:
                # TODO: Replace with the new API v3 endpoint.
                async with session.get(
                    "https://www.geoguessr.com/api/maps/explorer",
                    headers=self.headers | {"content-type": "application/json; charset=utf-8"}
                ) as response:
                    if response.status == 401:
                        logger.error("Failed to load all maps data: unauthorized. (main cookies may be expired).")

                    response.raise_for_status()

                    data = await response.json()
                    self.all_maps_data = {
                        map_data["slug"]: {"name": map_data["name"], "countryCode": map_data["countryCode"]}
                        for map_data in data
                    }
        except Exception:
            logger.critical("Failed to load all maps data.", exc_info=True)

        async with aiohttp.ClientSession(cookies=self.main_saved_cookies) as session:
            for page in range(1, 6):
                await asyncio.sleep(1)
                async with session.get("https://www.geoguessr.com/api/v3/social/maps/browse/popular/all", params={
                    "count": 36,  # Default behavior
                    "page": page,
                    "minCoords": 20,
                    "minLikes": 0,
                    "minGamesPlayed": 0,
                }, headers=self.headers | {"content-type": "application/json; charset=utf-8"}) as response:
                    response.raise_for_status()
                    data = await response.json()
                    for map_data in data:
                        self.all_maps_data.setdefault(map_data["slug"], {"name": map_data["name"], "countryCode": ""})

    @tasks.loop(hours=20, reconnect=False)
    async def load_map_data(self) -> None:
        """
        Hourly task to load the data for all maps available on GeoGuessr.
        """
        await asyncio.sleep(random.randint(0, 600))  # Random delay of up to 10 minutes to avoid rate limiting.
        await self._load_map_data()

    @staticmethod
    def _get_daily_embed(link: str, *, date: str | None = None) -> discord.Embed:
        """
        Get the embed for the daily GeoGuessr challenge.

        :param link: The GeoGuessr challenge link.
        :param date: The date of the challenge. Default: today. Format: YYYY-MM-DD.
        :return: The embed.
        """

        short_link = link.replace("https://", "")

        if date is None:  # Today
            date = datetime.datetime.now(tz=tzinfo).strftime("%B %d %Y")
            embed = discord.Embed(
                title=f"Daily GeoGuessr Challenge",
                description=f"Here is the link to today's GeoGuessr challenge:\n[{short_link}]({link})",
                colour=discord.Colour.from_rgb(167, 199, 231),
            )
            embed.set_footer(text=f"Use of external help is not allowed (e.g. Google) · Good luck!")

        else:
            date_obj = datetime.datetime.strptime(date, "%Y-%m-%d")
            date = date_obj.strftime("%B %d %Y")
            date_text = date_obj.strftime("%A, %B %d, %Y")
            embed = discord.Embed(
                title=f"Daily GeoGuessr Challenge",
                description=f"Here is the link to the GeoGuessr challenge on {date_text}:\n[{short_link}]({link})",
                colour=discord.Colour.from_rgb(167, 199, 231),
            )

        embed.set_author(name=date, url=link)
        return embed

    async def _send_daily_challenge(self, guild: discord.Guild, *, send_leaderboard: bool = False) -> None:
        """
        Send the daily GeoGuessr challenge for the specified guild.

        :param guild: The guild.
        """
        daily_config = await self.get_daily_config(guild.id)
        if daily_config is None:
            return

        channel = self.bot.get_channel(daily_config["channel"])
        if not channel:
            logger.error("Daily GeoGuessr challenge channel not found.")
            return

        # TODO: if cookies are invalid, need to tell user to contact @botowner.
        link = await self.get_daily_link(guild.id)
        if link is None:
            return

        embed = self._get_daily_embed(link)

        if send_leaderboard:
            # Add yesterday's leaderboard.
            yesterday = datetime.datetime.now(tz=tzinfo) - datetime.timedelta(days=1)
            leaderboard = await self.get_game_results(guild.id, yesterday.strftime("%Y-%m-%d"))
            if leaderboard is not None:
                if not leaderboard:
                    embed.add_field(
                        name=f"Yesterday's Top Players",
                        value="No one played yesterday.",
                        inline=False,
                    )
                else:
                    embed.add_field(
                        name=f"Yesterday's Top Players",
                        value="\n".join(
                            f"{i + 1}. **[{entry['username']}](https://www.geoguessr.com/user/{entry['user_id']})** "
                            f"- {entry['score']:,} points"
                            for i, entry in enumerate(leaderboard[:10])
                        ),
                        inline=False,
                    )

        await channel.send(embed=embed)

    async def send_missing_daily_challenges(self) -> None:
        """
        Send the daily GeoGuessr challenges for all guilds that have it set up.
        """
        await self.bot.wait_until_ready()
        await asyncio.sleep(30)  # Wait for everything to be ready

        async with self.config_lock:
            with CONFIG_PATH.open("r") as f:
                config = json.load(f)

        date = datetime.datetime.now(tz=tzinfo).strftime("%Y-%m-%d")

        for guild in self.bot.guilds:
            if (str(guild.id) in config and "daily_links" in config[str(guild.id)]
                    and "daily_config" in config[str(guild.id)]):
                if date not in config[str(guild.id)]["daily_links"]:  # Missing challenge
                    logger.info("Sending missing daily GeoGuessr challenge for guild %d.", guild.id)
                    await self._send_daily_challenge(guild, send_leaderboard=True)

    @tasks.loop(
        time=datetime.time(hour=0, minute=0, second=0, tzinfo=tzinfo),
        reconnect=False,
    )
    async def daily_challenge_task(self) -> None:
        """
        Task to send the daily GeoGuessr challenge.
        """
        await self.bot.wait_until_ready()
        logger.info("Sending daily GeoGuessr challenges.")
        for guild in self.bot.guilds:
            await self._send_daily_challenge(guild, send_leaderboard=True)

    async def get_daily_config(self, guild_id: int) -> dict[str, typing.Any] | None:
        """
        Get the daily config for the specified guild.

        :param guild_id: The ID of the guild.
        :return: The daily config. None if not set.
        """
        async with self.config_lock:
            with CONFIG_PATH.open("r") as f:
                config = json.load(f)
            if str(guild_id) in config and "daily_config" in config[str(guild_id)]:
                daily_config = config[str(guild_id)]["daily_config"]
                channel = self.bot.get_channel(daily_config["channel"])
                if not channel:  # Channel doesn't exist
                    try:
                        await self.bot.fetch_channel(daily_config["channel"])
                    except discord.NotFound:
                        with CONFIG_PATH.open("w") as f:
                            del config[str(guild_id)]["daily_config"]
                            json.dump(config, f, indent=4)
                if "slug_name" not in daily_config:  # Old version of code, for backwards compat
                    daily_config["slug_name"] = daily_config["map_name"]
                    logger.warning("slug_name not present in daily config for %d.", guild_id)
                return daily_config
            return None

    async def set_daily_config(
        self,
        guild_id: int,
        channel_id: int | None,
        slug_name: str = "world",
        time_limit: int = 0,
        no_move: bool = False,
        no_pan: bool = False,
        no_zoom: bool = False,
    ) -> None:
        """
        Set the daily config for the specified guild.

        :param guild_id: The ID of the guild.
        :param channel_id: The ID of the channel. Set to None to remove the daily channel.
        :param slug_name: The slug of the map to use.
        :param time_limit: The time limit for the challenge in seconds.
        :param no_move: Whether to forbid moving.
        :param no_pan: Whether to forbid panning.
        :param no_zoom: Whether to forbid zooming.
        """

        async with self.config_lock:
            with CONFIG_PATH.open("r") as f:
                config = json.load(f)
            if channel_id is None:
                if str(guild_id) in config and "daily_config" in config[str(guild_id)]:
                    del config[str(guild_id)]["daily_config"]
            else:
                map_name = await self.fetch_name_from_slug(slug_name)  # Get the proper name

                if str(guild_id) not in config:
                    config[str(guild_id)] = {}

                config[str(guild_id)]["daily_config"] = {
                    "channel": channel_id,
                    "map_name": map_name,
                    "slug_name": slug_name,
                    "time_limit": time_limit,
                    "no_move": no_move,
                    "no_pan": no_pan,
                    "no_zoom": no_zoom,
                }
                config[str(guild_id)]["daily_links"] = {}
            with CONFIG_PATH.open("w") as f:
                json.dump(config, f, indent=4)

    async def get_daily_link(self, guild_id: int, *, force: bool = False) -> str | None:
        """
        Get the daily GeoGuessr challenge link. If it doesn't exist, generate a new one.

        :param guild_id: The ID of the guild.
        :param force: Whether to force generation of a new link.
        :return: The GeoGuessr challenge link. None if daily challenge is not set up.
        """
        async with self.config_lock:
            with CONFIG_PATH.open("r") as f:
                config = json.load(f)
            if str(guild_id) in config and "daily_links" in config[str(guild_id)]:
                daily_link = config[str(guild_id)]["daily_links"].get(
                    datetime.datetime.now(tz=tzinfo).strftime("%Y-%m-%d"), None
                )
            else:
                daily_link = None

        if daily_link is None or force:
            daily_config = await self.get_daily_config(guild_id)
            if daily_config is None:
                return None

            daily_link = await self.get_geoguessr_challenge_link(
                daily_config["slug_name"],
                daily_config["time_limit"],
                daily_config["no_move"],
                daily_config["no_pan"],
                daily_config["no_zoom"],
                auto_guess=True,
            )
            if daily_link is None:
                logger.error("Failed to get GeoGuessr challenge link.")
                return None

            async with self.config_lock:
                # Save the daily link.
                with CONFIG_PATH.open("r") as f:
                    config = json.load(f)
                if str(guild_id) not in config:
                    config[str(guild_id)] = {}
                if "daily_links" not in config[str(guild_id)]:
                    config[str(guild_id)]["daily_links"] = {}
                config[str(guild_id)]["daily_links"][datetime.datetime.now(tz=tzinfo).strftime("%Y-%m-%d")] = daily_link
                with CONFIG_PATH.open("w") as f:
                    json.dump(config, f, indent=4)

        return daily_link

    @property
    def headers(self) -> dict[str, str]:
        """
        Get the headers to use for requests to GeoGuessr.
        """
        return {
            "Referer": "https://www.geoguessr.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }

    async def get_geoguessr_challenge_link(
        self,
        slug_name: str = "world",
        time_limit: int = 0,
        no_move: bool = False,
        no_pan: bool = False,
        no_zoom: bool = False,
        *,
        auto_guess: bool = False,
    ) -> str | None:
        """
        Get a GeoGuessr challenge link with the specified settings.

        :param slug_name: The slug of the map to use.
        :param time_limit: The time limit for the challenge in seconds.
        :param no_move: Whether to forbid moving.
        :param no_pan: Whether to forbid rotating.
        :param no_zoom: Whether to forbid zooming.
        :param auto_guess: Whether to automatically guess the location (for daily's).
        :return: The GeoGuessr challenge link. None if an error occurred.
        """

        logger.info(
            "Getting GeoGuessr challenge link with options: %s, %s, %s, %s, %s",
            slug_name,
            time_limit,
            no_move,
            no_pan,
            no_zoom,
        )

        # Get GeoGuessr cookies if not already saved.
        if not self.main_saved_cookies:
            logger.error("Cookies are out of date. Please set new cookies (main).")
            return None

        data = {
            "map": slug_name,
            "timeLimit": time_limit,
            "forbidMoving": no_move,
            "forbidZooming": no_zoom,
            "forbidRotating": no_pan,
            "accessLevel": 1,
        }

        # noinspection PyBroadException
        try:
            # Send POST request with specific cookies in the header using aiohttp
            async with aiohttp.ClientSession(cookies=self.main_saved_cookies) as session:
                url = "https://www.geoguessr.com/api/v3/challenges"
                async with session.post(url, headers=self.headers, json=data) as response:
                    if response.status == 401:  # Unauthorized
                        logger.error("Failed to get GeoGuessr challenge link: unauthorized "
                                     "(main cookies may be expired).")
                        return None

                    if response.status == 500:
                        logger.info("Failed to get GeoGuessr challenge link: invalid options. %s", data)
                        return None

                    response.raise_for_status()

                    resp = await response.json()
                    token = resp["token"]

        except Exception:
            logger.exception("Failed to get GeoGuessr challenge link.", exc_info=True)
            return None

        if auto_guess:
            # noinspection PyAsyncCall
            asyncio.create_task(self.auto_guess(token))

        return f"https://www.geoguessr.com/challenge/{token}"

    async def auto_guess(self, token: str) -> None:
        """
        Automatically guess the location in the GeoGuessr challenge (in Antarctica).
        :param token: The token of the challenge.
        """

        logger.info("Automatically guessing the location in the GeoGuessr challenge.")
        # noinspection PyBroadException
        try:
            # Send POST request with specific cookies in the header using aiohttp
            async with aiohttp.ClientSession(cookies=self.auto_saved_cookies) as session:
                url = f"https://www.geoguessr.com/api/v3/challenges/{token}"
                async with session.post(url, headers=self.headers, json={}) as response:
                    if response.status == 401:  # Unauthorized
                        logger.error("Failed to auto-guess GeoGuessr challenge: unauthorized "
                                     "(auto cookies may be expired).")
                        return

                    response.raise_for_status()

                    resp = await response.json()
                    round_token: str = resp["token"]

                state: typing.Literal["started", "finished"] = "started"

                while state != "finished":
                    await asyncio.sleep(random.uniform(0, 1))

                    # Add a random offset to avoid detection.
                    data = {"lat": -83 + random.uniform(0, 0.3), "lng": random.uniform(-0.8, 0.8)}

                    url = "https://www.geoguessr.com/api/v4/geo-coding/terrain"
                    async with session.post(url, headers=self.headers, json=data) as response:
                        response.raise_for_status()

                    data["token"] = round_token
                    data["timedOut"] = False

                    url = f"https://www.geoguessr.com/api/v3/games/{round_token}"
                    async with session.post(url, headers=self.headers, json=data) as response:
                        response.raise_for_status()

                    await asyncio.sleep(random.uniform(0, 1))

                    async with session.get(url, headers=self.headers, params={"client": "web"}) as response:
                        state = (await response.json())["state"]

        except Exception:
            logger.exception("Failed to auto-guess GeoGuessr challenge.", exc_info=True)
            return

        logger.info("Successfully guessed the location in the GeoGuessr challenge.")

    async def get_game_results(self, guild_id: int, date: str) -> list[dict[str, typing.Any]] | None:
        """
        Get the daily GeoGuessr results for the specified date.

        :param guild_id: The ID of the guild.
        :param date: The date of the leaderboard (YYYY-MM-DD).
        :return: The results. None if an error occurred or the results doesn't exist.
        """

        async with self.config_lock:
            with CONFIG_PATH.open("r") as f:
                config = json.load(f)

        # If the guild never set up daily challenges.
        if str(guild_id) not in config or "daily_links" not in config[str(guild_id)]:
            return None

        # If the guild set up daily challenges but the date is not in the daily_links.
        if date not in config[str(guild_id)]["daily_links"]:
            return None

        token = config[str(guild_id)]["daily_links"][date].rsplit("/", maxsplit=1)[-1]
        url = f"https://www.geoguessr.com/api/v3/results/highscores/{token}"
        # noinspection PyBroadException
        try:
            for repeat_c in range(2):  # Try twice in case of Unauthorized error
                async with aiohttp.ClientSession(cookies=self.auto_saved_cookies) as session:
                    # TODO: May need to handle pagination.
                    async with session.get(
                        url, headers=self.headers, params={"friends": "false", "limit": 9999, "minRounds": 5}
                    ) as response:

                        if response.status == 401:
                            if repeat_c == 0:  # The bot might've not guessed yet
                                await self.auto_guess(token)
                                continue
                            logger.error("Failed to get GeoGuessr leaderboard: unauthorized. "
                                         "(auto cookies may be expired).")
                            return None

                        response.raise_for_status()

                        data = await response.json()
                break

            results = []
            for entry in data["items"]:
                # TODO: Replace with player ID
                if entry["game"]["player"]["nick"] == os.environ["GEOGUESSR_AUTO_USERNAME"]:
                    continue  # Skip the bot's entry

                rounds = []
                for round_ in entry["game"]["player"]["guesses"]:
                    rounds.append(
                        {
                            "distance": float(round_["distanceInMeters"]),
                            "score": int(round_["roundScore"]["amount"]),
                        }
                    )

                results.append(
                    {
                        "username": entry["game"]["player"]["nick"],
                        "user_id": entry["game"]["player"]["id"],
                        "score": int(entry["game"]["player"]["totalScore"]["amount"]),
                        "distance": float(entry["game"]["player"]["totalDistanceInMeters"]),
                        "rounds": rounds,
                    }
                )
            results.sort(key=lambda x: x["score"], reverse=True)

        except Exception:
            logger.exception("Failed to get GeoGuessr leaderboard.", exc_info=True)
            if "data" in locals():
                logger.error("Data: %s", data)
            return None

        return results

    # noinspection PyUnusedLocal
    async def map_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """
        Autocomplete the map name for the GeoGuessr command.

        :param interaction: The interaction.
        :param current: The current input.
        :return: The list of autocomplete choices.
        """
        current = current.casefold()
        possibilities: list[tuple[str, str]] = []
        if not current:
            # World and Famous Places are available on top when no input is given.
            possibilities += [(self.all_maps_data["world"]["name"], "world"),
                              (self.all_maps_data["famous-places"]["name"], "famous-places")]
        possibilities += sorted(
            [
                (data["name"], slug)
                for slug, data in self.all_maps_data.items()
                if (current in data["name"].casefold() or current in data["countryCode"].casefold()) and not
                   (current and slug in {"world", "famous-places"})
            ]
        )
        possibilities = possibilities[:25]  # Limit to 25 choices
        return [app_commands.Choice(name=name, value=slug) for name, slug in possibilities]

    def _parse_slug_name(self, map_name: str) -> str | None:
        """
        Retrieve the slug name by the map name or slug name.

        :param map_name: The map name.
        :return: The parsed slug name. None if invalid.
        """

        if map_name in self.all_maps_data:  # Already a slug
            return map_name

        map_name_mapping = {}
        for slug, data in self.all_maps_data.items():
            map_name_mapping.setdefault(data["name"].casefold(), slug)
            map_name_mapping.setdefault(data["countryCode"].casefold(), slug)
        return map_name_mapping.get(map_name.casefold(), None)

    @staticmethod
    def _parse_map_url(url: str) -> str | None:
        """
        Retrieve the slug name by the map URL.

        :param url: The map URL.
        :return: The parsed slug name. None if invalid.
        """
        if (slug_name_match := re.search(r"(?:https?://)?www.geoguessr.com/maps/([^/]+)", url)) is not None:
            slug_name = slug_name_match.group(1).lower()
            if slug_name == 'community':
                return None
            return slug_name
        return None

    async def fetch_name_from_slug(self, slug_name: str) -> str:
        """
        Fetch the name of the map from the slug name.

        :param slug_name: The slug name.
        :return: The name of the map. None if not found.
        """
        if slug_name in self.all_maps_data:
            return self.all_maps_data[slug_name]["name"]

        # noinspection PyBroadException
        try:
            async with aiohttp.ClientSession(cookies=self.main_saved_cookies) as session:
                url = f"https://www.geoguessr.com/api/v3/search/map?page=0&count=1&q={slug_name}"
                async with session.get(url, headers=self.headers | {"Content-Type": "application/json"}) as response:
                    if response.status == 401:
                        logger.error("Failed to fetch map name: unauthorized. (main cookies may be expired).")
                        return slug_name
                    response.raise_for_status()
                    data = await response.json()
                    if not data or data[0]["id"] != slug_name:
                        logger.warning("No map found or wrong map data fetched: %s", data)
                        return slug_name
                    return data[0]["name"]
        except Exception:
            logger.exception("Failed to fetch map name.", exc_info=True)
            return slug_name

    @commands.cooldown(1, 60, commands.BucketType.user)
    @commands.hybrid_command()
    @app_commands.rename(
        map_name="map", time_limit="time-limit", no_move="no-moving", no_pan="no-panning", no_zoom="no-zooming"
    )
    @app_commands.autocomplete(map_name=map_name_autocomplete)
    async def geochallenge(
        self,
        ctx: commands.Context,
        map_name: str = "World",
        time_limit: commands.Range[int, 0] = 0,
        no_move: bool = False,
        no_pan: bool = False,
        no_zoom: bool = False,
    ) -> None:
        """
        Start a new GeoGuessr challenge.

        :param map_name: The name or URL of the map to use. Default: World.
        :param time_limit: The time limit for the challenge in seconds. Default: no time limit.
        :param no_move: Whether to forbid moving. Default: moving is allowed.
        :param no_pan: Whether to forbid panning. Default: panning is allowed.
        :param no_zoom: Whether to forbid zooming. Default: zooming is allowed.
        """

        if (slug_name := self._parse_slug_name(map_name)) is None:
            if (slug_name := self._parse_map_url(map_name)) is None:
                await ctx.reply("Map not found.", ephemeral=True)
                return

        link = await self.get_geoguessr_challenge_link(slug_name, time_limit, no_move, no_pan, no_zoom)
        if link is None:
            await ctx.reply("Failed to get GeoGuessr challenge link.", ephemeral=True)
            return

        await ctx.reply(f"Hey {ctx.author.mention}! Here is your GeoGuessr challenge link:\n{link}")

    @geochallenge.error
    async def geochallenge_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        """
        Handle errors that occur during the geochallenge command.

        :param ctx: The context in which the error occurred.
        :param error: The error that occurred.
        """
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.reply(f"You can only generate 1 challenge link per minute.", ephemeral=True)
        else:
            await ctx.reply("An error occurred while processing the command.", ephemeral=True)
            logger.error("An error occurred while processing the command.", exc_info=error)

    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @commands.hybrid_command()
    @app_commands.rename(
        map_name="map", time_limit="time-limit", no_move="no-moving", no_pan="no-panning", no_zoom="no-zooming"
    )
    @app_commands.autocomplete(map_name=map_name_autocomplete)
    async def setupgeodaily(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
        map_name: str = "World",
        time_limit: commands.Range[int, 0] = 180,
        no_move: bool = False,
        no_pan: bool = False,
        no_zoom: bool = False,
    ) -> None:
        """
        Set up the daily GeoGuessr challenge channel.

        :param channel: The channel to set as the daily GeoGuessr challenge channel.
        :param map_name: The name or URL of the map to use. Default: World.
        :param time_limit: The time limit for the challenge in seconds. Default: 3 minutes.
        :param no_move: Whether to forbid moving. Default: moving is allowed.
        :param no_pan: Whether to forbid panning. Default: panning is allowed.
        :param no_zoom: Whether to forbid zooming. Default: zooming is allowed.
        """
        if channel is None:
            channel = ctx.channel

        if (slug_name := self._parse_slug_name(map_name)) is None:
            if (slug_name := self._parse_map_url(map_name)) is None:
                await ctx.reply("Map not found.", ephemeral=True)
                return

        await self.set_daily_config(ctx.guild.id, channel.id, slug_name, time_limit, no_move, no_pan, no_zoom)

        await self.get_daily_link(ctx.guild.id, force=True)  # Force generation of a new link
        await self._send_daily_challenge(ctx.guild)

        time_limit_text = "No limit" if time_limit == 0 else f"{time_limit}s"

        mpz = (
            f"Moving: {'❌' if no_move else '✅'}\n"
            f"Panning: {'❌' if no_pan else '✅'}\n"
            f"Zooming: {'❌' if no_zoom else '✅'}"
        )

        await ctx.reply(
            f"Daily GeoGuessr challenge channel set to {channel.mention}!"
            f"\n\nMap: {map_name}\nTime Limit: {time_limit_text}\n{mpz}"
        )

    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @commands.hybrid_command()
    async def cancelgeodaily(self, ctx: commands.Context) -> None:
        """
        Stop sending daily GeoGuessr challenges.
        """
        if self.get_daily_config(ctx.guild.id) is None:
            await ctx.reply("Daily GeoGuessr challenges are not set up.", ephemeral=True)
            return

        await self.set_daily_config(ctx.guild.id, None)
        await ctx.reply("Daily GeoGuessr challenges have been stopped.")

    async def date_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """
        Autocomplete the date for the geodaily command.

        :param interaction: The interaction.
        :param current: The current input.
        :return: The list of autocomplete choices.
        """
        current = current.casefold()
        choices = ["today", "yesterday"]

        async with self.config_lock:
            with CONFIG_PATH.open("r") as f:
                config = json.load(f)

        if str(interaction.guild.id) in config and "daily_links" in config[str(interaction.guild.id)]:
            for date in reversed(config[str(interaction.guild.id)]["daily_links"].keys()):
                if current in date:
                    choices.append(date)
                if len(choices) >= 15:
                    break

        return [app_commands.Choice(name=choice.capitalize(), value=choice) for choice in choices]

    @commands.cooldown(1, 20, commands.BucketType.user)
    @commands.guild_only()
    @commands.hybrid_command()
    @app_commands.autocomplete(date=date_autocomplete)
    async def geodaily(self, ctx: commands.Context, *, date: str = "Today") -> None:
        """
        Shows the daily GeoGuessr challenge.

        :param date: The date of the challenge. Format: today, yesterday, or YYYY-MM-DD.
        """
        await ctx.defer()

        if date.casefold() == "today":
            date = datetime.datetime.now(tz=tzinfo).strftime("%Y-%m-%d")
        elif date.casefold() == "yesterday":
            date = (datetime.datetime.now(tz=tzinfo) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            try:
                date = datetime.datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                await ctx.reply("Invalid date. Use 'today', 'yesterday', or 'YYYY-MM-DD'.", ephemeral=True)
                return

        async with self.config_lock:
            with CONFIG_PATH.open("r") as f:
                config = json.load(f)

        link = None
        if str(ctx.guild.id) in config and "daily_links" in config[str(ctx.guild.id)]:
            link = config[str(ctx.guild.id)]["daily_links"].get(
                date, None
            )

        if link is None:
            if date == datetime.datetime.now(tz=tzinfo).strftime("%Y-%m-%d"):
                await ctx.reply("Daily GeoGuessr challenge is not set up.", ephemeral=True)
            else:
                await ctx.reply("Daily GeoGuessr challenge for that date is not available.", ephemeral=True)
            return

        embed = self._get_daily_embed(link, date=date)

        leaderboard = await self.get_game_results(ctx.guild.id, date)
        if leaderboard is not None:
            if not leaderboard:
                embed.add_field(
                    name="Top Players",
                    value="No one has played yet.",
                    inline=False,
                )
            else:
                # TODO: Pagination.
                embed.add_field(
                    name="Top Players",
                    value="\n".join(
                        f"{i + 1}. **[{entry['username']}](https://www.geoguessr.com/user/{entry['user_id']})** "
                        f"- {entry['score']:,} points"
                        for i, entry in enumerate(leaderboard[:10])
                    ),
                    inline=False,
                )

            embed.set_footer(text="Leaderboard updated at")
            embed.timestamp = datetime.datetime.now(tz=tzinfo)

        await ctx.reply(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message, /) -> None:
        """
        Process owner only commands sent in DMs.
        """
        if await self.bot.is_owner(message.author) and isinstance(message.channel, discord.DMChannel):
            if message.content.casefold().startswith('!maintoken '):
                token = message.content.split('!maintoken ', maxsplit=1)[1]
                self.main_saved_cookies = {"_ncfa": token}
                with MAIN_COOKIE_DUMP_PATH.open("w") as f:
                    json.dump(self.main_saved_cookies, f)
                await message.reply("Main cookies set.")

            elif message.content.casefold().startswith('!autotoken '):
                token = message.content.split('!autotoken ', maxsplit=1)[1]
                self.auto_saved_cookies = {"_ncfa": token}
                with AUTO_COOKIE_DUMP_PATH.open("w") as f:
                    json.dump(self.auto_saved_cookies, f)
                await message.reply("Auto cookies set.")

            elif message.content.casefold().startswith('!sync'):
                logger.info("Syncing commands.")
                await self.bot.tree.sync()
                await message.reply("Commands synced.")


class Bot(commands.Bot):
    """
    A subclass of commands.Bot used as GeoGuessr bot.
    """

    def __init__(self):
        """
        Initialize the bot.
        """
        # No additional intents are needed for this bot.
        intents = discord.Intents.none()
        intents.guilds = True
        intents.dm_messages = True

        super().__init__([], intents=intents)  # No prefix

    async def setup_hook(self) -> None:
        """
        Set up the bot.
        """
        await self.add_cog(GeoGuessr(self))
        if SYNCING_TREE:
            await self.tree.sync()

    async def on_ready(self) -> None:
        """
        Called when the bot is ready.
        """
        logging.info("Logged in as %s (%d).", self.user, self.user.id)

        if (authorized_guilds_env := os.getenv("AUTHORIZED_GUILDS")) is not None:
            authorized_guilds = list(map(int, authorized_guilds_env.split(",")))
            for guild in self.guilds:
                if guild.id not in authorized_guilds and guild.owner != self.user:
                    logging.info("Leaving unauthorized guild %s (%d).", guild, guild.id)
                    # noinspection PyBroadException
                    try:
                        await guild.leave()
                    except Exception:
                        logging.exception("Failed to leave unauthorized guild %s (%d).", guild, guild.id)

    # noinspection PyMethodMayBeStatic
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """
        Called when the bot joins a guild.

        :param guild: The guild that the bot joined.
        """
        logging.info("Joined guild %s (%d).", guild, guild.id)
        if (authorized_guilds_env := os.getenv("AUTHORIZED_GUILDS")) is not None:
            authorized_guilds = list(map(int, authorized_guilds_env.split(",")))
            if guild.id not in authorized_guilds:
                logging.info("Leaving unauthorized guild %s (%d).", guild, guild.id)
                await guild.leave()

    async def on_command_error(self, context: Context[BotT], exception: errors.CommandError, /) -> None:
        """
        Called when an error occurs while invoking a command.

        :param context: The context in which the error occurred.
        :param exception: The error that occurred.
        """
        # noinspection PyUnresolvedReferences
        if (context.interaction is not None and context.interaction.response.is_done() and
                isinstance(exception, errors.CommandOnCooldown | errors.CheckFailure | errors.MissingRequiredArgument |
                           errors.BadArgument | errors.CommandNotFound)):
            return  # Error already handled (probably)

        if isinstance(exception, errors.CommandOnCooldown):
            await context.reply(f"Command is on cooldown. Try again in {int(exception.retry_after)} second(s).",
                                ephemeral=True)
        elif isinstance(exception, errors.CheckFailure):
            await context.reply("You do not have permission to use this command.", ephemeral=True)
        elif isinstance(exception, errors.MissingRequiredArgument):
            await context.reply("Missing required argument.", ephemeral=True)
        elif isinstance(exception, errors.BadArgument):
            await context.reply("Invalid argument.", ephemeral=True)
        elif isinstance(exception, errors.CommandNotFound):
            pass
        else:
            await context.reply("An error occurred while processing the command.", ephemeral=True)
            logging.error("An error occurred while processing the command.", exc_info=exception)


_bot = Bot()
_bot.run(os.environ["DISCORD_BOT_TOKEN"])
