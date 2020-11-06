import itertools
from pathlib import Path
from typing import Any, List, Union, Dict, Optional, Iterator
import copy
import logging
import os

import great_expectations.exceptions as ge_exceptions
from great_expectations.core.batch import BatchDefinition, BatchMarkers, BatchRequest
from great_expectations.core.id_dict import BatchSpec, PartitionDefinitionSubset
from great_expectations.execution_engine import ExecutionEngine
from great_expectations.execution_environment.data_connector.asset.asset import Asset
from great_expectations.execution_environment.data_connector.data_connector import DataConnector

from great_expectations.execution_environment.data_connector.partition_query import (
    PartitionQuery,
    build_partition_query

)
from great_expectations.execution_environment.types import PathBatchSpec
from great_expectations.core.batch import (
    BatchRequest,
    BatchDefinition,
)
from great_expectations.execution_environment.data_connector.sorter import Sorter
from great_expectations.execution_environment.data_connector.util import (
    batch_definition_matches_batch_request,
    map_data_reference_string_to_batch_definition_list_using_regex,
    map_batch_definition_to_data_reference_string_using_regex,
    get_filesystem_one_level_directory_glob_path_list,
    build_sorters_from_config,
)
from great_expectations.data_context.util import instantiate_class_from_config
import great_expectations.exceptions as ge_exceptions

logger = logging.getLogger(__name__)

# TODO: <Alex>Should we make this a "set" object?</Alex>
KNOWN_EXTENSIONS = [
    ".csv",
    ".tsv",
    ".parquet",
    ".xls",
    ".xlsx",
    ".json",
    ".csv.gz",
    ".tsv.gz",
    ".feather",
]


