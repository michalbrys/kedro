# pylint: disable=expression-not-assigned, pointless-statement
import configparser
import json
import re
from pathlib import Path
from typing import Dict

import pytest
import yaml
from yaml.parser import ParserError

from kedro.config import MissingConfigException, OmegaConfLoader

_DEFAULT_RUN_ENV = "local"
_BASE_ENV = "base"


def _write_yaml(filepath: Path, config: Dict):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    yaml_str = yaml.dump(config)
    filepath.write_text(yaml_str)


def _write_json(filepath: Path, config: Dict):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    json_str = json.dumps(config)
    filepath.write_text(json_str)


def _write_dummy_ini(filepath: Path):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    config = configparser.ConfigParser()
    config["prod"] = {"url": "postgresql://user:pass@url_prod/db"}
    config["staging"] = {"url": "postgresql://user:pass@url_staging/db"}
    with filepath.open("wt") as configfile:  # save
        config.write(configfile)


@pytest.fixture
def base_config(tmp_path):
    filepath = str(tmp_path / "cars.csv")
    return {
        "trains": {"type": "MemoryDataSet"},
        "cars": {
            "type": "pandas.CSVDataSet",
            "filepath": filepath,
            "save_args": {"index": True},
        },
    }


@pytest.fixture
def local_config(tmp_path):
    filepath = str(tmp_path / "cars.csv")
    return {
        "cars": {
            "type": "pandas.CSVDataSet",
            "filepath": filepath,
            "save_args": {"index": False},
        },
        "boats": {"type": "MemoryDataSet"},
    }


@pytest.fixture
def create_config_dir(tmp_path, base_config, local_config):
    proj_catalog = tmp_path / _BASE_ENV / "catalog.yml"
    local_catalog = tmp_path / _DEFAULT_RUN_ENV / "catalog.yml"
    parameters = tmp_path / _BASE_ENV / "parameters.json"
    project_parameters = dict(param1=1, param2=2)

    _write_yaml(proj_catalog, base_config)
    _write_yaml(local_catalog, local_config)
    _write_json(parameters, project_parameters)


@pytest.fixture
def proj_catalog(tmp_path, base_config):
    proj_catalog = tmp_path / _BASE_ENV / "catalog.yml"
    _write_yaml(proj_catalog, base_config)


@pytest.fixture
def proj_catalog_nested(tmp_path):
    path = tmp_path / _BASE_ENV / "catalog" / "dir" / "nested.yml"
    _write_yaml(path, {"nested": {"type": "MemoryDataSet"}})


use_config_dir = pytest.mark.usefixtures("create_config_dir")
use_proj_catalog = pytest.mark.usefixtures("proj_catalog")


