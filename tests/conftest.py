import shutil
from pathlib import Path

import pytest

from coose import db
from coose.config import Config

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "story-template"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    (tmp_path / "coose.toml").write_text(
        '[server]\ndata_dir = "data"\nstories_dir = "stories"\n', encoding="utf-8"
    )
    cfg = Config.load(tmp_path / "coose.toml")
    cfg.stories_dir.mkdir()
    return cfg


@pytest.fixture
def conn(config: Config):
    c = db.connect(config.server_db_path)
    yield c
    c.close()


@pytest.fixture
def story_dir(config: Config) -> Path:
    """A valid story copied from the template, named 'demo'."""
    dest = config.stories_dir / "demo"
    shutil.copytree(TEMPLATE, dest)
    return dest
