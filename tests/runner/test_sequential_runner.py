from typing import Any, Dict

import pandas as pd
import pytest
import re

from kedro.framework.hooks import _create_hook_manager
from kedro.io import (
    AbstractDataSet,
    DataCatalog,
    DataSetError,
    LambdaDataSet,
    MemoryDataSet,
)
from kedro.pipeline import Pipeline, node
from kedro.runner import SequentialRunner
from tests.runner.conftest import identity, sink, source, exception_fn


@pytest.fixture
def memory_catalog():
    ds1 = MemoryDataSet({"data": 42})
    ds2 = MemoryDataSet([1, 2, 3, 4, 5])
    return DataCatalog({"ds1": ds1, "ds2": ds2})


@pytest.fixture
def pandas_df_feed_dict():
    pandas_df = pd.DataFrame({"Name": ["Alex", "Bob"], "Age": [15, 25]})
    return {"ds3": pandas_df}


@pytest.fixture
def conflicting_feed_dict(pandas_df_feed_dict):
    ds1 = MemoryDataSet({"data": 0})
    ds3 = pandas_df_feed_dict["ds3"]
    return {"ds1": ds1, "ds3": ds3}


def multi_input_list_output(arg1, arg2):
    return [arg1, arg2]


class TestValidSequentialRunner:
    def test_run_with_plugin_manager(self, fan_out_fan_in, catalog):
        catalog.add_feed_dict(dict(A=42))
        result = SequentialRunner().run(
            fan_out_fan_in, catalog, hook_manager=_create_hook_manager()
        )
        assert "Z" in result
        assert result["Z"] == (42, 42, 42)

    def test_run_without_plugin_manager(self, fan_out_fan_in, catalog):
        catalog.add_feed_dict(dict(A=42))
        result = SequentialRunner().run(fan_out_fan_in, catalog)
        assert "Z" in result
        assert result["Z"] == (42, 42, 42)


@pytest.mark.parametrize("is_async", [False, True])
class TestSeqentialRunnerBranchlessPipeline:
    def test_no_input_seq(self, is_async, branchless_no_input_pipeline, catalog):
        outputs = SequentialRunner(is_async=is_async).run(
            branchless_no_input_pipeline, catalog
        )
        assert "E" in outputs
        assert len(outputs) == 1

    def test_no_data_sets(self, is_async, branchless_pipeline):
        catalog = DataCatalog({}, {"ds1": 42})
        outputs = SequentialRunner(is_async=is_async).run(branchless_pipeline, catalog)
        assert "ds3" in outputs
        assert outputs["ds3"] == 42

    def test_no_feed(self, is_async, memory_catalog, branchless_pipeline):
        outputs = SequentialRunner(is_async=is_async).run(
            branchless_pipeline, memory_catalog
        )
        assert "ds3" in outputs
        assert outputs["ds3"]["data"] == 42

    def test_node_returning_none(self, is_async, saving_none_pipeline, catalog):
        pattern = "Saving 'None' to a 'DataSet' is not allowed"
        with pytest.raises(DataSetError, match=pattern):
            SequentialRunner(is_async=is_async).run(saving_none_pipeline, catalog)

    def test_result_saved_not_returned(self, is_async, saving_result_pipeline):
        """The pipeline runs ds->dsX but save does not save the output."""

        def _load():
            return 0

        def _save(arg):
            assert arg == 0

        catalog = DataCatalog(
            {
                "ds": LambdaDataSet(load=_load, save=_save),
                "dsX": LambdaDataSet(load=_load, save=_save),
            }
        )
        output = SequentialRunner(is_async=is_async).run(
            saving_result_pipeline, catalog
        )

        assert output == {}


@pytest.fixture
def unfinished_outputs_pipeline():
    return Pipeline(
        [
            node(identity, dict(arg="ds4"), "ds8", name="node1"),
            node(sink, "ds7", None, name="node2"),
            node(multi_input_list_output, ["ds3", "ds4"], ["ds6", "ds7"], name="node3"),
            node(identity, "ds2", "ds5", name="node4"),
            node(identity, "ds1", "ds4", name="node5"),
        ]
    )  # Outputs: ['ds8', 'ds5', 'ds6'] == ['ds1', 'ds2', 'ds3']