class TestOmegaConfLoader:
    @use_config_dir
    def test_load_core_config_dict_syntax(self, tmp_path):
        """Make sure core config can be fetched with a dict [] access."""
        conf = OmegaConfLoader(str(tmp_path))
        params = conf["parameters"]
        catalog = conf["catalog"]

        assert params["param1"] == 1
        assert catalog["trains"]["type"] == "MemoryDataSet"

    @use_config_dir
    def test_load_core_config_get_syntax(self, tmp_path):
        """Make sure core config can be fetched with .get()"""
        conf = OmegaConfLoader(str(tmp_path))
        params = conf.get("parameters")
        catalog = conf.get("catalog")

        assert params["param1"] == 1
        assert catalog["trains"]["type"] == "MemoryDataSet"

    @use_config_dir
    def test_load_local_config_overrides_base(self, tmp_path):
        """Make sure that configs from `local/` override the ones
        from `base/`"""
        conf = OmegaConfLoader(str(tmp_path))
        params = conf["parameters"]
        catalog = conf["catalog"]

        assert params["param1"] == 1
        assert catalog["trains"]["type"] == "MemoryDataSet"
        assert catalog["cars"]["type"] == "pandas.CSVDataSet"
        assert catalog["boats"]["type"] == "MemoryDataSet"
        assert not catalog["cars"]["save_args"]["index"]

    @use_proj_catalog
    def test_load_base_config(self, tmp_path, base_config):
        """Test config loading if `local/` directory is empty"""
        (tmp_path / _DEFAULT_RUN_ENV).mkdir(exist_ok=True)
        catalog = OmegaConfLoader(str(tmp_path))["catalog"]
        assert catalog == base_config

    @use_proj_catalog
    def test_duplicate_patterns(self, tmp_path, base_config):
        """Test config loading if the glob patterns cover the same file"""
        (tmp_path / _DEFAULT_RUN_ENV).mkdir(exist_ok=True)
        conf = OmegaConfLoader(str(tmp_path))
        catalog1 = conf["catalog"]
        catalog2 = conf["catalog"]
        assert catalog1 == catalog2 == base_config

    def test_subdirs_dont_exist(self, tmp_path, base_config):
        """Check the error when config paths don't exist"""
        pattern = (
            r"Given configuration path either does not exist "
            r"or is not a valid directory\: {}"
        )
        with pytest.raises(MissingConfigException, match=pattern.format(".*base")):
            OmegaConfLoader(str(tmp_path))["catalog"]
        with pytest.raises(MissingConfigException, match=pattern.format(".*local")):
            proj_catalog = tmp_path / _BASE_ENV / "catalog.yml"
            _write_yaml(proj_catalog, base_config)
            OmegaConfLoader(str(tmp_path))["catalog"]

    @pytest.mark.usefixtures("create_config_dir", "proj_catalog", "proj_catalog_nested")
    def test_nested(self, tmp_path):
        """Test loading the config from subdirectories"""
        config_loader = OmegaConfLoader(str(tmp_path))
        config_loader.default_run_env = "prod"

        prod_catalog = tmp_path / "prod" / "catalog.yml"
        _write_yaml(prod_catalog, {})

        catalog = config_loader["catalog"]
        assert catalog.keys() == {"cars", "trains", "nested"}
        assert catalog["cars"]["type"] == "pandas.CSVDataSet"
        assert catalog["cars"]["save_args"]["index"] is True
        assert catalog["nested"]["type"] == "MemoryDataSet"

    @use_config_dir
    def test_nested_subdirs_duplicate(self, tmp_path, base_config):
        """Check the error when the configs from subdirectories contain
        duplicate keys"""
        nested = tmp_path / _BASE_ENV / "catalog" / "dir" / "nested.yml"
        _write_yaml(nested, base_config)

        pattern = (
            r"Duplicate keys found in "
            r"(.*catalog\.yml and .*nested\.yml|.*nested\.yml and .*catalog\.yml)"
            r"\: cars, trains"
        )
        with pytest.raises(ValueError, match=pattern):
            OmegaConfLoader(str(tmp_path))["catalog"]

    @use_config_dir
    def test_multiple_nested_subdirs_duplicates(
        self, tmp_path, base_config, local_config
    ):
        """Check the error when several config files from subdirectories contain
        duplicate keys"""
        nested = tmp_path / _BASE_ENV / "catalog" / "dir" / "nested.yml"
        _write_yaml(nested, base_config)

        local = tmp_path / _BASE_ENV / "catalog" / "dir" / "local.yml"
        _write_yaml(local, local_config)

        pattern_catalog_nested = (
            r"Duplicate keys found in "
            r"(.*catalog\.yml and .*nested\.yml|.*nested\.yml and .*catalog\.yml)"
            r"\: cars, trains"
        )
        pattern_catalog_local = (
            r"Duplicate keys found in "
            r"(.*catalog\.yml and .*local\.yml|.*local\.yml and .*catalog\.yml)"
            r"\: cars"
        )
        pattern_nested_local = (
            r"Duplicate keys found in "
            r"(.*nested\.yml and .*local\.yml|.*local\.yml and .*nested\.yml)"
            r"\: cars"
        )

        with pytest.raises(ValueError) as exc:
            OmegaConfLoader(str(tmp_path))["catalog"]
        assert re.search(pattern_catalog_nested, str(exc.value))
        assert re.search(pattern_catalog_local, str(exc.value))
        assert re.search(pattern_nested_local, str(exc.value))

    @use_config_dir
    def test_bad_config_syntax(self, tmp_path):
        conf_path = tmp_path / _BASE_ENV
        conf_path.mkdir(parents=True, exist_ok=True)
        (conf_path / "catalog.yml").write_text("bad:\nconfig")

        pattern = f"Invalid YAML or JSON file {conf_path}"
        with pytest.raises(ParserError, match=re.escape(pattern)):
            OmegaConfLoader(str(tmp_path))["catalog"]

    def test_lots_of_duplicates(self, tmp_path):
        data = {str(i): i for i in range(100)}
        _write_yaml(tmp_path / _BASE_ENV / "catalog1.yml", data)
        _write_yaml(tmp_path / _BASE_ENV / "catalog2.yml", data)

        conf = OmegaConfLoader(str(tmp_path))
        pattern = (
            r"Duplicate keys found in "
            r"(.*catalog2\.yml and .*catalog1\.yml|.*catalog1\.yml and .*catalog2\.yml)"
            r"\: .*\.\.\.$"
        )
        with pytest.raises(ValueError, match=pattern):
            conf["catalog"]

    @use_config_dir
    def test_same_key_in_same_dir(self, tmp_path, base_config):
        """Check the error if 2 files in the same config dir contain
        the same top-level key"""
        dup_json = tmp_path / _BASE_ENV / "catalog.json"
        _write_json(dup_json, base_config)

        pattern = (
            r"Duplicate keys found in "
            r"(.*catalog\.yml and .*catalog\.json|.*catalog\.json and .*catalog\.yml)"
            r"\: cars, trains"
        )
        with pytest.raises(ValueError, match=pattern):
            OmegaConfLoader(str(tmp_path))["catalog"]

    @use_config_dir
    def test_pattern_key_not_found(self, tmp_path):
        """Check the error if no config files satisfy a given pattern"""
        key = "non-existent-pattern"
        pattern = f"No config patterns were found for '{key}' in your config loader"
        with pytest.raises(KeyError, match=pattern):
            OmegaConfLoader(str(tmp_path))[key]

    @use_config_dir
    def test_cannot_load_non_yaml_or_json_files(self, tmp_path):
        db_patterns = {"db": ["db*"]}
        db_config_path = tmp_path / _BASE_ENV / "db.ini"
        _write_dummy_ini(db_config_path)

        conf = OmegaConfLoader(str(tmp_path), config_patterns=db_patterns)
        pattern = (
            r"No files of YAML or JSON format found in "
            r".*base or "
            r".*local "
            r"matching the glob pattern\(s\): "
            r"\[\'db\*\'\]"
        )
        with pytest.raises(MissingConfigException, match=pattern):
            conf["db"]

    @use_config_dir
    def test_no_files_found(self, tmp_path):
        """Check the error if no config files satisfy a given pattern"""
        pattern = (
            r"No files of YAML or JSON format found in "
            r".*base or "
            r".*local "
            r"matching the glob pattern\(s\): "
            r"\[\'credentials\*\', \'credentials\*/\**\', \'\**/credentials\*\'\]"
        )
        with pytest.raises(MissingConfigException, match=pattern):
            OmegaConfLoader(str(tmp_path))["credentials"]

    def test_overlapping_patterns(self, tmp_path, mocker):
        """Check that same configuration file is not loaded more than once."""
        _write_yaml(
            tmp_path / _BASE_ENV / "catalog0.yml",
            {"env": _BASE_ENV, "common": "common"},
        )
        _write_yaml(
            tmp_path / "dev" / "catalog1.yml", {"env": "dev", "dev_specific": "wiz"}
        )
        _write_yaml(tmp_path / "dev" / "user1" / "catalog2.yml", {"user1_c2": True})

        catalog_patterns = {
            "catalog": [
                "catalog*",
                "catalog*/**",
                "../**/user1/catalog2*",
                "../**/catalog2*",
            ]
        }

        catalog = OmegaConfLoader(
            conf_source=str(tmp_path), env="dev", config_patterns=catalog_patterns
        )["catalog"]
        expected_catalog = {
            "env": "dev",
            "common": "common",
            "dev_specific": "wiz",
            "user1_c2": True,
        }
        assert catalog == expected_catalog

        mocked_load = mocker.patch("omegaconf.OmegaConf.load")
        expected_path = (tmp_path / "dev" / "user1" / "catalog2.yml").resolve()
        assert mocked_load.called_once_with(expected_path)

    def test_yaml_parser_error(self, tmp_path):
        conf_path = tmp_path / _BASE_ENV
        conf_path.mkdir(parents=True, exist_ok=True)

        example_catalog = """
        example_iris_data:
              type: pandas.CSVDataSet
          filepath: data/01_raw/iris.csv
        """

        (conf_path / "catalog.yml").write_text(example_catalog)

        msg = (
            f"Invalid YAML or JSON file {conf_path / 'catalog.yml'}, unable to read"
            f" line 3, position 10."
        )
        with pytest.raises(ParserError, match=re.escape(msg)):
            OmegaConfLoader(str(tmp_path))["catalog"]

    def test_customised_config_patterns(self, tmp_path):
        config_loader = OmegaConfLoader(
            conf_source=str(tmp_path),
            config_patterns={
                "spark": ["spark*/"],
                "parameters": ["params*", "params*/**", "**/params*"],
            },
        )
        assert config_loader.config_patterns["catalog"] == [
            "catalog*",
            "catalog*/**",
            "**/catalog*",
        ]
        assert config_loader.config_patterns["spark"] == ["spark*/"]
        assert config_loader.config_patterns["parameters"] == [
            "params*",
            "params*/**",
            "**/params*",
        ]

    def test_destructive_merging_strategy(self, tmp_path):
        mlflow_patterns = {"mlflow": ["mlflow*", "mlflow*/**", "**/mlflow*"]}
        base_mlflow = tmp_path / _BASE_ENV / "mlflow.yml"
        base_config = {
            "tracking": {
                "disable_tracking": {"pipelines": "[on_exit_notification]"},
                "experiment": {
                    "name": "name-of-local-experiment",
                },
                "params": {"long_params_strategy": "tag"},
            }
        }
        local_mlflow = tmp_path / _DEFAULT_RUN_ENV / "mlflow.yml"
        local_config = {
            "tracking": {
                "experiment": {
                    "name": "name-of-prod-experiment",
                },
            }
        }

        _write_yaml(base_mlflow, base_config)
        _write_yaml(local_mlflow, local_config)

        conf = OmegaConfLoader(str(tmp_path), config_patterns=mlflow_patterns)["mlflow"]

        assert conf == {
            "tracking": {
                "experiment": {
                    "name": "name-of-prod-experiment",
                },
            }
        }

    @use_config_dir
    def test_adding_extra_keys_to_confloader(self, tmp_path):
        """Make sure extra keys can be added directly to the config loader instance."""
        conf = OmegaConfLoader(str(tmp_path))
        catalog = conf["catalog"]
        conf["spark"] = {"spark_config": "emr.blabla"}

        assert catalog["trains"]["type"] == "MemoryDataSet"
        assert conf["spark"] == {"spark_config": "emr.blabla"}

    @use_config_dir
    def test_bypass_catalog_config_loading(self, tmp_path):
        """Make sure core config loading can be bypassed by setting the key and values
        directly on the config loader instance."""
        conf = OmegaConfLoader(str(tmp_path))
        conf["catalog"] = {"catalog_config": "something_new"}

        assert conf["catalog"] == {"catalog_config": "something_new"}
