#!/usr/bin/env python3 -u
# copyright: sktime developers, BSD-3-Clause License (see LICENSE file)

from typing import Iterator, Optional, Tuple

import numpy as np
import pandas as pd

from sktime.base import BaseObject
from sktime.datatypes import check_is_scitype, convert_to
from sktime.forecasting.base import ForecastingHorizon
from sktime.split.base._config import (
    ACCEPTED_Y_TYPES,
    DEFAULT_FH,
    DEFAULT_WINDOW_LENGTH,
    FORECASTING_HORIZON_TYPES,
    PANDAS_MTYPES,
    SPLIT_GENERATOR_TYPE,
    SPLIT_TYPE,
)
from sktime.utils.validation import NON_FLOAT_WINDOW_LENGTH_TYPES
from sktime.utils.validation.forecasting import check_fh


class BaseSplitter(BaseObject):
    r"""Base class for temporal cross-validation splitters.

    The purpose of this implementation is to fill the gap relative to
    `sklearn.model_selection.TimeSeriesSplit
    <https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html>`__
    which implements only expanding window split strategy, and only integer based.

    The most important method in this class is `.split(y)` which generates indices
    of non-overlapping train/test splits of a time series `y`.
    The length of the train split is determined by `window_length`.
    The length of the test split is determined by forecasting horizon `fh`.

    In general, splitting a time series :math:`y=(y_1,\ldots,y_T)`
    into train/test splits means separating it into two non-overlapping series:
    train :math:`(y_{t(1)},\ldots,y_{t(k)})`
    and test :math:`(y_{t(k+1)},\ldots,y_{t(k+l)})`,
    where :math:`k,l` are all integers greater than zero,
    and :math:`t(k)<t(k+1)` are ordered time indices.
    The exact set of indices depends on a concrete splitter.
    Method `.split` is used to generate a pair of index sets:
    train :math:`(t(1),\ldots,t(k))` and test :math:`(t(k+1),\ldots,t(k+l))`.

    In case `window_length` and `fh` are integer valued,
    they translate into :math:`k` and :math:`l`, respectively.

    In case `window_length` and `fh` can be interpreted
    as time interval length (time deltas), then they correspond to
    :math:`t(k)-t(1)` and :math:`t(k+l)-t(k+1)`, respectively.

    Method `.get_n_splits` returns the number of splitting iterations.
    This number depends on a concrete splitting strategy and splitter parameters.

    Method `.get_cutoffs` returns the cutoff points between each train/test split.
    Using the above notation, for a single split it corresponds
    to the last integer index of the training window, :math:`k`

    In order to illustrate the difference in integer/interval arithmetic
    in calculating train/test indices, let us consider the following examples.
    Suppose, the arguments of a splitter are `cutoff = 10` and `window_length = 6`.
    Then, we have `train_start = cutoff - window_length = 4`.
    For timedelta-like values the logic is a bit more complicated.
    The time point corresponding to the `cutoff`
    (index value of the `y` series) is shifted back
    by the timedelta `window_length`,
    and then the integer position of the resulting datetime
    is considered to be the training window start.
    For example, for `cutoff = 10`, and `window_length = pd.Timedelta(6, unit="D")`,
    we have `y[cutoff] = pd.Timestamp("2021-01-10")`,
    and `y[cutoff] - window_length = pd.Timestamp("2021-01-04")`,
    which leads to `train_start = y.loc(y[cutoff] - window_length) = 4`.
    Similar timedelta arithmetic applies to other splitter arguments.

    Parameters
    ----------
    window_length : int or timedelta or pd.DateOffset
        Length of rolling window
    fh : array-like  or int, optional, (default=None)
        Single step ahead or array of steps ahead to forecast.
    """

    _tags = {"split_hierarchical": False}
    # split_hierarchical: whether _split supports hierarchical types natively
    # if not, splitter broadcasts over instances

    def __init__(
        self,
        fh: FORECASTING_HORIZON_TYPES = DEFAULT_FH,
        window_length: NON_FLOAT_WINDOW_LENGTH_TYPES = DEFAULT_WINDOW_LENGTH,
    ) -> None:
        self.window_length = window_length
        self.fh = fh

        super().__init__()

    def split(self, y: ACCEPTED_Y_TYPES) -> SPLIT_GENERATOR_TYPE:
        """Get iloc references to train/test splits of `y`.

        Parameters
        ----------
        y : pd.Index or time series in sktime compatible time series format,
                time series can be in any Series, Panel, or Hierarchical mtype format
            Index of time series to split, or time series to split
            If time series, considered as index of equivalent pandas type container:
                pd.DataFrame, pd.Series, pd-multiindex, or pd_multiindex_hier mtype

        Yields
        ------
        train : 1D np.ndarray of dtype int
            Training window indices, iloc references to training indices in y
        test : 1D np.ndarray of dtype int
            Test window indices, iloc references to test indices in y
        """
        y_index = self._coerce_to_index(y)

        if not isinstance(y_index, pd.MultiIndex):
            split = self._split
        elif self.get_tag("split_hierarchical", False, raise_error=False):
            split = self._split
        else:
            split = self._split_vectorized

        for train, test in split(y_index):
            yield train[train >= 0], test[test >= 0]

    def _split(self, y: pd.Index) -> SPLIT_GENERATOR_TYPE:
        """Get iloc references to train/test splits of `y`.

        private _split containing the core logic, called from split

        Parameters
        ----------
        y : pd.Index or time series in sktime compatible time series format
            Time series to split, or index of time series to split

        Yields
        ------
        train : 1D np.ndarray of dtype int
            Training window indices, iloc references to training indices in y
        test : 1D np.ndarray of dtype int
            Test window indices, iloc references to test indices in y
        """
        raise NotImplementedError("abstract method")

    def _split_vectorized(self, y: pd.MultiIndex) -> SPLIT_GENERATOR_TYPE:
        """Get iloc references to train/test splits of `y`, for pd.MultiIndex.

        This applies _split per time series instance in the multiindex.
        Instances in this context are defined by levels except last level.

        Parameters
        ----------
        y : pd.MultiIndex, with last level time-like
            as used in pd_multiindex and pd_multiindex_hier sktime mtypes

        Yields
        ------
        train : 1D np.ndarray of dtype int
            Training window indices, iloc references to training indices in y
        test : 1D np.ndarray of dtype int
            Test window indices, iloc references to test indices in y
        """
        srs = pd.DataFrame(index=y).reset_index(-1).iloc[:, 0]
        index = srs.index
        anchors = pd.Series(range(len(srs)), index).groupby(index).first().tolist()
        splits = (self._split(pd.Index(inst.values)) for _, inst in srs.groupby(index))

        train = []
        test = []
        for split_inst, anchor in zip(splits, anchors):
            train_inst, test_inst = zip(*split_inst)
            train.append(tuple(indices + anchor for indices in train_inst))
            test.append(tuple(indices + anchor for indices in test_inst))

        train = map(np.concatenate, zip(*train))
        test = map(np.concatenate, zip(*test))

        yield from zip(train, test)

    def split_loc(self, y: ACCEPTED_Y_TYPES) -> Iterator[Tuple[pd.Index, pd.Index]]:
        """Get loc references to train/test splits of `y`.

        Parameters
        ----------
        y : pd.Index or time series in sktime compatible time series format,
                time series can be in any Series, Panel, or Hierarchical mtype format
            Time series to split, or index of time series to split

        Yields
        ------
        train : pd.Index
            Training window indices, loc references to training indices in y
        test : pd.Index
            Test window indices, loc references to test indices in y
        """
        y_index = self._coerce_to_index(y)

        for train, test in self.split(y_index):
            yield y_index[train], y_index[test]

    def split_series(self, y: ACCEPTED_Y_TYPES) -> Iterator[SPLIT_TYPE]:
        """Split `y` into training and test windows.

        Parameters
        ----------
        y : time series in sktime compatible time series format,
                time series can be in any Series, Panel, or Hierarchical mtype format
            e.g., pd.Series, pd.DataFrame, np.ndarray
            Time series to split, or index of time series to split

        Yields
        ------
        train : time series of same sktime mtype as `y`
            training series in the split
        test : time series of same sktime mtype as `y`
            test series in the split
        """
        y, y_orig_mtype = self._check_y(y)

        for train, test in self.split(y.index):
            y_train = y.iloc[train]
            y_test = y.iloc[test]
            y_train = convert_to(y_train, y_orig_mtype)
            y_test = convert_to(y_test, y_orig_mtype)
            yield y_train, y_test

    def _coerce_to_index(self, y: ACCEPTED_Y_TYPES) -> pd.Index:
        """Check and coerce y to pandas index.

        Parameters
        ----------
        y : pd.Index or time series in sktime compatible time series format (any)
            Index of time series to split, or time series to split
            If time series, considered as index of equivalent pandas type container:
                pd.DataFrame, pd.Series, pd-multiindex, or pd_multiindex_hier mtype

        Returns
        -------
        y_index : y, if y was pd.Index; otherwise _check_y(y).index
        """
        if not isinstance(y, pd.Index):
            y, _ = self._check_y(y, allow_index=True)
            y_index = y.index
        else:
            y_index = y
        return y_index

    def _check_y(self, y, allow_index=False):
        """Check and coerce y to a pandas based mtype.

        Parameters
        ----------
        y : pd.Series, pd.DataFrame, or np.ndarray (1D or 2D), optional (default=None)
            Time series to check, must conform with one of the sktime type conventions.

        Returns
        -------
        y_inner : time series y coerced to one of the sktime pandas based mtypes:
            pd.DataFrame, pd.Series, pd-multiindex, pd_multiindex_hier
            returns pd.Series only if y was pd.Series, otherwise a pandas.DataFrame
        y_mtype : original mtype of y

        Raises
        ------
        TypeError if y is not one of the permissible mtypes
        """
        if allow_index and isinstance(y, pd.Index):
            return y, "pd.Index"

        ALLOWED_SCITYPES = ["Series", "Panel", "Hierarchical"]
        ALLOWED_MTYPES = [
            "pd.Series",
            "pd.DataFrame",
            "np.ndarray",
            "nested_univ",
            "numpy3D",
            # "numpyflat",
            "pd-multiindex",
            # "pd-wide",
            # "pd-long",
            "df-list",
            "pd_multiindex_hier",
        ]
        y_valid, _, y_metadata = check_is_scitype(
            y, scitype=ALLOWED_SCITYPES, return_metadata=True, var_name="y"
        )
        if allow_index:
            msg = (
                "y must be a pandas.Index, or a time series in an sktime compatible "
                "format, of scitype Series, Panel or Hierarchical, "
                "for instance a pandas.DataFrame with sktime compatible time indices, "
                "or with MultiIndex and last(-1) level an sktime compatible time index."
                f" Allowed compatible mtype format specifications are: {ALLOWED_MTYPES}"
                "See the forecasting tutorial examples/01_forecasting.ipynb, or"
                " the data format tutorial examples/AA_datatypes_and_datasets.ipynb, "
                "If you think y is already in an sktime supported input format, "
                "run sktime.datatypes.check_raise(y, mtype) to diagnose the error, "
                "where mtype is the string of the type specification you want for y. "
            )
        else:
            msg = (
                "y must be in an sktime compatible format, "
                "of scitype Series, Panel or Hierarchical, "
                "for instance a pandas.DataFrame with sktime compatible time indices, "
                "or with MultiIndex and last(-1) level an sktime compatible time index."
                f" Allowed compatible mtype format specifications are: {ALLOWED_MTYPES}"
                "See the forecasting tutorial examples/01_forecasting.ipynb, or"
                " the data format tutorial examples/AA_datatypes_and_datasets.ipynb, "
                "If you think y is already in an sktime supported input format, "
                "run sktime.datatypes.check_raise(y, mtype) to diagnose the error, "
                "where mtype is the string of the type specification you want for y. "
            )
        if not y_valid:
            raise TypeError(msg)

        y_inner = convert_to(y, to_type=PANDAS_MTYPES)

        mtype = y_metadata["mtype"]

        return y_inner, mtype

    def get_n_splits(self, y: Optional[ACCEPTED_Y_TYPES] = None) -> int:
        """Return the number of splits.

        Parameters
        ----------
        y : pd.Series or pd.Index, optional (default=None)
            Time series to split

        Returns
        -------
        n_splits : int
            The number of splits.
        """
        raise NotImplementedError("abstract method")

    def get_cutoffs(self, y: Optional[ACCEPTED_Y_TYPES] = None) -> np.ndarray:
        """Return the cutoff points in .iloc[] context.

        Parameters
        ----------
        y : pd.Series or pd.Index, optional (default=None)
            Time series to split

        Returns
        -------
        cutoffs : 1D np.ndarray of int
            iloc location indices, in reference to y, of cutoff indices
        """
        raise NotImplementedError("abstract method")

    def get_fh(self) -> ForecastingHorizon:
        """Return the forecasting horizon.

        Returns
        -------
        fh : ForecastingHorizon
            The forecasting horizon
        """
        return check_fh(self.fh)

    @staticmethod
    def _get_train_window(
        y: pd.Index, train_start: int, split_point: int
    ) -> np.ndarray:
        """Get train window.

        For formal definition of the train window see docstring of the `BaseSplitter`

        Parameters
        ----------
        y : pd.Index
            Index of time series to split
        train_start : int
            Integer index of the training window start
        split_point : int
            Integer index of the train window end

        Returns
        -------
        np.ndarray with integer indices of the train window
        """
        if split_point > max(0, train_start):
            return np.argwhere(
                (y >= y[max(train_start, 0)]) & (y <= y[min(split_point, len(y)) - 1])
            ).flatten()
        return np.array([], dtype=int)
