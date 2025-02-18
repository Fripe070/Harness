from pathlib import Path

import aiofiles
import strictyaml

DEFAULT_CONFIG_FILE_NAME = "default_config.yml"


class StraponConfig:
    def __init__(self, schema: strictyaml.Map) -> None:
        self.schema = schema
        self._data: strictyaml.YAML | None = None
        self._config_path: Path | None = None

    @property
    def data(self) -> dict | None:
        if self._data is None:
            return None
        assert isinstance(self._data.data, dict), "Config data is not a dict."
        return self._data.data

    async def load(self, config_path: Path) -> strictyaml.YAML:
        async with aiofiles.open(config_path, "r", encoding="utf-8") as file:
            data = strictyaml.load(
                await file.read(),
                schema=self.schema,
                label=config_path.relative_to(Path()).as_posix(),
            )
        if not isinstance(data.data, dict):
            raise ValueError("Config data is not a dict.")
        self._data = data
        return data

    async def save(self, config_path: Path) -> None:
        if self._data is None:
            raise ValueError("No config loaded.")
        async with aiofiles.open(config_path, "w", encoding="utf-8") as file:
            await file.write(self._data.as_yaml())

