import asyncio
import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from discord.ext import commands
from packaging.requirements import Requirement

from harness.components.config import DEFAULT_CONFIG_FILE_NAME, StraponConfig

if TYPE_CHECKING:
    from harness.bot import HarnessBot

__all__ = ["Strapon", "StraponCog", "StraponMetadata", "RequirementInstallSuccessError"]


def importable_normalise(name: str) -> str:
    """Normalises a project name to an importable string in (almost) accordance with PEP 503.

    :param name: Project name obtained from pyproject.toml.
    :return: An importable name.
    """
    return re.sub(r"[-_.]+", "_", name).lower()


class StraponMetadata:
    def __init__(self, package_dir: Path) -> None:
        self.display_name: str
        self.id: str
        self.requirements: list[Requirement]
        self.import_name: str = package_dir.relative_to(Path().resolve()).as_posix().replace("/", ".")

        pyproject_file = package_dir / "pyproject.toml"
        if not pyproject_file.is_file():
            raise FileNotFoundError(f"Strapon project file not found at: {pyproject_file.resolve()}")
        with open(pyproject_file) as file:  # File read in init is... not great but maybe fine?
            pyproject = tomllib.loads(file.read())

        if "project" not in pyproject or "name" not in pyproject["project"]:
            raise ValueError("Invalid pyproject.toml file. Missing project name.")
        self.display_name = pyproject["project"]["name"]

        if "project" not in pyproject or "id" not in pyproject["project"]:
            raise ValueError("Invalid pyproject.toml file. Missing project id.")
        self.id = pyproject["project"]["id"]
        if self.id != importable_normalise(self.id):
            raise ValueError("Project ID must be importable.")
        if self.id != package_dir.name:
            raise ValueError("Project ID must match package name.")

        self.requirements = list(map(Requirement, pyproject["project"].get("dependencies", [])))


class RequirementInstallSuccessError(Exception):
    ...


class Strapon:
    def __init__(
        self,
        bot: "HarnessBot",
        path: os.PathLike[str],
        config: StraponConfig | None = None,
    ) -> None:
        self._cogs: set[commands.Cog] = set()

        self.bot = bot
        self.package_dir = Path(path)
        self.logger = self.bot.logger.getChild(self.package_dir.name)
        self.metadata = StraponMetadata(self.package_dir)

        self.storage_path = self.bot.data_dir / "storage" / self.metadata.id
        self.storage_path.mkdir(parents=True, exist_ok=True)

        config_path = self.bot.data_dir / "config" / f"{self.metadata.id}.yml"
        default_config_path = self.package_dir / DEFAULT_CONFIG_FILE_NAME

        self.config = config
        self.default_config_file: Path | None = None

        if self.config is not None:
            self.config._config_path = config_path
            self.default_config_file = default_config_path
            if not default_config_path.is_file():
                raise FileNotFoundError(
                    f"A default config file is required if a config object is provided. "
                    f"File not found at: {default_config_path.resolve()}",
                )

    async def load(self) -> None:
        if self.config is not None:
            # noinspection PyProtectedMember
            config_path = self.config._config_path
            assert config_path is not None, "Invalid config object. How did we get here?"
            assert self.default_config_file is not None, "Strapon has config but no default config file."
            if not config_path.is_file():
                config_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(self.default_config_file, config_path)
            try:
                await self.config.load(config_path)
            except Exception as error:
                raise ValueError(f"Failed to load config for strapon {self.metadata.id}") from error

        # We do this after the other far cheaper operations
        installed_new = await self.install_requirements()
        if installed_new:
            raise RequirementInstallSuccessError("New requirements were installed. Strapon reload required.")

        for cog in self._cogs:
            await self.bot.add_cog(cog)

    def register_cog(self, cog: commands.Cog) -> None:
        self._cogs.add(cog)

    async def install_requirements(self) -> bool:
        installed_distributions = tuple(importlib.metadata.distributions())

        def is_unsatisfied(requirement: Requirement) -> bool:
            for installed in installed_distributions:
                if requirement.name != installed.name:
                    continue
                if importlib.metadata.version(requirement.name) in requirement.specifier:
                    return False
            return True

        if not (missing_requirements := tuple(filter(is_unsatisfied, self.metadata.requirements))):
            return False
        self.logger.info(
            "Installing missing requirements: "
            + ", ".join(f'"{req.name}' for req in missing_requirements),
        )
        self.logger.debug(f"Using {'uv' if shutil.which('uv') else 'pip'} to install requirements")
        if shutil.which("uv"):
            cmd = ["uv", "pip", "install", "--python", sys.executable, *map(str, missing_requirements)]
        else:
            cmd = [sys.executable, "-m", "pip", "install", *map(str, missing_requirements)]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None

        async def log_stream(stream: asyncio.StreamReader, logger_func: Callable[[str], None]) -> None:
            while line := (await stream.readline()).decode().rstrip():
                logger_func(line)

        await asyncio.gather(
            log_stream(process.stdout, self.logger.info),
            log_stream(process.stderr, self.logger.error),
        )
        return_code = await process.wait()
        if return_code != 0:
            self.logger.error(f"Failed to install requirements with exit code {return_code}")
            raise subprocess.CalledProcessError(return_code, cmd)
        return True


class StraponCog(commands.Cog):
    ...
