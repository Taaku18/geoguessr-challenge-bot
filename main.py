from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import pathlib
import random
import typing
from zoneinfo import ZoneInfo

import aiohttp
from discord.ext.commands import Context, errors
from dotenv import load_dotenv
from playwright.async_api import async_playwright, BrowserContext, Browser, Playwright

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


class Geoguessr(commands.Cog):
    """
    A cog for Geoguessr-related commands.
    """

    def __init__(self, bot: Bot):
        """
        Initialize the Geoguessr cog.

        :param bot: The bot instance.
        """
        self.bot = bot
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.main_context: BrowserContext | None = None
        self.auto_context: BrowserContext | None = None
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
        Callback for when the Geoguessr cog loads.
        """
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)

        self.main_context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        await self.main_context.set_extra_http_headers(
            {"Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate, br"}
        )
        self.auto_context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        await self.auto_context.set_extra_http_headers(
            {"Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate, br"}
        )

        # Get Geoguessr cookies if not already saved.
        if not self.main_saved_cookies:
            await self.get_geoguessr_cookies("main")

        if not self.auto_saved_cookies:
            await self.get_geoguessr_cookies("auto")

        await self._load_map_data()
        self.load_map_data.start()
        self.daily_challenge_task.start()

    async def cog_unload(self) -> None:
        """
        Callback for when the Geoguessr cog unloads.
        """
        self.load_map_data.cancel()
        self.load_map_data.stop()
        self.daily_challenge_task.cancel()
        self.daily_challenge_task.stop()
        await self.main_context.close()
        await self.auto_context.close()
        await self.browser.close()
        await self.playwright.stop()
        self.main_context = self.auto_context = self.browser = self.playwright = None

    async def _load_map_data(self) -> None:
        """
        Load the data for all maps available on Geoguessr.
        """
        # Load all maps data.
        try:
            for repeat_c in range(2):  # Try twice in case of Unauthorized error
                async with aiohttp.ClientSession(cookies=self.main_saved_cookies) as session:
                    async with session.get("https://www.geoguessr.com/api/maps/explorer") as response:
                        if response.status == 401 and repeat_c == 0:
                            self.main_saved_cookies = {}
                            await self.get_geoguessr_cookies("main")
                            continue

                        response.raise_for_status()

                        data = await response.json()
                        self.all_maps_data = {
                            map_data["slug"]: {"name": map_data["name"], "countryCode": map_data["countryCode"]}
                            for map_data in data
                        }
                break
        except Exception:
            logger.critical("Failed to load all maps data.", exc_info=True)

    @tasks.loop(hours=1, reconnect=False)
    async def load_map_data(self) -> None:
        """
        Hourly task to load the data for all maps available on Geoguessr.
        """
        await asyncio.sleep(random.randint(0, 600))  # Random delay of up to 10 minutes to avoid rate limiting.
        await self._load_map_data()

    @staticmethod
    def _get_daily_embed(link: str) -> discord.Embed:
        date = datetime.datetime.now(tz=tzinfo).strftime("%B %d %Y")
        short_link = link.replace("https://", "")
        embed = discord.Embed(
            title=f"Daily Geoguessr Challenge",
            description=f"Here is the link to today's Geoguessr challenge:\n[{short_link}]({link})",
            colour=discord.Colour.from_rgb(167, 199, 231),
        )

        embed.set_author(name=date, url=link)

        embed.set_footer(text=f"Use of external help is not allowed (e.g. Google) · Good luck!")
        return embed

    async def _send_daily_challenge(self, guild: discord.Guild, *, send_leaderboard: bool = False) -> None:
        """
        Send the daily Geoguessr challenge for the specified guild.

        :param guild: The guild.
        """
        daily_config = await self.get_daily_config(guild.id)
        if daily_config is None:
            return

        channel = self.bot.get_channel(daily_config["channel"])
        if not channel:
            logger.error("Daily Geoguessr challenge channel not found.")
            return

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
                            f"- {entry['score']} points"
                            for i, entry in enumerate(leaderboard[:10])
                        ),
                        inline=False,
                    )

        await channel.send(embed=embed)

    @tasks.loop(
        time=datetime.time(hour=0, minute=0, second=0, tzinfo=tzinfo),
        reconnect=False,
    )
    async def daily_challenge_task(self) -> None:
        """
        Task to send the daily Geoguessr challenge.
        """
        logger.info("Sending daily Geoguessr challenges.")
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
                return daily_config
            return None

    async def set_daily_config(
        self,
        guild_id: int,
        channel_id: int | None,
        map_name: str = "world",
        time_limit: int = 0,
        no_move: bool = False,
        no_pan: bool = False,
        no_zoom: bool = False,
    ) -> None:
        """
        Set the daily config for the specified guild.

        :param guild_id: The ID of the guild.
        :param channel_id: The ID of the channel. Set to None to remove the daily channel.
        :param map_name: The name of the map to use.
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
                if str(guild_id) not in config:
                    config[str(guild_id)] = {}

                config[str(guild_id)]["daily_config"] = {
                    "channel": channel_id,
                    "map_name": map_name,
                    "time_limit": time_limit,
                    "no_move": no_move,
                    "no_pan": no_pan,
                    "no_zoom": no_zoom,
                }
            with CONFIG_PATH.open("w") as f:
                json.dump(config, f, indent=4)

    async def get_daily_link(self, guild_id: int, *, force: bool = False) -> str | None:
        """
        Get the daily Geoguessr challenge link. If it doesn't exist, generate a new one.

        :param guild_id: The ID of the guild.
        :param force: Whether to force generation of a new link.
        :return: The Geoguessr challenge link. None if daily challenge is not set up.
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
                daily_config["map_name"],
                daily_config["time_limit"],
                daily_config["no_move"],
                daily_config["no_pan"],
                daily_config["no_zoom"],
                auto_guess=True,
            )
            if daily_link is None:
                logger.error("Failed to get Geoguessr challenge link.")
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
        Get the headers to use for requests to Geoguessr.
        """
        return {
            "Referer": "https://www.geoguessr.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }

    async def get_geoguessr_cookies(self, type_: typing.Literal["main", "auto"], /) -> None:
        """
        Get the cookies required to access Geoguessr.
        """
        logging.info("Getting Geoguessr cookies for %s.", type_)
        context = self.main_context if type_ == "main" else self.auto_context
        page = await context.new_page()

        # Navigate to geoguessr.com.
        await page.goto("https://www.geoguessr.com")

        # Check if the login button exists.
        login_button = await page.query_selector('a[href="/signin"]')
        if login_button:  # If the login button exists, the user is not logged in

            await page.goto("https://www.geoguessr.com/signin")
            await asyncio.sleep(1)

            if type_ == "main":
                email = os.environ["GEOGUESSR_EMAIL"]
                password = os.environ["GEOGUESSR_PASSWORD"]
            else:
                email = os.environ["GEOGUESSR_AUTO_EMAIL"]
                password = os.environ["GEOGUESSR_AUTO_PASSWORD"]

            # Find the email and password input fields and fill them and submit the form.
            await page.fill('input[type="email"]', email)
            await page.fill('input[type="password"]', password)
            await page.click('button[type="submit"]')

            # Wait for navigation to complete after login.
            await page.wait_for_url(lambda x: True, wait_until="commit")
            await asyncio.sleep(1)
            await page.goto("https://www.geoguessr.com")
            await page.wait_for_load_state("load")

        await page.close()

        # Save the cookies.
        cookies = {cookie["name"]: cookie["value"] for cookie in await context.cookies()}

        if type_ == "main":
            self.main_saved_cookies = cookies
            with MAIN_COOKIE_DUMP_PATH.open("w") as f:
                json.dump(cookies, f)
        else:
            self.auto_saved_cookies = cookies
            with AUTO_COOKIE_DUMP_PATH.open("w") as f:
                json.dump(cookies, f)

    async def get_geoguessr_challenge_link(
        self,
        map_name: str = "world",
        time_limit: int = 0,
        no_move: bool = False,
        no_pan: bool = False,
        no_zoom: bool = False,
        *,
        auto_guess: bool = False,
    ) -> str | None:
        """
        Get a Geoguessr challenge link with the specified settings.

        :param map_name: The name of the map to use.
        :param time_limit: The time limit for the challenge in seconds.
        :param no_move: Whether to forbid moving.
        :param no_pan: Whether to forbid rotating.
        :param no_zoom: Whether to forbid zooming.
        :param auto_guess: Whether to automatically guess the location (for daily's).
        :return: The Geoguessr challenge link. None if an error occurred.
        """

        logger.info(
            "Getting Geoguessr challenge link with options: %s, %s, %s, %s, %s",
            map_name,
            time_limit,
            no_move,
            no_pan,
            no_zoom,
        )

        # Get Geoguessr cookies if not already saved.
        if not self.main_saved_cookies:
            await self.get_geoguessr_cookies("main")

        data = {
            "map": map_name,
            "timeLimit": time_limit,
            "forbidMoving": no_move,
            "forbidZooming": no_zoom,
            "forbidRotating": no_pan,
        }

        try:
            for repeat_c in range(2):  # Try twice in case of Unauthorized error
                # Send POST request with specific cookies in the header using aiohttp
                async with aiohttp.ClientSession(cookies=self.main_saved_cookies) as session:
                    url = "https://www.geoguessr.com/api/v3/challenges"
                    async with session.post(url, headers=self.headers, json=data) as response:
                        if response.status == 401 and repeat_c == 0:  # Unauthorized
                            self.main_saved_cookies = {}
                            await self.get_geoguessr_cookies("main")
                            continue

                        if response.status == 500:
                            logger.info("Failed to get Geoguessr challenge link: invalid options. %s", data)
                            return None

                        response.raise_for_status()

                        resp = await response.json()
                        token = resp["token"]
                break

        except Exception:
            logger.exception("Failed to get Geoguessr challenge link.", exc_info=True)
            return None

        if auto_guess:
            # noinspection PyAsyncCall
            asyncio.create_task(self.auto_guess(token))

        return f"https://www.geoguessr.com/challenge/{token}"

    async def auto_guess(self, token: str) -> None:
        """
        Automatically guess the location in the Geoguessr challenge (in Antarctica).
        :param token: The token of the challenge.
        """

        logger.info("Automatically guessing the location in the Geoguessr challenge.")
        try:
            for repeat_c in range(2):  # Try twice in case of Unauthorized error
                # Send POST request with specific cookies in the header using aiohttp
                async with aiohttp.ClientSession(cookies=self.auto_saved_cookies) as session:
                    url = f"https://www.geoguessr.com/api/v3/challenges/{token}"
                    async with session.post(url, headers=self.headers, json={}) as response:
                        if response.status == 401 and repeat_c == 0:  # Unauthorized
                            self.auto_saved_cookies = {}
                            await self.get_geoguessr_cookies("auto")
                            continue

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
                break

        except Exception:
            logger.exception("Failed to get Geoguessr challenge link.", exc_info=True)
            return

        logger.info("Successfully guessed the location in the Geoguessr challenge.")

    async def get_game_results(self, guild_id: int, date: str) -> list[dict[str, typing.Any]] | None:
        """
        Get the daily geoguessr results for the specified date.

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
        try:
            for repeat_c in range(3):  # Try twice in case of Unauthorized error
                async with aiohttp.ClientSession(cookies=self.auto_saved_cookies) as session:
                    # TODO: May need to handle pagination.
                    async with session.get(
                        url, headers=self.headers, params={"friends": "false", "limit": 9999, "minRounds": 5}
                    ) as response:

                        if response.status == 401:
                            if repeat_c == 0:
                                self.auto_saved_cookies = {}
                                await self.get_geoguessr_cookies("auto")
                                continue
                            elif repeat_c == 1:  # The bot hasn't guessed yet
                                await self.auto_guess(token)
                                continue

                        response.raise_for_status()

                        data = await response.json()
                break

            results = []
            for entry in data["items"]:
                if entry["playerName"] == os.environ["GEOGUESSR_AUTO_USERNAME"]:
                    continue  # Skip the bot's entry

                rounds = []
                for round_ in entry["game"]["player"]["guesses"]:
                    rounds.append(
                        {
                            "distance": round_["distanceInMeters"],
                            "score": round_["roundScore"]["amount"],
                        }
                    )

                results.append(
                    {
                        "username": entry["playerName"],
                        "user_id": entry["userId"],
                        "score": entry["game"]["player"]["totalScore"]["amount"],
                        "distance": entry["game"]["player"]["totalDistanceInMeters"],
                        "rounds": rounds,
                    }
                )
            results.sort(key=lambda x: int(x["score"]), reverse=True)

        except Exception:
            logger.exception("Failed to get Geoguessr leaderboard.", exc_info=True)
            return None

        return results

    # noinspection PyUnusedLocal
    async def map_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """
        Autocomplete the map name for the Geoguessr command.

        :param interaction: The interaction.
        :param current: The current input.
        :return: The list of autocomplete choices.
        """
        current = current.casefold()
        # World and Famous Places are always available on top.
        possibilities: list[tuple[str, str]] = [("World", "world"), ("Famous Places", "famous-places")]
        possibilities += sorted(
            [
                (data["name"], slug)
                for slug, data in self.all_maps_data.items()
                if current in data["name"].casefold() or current in data["countryCode"].casefold()
            ]
        )
        possibilities = possibilities[:25]  # Limit to 25 choices
        return [app_commands.Choice(name=name, value=slug) for name, slug in possibilities]

    def _parse_map_name(self, map_name: str) -> str | None:
        """
        Parse the map name.

        :param map_name: The map name.
        :return: The parsed map name. None if invalid.
        """

        if map_name not in {"world", "famous-places"} and map_name not in self.all_maps_data:  # Not a valid slug
            map_name_mapping = {
                "world": "world",
                "famous places": "famous-places",
            }
            for slug, data in self.all_maps_data.items():
                map_name_mapping[data["name"].casefold()] = slug
                map_name_mapping[data["countryCode"].casefold()] = slug
            if map_name.casefold() in map_name_mapping:
                map_name = map_name_mapping[map_name.casefold()]
            else:
                return None
        return map_name

    @commands.cooldown(1, 60, commands.BucketType.user)
    @commands.hybrid_command()
    @app_commands.rename(
        map_name="map", time_limit="time-limit", no_move="no-moving", no_pan="no-panning", no_zoom="no-zooming"
    )
    @app_commands.autocomplete(map_name=map_name_autocomplete)
    async def geochallenge(
        self,
        ctx: commands.Context,
        map_name: str = "world",
        time_limit: commands.Range[int, 0] = 0,
        no_move: bool = False,
        no_pan: bool = False,
        no_zoom: bool = False,
    ) -> None:
        """
        Start a new Geoguessr challenge.

        :param map_name: The name of the map to use. Default: world.
        :param time_limit: The time limit for the challenge in seconds. Default: no time limit.
        :param no_move: Whether to forbid moving. Default: moving is allowed.
        :param no_pan: Whether to forbid panning. Default: panning is allowed.
        :param no_zoom: Whether to forbid zooming. Default: zooming is allowed.
        """

        if (map_name := self._parse_map_name(map_name)) is None:
            await ctx.reply("Invalid map name.", ephemeral=True)
            return

        link = await self.get_geoguessr_challenge_link(map_name, time_limit, no_move, no_pan, no_zoom)
        if link is None:
            await ctx.reply("Failed to get Geoguessr challenge link.", ephemeral=True)
            return

        await ctx.reply(f"Hey {ctx.author.mention}! Here is your Geoguessr challenge link:\n{link}")

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
        map_name: str = "world",
        time_limit: commands.Range[int, 0] = 180,
        no_move: bool = False,
        no_pan: bool = False,
        no_zoom: bool = False,
    ) -> None:
        """
        Set up the daily Geoguessr challenge channel.

        :param channel: The channel to set as the daily Geoguessr challenge channel.
        :param map_name: The name of the map to use. Default: world.
        :param time_limit: The time limit for the challenge in seconds. Default: 3 minutes.
        :param no_move: Whether to forbid moving. Default: moving is allowed.
        :param no_pan: Whether to forbid panning. Default: panning is allowed.
        :param no_zoom: Whether to forbid zooming. Default: zooming is allowed.
        """
        if channel is None:
            channel = ctx.channel

        if (map_name := self._parse_map_name(map_name)) is None:
            await ctx.reply("Invalid map name.", ephemeral=True)
            return

        await self.get_daily_link(ctx.guild.id, force=True)  # Force generation of a new link

        await self.set_daily_config(ctx.guild.id, channel.id, map_name, time_limit, no_move, no_pan, no_zoom)
        await self._send_daily_challenge(ctx.guild)

        if map_name in {"world", "famous-places"}:
            proper_name = map_name.title().replace("-", " ")
        else:
            proper_name = self.all_maps_data[map_name]["name"]

        time_limit_text = "No limit" if time_limit == 0 else f"{time_limit}s"

        mpz = (
            f"Moving: {'❌' if no_move else '✅'}\n"
            f"Panning: {'❌' if no_pan else '✅'}\n"
            f"Zooming: {'❌' if no_zoom else '✅'}"
        )

        await ctx.reply(
            f"Daily Geoguessr challenge channel set to {channel.mention}!"
            f"\n\nMap: {proper_name}\nTime Limit: {time_limit_text}\n{mpz}"
        )

    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @commands.hybrid_command()
    async def cancelgeodaily(self, ctx: commands.Context) -> None:
        """
        Stop sending daily Geoguessr challenges.
        """
        if self.get_daily_config(ctx.guild.id) is None:
            await ctx.reply("Daily Geoguessr challenges are not set up.", ephemeral=True)
            return

        await self.set_daily_config(ctx.guild.id, None)
        await ctx.reply("Daily Geoguessr challenges have been stopped.")

    @commands.cooldown(1, 20, commands.BucketType.user)
    @commands.guild_only()
    @commands.hybrid_command()
    async def geodaily(self, ctx: commands.Context) -> None:
        """
        Shows the daily Geoguessr challenge.
        """
        await ctx.defer()
        link = await self.get_daily_link(ctx.guild.id)
        if link is None:
            await ctx.reply("Daily Geoguessr challenge is not set up.", ephemeral=True)
            return

        embed = self._get_daily_embed(link)

        leaderboard = await self.get_game_results(ctx.guild.id, datetime.datetime.now(tz=tzinfo).strftime("%Y-%m-%d"))
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
                        f"- {entry['score']} points"
                        for i, entry in enumerate(leaderboard[:10])
                    ),
                    inline=False,
                )

            embed.set_footer(text="Leaderboard updated at")
            embed.timestamp = datetime.datetime.now(tz=tzinfo)

        await ctx.reply(embed=embed)


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

        super().__init__([], intents=intents)  # No prefix

    async def setup_hook(self) -> None:
        """
        Set up the bot.
        """
        await self.add_cog(Geoguessr(self))
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
                    try:
                        await guild.leave()
                    except Exception:
                        logging.exception("Failed to leave unauthorized guild %s (%d).", guild, guild.id)

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
        if isinstance(exception, errors.CommandOnCooldown):
            await context.reply(f"Command is on cooldown. Try again in {int(exception.retry_after)} second(s).", ephemeral=True)
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
