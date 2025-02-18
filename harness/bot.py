import asyncio
import datetime
import importlib.machinery
import importlib.util
import inspect
import itertools
import logging
import shutil
import sys
from pathlib import Path
from types import TracebackType
from typing import Any

import discord
import strictyaml
from discord.ext import commands

from harness.components.config import DEFAULT_CONFIG_FILE_NAME, StraponConfig
from harness.components.strapon import RequirementInstallSuccessError, Strapon, StraponMetadata
from harness.internal_utils import IndentFormatter

BOT_CONFIG_SCHEMA = strictyaml.Map({
    "token": strictyaml.Str(),
    "prefix_or_mention": strictyaml.Bool(),
    "prefixes": strictyaml.UniqueSeq(strictyaml.Str()),
    "enabled_strapons": strictyaml.UniqueSeq(strictyaml.Str()),
})


class HarnessBot(commands.Bot):
    logger = logging.getLogger(__name__)

    def __init__(self):
        super().__init__(
            command_prefix=["t!"],
            intents=discord.Intents.all(),
        )
        self.data_dir = Path("data")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.strapons_dir = self.data_dir / "strapons"
        self.strapons_dir.mkdir(parents=True, exist_ok=True)

        self.logs_dir = self.data_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.bot_config_file = self.data_dir / "config.yml"
        self.bot_config: StraponConfig = StraponConfig(BOT_CONFIG_SCHEMA)

        self._registered_strapons: dict[str, Strapon] = {}

    async def start(self, *_, **__) -> None:
        if not self.bot_config_file.is_file():
            default_config_path = Path(__file__).parent / DEFAULT_CONFIG_FILE_NAME
            shutil.copy(default_config_path, self.bot_config_file)
            self.logger.warning(
                f"Config file not found. Copied default config to: {self.bot_config_file.resolve()}.\n"
                f"Please fill out the config and restart the bot.",
            )
            await self.close()
            return

        await self.bot_config.load(self.bot_config_file)
        assert self.bot_config.data is not None, "Bot config failed to load without error?"

        self.command_prefix = (
            commands.when_mentioned_or(*self.bot_config.data["prefixes"])
            if self.bot_config.data["prefix_or_mention"] else
            self.bot_config.data["prefixes"]
        )

        await super().start(token=str(self.bot_config.data["token"]))

    def run(self, *_, **__):
        self.setup_bot_logging()
        super().run(token="", log_handler=None)

    async def setup_hook(self) -> None:  # Called in client.login(), which gets called by start()
        await self.load_all_strapons()

    async def close(self) -> None:
        self.logger.info("Shutting down bot.")
        await super().close()

    def setup_bot_logging(self) -> None:
        # ty andrew for writing ost of this, so I don't need to <3
        def handle_exception(exc_type: type[BaseException], value: BaseException, traceback: TracebackType) -> None:
            self.logger.critical(f"Uncaught {exc_type.__name__}: {value}", exc_info=(exc_type, value, traceback))
        sys.excepthook = handle_exception

        # noinspection PyProtectedMember
        discord.utils.setup_logging(formatter=IndentFormatter(discord.utils._ColourFormatter()))

        timestamp_format = "%Y-%m-%d"
        timestamp_length = len(datetime.date.today().strftime(timestamp_format))

        latest_log_file = self.logs_dir / "latest.log"
        if latest_log_file.is_file():
            with latest_log_file.open(encoding="utf-8") as file:
                date_str = file.read(timestamp_length)
            try:
                datetime.datetime.strptime(date_str, timestamp_format).date()
            except ValueError:
                logging.warning(f"Invalid timestamp in log file: {date_str}")
                date_str = "INVALID"

            for log_number in itertools.count(start=1):
                timestamp_log_path = self.logs_dir / f"{date_str}.{log_number}.log"
                if not timestamp_log_path.is_file():
                    latest_log_file.rename(timestamp_log_path)
                    break

        discord.utils.setup_logging(
            handler=logging.FileHandler(latest_log_file, "w", encoding="utf-8"),
            formatter=IndentFormatter(logging.Formatter(
                fmt="{asctime} [{levelname}] {name}: {message}",
                datefmt="%Y-%m-%d %H:%M:%S",
                style="{",
            )),
        )

        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("discord").setLevel(logging.INFO)

    async def load_extension(self, name: str, *, package: str | None = None) -> None:
        name = self._resolve_name(name, package)
        # noinspection PyUnresolvedReferences
        if name in self._BotBase__extensions:  # pyright: ignore [reportAttributeAccessIssue]
            raise commands.errors.ExtensionAlreadyLoaded(name)

        spec = importlib.util.find_spec(name)
        if spec is None:
            raise commands.errors.ExtensionNotFound(name)

        try:
            await self._load_from_module_spec(spec, name)
        except RequirementInstallSuccessError:
            self.logger.debug(f"Requirements installed for {name}. Re-importing.")
            await self._load_from_module_spec(spec, name)

    # noinspection PyDefaultArgument
    async def _load_from_module_spec(
        self,
        spec: importlib.machinery.ModuleSpec,
        key: str,
    ) -> None:
        # precondition: key not in self.__extensions
        lib = importlib.util.module_from_spec(spec)
        sys.modules[key] = lib
        try:
            spec.loader.exec_module(lib)  # pyright: ignore [reportOptionalMemberAccess]
        except Exception as error:
            del sys.modules[key]
            raise commands.errors.ExtensionFailed(key, error) from error

        try:
            setup_func = lib.setup
        except AttributeError:
            del sys.modules[key]
            raise commands.errors.NoEntryPointError(key)  # noqa: B904 Pulled straight from d.py, I literally don't care
        if not inspect.iscoroutinefunction(setup_func):
            del sys.modules[key]
            raise commands.errors.ExtensionFailed(key, ValueError("setup() must be a coroutine"))

        try:
            ret_value: Strapon | Any = await setup_func(self)
            if not isinstance(ret_value, Strapon):
                raise ValueError(f"Setup function for {key} did not return a {Strapon.__name__}.")
            await ret_value.load()
        except Exception as error:
            del sys.modules[key]
            await self._remove_module_references(lib.__name__)
            await self._call_module_finalizers(lib, key)
            if isinstance(error, RequirementInstallSuccessError):
                raise error # We still do the cleanup, but it's not a "real" error
            raise commands.errors.ExtensionFailed(key, error) from error
        else:
            # noinspection PyUnresolvedReferences
            self._BotBase__extensions[key] = lib  # pyright: ignore [reportAttributeAccessIssue]
            self.logger.info(f"Loaded strapon: {ret_value.metadata.id!r}")

    async def load_all_strapons(self) -> None:
        strapons_dir = self.strapons_dir.resolve()
        if not strapons_dir.is_dir():
            raise FileNotFoundError(f"Strapon directory not found at: {strapons_dir}")

        assert self.bot_config.data is not None and "enabled_strapons" in self.bot_config.data, "Bot config not loaded?"

        potential_strapons: set[StraponMetadata] = set()
        for subdir_path in strapons_dir.iterdir():
            if (
                not (subdir_path / "__init__.py").is_file()
                or not (subdir_path / "pyproject.toml").is_file()
            ):
                self.logger.warning(f"Skipping non-strapon directory: {subdir_path}")
                continue
            metadata = StraponMetadata(subdir_path)
            if metadata.id in self.bot_config.data["enabled_strapons"]:
                potential_strapons.add(metadata)
        if not potential_strapons:
            self.logger.debug("No strapons to equip.")
            return

        failed: set[StraponMetadata] = set()
        async def load_wrapper(meta: StraponMetadata) -> None:
            try:
                await self.load_extension(meta.import_name)
            except Exception as error:
                self.logger.exception(f"Failed to equip strapon {meta.id!r}", exc_info=error)
                failed.add(meta)

        self.logger.info(f"Equipping {len(potential_strapons)} strapons.")
        await asyncio.gather(*(
            load_wrapper(metadata)
            for metadata in potential_strapons
        ))
        if len(potential_strapons - failed) > 0:
            self.logger.info(
                "Finished equipping strapons: "
                + ", ".join(meta.id for meta in potential_strapons - failed),
            )
        else:
            self.logger.warning("All strapons failed to equip.")
        if failed:
            self.logger.warning(
                "Failed to equip strapons: "
                + ", ".join(meta.id for meta in failed),
            )