@pytest.mark.parametrize("is_async", [False, True])
class TestSeqentialRunnerBranchedPipeline:
    def test_input_seq(
        self,
        is_async,
        memory_catalog,
        unfinished_outputs_pipeline,
        pandas_df_feed_dict,
    ):
        memory_catalog.add_feed_dict(pandas_df_feed_dict, replace=True)
        outputs = SequentialRunner(is_async=is_async).run(
            unfinished_outputs_pipeline, memory_catalog
        )
        assert set(outputs.keys()) == {"ds8", "ds5", "ds6"}
        # the pipeline runs ds2->ds5
        assert outputs["ds5"] == [1, 2, 3, 4, 5]
        assert isinstance(outputs["ds8"], dict)
        # the pipeline runs ds1->ds4->ds8
        assert outputs["ds8"]["data"] == 42
        # the pipline runs ds3
        assert isinstance(outputs["ds6"], pd.DataFrame)

    def test_conflict_feed_catalog(
        self,
        is_async,
        memory_catalog,
        unfinished_outputs_pipeline,
        conflicting_feed_dict,
    ):
        """ds1 and ds3 will be replaced with new inputs."""
        memory_catalog.add_feed_dict(conflicting_feed_dict, replace=True)
        outputs = SequentialRunner(is_async=is_async).run(
            unfinished_outputs_pipeline, memory_catalog
        )
        assert isinstance(outputs["ds8"], dict)
        assert outputs["ds8"]["data"] == 0
        assert isinstance(outputs["ds6"], pd.DataFrame)

    def test_unsatisfied_inputs(self, is_async, unfinished_outputs_pipeline, catalog):
        """ds1, ds2 and ds3 were not specified."""
        with pytest.raises(ValueError, match=r"not found in the DataCatalog"):
            SequentialRunner(is_async=is_async).run(
                unfinished_outputs_pipeline, catalog
            )


class LoggingDataSet(AbstractDataSet):
    def __init__(self, log, name, value=None):
        self.log = log
        self.name = name
        self.value = value

    def _load(self) -> Any:
        self.log.append(("load", self.name))
        return self.value

    def _save(self, data: Any) -> None:
        self.value = data

    def _release(self) -> None:
        self.log.append(("release", self.name))
        self.value = None

    def _describe(self) -> Dict[str, Any]:
        return {}


