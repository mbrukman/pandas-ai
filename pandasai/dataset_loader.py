import os
import yaml
import pandas as pd
from datetime import datetime, timedelta
import hashlib
from .dataframe.base import DataFrame
import importlib
from typing import Dict, Any


class DatasetLoader:
    SUPPORTED_SOURCES = {
        "mysql": "pandasai_sql",
        "postgres": "pandasai_sql",
        "yahoo_finance": "pandasai.connectors.yfinance",
        "bigquery": "pandasai.ee.connectors.bigquery",
        "snowflake": "pandasai.ee.connectors.snowflake",
        "databricks": "pandasai.ee.connectors.databricks",
        "oracle": "pandasai.ee.connectors.oracle",
    }

    def __init__(self):
        self.schema = None
        self.dataset_path = None

    def load(self, dataset_path: str) -> DataFrame:
        self.dataset_path = dataset_path
        self._load_schema()
        self._validate_source_type()

        cache_file = self._get_cache_file_path()

        if self._is_cache_valid(cache_file):
            return self._read_cache(cache_file)

        df = self._load_from_source()
        df = self._apply_transformations(df)
        self._cache_data(df, cache_file)

        return DataFrame(df)

    def _load_schema(self):
        schema_path = os.path.join("datasets", self.dataset_path, "schema.yaml")
        if not os.path.exists(schema_path):
            raise FileNotFoundError(f"Schema file not found: {schema_path}")

        with open(schema_path, "r") as file:
            self.schema = yaml.safe_load(file)

    def _validate_source_type(self):
        source_type = self.schema["source"]["type"]
        if source_type not in self.SUPPORTED_SOURCES:
            raise ValueError(f"Unsupported database type: {source_type}")

    def _get_cache_file_path(self) -> str:
        if "path" in self.schema["destination"]:
            return os.path.join(
                "datasets", self.dataset_path, self.schema["destination"]["path"]
            )

        file_extension = (
            "parquet" if self.schema["destination"]["format"] == "parquet" else "csv"
        )
        return os.path.join("datasets", self.dataset_path, f"data.{file_extension}")

    def _is_cache_valid(self, cache_file: str) -> bool:
        if not os.path.exists(cache_file):
            return False

        file_mtime = datetime.fromtimestamp(os.path.getmtime(cache_file))
        update_frequency = self.schema["update_frequency"]

        if update_frequency == "weekly":
            return file_mtime > datetime.now() - timedelta(weeks=1)

        return False

    def _read_cache(self, cache_file: str) -> DataFrame:
        cache_format = self.schema["destination"]["format"]
        if cache_format == "parquet":
            return DataFrame(pd.read_parquet(cache_file))
        elif cache_format == "csv":
            return DataFrame(pd.read_csv(cache_file))
        else:
            raise ValueError(f"Unsupported cache format: {cache_format}")

    def _load_from_source(self) -> pd.DataFrame:
        source_type = self.schema["source"]["type"]
        connection_info = self.schema["source"].get("connection", {})
        query = self._generate_query(self.schema)

        try:
            module_name = self.SUPPORTED_SOURCES[source_type]
            module = importlib.import_module(module_name)

            if source_type in ["mysql", "postgres"]:
                load_function = getattr(module, f"load_from_{source_type}")
                return load_function(connection_info, query)
            else:
                connector_class = getattr(
                    module, f"{source_type.capitalize()}Connector"
                )
                connector = connector_class(config=connection_info)
                return connector.execute_query(query)
        except ImportError as e:
            raise ImportError(
                f"{source_type.capitalize()} connector not found. "
                f"Please install the {module_name} library."
            ) from e

    def _generate_query(self, schema: Dict[str, Any] = None) -> str:
        if schema is None:
            schema = self.schema

        columns = ", ".join([col["name"] for col in schema["columns"]])
        table_name = schema["source"]["table"]
        query = f"SELECT {columns} FROM {table_name}"

        if "order_by" in schema:
            order_by = schema["order_by"]
            order_by_clause = (
                ", ".join(order_by) if isinstance(order_by, list) else order_by
            )
            query += f" ORDER BY {order_by_clause}"

        if "limit" in schema:
            query += f" LIMIT {schema['limit']}"

        return query

    def _apply_transformations(self, df: pd.DataFrame) -> pd.DataFrame:
        for transform in self.schema.get("transformations", []):
            if transform["type"] == "anonymize":
                df[transform["params"]["column"]] = df[
                    transform["params"]["column"]
                ].apply(self._anonymize)
            elif transform["type"] == "convert_timezone":
                df[transform["params"]["column"]] = pd.to_datetime(
                    df[transform["params"]["column"]]
                ).dt.tz_convert(transform["params"]["to"])
        return df

    @staticmethod
    def _anonymize(value: Any) -> Any:
        if not isinstance(value, str) or "@" not in value:
            return value
        try:
            local, domain = value.rsplit("@", 1)
            return f"{hashlib.md5(local.encode()).hexdigest()}@{domain}"
        except ValueError:
            return value

    def _cache_data(self, df: pd.DataFrame, cache_file: str):
        cache_format = self.schema["destination"]["format"]
        if cache_format == "parquet":
            df.to_parquet(cache_file, index=False)
        elif cache_format == "csv":
            df.to_csv(cache_file, index=False)
        else:
            raise ValueError(f"Unsupported cache format: {cache_format}")
