# -*- coding: utf-8 -*-
from typing import Iterable, List, Optional

import brightway2 as bw
from bw2data.backends.peewee import sqlite3_lci_db
import pandas as pd

from .brightway import (
    convert_key_to_fields, select_superstructure_indexes,
    find_missing_exchanges, select_exchange_data,
    swap_exchange_activities, nullify_exchanges,
    check_for_invalid_codes, handle_code_weirdness,
)
from .utils import SUPERSTRUCTURE, TO_ALL


class Builder(object):
    def __init__(self):
        # Initial values for the builder to set.
        self.name: Optional[str] = None
        self.selected_deltas: list = []
        self.unique_indexes: set = set()

        # Values created by finding exchanges missing from superstructure.
        self.missing_exchanges: list = []

        # Actual superstructure scenario dataframe.
        self.superstructure: Optional[pd.DataFrame] = None

    @classmethod
    def initialize(cls, initial: str, deltas: List[str]) -> 'Builder':
        """Initialize the builder object with the given initial database
        and all the deltas for this database.
        """
        builder = cls()
        builder.name = initial
        builder.unique_indexes = select_superstructure_indexes(initial)
        builder.selected_deltas = deltas
        return builder

    @classmethod
    def superstructure_from_databases(cls, databases: List[str]) -> 'Builder':
        """
        docstring
        """
        assert len(databases) > 1, "Multiple items required"
        assert all(db in bw.databases for db in databases), "All databases must exist in the project"
        initial, deltas = databases[0], databases[1:]
        print("Superstructure: {}, deltas: {}".format(initial, ", ".join(deltas)))
        builder = cls.initialize(initial, deltas)
        print("Amount of exchanges in superstructure: {}".format(len(builder.unique_indexes)))
        builder.find_missing_exchanges()
        print("Total amount of exchanges in superstructure: {}".format(len(builder.unique_indexes)))
        if builder.missing_exchanges:
            print("Storing new exchanges for superstructure.".format(len(builder.missing_exchanges)))
            builder.expand_superstructure()
        return builder

    def find_missing_exchanges(self) -> None:
        """Iterate through the delta databases and find the missing exchanges."""
        for d in self.selected_deltas:
            d_set, d_list = find_missing_exchanges(self.unique_indexes, d)
            self.missing_exchanges.extend(d_list)
            self.unique_indexes = self.unique_indexes.union(d_set)
            print("{} adds {} new exchanges to superstructure".format(d, len(d_set)))

    def expand_superstructure(self) -> None:
        """Given that we have an initialized Builder, prepare and store
        the new exchanges.
        """
        deltas_set = set(self.selected_deltas)
        nulled = nullify_exchanges([x.data for x in self.missing_exchanges])
        # Prepare new exchanges by swapping the
        new_excs = [
            swap_exchange_activities(row, self.name, deltas_set) for row in nulled
        ]
        # Save all of the new exchanges to the superstructure database.
        with sqlite3_lci_db.transaction() as txn:
            for i, exc in enumerate(new_excs):
                exc.save()
                if i % 1000 == 0:
                    txn.commit()

    def build_complete_superstructure(self) -> None:
        """After initializing the superstructure, construct difference data."""
        assert self.name is not None, "Superstructure name is not set."
        assert self.selected_deltas, "No deltas found for superstructure."

        super_dict = self.construct_ss_dictionary(select_exchange_data(self.name))
        # Iterate through deltas to find the differences between superstructure
        # and each delta.
        for db in self.selected_deltas:
            db_data = select_exchange_data(db)
            self.match_exchanges(super_dict, db_data, db)

        # Now feed the dictionary into a pandas DataFrame
        self.superstructure = self.build_superstructure_dataframe(super_dict)

    def filter_superstructure(self) -> None:
        """Take the built superstructure and drop any exchanges where there
        is no differences between all databases.
        """
        scen_idx = self.superstructure.columns.difference(SUPERSTRUCTURE, sort=False)
        delta_idx = scen_idx.drop([self.name])

        # First, drop the rows where no delta file has any changes.
        only_nans = self.superstructure[delta_idx].isna().all(axis=1)
        self.superstructure = self.superstructure.drop(
            self.superstructure.index[only_nans]
        )

        # Now, drop the rows where there are no differences between the scenarios
        same_vals = self.superstructure[scen_idx].nunique(axis=1) == 1
        self.superstructure = self.superstructure.drop(
            self.superstructure.index[same_vals]
        )

        # Reset the index to clean up all of the unused indexes.
        self.superstructure = self.superstructure.reset_index(drop=True)

        # Finally, replace any NaN values in the delta amounts with a 0
        missing = delta_idx[self.superstructure[delta_idx].isna().any()]
        if not missing.empty:
            self.superstructure[missing] = self.superstructure[missing].apply(
                lambda x: x.fillna(0), axis=1
            )

    def validate_superstructure(self) -> None:
        """Parse the DataFrame and check that all the relevant keys exist in
        the superstructure database.

        If this is not the case, begin trying to repair it.
        """
        print("Searching superstructure for invalid keys")
        missing_codes = check_for_invalid_codes(self.superstructure, self.name)
        if missing_codes:
            print("Found {} unknown keys, attempting repair.".format(len(missing_codes)))
            corrections = handle_code_weirdness(
                missing_codes, set(self.selected_deltas), self.name
            )
            self.superstructure = self._fix_broken_keys(self.superstructure, corrections)
            still_missing = check_for_invalid_codes(self.superstructure, self.name)
            if still_missing:
                raise KeyError("There are broken keys in the superstructure", still_missing)
            print("Finished fixing keys.")
        else:
            print("No unknown keys found.")

    @staticmethod
    def construct_ss_dictionary(data: Iterable) -> dict:
        """Construct an initial dictionary for the superstructure."""
        return {(x.get("input")[1], x.get("output")[1]): [x] for x in data}

    @staticmethod
    def match_exchanges(origin: dict, delta: Iterable, db_name: str) -> None:
        """Matches a delta iterable against the superstructure dictionary,
        appending exchanges to the relevant keys.

        Any exchanges not present in the delta will have a very simple placeholder.
        """
        expected_len = len(next(iter(origin.values()))) + 1
        for exc in delta:
            keys = (exc.get("input")[1], exc.get("output")[1])
            assert keys in origin, "Exchange {} does not exist in superstructure".format(keys)
            origin[keys].append(
                {"output": exc.get("output"), "amount": exc.get("amount")}
            )

        # Add 0 values for exchanges that do not exist in the delta.
        for i in (m for m in origin.values() if len(m) != expected_len):
            i.append({"output": [db_name], "amount": 0})

    @staticmethod
    def build_superstructure_dataframe(data: dict) -> pd.DataFrame:
        """Given a list of tuples, construct a Superstructure DataFrame.

        the first item in each will be the inital scenario (superstructure)
        with each following item representing a new scenario.
        """
        initial = next(iter(data.values()))
        df_index = SUPERSTRUCTURE.append(Builder.create_scenario_index(initial))

        df = pd.DataFrame([
            Builder.parse_exchange_data_for_dataframe(row)
            for row in data.values()
        ], columns=df_index)

        return df

    @staticmethod
    def finalize_superstructure(df: pd.DataFrame) -> pd.DataFrame:
        """Ensure the dataframe is complete by matching the output activity
        keys.
        """
        substitute = convert_key_to_fields(df.loc[:, TO_ALL])
        df[substitute.columns] = substitute
        return df

    @staticmethod
    def create_scenario_index(data: Iterable) -> pd.Index:
        """Given an iterable of exchange dictionaries, build an ordered Index
        that represents the scenario names.
        """
        names = [x.get("output")[0] for x in data if "output" in x]
        return pd.Index(names)

    @staticmethod
    def parse_exchange_data_for_dataframe(data: Iterable) -> dict:
        """Build a dictionary that can be fed into a superstructure DataFrame."""
        iterator = iter(data)
        ss_exc = next(iterator)
        inp = ss_exc.get("input")
        outp = ss_exc.get("output")
        result = {
            "from database": inp[0], "from key": inp,
            "to database": outp[0], "to key": outp,
            "from activity name": ss_exc.get("name"),
        }
        if "type" in ss_exc and ss_exc["type"] == "biosphere":
            result["from categories"] = ss_exc.get("categories")
        elif ss_exc.get("type") == "technosphere":
            result["from reference product"] = ss_exc.get("product")
            result["from location"] = ss_exc.get("location")

        # Now actually read the amounts from the exchanges.
        result.update(Builder.extract_amounts_for_dataframe(data))
        return result

    @staticmethod
    def extract_amounts_for_dataframe(data: Iterable) -> dict:
        return {
            exc.get("output")[0]: exc.get("amount")
            for exc in data
        }

    @staticmethod
    def _fix_broken_keys(df: pd.DataFrame, correct_codes: dict) -> pd.DataFrame:
        """Given a superstructure dataframe and fix the broken keys by
        switching in the correct codes.

        With help from https://stackoverflow.com/a/49259581/14506150
        """
        from_keys = df.index[df["from key"].isin(correct_codes)]
        to_keys = df.index[df["to key"].isin(correct_codes)]

        # Only apply the mapping to the column if the broken keys are found.
        if not from_keys.empty:
            df.loc[from_keys, "from key"] = df.loc[from_keys, "from key"].map(correct_codes)
        if not to_keys.empty:
            df.loc[to_keys, "to key"] = df.loc[to_keys, "to key"].map(correct_codes)

        return df