@pytest.mark.parametrize("is_async", [False, True])
class TestSequentialRunnerRelease:
    def test_dont_release_inputs_and_outputs(self, is_async):
        log = []
        pipeline = Pipeline(
            [node(identity, "in", "middle"), node(identity, "middle", "out")]
        )
        catalog = DataCatalog(
            {
                "in": LoggingDataSet(log, "in", "stuff"),
                "middle": LoggingDataSet(log, "middle"),
                "out": LoggingDataSet(log, "out"),
            }
        )
        SequentialRunner(is_async=is_async).run(pipeline, catalog)

        # we don't want to see release in or out in here
        assert log == [("load", "in"), ("load", "middle"), ("release", "middle")]

    def test_release_at_earliest_opportunity(self, is_async):
        log = []
        pipeline = Pipeline(
            [
                node(source, None, "first"),
                node(identity, "first", "second"),
                node(sink, "second", None),
            ]
        )
        catalog = DataCatalog(
            {
                "first": LoggingDataSet(log, "first"),
                "second": LoggingDataSet(log, "second"),
            }
        )
        SequentialRunner(is_async=is_async).run(pipeline, catalog)

        # we want to see "release first" before "load second"
        assert log == [
            ("load", "first"),
            ("release", "first"),
            ("load", "second"),
            ("release", "second"),
        ]

    def test_count_multiple_loads(self, is_async):
        log = []
        pipeline = Pipeline(
            [
                node(source, None, "dataset"),
                node(sink, "dataset", None, name="bob"),
                node(sink, "dataset", None, name="fred"),
            ]
        )
        catalog = DataCatalog({"dataset": LoggingDataSet(log, "dataset")})
        SequentialRunner(is_async=is_async).run(pipeline, catalog)

        # we want to the release after both the loads
        assert log == [("load", "dataset"), ("load", "dataset"), ("release", "dataset")]

    def test_release_transcoded(self, is_async):
        log = []
        pipeline = Pipeline(
            [node(source, None, "ds@save"), node(sink, "ds@load", None)]
        )
        catalog = DataCatalog(
            {
                "ds@save": LoggingDataSet(log, "save"),
                "ds@load": LoggingDataSet(log, "load"),
            }
        )

        SequentialRunner(is_async=is_async).run(pipeline, catalog)

        # we want to see both datasets being released
        assert log == [("release", "save"), ("load", "load"), ("release", "load")]

    @pytest.mark.parametrize(
        "pipeline",
        [
            Pipeline([node(identity, "ds1", "ds2", confirms="ds1")]),
            Pipeline(
                [
                    node(identity, "ds1", "ds2"),
                    node(identity, "ds2", None, confirms="ds1"),
                ]
            ),
        ],
    )
    def test_confirms(self, mocker, pipeline, is_async):
        fake_dataset_instance = mocker.Mock()
        catalog = DataCatalog(data_sets={"ds1": fake_dataset_instance})
        SequentialRunner(is_async=is_async).run(pipeline, catalog)
        fake_dataset_instance.confirm.assert_called_once_with()


@pytest.fixture
def resume_scenario_pipeline():
    return Pipeline(
        [
            node(identity, "ds0_A", "ds1_A", name="node1_A"),
            node(identity, "ds0_B", "ds1_B", name="node1_B"),
            node(
                multi_input_list_output,
                ["ds1_A", "ds1_B"],
                ["ds2_A", "ds2_B"],
                name="node2",
            ),
            node(identity, "ds2_A", "ds3_A", name="node3_A"),
            node(identity, "ds2_B", "ds3_B", name="node3_B"),
            node(identity, "ds3_A", "ds4_A", name="node4_A"),
            node(identity, "ds3_B", "ds4_B", name="node4_B"),
        ]
    )


@pytest.fixture
def resume_scenario_catalog():
    def _load():
        return 0

    def _save(arg):
        assert arg == 0

    persistent_dataset = LambdaDataSet(load=_load, save=_save)
    return DataCatalog(
        {
            "ds0_A": persistent_dataset,
            "ds0_B": persistent_dataset,
            "ds2_A": persistent_dataset,
            "ds2_B": persistent_dataset,
        }
    )


@pytest.mark.parametrize(
    "failing_node_indexes,expected_pattern",
    [
        ([0], r"No nodes ran."),
        ([2], r"(node1_A,node1_B|node1_B,node1_A)"),
        ([3], r"(node3_A,node3_B|node3_B,node3_A)"),
        ([5], r"(node3_A,node3_B|node3_B,node3_A)"),
        ([3, 5], r"(node3_A,node3_B|node3_B,node3_A)"),
        ([2, 5], r"(node1_A,node1_B|node1_B,node1_A)"),
    ],
)
class TestSuggestResumeScenario:
    def test_suggest_resume_scenario(
        self,
        caplog,
        resume_scenario_catalog,
        resume_scenario_pipeline,
        failing_node_indexes,
        expected_pattern,
    ):
        for idx in failing_node_indexes:
            failing_node = resume_scenario_pipeline.nodes[idx]
            resume_scenario_pipeline -= Pipeline([failing_node])
            resume_scenario_pipeline += Pipeline(
                [failing_node._copy(func=exception_fn)]
            )
        with pytest.raises(Exception):
            SequentialRunner().run(
                resume_scenario_pipeline,
                resume_scenario_catalog,
                hook_manager=_create_hook_manager(),
            )
        assert re.search(expected_pattern, caplog.text)