class ConfiguredAssetFilesystemDataConnector(DataConnector):
    def __init__(
        self,
        name: str,
        execution_environment_name: str,
        base_directory: str,
        default_regex: dict,
        assets: dict,
        glob_directive: str = "*",
        sorters: list = None,
        execution_engine: ExecutionEngine = None,
        data_context_root_directory: str = None,
    ):
        logger.debug(f'Constructing ConfiguredAssetFilesystemDataConnector "{name}".')
        super().__init__(
            name=name,
            execution_environment_name=execution_environment_name,
            execution_engine=execution_engine,
        )
        self._data_context_root_directory = data_context_root_directory

        self._base_directory = self._normalize_directory_path(dir_path=base_directory)

        self._glob_directive = glob_directive

        # TODO: Maybe make this a typed object?
        self._default_regex = default_regex

        if assets is None:
            assets = {}
        _assets: Dict[str, Union[dict, Asset]] = assets
        self._assets = _assets
        self._build_assets_from_config(config=assets)

        self._sorters = build_sorters_from_config(config_list=sorters)

    @property
    def assets(self) -> Dict[str, Union[dict, Asset]]:
        return self._assets

    @property
    def base_directory(self) -> str:
        return str(self._base_directory)

    @property
    def glob_directive(self) -> str:
        return self._glob_directive

    @property
    def sorters(self) -> Optional[dict]:
        return self._sorters

    def _build_assets_from_config(self, config: Dict[str, dict]):
        for name, asset_config in config.items():
            if asset_config is None:
                asset_config = {}
            new_asset: Asset = self._build_asset_from_config(
                name=name,
                config=asset_config,
            )
            self.assets[name] = new_asset

    def _build_asset_from_config(self, name: str, config: dict):
        """Build an Asset using the provided configuration and return the newly-built Asset."""
        runtime_environment: dict = {
            "name": name,
            "data_connector": self
        }
        asset: Asset = instantiate_class_from_config(
            config=config,
            runtime_environment=runtime_environment,
            config_defaults={
                "module_name": "great_expectations.execution_environment.data_connector.asset",
                "class_name": "Asset"
            },
        )
        if not asset:
            raise ge_exceptions.ClassInstantiationError(
                module_name="great_expectations.execution_environment.data_connector.asset",
                package_name=None,
                class_name=config["class_name"],
            )
        return asset

    def get_available_data_asset_names(self) -> List[str]:
        """Return the list of asset names known by this data connector.

        Returns:
            A list of available names
        """
        return list(self.assets.keys())

    # TODO: <Alex>This code is broken; it is used only by deprecated classes and methods.</Alex>
    def _validate_runtime_keys_configuration(self, runtime_keys: List[str]):
        if runtime_keys and len(runtime_keys) > 0:
            if not (self.runtime_keys and set(runtime_keys) <= set(self.runtime_keys)):
                raise ge_exceptions.PartitionerError(
                    f"""Partitioner "{self.name}" was invoked with one or more runtime keys that do not appear among the
configured runtime keys.
                    """
                )

    # TODO: <Alex>This method should be used in other file path type DataConnector classes (currently it is not).</Alex>
    def _normalize_directory_path(self, dir_path: str) -> str:
        # If directory is a relative path, interpret it as relative to the data context's
        # context root directory (parent directory of great_expectation dir)
        if Path(dir_path).is_absolute() or self._data_context_root_directory is None:
            return dir_path
        else:
            return Path(self._data_context_root_directory).joinpath(dir_path)

    def _refresh_data_references_cache(
        self,
    ):
        """
        """
        # Map data_references to batch_definitions
        self._data_references_cache = {}

        for data_asset_name in self.get_available_data_asset_names():
            self._data_references_cache[data_asset_name] = {}

            for data_reference in self._get_data_reference_list(
                data_asset_name=data_asset_name
            ):
                mapped_batch_definition_list: List[BatchDefinition] = self._map_data_reference_to_batch_definition_list(
                    data_reference=data_reference,
                    data_asset_name=data_asset_name,
                )
                self._data_references_cache[data_asset_name][data_reference] = mapped_batch_definition_list

    def _get_data_reference_list(self, data_asset_name: Optional[str] = None) -> List[str]:
        """List objects in the underlying data store to create a list of data_references.

        This method is used to refresh the cache.
        """
        data_asset_path: str = self.base_directory
        if data_asset_name is not None and self.assets and data_asset_name in self.assets:
            asset: Asset = self.assets[data_asset_name]
            if asset.base_directory:
                data_asset_path = str(Path(self.base_directory).joinpath(asset.base_directory))

        path_list: List[str] = get_filesystem_one_level_directory_glob_path_list(
            base_directory_path=data_asset_path,
            glob_directive=self._glob_directive
        )

        return path_list

    def _get_data_reference_list_from_cache_by_data_asset_name(self, data_asset_name: str) -> List[str]:
        """
        Fetch data_references corresponding to data_asset_name from the cache.
        """
        # TODO: <Alex>There is no reason for the BatchRequest semantics here; this should be replaced with a method that accepts just the required arguments.</Alex>
        batch_definition_list = self.get_batch_definition_list_from_batch_request(
            batch_request=BatchRequest(
                execution_environment_name=self.execution_environment_name,
                data_connector_name=self.name,
                data_asset_name=data_asset_name,
            )
        )

        regex_config: dict = copy.deepcopy(self._default_regex)
        # Override the defaults
        if data_asset_name is not None and self.assets and data_asset_name in self.assets:
            asset: Asset = self.assets[data_asset_name]
            if asset.pattern:
                regex_config["pattern"] = asset.pattern
            if asset.group_names:
                regex_config["group_names"] = asset.group_names
        pattern: str = regex_config["pattern"]
        group_names: List[str] = regex_config["group_names"]

        path_list: List[str] = [
            map_batch_definition_to_data_reference_string_using_regex(
                batch_definition=batch_definition,
                regex_pattern=pattern,
                group_names=group_names
            )
            for batch_definition in batch_definition_list
        ]

        #TODO: Sort with a real sorter here
        path_list.sort()

        return path_list

    def get_data_reference_list_count(self) -> int:
        if self._data_references_cache is None:
            raise ValueError(
                f"data references cache for {self.__class__.__name__} {self.name} has not yet been populated."
            )

        total_references: int = 0
        for data_asset_name in self._data_references_cache:
            total_references += len(self._data_references_cache[data_asset_name])

        return total_references

    def get_unmatched_data_references(self) -> List[str]:
        if self._data_references_cache is None:
            raise ValueError('_data_references_cache is None.  Have you called "_refresh_data_references_cache()" yet?')

        unmatched_data_references: List[str] = []
        for data_asset_name, data_reference_sub_cache in self._data_references_cache.items():
            unmatched_data_references += [k for k, v in data_reference_sub_cache.items() if v is None]

        return unmatched_data_references

    def get_batch_definition_list_from_batch_request(
        self,
        batch_request: BatchRequest,
    ) -> List[BatchDefinition]:
        self._validate_batch_request(batch_request=batch_request)
        self._validate_sorters_configuration(batch_request=batch_request)

        if self._data_references_cache is None:
            self._refresh_data_references_cache()

        batch_definition_list: List[BatchDefinition] = list(
            filter(
                lambda batch_definition: batch_definition_matches_batch_request(
                    batch_definition=batch_definition,
                    batch_request=batch_request
                ),
                [
                    batch_definitions[0]
                    for data_reference_sub_cache in self._data_references_cache.values()
                    for batch_definitions in data_reference_sub_cache.values()
                    if batch_definitions is not None
                ]
            )
        )

        if batch_request.partition_request is not None:
            partition_query_obj: PartitionQuery = build_partition_query(
                partition_request_dict=batch_request.partition_request
            )
            batch_definition_list = partition_query_obj.select_from_partition_request(
                batch_definition_list=batch_definition_list
            )

        if len(self.sorters) > 0:
            sorted_batch_definition_list = self._sort_batch_definition_list(
                batch_definition_list=batch_definition_list
            )
            return sorted_batch_definition_list
        else:
            return batch_definition_list

    # TODO: <Alex>Opportunity to combine code with other connectors into a utility method.</Alex>
    def _validate_sorters_configuration(self, batch_request):
        # Override the default
        if len(self.sorters) > 0:
            regex_config = self._default_regex
            if (
                batch_request.data_asset_name is not None
                and self.assets and batch_request.data_asset_name in self.assets
            ):
                asset: Asset = self.assets[batch_request.data_asset_name]
                if asset.group_names:
                    regex_config["group_names"] = asset.group_names
            group_names: List[str] = regex_config["group_names"]
            if any([sorter not in group_names for sorter in self.sorters]):
                raise ge_exceptions.DataConnectorError(
                    f'''ConfiguredAssetFilesystemDataConnector "{self.name}" specifies one or more sort keys that do not appear among the
configured group_name.
                      '''
                )
            if len(group_names) < len(self.sorters):
                raise ge_exceptions.DataConnectorError(
                    f'''ConfiguredAssetFilesystemDataConnector "{self.name}" is configured with {len(group_names)} group names; 
this is fewer than number of sorters specified, which is {len(self.sorters)}.
                    '''
                )

    def _sort_batch_definition_list(self, batch_definition_list):
        sorters_list: List[Sorter] = []
        for sorter in self.sorters.values():
            sorters_list.append(sorter)
        sorters: Iterator[Sorter] = reversed(sorters_list)
        for sorter in sorters:
            batch_definition_list = sorter.get_sorted_batch_definitions(batch_definitions=batch_definition_list)
        return batch_definition_list

    def _map_data_reference_to_batch_definition_list(
        self,
        data_reference: str,
        data_asset_name: str = None
    ) -> Optional[List[BatchDefinition]]:
        regex_config: dict = copy.deepcopy(self._default_regex)
        # Override the defaults
        if data_asset_name is not None and self.assets and data_asset_name in self.assets:
            asset: Asset = self.assets[data_asset_name]
            if asset.pattern:
                regex_config["pattern"] = asset.pattern
            if asset.group_names:
                regex_config["group_names"] = asset.group_names
        pattern: str = regex_config["pattern"]
        group_names: List[str] = regex_config["group_names"]

        return map_data_reference_string_to_batch_definition_list_using_regex(
            execution_environment_name=self.execution_environment_name,
            data_connector_name=self.name,
            data_asset_name=data_asset_name,
            data_reference=data_reference,
            regex_pattern=pattern,
            group_names=group_names
        )

    def _map_batch_definition_to_data_reference(self, batch_definition: BatchDefinition) -> str:
        data_asset_name: str = batch_definition.data_asset_name

        regex_config: dict = copy.deepcopy(self._default_regex)
        # Override the defaults
        if data_asset_name is not None and self.assets and data_asset_name in self.assets:
            asset: Asset = self.assets[data_asset_name]
            if asset.pattern:
                regex_config["pattern"] = asset.pattern
            if asset.group_names:
                regex_config["group_names"] = asset.group_names
        pattern: str = regex_config["pattern"]
        group_names: List[str] = regex_config["group_names"]

        return map_batch_definition_to_data_reference_string_using_regex(
            batch_definition=batch_definition,
            regex_pattern=pattern,
            group_names=group_names
        )

    def _generate_batch_spec_parameters_from_batch_definition(
        self,
        batch_definition: BatchDefinition
    ) -> dict:
        path: str = self._map_batch_definition_to_data_reference(batch_definition=batch_definition)
        if not path:
            raise ValueError(
                f'''No data reference for data asset name "{batch_definition.data_asset_name}" matches the given
partition definition {batch_definition.partition_definition} from batch definition {batch_definition}.
                '''
            )
        path = str(Path(self.base_directory).joinpath(path))
        return {
            "path": path
        }

    def build_batch_spec(
        self,
        batch_definition: BatchDefinition
    ) -> PathBatchSpec:
        batch_spec = super().build_batch_spec(batch_definition=batch_definition)
        return PathBatchSpec(batch_spec)
